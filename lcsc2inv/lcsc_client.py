"""从 LCSC 商品页 SSR HTML 中抽取 ld+json 并构造 `LCSCPart`。

数据源策略：
- 主路径：解析 `<script type="application/ld+json">` 中的 Schema.org `Product` 对象
- 兜底：尝试 `<script id="__NEXT_DATA__">` 中的 `productDetailsVo`
- URL 模式：统一使用 `https://www.lcsc.com/product-detail/C{code}.html`

HTTP 获取：
- `Fetcher` 类封装 requests Session + 速率限制 + 文件缓存 + 失败重试
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from lcsc2inv.config import Settings, get_settings
from lcsc2inv.lcsc_models import Brand, LCSCPart, Offer, PropertyValue, SubjectOf

logger = logging.getLogger(__name__)

LCSC_PRODUCT_URL_TEMPLATE = "https://www.lcsc.com/product-detail/{code}.html"
# 国内立创商城（item.szlcsc.com）使用数字内部 ID，不接受 C-code 直链
LCSC_CN_PRODUCT_URL_TEMPLATE = "https://item.szlcsc.com/{numeric_id}.html"
LCSC_URL_PATTERN = re.compile(r"/product-detail/C(\d+)\.html", re.IGNORECASE)
LCSC_CN_URL_PATTERN = re.compile(r"item\.szlcsc\.com/(\d+)\.html", re.IGNORECASE)
LDJSON_PATTERN = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
NEXTDATA_PATTERN = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def parse_lcsc_code(value: str) -> str:
    """从 C-code 字符串或 LCSC URL 中抽出 C-code。

    支持两种 URL 模式：
    - 国际版：`https://www.lcsc.com/product-detail/C28323.html` → `C28323`
    - 国内版：`https://item.szlcsc.com/360864.html` → `CN:360864`

    >>> parse_lcsc_code('C28323')
    'C28323'
    >>> parse_lcsc_code('https://www.lcsc.com/product-detail/C28323.html')
    'C28323'
    >>> parse_lcsc_code('  c-12345 ')
    'C12345'
    """
    if not value:
        raise ValueError("input is empty")
    v = value.strip()
    # 国际版 C-code URL
    m = LCSC_URL_PATTERN.search(v)
    if m:
        return f"C{m.group(1)}"
    # 国内版数字 URL 也接受（保留为 "CN:<id>" 形式供调用方识别）
    m = LCSC_CN_URL_PATTERN.search(v)
    if m:
        return f"CN:{m.group(1)}"
    v = v.upper().replace(" ", "")
    # 容许前缀分隔符：`C-191386`、`C.191386`、`C/191386`
    v = re.sub(r"^C[-./\\]+", "C", v)
    if re.fullmatch(r"C\d+", v):
        return v
    # 容许国内站 ID 形式：`CN:360864` 或 `CN-360864`
    m = re.fullmatch(r"CN[-.:]?(\d+)", v)
    if m:
        return f"CN:{m.group(1)}"
    # 裸数字也接受（视作国内版 numeric id）
    if re.fullmatch(r"\d+", v):
        return f"CN:{v}"
    raise ValueError(f"无法识别为 LCSC C-code 或商品 URL: {value!r}")


def product_url(code: str, base: str = "www.lcsc.com") -> str:
    """根据 code 构造商品详情 URL。

    `CN:<id>` 形式走国内站，其它走国际站。
    """
    if code.startswith("CN:"):
        numeric_id = code[3:]
        if "szlcsc.com" in base:
            return f"https://{base.rstrip('/')}/{numeric_id}.html"
        return LCSC_CN_PRODUCT_URL_TEMPLATE.format(numeric_id=numeric_id)
    if "szlcsc.com" in base:
        # 国内站 URL 需要 numeric_id，但 code 是 C-code，无法直链
        raise ValueError(
            f"国内站 {base} 不支持 C-code 直链，需要先通过搜索接口把 {code} 转为 numeric id。"
            f"当前 lcsc2inventree 不支持该转换；建议使用国际站 www.lcsc.com。"
        )
    return LCSC_PRODUCT_URL_TEMPLATE.format(code=code)


def _extract_ldjson_dict(soup: BeautifulSoup) -> dict[str, Any] | None:
    """在 HTML 中找出 Schema.org Product 的 ld+json dict。

    容错：可能存在多个 ld+json 块（面包屑、网站级），需逐个尝试。
    """
    for raw in LDJSON_PATTERN.findall(str(soup)):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # ld+json 可能是 @graph 列表
        candidates: list[dict] = []
        if isinstance(data, dict):
            candidates.append(data)
            if isinstance(data.get("@graph"), list):
                candidates.extend(x for x in data["@graph"] if isinstance(x, dict))
        for c in candidates:
            if c.get("@type") == "Product" and c.get("sku"):
                return c
    return None


def _extract_nextdata_dict(soup: BeautifulSoup) -> dict[str, Any] | None:
    """解析 __NEXT_DATA__ 中的 productDetailsVo。LCSC Next.js 重构后该字段

    仍包含 MPN / parameters / catalog 等结构，作为 ld+json 失效时的兜底。
    """
    m = NEXTDATA_PATTERN.search(str(soup))
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    # 路径：props.pageProps.productDetailsVo (不同部署可能略不同)
    props = data.get("props", {}).get("pageProps", {})
    return props.get("productDetailsVo") or props.get("productDetail") or None


def _additional_props(raw: list[Any]) -> list[PropertyValue]:
    out: list[PropertyValue] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if name is None or value is None:
            continue
        out.append(
            PropertyValue(
                name=str(name),
                value=str(value),
                unitText=item.get("unitText") or item.get("unit"),
            )
        )
    return out


def parse_ldjson(html: str, *, page_url: str | None = None) -> LCSCPart:
    """将 LCSC 商品页 HTML 解析为 `LCSCPart`。

    Raises:
        ValueError: 当 HTML 中找不到 ld+json 或缺少 `sku` 时。
    """
    soup = BeautifulSoup(html, "lxml")
    raw = _extract_ldjson_dict(soup)
    if raw is None:
        # 尝试 Next.js __NEXT_DATA__ 兜底
        nd = _extract_nextdata_dict(soup)
        if nd is None:
            raise ValueError("HTML 中找不到 application/ld+json / __NEXT_DATA__")
        return _from_nextdata(nd, page_url=page_url)

    sku = str(raw.get("sku") or "").strip()
    if not sku:
        raise ValueError("ld+json 缺少 sku")

    brand = None
    b = raw.get("brand")
    if isinstance(b, dict) and b.get("name"):
        brand = Brand(name=str(b["name"]))

    offer = None
    o = raw.get("offers")
    if isinstance(o, dict):
        offer = Offer.from_raw(o)

    datasheet = None
    s = raw.get("subjectOf")
    if isinstance(s, dict) and s.get("url"):
        datasheet = SubjectOf(name=s.get("name"), url=s.get("url"))
        datasheet_url = datasheet.url
    else:
        datasheet_url = None

    return LCSCPart(
        sku=sku,
        mpn=(str(raw["mpn"]).strip() if raw.get("mpn") else None),
        name=raw.get("name"),
        description=raw.get("description"),
        brand=brand,
        category=raw.get("category"),
        image_urls=list(raw.get("image") or []),
        additional_properties=_additional_props(raw.get("additionalProperty") or []),
        offer=offer,
        datasheet_url=datasheet_url,
        page_url=page_url or (offer.url if offer and offer.url else product_url(sku)),
    )


def _from_nextdata(nd: dict[str, Any], *, page_url: str | None) -> LCSCPart:
    """从 Next.js 的 `productDetailsVo` 构造 LCSCPart（ld+json 失效时的兜底）。"""
    sku = str(nd.get("productCode") or nd.get("lcscCode") or "").strip()
    if not sku:
        raise ValueError("__NEXT_DATA__ 缺少 productCode")
    brand_name = nd.get("brandNameEn") or nd.get("brandName")
    props_raw = nd.get("paramVOList") or nd.get("paramList") or []
    # Next.js 结构是 [{ paramNameEn, paramValueEn }]
    properties: list[PropertyValue] = []
    for p in props_raw:
        if not isinstance(p, dict):
            continue
        name = p.get("paramNameEn") or p.get("paramName")
        value = p.get("paramValueEn") or p.get("paramValue")
        if name and value:
            properties.append(PropertyValue(name=str(name), value=str(value)))
    images = []
    for k in ("productImages", "images"):
        v = nd.get(k)
        if isinstance(v, list):
            images = [str(x) for x in v if x]
            break
    return LCSCPart(
        sku=sku,
        mpn=nd.get("productModel"),
        name=nd.get("productTitleEn") or nd.get("productTitle"),
        description=nd.get("productIntroEn") or nd.get("productDescEn"),
        brand=Brand(name=str(brand_name)) if brand_name else None,
        category="/".join(filter(None, [nd.get("parentCatalogName"), nd.get("catalogName")])),
        image_urls=images,
        additional_properties=properties,
        offer=None,  # Next.js 的价格/库存不在同一结构，跳过
        datasheet_url=nd.get("pdfUrl"),
        page_url=page_url or product_url(sku),
    )


def load_fixture(path: str | Path) -> LCSCPart:
    """从本地文件读取 HTML 并解析（用于测试 / 离线运行）。"""
    p = Path(path)
    html = p.read_text(encoding="utf-8")
    # fixture 文件名约定：
    #   C28323.html         → 国际版 C-code
    #   CN_360864.html      → 国内版 numeric id（前缀 CN_）
    #   cn_360864.html      → 同上
    stem = p.stem
    if stem.upper().startswith("CN_") or stem.lower().startswith("cn"):
        # 去掉前缀，取数字
        numeric = re.sub(r"^[Cc][Nn][_-]?", "", stem)
        page_url = LCSC_CN_PRODUCT_URL_TEMPLATE.format(numeric_id=numeric)
        # 从页面抽 sku 作主 ID
        return parse_ldjson(html, page_url=page_url)
    page_url = product_url(parse_lcsc_code(stem))
    return parse_ldjson(html, page_url=page_url)


# ---------------------------------------------------------------------------
# HTTP fetcher
# ---------------------------------------------------------------------------


class LcscFetchError(RuntimeError):
    """抓取 LCSC 商品页失败。"""


@dataclass(slots=True)
class Fetcher:
    """带速率限制 + 文件缓存 + 重试的 LCSC HTTP 客户端。

    单线程使用：内部维护 `last_request_at`，每次 `fetch` 前 sleep 到满足
    `settings.lcsc_request_interval`（加 ±20% 抖动）。
    """

    settings: Settings
    session: requests.Session = None  # type: ignore[assignment]
    last_request_at: float = 0.0

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()
            self.session.headers.update(
                {
                    "User-Agent": self.settings.lcsc_user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
                    "Cache-Control": "no-cache",
                }
            )

    # -- cache ----------------------------------------------------------

    def _cache_path(self, code: str) -> Path:
        d = self.settings.cache_dir_path
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{code}.json"

    def _cache_meta_path(self, code: str) -> Path:
        return self._cache_path(code).with_suffix(".meta.json")

    def _cache_read(self, code: str) -> str | None:
        if not self.settings.lcsc_cache_enabled:
            return None
        p = self._cache_path(code)
        meta = self._cache_meta_path(code)
        if not p.exists() or not meta.exists():
            return None
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        age_days = (time.time() - float(m.get("fetched_at", 0))) / 86400
        if age_days > self.settings.lcsc_cache_ttl_days:
            return None
        return p.read_text(encoding="utf-8")

    def _cache_write(self, code: str, html: str) -> None:
        if not self.settings.lcsc_cache_enabled:
            return
        self._cache_path(code).write_text(html, encoding="utf-8")
        self._cache_meta_path(code).write_text(
            json.dumps({"fetched_at": time.time()}), encoding="utf-8"
        )

    # -- rate limit -----------------------------------------------------

    def _throttle(self) -> None:
        interval = max(0.0, self.settings.lcsc_request_interval)
        if interval <= 0:
            return
        now = time.monotonic()
        wait = interval - (now - self.last_request_at)
        if wait > 0:
            # 加 ±20% 抖动避免指纹特征
            jitter = wait * random.uniform(-0.2, 0.2)
            time.sleep(max(0.0, wait + jitter))
        self.last_request_at = time.monotonic()

    # -- fetch ----------------------------------------------------------

    def fetch(self, code: str) -> LCSCPart:
        """抓取并解析单个 LCSC C-code。优先缓存。"""
        code = parse_lcsc_code(code)
        cached_html = self._cache_read(code)
        if cached_html is not None:
            logger.debug("缓存命中 %s", code)
            return parse_ldjson(cached_html, page_url=product_url(code))

        url = product_url(code)
        html = self._fetch_html(url)
        self._cache_write(code, html)
        return parse_ldjson(html, page_url=url)

    def _fetch_html(self, url: str) -> str:
        last_exc: Exception | None = None
        for attempt in Retrying(
            stop=stop_after_attempt(self.settings.lcsc_max_retries),
            wait=wait_exponential(multiplier=1.0, min=1.0, max=8.0),
            retry=retry_if_exception_type((requests.RequestException, LcscFetchError)),
            reraise=True,
        ):
            with attempt:
                self._throttle()
                try:
                    resp = self.session.get(url, timeout=20)
                except requests.RequestException as exc:
                    last_exc = exc
                    raise
                if resp.status_code in (403, 429):
                    raise LcscFetchError(f"被限流 HTTP {resp.status_code} url={url}")
                if resp.status_code >= 400:
                    raise LcscFetchError(f"HTTP {resp.status_code} url={url}")
                if "application/ld+json" not in resp.text and "__NEXT_DATA__" not in resp.text:
                    raise LcscFetchError(f"页面缺 ld+json / __NEXT_DATA__，可能被反爬 url={url}")
                return resp.text
        # 不应该到这里，但保险起见
        raise LcscFetchError(f"多次重试失败 url={url} last={last_exc}")

    # -- image download -------------------------------------------------

    def _image_cache_path(self, code: str) -> Path:
        d = self.settings.cache_dir_path / "images"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{code}.jpg"

    def _image_meta_path(self, code: str) -> Path:
        return self._image_cache_path(code).with_suffix(".meta.json")

    def _image_cache_hit(self, code: str) -> Path | None:
        """图片缓存命中且未过期：返回本地路径；否则 None。"""
        if not self.settings.lcsc_cache_enabled:
            return None
        img = self._image_cache_path(code)
        meta = self._image_meta_path(code)
        if not img.exists() or not meta.exists():
            return None
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        age_days = (time.time() - float(m.get("fetched_at", 0))) / 86400
        if age_days > self.settings.lcsc_cache_ttl_days:
            return None
        return img

    def download_image(self, code: str, url: str) -> Path:
        """下载 LCSC 商品图（image_urls[0]）到本地缓存并返回路径。

        复用 `_throttle()`（共享 1 req/s 限速）+ tenacity 重试。
        缓存文件：`<cache_dir>/images/<code>.jpg` + `<code>.meta.json`。
        调用方负责使用后清理本地文件（典型场景：upload 完即 unlink）。
        """
        cached = self._image_cache_hit(code)
        if cached is not None:
            return cached

        last_exc: Exception | None = None
        for attempt in Retrying(
            stop=stop_after_attempt(self.settings.lcsc_max_retries),
            wait=wait_exponential(multiplier=1.0, min=1.0, max=8.0),
            retry=retry_if_exception_type((requests.RequestException, LcscFetchError)),
            reraise=True,
        ):
            with attempt:
                self._throttle()
                try:
                    resp = self.session.get(url, timeout=20)
                except requests.RequestException as exc:
                    last_exc = exc
                    raise
                if resp.status_code in (403, 429):
                    raise LcscFetchError(f"图片下载被限流 HTTP {resp.status_code} url={url}")
                if resp.status_code >= 400:
                    raise LcscFetchError(f"图片下载 HTTP {resp.status_code} url={url}")
                img_path = self._image_cache_path(code)
                img_path.write_bytes(resp.content)
                self._image_meta_path(code).write_text(
                    json.dumps({"fetched_at": time.time()}), encoding="utf-8"
                )
                return img_path
        raise LcscFetchError(f"图片多次重试失败 url={url} last={last_exc}")


def default_fetcher(settings: Settings | None = None) -> Fetcher:
    return Fetcher(settings=settings or get_settings())