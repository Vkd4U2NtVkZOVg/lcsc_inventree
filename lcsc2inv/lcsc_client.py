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
import os
import random
import re
import time
from dataclasses import dataclass, field
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
# 国内站搜索页（带 JS 挑战，需浏览器渲染）；C-code → 第一个匹配商品 → 数字 ID
SO_SEARCH_URL_TEMPLATE = "https://so.szlcsc.com/global.html?k={code}"
LCSC_URL_PATTERN = re.compile(r"/product-detail/C(\d+)\.html", re.IGNORECASE)
LCSC_CN_URL_PATTERN = re.compile(
    r"item\.szlcsc\.com/(?:mro/)?(\d+)\.html", re.IGNORECASE
)
LDJSON_PATTERN = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
NEXTDATA_PATTERN = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# 国内站商品描述里的营销模板文案（固定整句，出现即删）
LCSC_CN_DESC_BOILERPLATE = (
    "提供高清引脚图、PCB焊盘图、3D模型及Datasheet数据手册，"
    "支持选型与设计参考，正品现货，一站式元器件采购尽在立创商城。"
)


def clean_description(text: str | None) -> str | None:
    """去掉国内站商品描述里的营销模板文案。

    删除后若句尾残留逗号/空白一并清理；全空返回 None。
    """
    if not text:
        return text
    s = text.replace(LCSC_CN_DESC_BOILERPLATE, "")
    s = re.sub(r"[，,]\s*$", "", s).strip()
    return s or None


def _looks_like_waf(html: str) -> bool:
    """立创国内站常见阿里云 WAF 挑战页，不含商品数据。"""
    markers = ("aliyun_waf", "acw_sc__v2", "renderData")
    return any(marker in html for marker in markers)


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


def _extract_cn_footprint(soup: BeautifulSoup) -> str | None:
    """国内站商品页的「商品封装」在 <dt>商品封装</dt><dd>…</dd> 参数表里。

    ld+json / __NEXT_DATA__ 都不带该字段，只能从 SSR HTML 抽取。
    """
    for dt in soup.find_all("dt"):
        if "封装" not in dt.get_text(strip=True):
            continue
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue
        # dd 文本可能带零宽空格（如 "弯插\u200b"）
        value = dd.get_text(strip=True).replace("\u200b", "").strip()
        if value:
            return value
    return None


def _with_cn_footprint(part: LCSCPart, soup: BeautifulSoup) -> LCSCPart:
    """页面参数表里有封装而结构化数据没有时，补进 additional_properties。"""
    if part.package:
        return part
    value = _extract_cn_footprint(soup)
    if value:
        part.additional_properties.append(PropertyValue(name="封装", value=value))
    return part


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
        return _with_cn_footprint(_from_nextdata(nd, page_url=page_url), soup)

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

    return _with_cn_footprint(
        LCSCPart(
            sku=sku,
            mpn=(str(raw["mpn"]).strip() if raw.get("mpn") else None),
            name=raw.get("name"),
            description=clean_description(raw.get("description")),
            brand=brand,
            category=raw.get("category"),
            image_urls=list(raw.get("image") or []),
            additional_properties=_additional_props(raw.get("additionalProperty") or []),
            offer=offer,
            datasheet_url=datasheet_url,
            page_url=page_url or (offer.url if offer and offer.url else product_url(sku)),
        ),
        soup,
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
        description=clean_description(nd.get("productIntroEn") or nd.get("productDescEn")),
        brand=Brand(name=str(brand_name)) if brand_name else None,
        category="/".join(filter(None, [nd.get("parentCatalogName"), nd.get("catalogName")])),
        image_urls=images,
        additional_properties=properties,
        offer=None,  # Next.js 的价格/库存不在同一结构，跳过
        datasheet_url=nd.get("pdfUrl"),
        page_url=page_url or product_url(sku),
    )


def extract_cn_product_id(html: str) -> int | None:
    """从国际站商品页 `__NEXT_DATA__` 中取国内站数字商品 ID。

    国际站 `props.pageProps.webData.productId` 与国内站 `item.szlcsc.com/<id>.html`
    的数字 ID 同源（同一商品库），可作 C-code → 国内页的直连映射。
    """
    m = NEXTDATA_PATTERN.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    wd = data.get("props", {}).get("pageProps", {}).get("webData") or {}
    pid = wd.get("productId")
    try:
        return int(pid) if pid else None
    except (TypeError, ValueError):
        return None


def parse_cn_page(html: str, *, page_url: str | None = None) -> LCSCPart:
    """解析国内站 `item.szlcsc.com` 商品页为 `LCSCPart`。

    当前站点结构是 `__NEXT_DATA__` → `props.pageProps.webData`：
    - `productRecord`：productCode / productModel / productName / 图片
    - `brandVO`：品牌
    - `paramList`：中文参数表
    - `totalStockNumber`：总库存

    旧版页面（无 webData.productRecord）回退到 `productDetailsVo` 解析。
    Raises:
        ValueError: 结构对不上且无 productCode 时。
    """
    m = NEXTDATA_PATTERN.search(html)
    if not m:
        raise ValueError("国内站页面缺少 __NEXT_DATA__")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"__NEXT_DATA__ JSON 解析失败: {exc}") from exc
    pp = data.get("props", {}).get("pageProps", {})
    web = pp.get("webData") or {}
    rec = web.get("productRecord") or {}
    sku = str(rec.get("productCode") or "").strip()
    if not sku:
        # 旧版结构兜底
        return _from_nextdata(pp.get("productDetailsVo") or pp or {}, page_url=page_url)

    brand = None
    bv = web.get("brandVO") or {}
    brand_name = bv.get("brandName") or bv.get("brandNameEn")
    if brand_name:
        brand = Brand(name=str(brand_name))

    params: list[PropertyValue] = []
    for p in web.get("paramList") or []:
        if not isinstance(p, dict):
            continue
        name = p.get("parameterName")
        value = p.get("parameterValue") or p.get("parameterDetailValue")
        if name and value:
            params.append(PropertyValue(name=str(name).strip(), value=str(value).strip()))

    images: list[str] = []
    raw_imgs = rec.get("luceneBreviaryImageUrls")
    if isinstance(raw_imgs, str) and raw_imgs:
        # 多图以 <$> 分隔，末尾常混入品牌证书图（certificate 路径），一并过滤
        images = [
            u.strip()
            for u in raw_imgs.split("<$>")
            if u.strip().startswith("http") and "/certificate/" not in u
        ]
    if not images and rec.get("breviaryImageUrl"):
        images = [str(rec["breviaryImageUrl"])]

    offer = None
    stock = web.get("totalStockNumber")
    if stock is not None:
        try:
            offer = Offer(priceCurrency="CNY", inventoryLevel=int(stock))
        except (TypeError, ValueError):
            offer = None

    catalog = web.get("currentCatalog") or {}
    category = catalog.get("catalogNameEn") or catalog.get("catalogName") or None
    cn_category = (catalog.get("catalogName") or None) if category != catalog.get("catalogName") else None

    return LCSCPart(
        sku=sku,
        mpn=(str(rec["productModel"]).strip() if rec.get("productModel") else None),
        name=rec.get("productName"),
        description=rec.get("productName"),
        brand=brand,
        category=category,
        image_urls=images,
        additional_properties=params,
        offer=offer,
        datasheet_url=None,
        page_url=page_url,
        cn_category_name=cn_category,
    )


def merge_cn_part(cn: LCSCPart, intl: LCSCPart) -> LCSCPart:
    """国内站数据为主（中文名 / 描述 / 参数），国际站补齐其余字段。

    名称 = MPN（器件真实型号，如 "CL21C470JBANNNC"）；描述 = 国内站中文
    规格串，缺类目词时拼上中文类目（如 "贴片电容 47pF ±5% 50V"）。
    分类 / 价格 / 品牌名 / 图片优先国际站：分类映射表是英文 key，
    品牌名与已有 Manufacturer 保持一致，国际站图片为高清原图；
    国际站无实图（no_goods_pic 占位）时退回国内站图片。
    """
    def _with_category(text: str | None) -> str | None:
        """描述缺中文类目词时拼前缀；已含（如 "厚膜电阻…"）则原样返回。"""
        if not text or not cn_cat:
            return text
        tail = cn_cat[-2:] if len(cn_cat) >= 2 else cn_cat
        if cn_cat in text or tail in text:
            return text
        return f"{cn_cat} {text}"

    cn_cat = cn.cn_category_name
    # Part 名称 = 器件真实型号（MPN）；中文规格串（含类目前缀）只作描述
    name = intl.mpn or cn.mpn or intl.name or cn.name or cn.sku
    description = _with_category(cn.description or intl.description)
    intl_images = [u for u in intl.image_urls if "no_goods_pic" not in u]
    return LCSCPart(
        sku=cn.sku,
        mpn=intl.mpn or cn.mpn,
        name=name,
        description=description,
        brand=intl.brand or cn.brand,
        category=intl.category or cn.category,
        image_urls=intl_images or cn.image_urls,
        additional_properties=intl.additional_properties or cn.additional_properties,
        offer=intl.offer or cn.offer,
        datasheet_url=intl.datasheet_url or cn.datasheet_url,
        page_url=intl.page_url,
        cn_category_name=cn_cat,
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


# 进程级共享的 Playwright 浏览器。
# sync API 启动后会在所在线程留下一个运行中的事件循环；web 层每个请求都新建
# Fetcher 实例，若把浏览器存在实例上，同一线程的第二次 start() 会撞上该循环并报
# 「Playwright Sync API inside the asyncio loop」。因此浏览器全局唯一，gunicorn
# 单 worker 单线程下所有请求复用；Chromium 崩溃时自动重启。
_GLOBAL_BROWSER: dict[str, Any] = {"pw": None, "browser": None}


def _get_shared_browser():
    if (
        _GLOBAL_BROWSER["browser"] is not None
        and _GLOBAL_BROWSER["browser"].is_connected()
    ):
        return _GLOBAL_BROWSER["browser"]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LcscFetchError("当前环境没有 Playwright 浏览器") from exc
    if _GLOBAL_BROWSER["pw"] is None:
        logger.info("[浏览器] 启动共享 Playwright Chromium…")
        _GLOBAL_BROWSER["pw"] = sync_playwright().start()
    logger.info("[浏览器] 启动 Chromium（共享实例）")
    _GLOBAL_BROWSER["browser"] = _GLOBAL_BROWSER["pw"].chromium.launch(headless=True)
    return _GLOBAL_BROWSER["browser"]


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
        """抓取并解析单个 LCSC C-code 或商品 URL。优先缓存。"""
        raw_value = code.strip() if isinstance(code, str) else code
        code = parse_lcsc_code(raw_value)
        # 国内 MRO 页面使用 /mro/<id>.html；规范化为 CN:<id> 后不能再丢失该路径。
        source_url = raw_value if isinstance(raw_value, str) and "/mro/" in raw_value.lower() else None
        cached_html = self._cache_read(code)
        if cached_html is not None:
            logger.debug("缓存命中 %s", code)
            return self._parse_page(cached_html, code, source_url)
        url = source_url or product_url(code)
        logger.info("[国际站] %s: 抓取商品页 %s", code, url)
        html = self._fetch_html(url)
        self._cache_write(code, html)
        return self._parse_page(html, code, url)

    def _parse_page(self, html: str, code: str, page_url: str | None) -> LCSCPart:
        """按编码形态选择解析器：国内站优先 webData 新结构，失败回退 ld+json。"""
        if code.startswith("CN:"):
            try:
                return parse_cn_page(html, page_url=page_url)
            except ValueError as exc:
                logger.debug("国内站 webData 解析失败，回退 ld+json: %s", exc)
        return parse_ldjson(html, page_url=page_url)

    # -- CN data source（国内站 item.szlcsc.com）---------------------------

    _CN_ID_MAP_FILENAME = "cn_id_map.json"

    def _cn_id_map_path(self) -> Path:
        return self.settings.cache_dir_path / self._CN_ID_MAP_FILENAME

    def _cn_id_map_get(self, code: str) -> int | None:
        """读取 C-code → 国内站数字 ID 的持久映射（ID 与商品一一对应，无需 TTL）。"""
        p = self._cn_id_map_path()
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        value = (data.get("entries") or {}).get(code)
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    def _cn_id_map_put(self, code: str, numeric_id: int) -> None:
        p = self._cn_id_map_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        entries: dict[str, int] = {}
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                entries = {k: int(v) for k, v in (raw.get("entries") or {}).items()}
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                entries = {}
        entries[code] = int(numeric_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False, indent=0),
            encoding="utf-8",
        )
        os.replace(tmp, p)

    def _cn_numeric_id(self, code: str) -> int | None:
        """C-code → 国内站数字 ID：先查映射缓存，未命中再从国际站页面抽取。"""
        cached = self._cn_id_map_get(code)
        if cached:
            return cached
        html = self._cache_read(code)
        if html is None:
            try:
                html = self._fetch_html(product_url(code))
            except LcscFetchError as exc:
                logger.warning("抓取国际站页面失败，无法取得国内商品 ID %s: %s", code, exc)
                return None
            self._cache_write(code, html)
        numeric = extract_cn_product_id(html)
        if numeric:
            logger.info("[CN数据源] %s: 国际站页解析出数字 ID #%d", code, numeric)
            self._cn_id_map_put(code, numeric)
        return numeric

    def _cn_numeric_id_via_search(self, code: str) -> int | None:
        """优先用国内站搜索页解析数字 ID（国际站不可达时的主路径）。"""
        cached = self._cn_id_map_get(code)
        if cached:
            logger.info("[CN数据源] %s: 数字 ID 缓存命中 #%d", code, cached)
            return cached
        logger.info("[CN数据源] %s: 无 ID 缓存，打开国内站搜索页解析…", code)
        numeric = self._search_first_cn_id(code)
        if numeric:
            logger.info("[CN数据源] %s: 搜索页命中第一个匹配项 #%d", code, numeric)
            self._cn_id_map_put(code, numeric)
        return numeric

    def _get_browser(self):
        """惰性获取进程级共享 Playwright Chromium（WAF 挑战页 / 搜索页渲染）。"""
        return _get_shared_browser()

    def _search_first_cn_id(self, code: str) -> int | None:
        """在 so.szlcsc.com 搜索页取第一个匹配项的数字 ID。

        搜索页带 JS 挑战（瑞数），必须 Playwright 渲染；从渲染后的页面内容里
        抽取第一个 `item.szlcsc.com/<id>.html` 链接。任何失败返回 None。
        """
        try:
            browser = self._get_browser()
        except LcscFetchError:
            return None
        url = SO_SEARCH_URL_TEMPLATE.format(code=code)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            try:
                # 挑战页会自动重载；等第一个商品链接出现，超时后兜底扫全文
                page.wait_for_selector('a[href*="item.szlcsc.com"]', timeout=15000)
            except Exception:  # noqa: BLE001
                page.wait_for_timeout(2000)
            content = page.content()
            ids = re.findall(r"item\.szlcsc\.com/(\d+)\.html", content)
            if not ids:
                logger.warning("国内站搜索页未找到商品链接 %s", code)
                return None
            return int(ids[0])
        except Exception as exc:  # noqa: BLE001
            logger.warning("国内站搜索页解析 %s 失败: %s", code, exc)
            return None
        finally:
            page.close()

    def _fetch_cn_page_part(self, numeric: int) -> LCSCPart | None:
        """抓取并解析国内站商品页（带 7 天 HTML 缓存）；失败返回 None。"""
        cn_url = LCSC_CN_PRODUCT_URL_TEMPLATE.format(numeric_id=numeric)
        cn_key = f"CN:{numeric}"
        html = self._cache_read(cn_key)
        if html is None:
            logger.info("[CN数据源] 国内页 #%d: 抓取 %s", numeric, cn_url)
            try:
                html = self._fetch_html(cn_url)
            except LcscFetchError as exc:
                logger.warning("抓取国内站页面失败 #%s: %s", numeric, exc)
                return None
            self._cache_write(cn_key, html)
        else:
            logger.info("[CN数据源] 国内页 #%d: HTML 缓存命中", numeric)
        try:
            return parse_cn_page(html, page_url=cn_url)
        except ValueError as exc:
            logger.warning("国内站页面解析失败 #%s: %s", numeric, exc)
            return None

    def fetch_cn_part(self, code: str) -> LCSCPart:
        """抓取国内站（item.szlcsc.com）商品数据，中文名/描述/参数优先。

        数字 ID 解析顺序（结果永久缓存于 `cn_id_map.json`）：
        1. `so.szlcsc.com` 搜索页第一个匹配项（Playwright，国内直连可靠）
        2. 国际站商品页 `webData.productId`（requests，国际站可达时更快）

        国际站数据用于补齐分类 / 价格 / 品牌名；**国际站不可达时不阻塞导入**，
        直接返回国内站数据。仅当两条路径都失败时抛出原异常。

        Raises:
            LcscFetchError / ValueError: 国内国际均失败时照常抛出。
        """
        code = parse_lcsc_code(code)
        if code.startswith("CN:"):
            return self.fetch(code)
        logger.info("[CN数据源] %s: 开始获取商品数据", code)

        intl_part: LCSCPart | None = None
        intl_error: Exception | None = None
        numeric = self._cn_numeric_id_via_search(code)
        if not numeric:
            try:
                intl_part = self.fetch(code)  # 顺带把国际站 HTML 写入缓存
            except (LcscFetchError, ValueError) as exc:
                intl_error = exc
            numeric = self._cn_numeric_id(code)
        if not numeric:
            if intl_error is not None:
                raise intl_error
            return intl_part if intl_part is not None else self.fetch(code)

        cn_part = self._fetch_cn_page_part(numeric)
        if cn_part is None or cn_part.sku != code:
            if cn_part is not None:
                logger.warning(
                    "国内站商品编码不一致（%s != %s），忽略国内数据", cn_part.sku, code
                )
            if intl_part is not None:
                return intl_part
            return self.fetch(code)

        if intl_part is None:
            try:
                intl_part = self.fetch(code)
            except LcscFetchError as exc:
                logger.warning("国际站不可达，仅用国内站数据 %s: %s", code, exc)
                logger.info("[CN数据源] %s: 完成（仅国内站数据）", code)
                return cn_part
        logger.info("[CN数据源] %s: 完成（国内站文本 + 国际站分类/价格合并）", code)
        return merge_cn_part(cn_part, intl_part)

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
                    # 统一包装：调用方只 catch LcscFetchError（裸 ConnectionError 会穿透）
                    raise LcscFetchError(f"请求失败 url={url}: {exc}") from exc
                if resp.status_code in (403, 429):
                    raise LcscFetchError(f"被限流 HTTP {resp.status_code} url={url}")
                if resp.status_code >= 400:
                    raise LcscFetchError(f"HTTP {resp.status_code} url={url}")
                if "application/ld+json" not in resp.text and "__NEXT_DATA__" not in resp.text:
                    if _looks_like_waf(resp.text):
                        return self._fetch_html_browser(url)
                    raise LcscFetchError(f"页面缺 ld+json / __NEXT_DATA__，可能被反爬 url={url}")
                return resp.text
        # 不应该到这里，但保险起见
        raise LcscFetchError(f"多次重试失败 url={url} last={last_exc}")

    def _fetch_html_browser(self, url: str) -> str:
        """国内站 WAF 挑战页需要执行 JS 后才能拿到商品 HTML。"""
        try:
            browser = self._get_browser()
        except LcscFetchError as exc:
            raise LcscFetchError(
                "国内站返回了反爬挑战页，但当前环境没有 Playwright 浏览器"
            ) from exc
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_function(
                "() => document.documentElement.outerHTML.includes('application/ld+json') || document.documentElement.outerHTML.includes('__NEXT_DATA__')",
                timeout=12000,
            )
            html = page.content()
        finally:
            page.close()
        if "application/ld+json" not in html and "__NEXT_DATA__" not in html:
            raise LcscFetchError(f"浏览器抓取后仍缺少商品数据 url={url}")
        return html

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
                    raise LcscFetchError(f"图片请求失败 url={url}: {exc}") from exc
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