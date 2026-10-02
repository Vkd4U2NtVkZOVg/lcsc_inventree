"""淘宝商品页 MHTML（浏览器「网页另存为 MHTML」）解析。

背景：淘宝商品页是前端渲染 + 强反爬，无法像 LCSC 一样直接抓取；但用户从
浏览器「另存为 MHTML」保存的页面是**渲染后的完整 DOM**，且所有图片以
base64 内嵌（离线可用）。本模块从 MHTML 提取：

- 商品标题 / 价格（高亮价）/ 店铺名 / 商品链接（含 item id、skuId）
- 选中 SKU（用户保存页面前勾选的规格）+ 全部可选规格
- 「规格参数」「重点参数」表
- 图集 URL 列表 + 内嵌图片字节（按 URL 匹配，用于离线传图）

页面为 CSS-module 类名（如 `mainTitle--xYz123`，哈希后缀随构建变化），
一律按**前缀**匹配。纯解析模块（不依赖 InvenTree / Flask），便于离线测试。
"""

from __future__ import annotations

import email
import logging
import re
from dataclasses import dataclass, field
from email.message import Message

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

ITEM_ID_RE = re.compile(r"[?&]id=(\d+)")
SKU_ID_RE = re.compile(r"[?&]skuId=(\d+)")
PRICE_RE = re.compile(r"[\d.]+")

TAOBAO_TITLE_SUFFIXES = ("-淘宝网", "-淘宝网", "-taobao.com")


@dataclass(slots=True)
class TaobaoSku:
    """一个可选规格（SKU 选项）。"""

    text: str
    image_url: str | None = None
    selected: bool = False


@dataclass(slots=True)
class TaobaoItem:
    """从淘宝 MHTML 提取的商品数据。"""

    url: str
    item_id: str | None
    sku_id: str | None
    title: str
    shop: str | None
    price: float | None
    currency: str = "CNY"
    selected_sku: str | None = None
    skus: list[TaobaoSku] = field(default_factory=list)
    params: list[tuple[str, str]] = field(default_factory=list)  # (名称, 值)
    gallery: list[str] = field(default_factory=list)  # 图集 URL（去重保序）
    embedded: dict[str, bytes] = field(default_factory=dict)  # 去查询串 URL -> 字节
    source_filename: str | None = None

    def default_ipn(self) -> str:
        return f"TB{self.item_id}" if self.item_id else "TB"

    def default_name(self) -> str:
        """默认器件名：标题（去「-淘宝网」后缀）。"""
        return self.title

    def default_description(self) -> str:
        desc = self.title
        if self.selected_sku and self.selected_sku not in desc:
            desc = f"{desc}（{self.selected_sku}）"
        return desc

    def main_image_url(self) -> str | None:
        """主图 URL：选中 SKU 的图优先，其次图集第一张。"""
        if self.selected_sku:
            for sku in self.skus:
                if sku.text == self.selected_sku and sku.image_url:
                    return sku.image_url
        return self.gallery[0] if self.gallery else None

    def embedded_bytes(self, url: str | None) -> tuple[bytes, str] | None:
        """按 URL 取内嵌图片字节，返回 (bytes, 扩展名)；无则 None。

        URL 匹配忽略查询串（淘宝图链常带 `.webp`/`_q50.jpg` 等处理后缀，
        内嵌资源的 Content-Location 与页面引用一致）。
        """
        if not url:
            return None
        key = url.split("?")[0]
        data = self.embedded.get(key) or self.embedded.get(url)
        if not data:
            return None
        return data, _image_ext(data, url)


def _image_ext(data: bytes, url: str) -> str:
    """按魔数判断图片扩展名（webp/png/jpeg/gif），兜底看 URL。"""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    m = re.search(r"\.(jpe?g|png|webp|gif)", url, re.I)
    return f".{m.group(1).lower()}" if m else ".jpg"


def _decode_part(part: Message) -> str | None:
    """解码 text/html 段：淘宝保存为 UTF-8，兜底 gb18030。"""
    raw = part.get_payload(decode=True)
    if raw is None:
        return None
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _main_html(msg: Message) -> tuple[str, str | None]:
    """找商品页主 HTML：优先 item.taobao.com 的段，其次最大的 text/html 段。"""
    best: tuple[int, str, str | None] | None = None  # (size, html, location)
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        html = _decode_part(part)
        if not html:
            continue
        loc = part.get("Content-Location", "") or None
        if loc and "item.taobao.com" in loc:
            return html, loc
        if best is None or len(html) > best[0]:
            best = (len(html), html, loc)
    if best is None:
        raise ValueError("MHTML 中找不到 text/html 内容")
    return best[1], best[2]


def _by_prefix(soup: BeautifulSoup, prefix: str) -> list:
    """按 class 前缀匹配元素（CSS-module 哈希后缀随构建变化）。"""
    return [
        el
        for el in soup.find_all(True)
        if any(c.startswith(prefix + "--") for c in (el.get("class") or []))
    ]


def _first_text(soup: BeautifulSoup, prefix: str) -> str | None:
    els = _by_prefix(soup, prefix)
    return els[0].get_text(strip=True) or None if els else None


def _first_img_src(el) -> str | None:
    img = el.find("img")
    if img is None:
        return None
    return img.get("src") or img.get("data-src") or None


def _parse_title(soup: BeautifulSoup) -> str:
    t = _first_text(soup, "mainTitle")
    if t:
        return t.strip()
    if soup.title:
        text = soup.title.get_text(strip=True)
        for suffix in TAOBAO_TITLE_SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)]
        return text.strip()
    return ""


def _parse_price(soup: BeautifulSoup) -> float | None:
    for el in _by_prefix(soup, "highlightPrice"):
        m = PRICE_RE.search(el.get_text(" ", strip=True))
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                continue
    return None


def _parse_skus(soup: BeautifulSoup) -> list[TaobaoSku]:
    out: list[TaobaoSku] = []
    for el in _by_prefix(soup, "valueItem"):
        classes = el.get("class") or []
        text_el = _by_prefix(el, "valueItemText")
        text = (text_el[0].get_text(strip=True) if text_el else "") or ""
        if not text:
            img = el.find("img")
            text = (img.get("alt") or "").strip() if img else ""
        if not text:
            continue
        out.append(
            TaobaoSku(
                text=text,
                image_url=_first_img_src(el),
                selected=any(c.startswith("isSelected--") for c in classes),
            )
        )
    return out


def _parse_params(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """规格参数表（title=名称, subtitle=值）。

    「重点参数」emphasisParams 是反的：Title 是值，SubTitle 是名称。
    """
    out: list[tuple[str, str]] = []
    for it in _by_prefix(soup, "generalParamsInfoItem"):
        t = _by_prefix(it, "generalParamsInfoItemTitle")
        s = _by_prefix(it, "generalParamsInfoItemSubTitle")
        if t and s:
            name, value = t[0].get_text(strip=True), s[0].get_text(strip=True)
            if name and value:
                out.append((name, value))
    seen = {n for n, _ in out}
    for it in _by_prefix(soup, "emphasisParamsInfoItem"):
        t = _by_prefix(it, "emphasisParamsInfoItemTitle")
        s = _by_prefix(it, "emphasisParamsInfoItemSubTitle")
        if t and s:
            name, value = s[0].get_text(strip=True), t[0].get_text(strip=True)
            if name and value and name not in seen:
                out.append((name, value))
                seen.add(name)
    return out


def _parse_gallery(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for el in _by_prefix(soup, "thumbnailPic") + _by_prefix(soup, "mainPic"):
        src = el.get("src") or el.get("data-src")
        if not src:
            img = el.find("img")
            src = img.get("src") or img.get("data-src") if img else None
        if not src:
            continue
        key = src.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(src)
    return out


def _embedded_images(msg: Message) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for part in msg.walk():
        loc = part.get("Content-Location", "")
        if not loc or part.get_content_maintype() == "text":
            continue
        raw = part.get_payload(decode=True)
        if not raw:
            continue
        out.setdefault(loc.split("?")[0], raw)
    return out


def parse_mhtml(data: bytes, *, source_filename: str | None = None) -> TaobaoItem:
    """解析淘宝商品页 MHTML 字节流为 `TaobaoItem`。

    Raises:
        ValueError: 不是合法 MHTML 或找不到商品页内容。
    """
    try:
        msg = email.message_from_bytes(data)
    except Exception as exc:  # noqa: BLE001 — 统一为清晰报错
        raise ValueError(f"无法解析 MHTML: {exc}") from exc
    if not msg.is_multipart():
        raise ValueError("文件不是 MHTML（multipart）格式")

    html, part_loc = _main_html(msg)
    url = (msg.get("Snapshot-Content-Location") or part_loc or "").strip()
    soup = BeautifulSoup(html, "lxml")

    m = ITEM_ID_RE.search(url)
    item_id = m.group(1) if m else None
    m = SKU_ID_RE.search(url)
    sku_id = m.group(1) if m else None

    skus = _parse_skus(soup)
    selected = next((s.text for s in skus if s.selected), None)

    return TaobaoItem(
        url=url,
        item_id=item_id,
        sku_id=sku_id,
        title=_parse_title(soup),
        shop=_first_text(soup, "shopName"),
        price=_parse_price(soup),
        selected_sku=selected,
        skus=skus,
        params=_parse_params(soup),
        gallery=_parse_gallery(soup),
        embedded=_embedded_images(msg),
        source_filename=source_filename,
    )
