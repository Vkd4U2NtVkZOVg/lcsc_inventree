"""针对 `lcsc_client.parse_ldjson` 的离线测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsc2inv.lcsc_client import parse_lcsc_code, parse_ldjson, product_url

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_parse_lcsc_code_strip():
    assert parse_lcsc_code("C28323") == "C28323"
    assert parse_lcsc_code(" c12345 ") == "C12345"
    assert parse_lcsc_code("C-191386") == "C191386"


def test_parse_lcsc_code_from_url():
    url = "https://www.lcsc.com/product-detail/C28323.html"
    assert parse_lcsc_code(url) == "C28323"


def test_parse_lcsc_code_invalid():
    import pytest
    with pytest.raises(ValueError):
        parse_lcsc_code("not-a-code")
    with pytest.raises(ValueError):
        parse_lcsc_code("")


def test_product_url():
    assert product_url("C28323") == "https://www.lcsc.com/product-detail/C28323.html"


def test_parse_ldjson_capacitor(capacitor):
    assert capacitor.sku == "C28323"
    assert capacitor.mpn == "CL21B105KBFNNNE"
    assert capacitor.brand.name == "Samsung Electro-Mechanics"
    assert capacitor.category == "Capacitors/Ceramic Capacitors"
    assert capacitor.description and "1uF" in capacitor.description
    # additional_property
    assert capacitor.get_property("Capacitance") == "1uF"
    assert capacitor.get_property("Tolerance") == "±10%"
    assert capacitor.get_property("Voltage Rating") == "50V"
    # offer
    assert capacitor.offer is not None
    assert capacitor.offer.price == 0.0344
    assert capacitor.offer.inventory_level == 2000
    # datasheet
    assert capacitor.datasheet_url and "datasheet.lcsc.com" in capacitor.datasheet_url


def test_parse_ldjson_resistor(resistor):
    assert resistor.sku == "C25804"
    assert resistor.category == "Resistors/Chip Resistor - Surface Mount"
    assert resistor.get_property("Resistance") == "10kΩ"
    assert resistor.get_property("Power(Watts)") == "100mW"
    assert resistor.get_property("Tolerance") == "±1%"


def test_parse_ldjson_optoisolator(optoisolator):
    assert optoisolator.sku == "C191386"
    assert optoisolator.category.startswith("Optoisolators")
    assert optoisolator.get_property("Isolation Voltage(Vrms)") == "5kV"
    assert optoisolator.get_property("Output Current") == "50mA"


def test_category_parts(capacitor):
    assert capacitor.category_parts == ["Capacitors", "Ceramic Capacitors"]
    assert capacitor.category_top == "Capacitors"
    assert capacitor.category_sub == "Ceramic Capacitors"


def test_get_property_case_insensitive(capacitor):
    assert capacitor.get_property("capacitance") == "1uF"
    assert capacitor.get_property("CAPACITANCE") == "1uF"
    assert capacitor.get_property("Nonexistent") is None


def test_parse_lcsc_code_cn():
    """国内站 URL / 纯数字 ID。"""
    from lcsc2inv.lcsc_client import parse_lcsc_code
    assert parse_lcsc_code("https://item.szlcsc.com/360864.html") == "CN:360864"
    assert parse_lcsc_code("https://item.szlcsc.com/mro/1049789.html") == "CN:1049789"
    assert parse_lcsc_code("360864") == "CN:360864"


def test_chinese_site_fixture():
    """国内站 ld+json：@graph 包装、CNY 价格、QuantitativeValue 库存、中文类别。"""
    from lcsc2inv.lcsc_client import load_fixture
    p = load_fixture(FIXTURE_DIR / "CN_360864.html")
    assert p.sku == "C386757"   # LCSC C-code（即使走国内站，sku 字段仍是 C-code）
    assert p.mpn == "R-RJ45R08P-C000"
    assert p.brand.name == "Ckmtw(灿科盟)"
    # CNY + QuantitativeValue 解析
    assert p.offer is not None
    assert p.offer.price == 1.23
    assert p.offer.price_currency == "CNY"
    assert p.offer.inventory_level == 56588
    # 中文类别（不会自动归类到任何 InvenTree 类别）
    assert p.category == "以太网连接器(RJ45 RJ11)"
    # 数据手册
    assert p.datasheet_url and "atta.szlcsc.com" in p.datasheet_url
    # page_url 是国内站格式
    assert p.page_url == "https://item.szlcsc.com/360864.html"


def test_clean_chinese_site_no_params():
    """国内站 ld+json 不含 additionalProperty，但封装可从页面参数表抽取。"""
    from lcsc2inv.lcsc_client import load_fixture
    from lcsc2inv.mapping import to_inventree_parameters
    p = load_fixture(FIXTURE_DIR / "CN_360864.html")
    params = to_inventree_parameters(p, category_top=p.category_top, category_sub=p.category_sub)
    # 类别是中文，不在 YAML 里；但 Base 链路始终有 Package 映射
    assert set(params) == {"Package"}
    assert params["Package"]["value"] == "弯插"
    assert p.mpn == "R-RJ45R08P-C000"
    assert p.brand.name == "Ckmtw(灿科盟)"
    # CNY + QuantitativeValue 解析
    assert p.offer is not None
    assert p.offer.price == 1.23
    assert p.offer.price_currency == "CNY"
    assert p.offer.inventory_level == 56588
    # 中文类别（不会自动归类到任何 InvenTree 类别）
    assert p.category == "以太网连接器(RJ45 RJ11)"
    # 数据手册
    assert p.datasheet_url and "atta.szlcsc.com" in p.datasheet_url
    # page_url 是国内站格式
    assert p.page_url == "https://item.szlcsc.com/360864.html"


def test_chinese_site_footprint_from_html():
    """国内站「商品封装」在 <dt>/<dd> 参数表里，解析后补进 additional_properties。"""
    from lcsc2inv.lcsc_client import load_fixture

    p = load_fixture(FIXTURE_DIR / "CN_360864.html")
    assert p.get_property("封装") == "弯插"
    assert p.package == "弯插"


def test_international_site_package_property(capacitor):
    """国际站 Package 参数直接暴露为 package 属性。"""
    assert capacitor.package == "0805"

def test_cn_description_boilerplate_removed():
    """国内站描述里的营销模板文案在解析时即被删除。"""
    from lcsc2inv.lcsc_client import LCSC_CN_DESC_BOILERPLATE, load_fixture

    p = load_fixture(FIXTURE_DIR / "CN_360864.html")
    assert p.description and LCSC_CN_DESC_BOILERPLATE not in p.description
    # 主体信息保留
    assert p.description.startswith("R-RJ45R08P-C000")
    assert p.description.endswith("。")


def test_clean_description_variants():
    from lcsc2inv.lcsc_client import LCSC_CN_DESC_BOILERPLATE as B, clean_description

    # 前面是逗号的变体：残留逗号被清掉
    assert clean_description("电阻10k，" + B) == "电阻10k"
    # 多段文本中间出现：仅删除该句
    assert clean_description("前段。" + B + "后段。") == "前段。后段。"
    # 纯模板文案 → 清空为 None
    assert clean_description(B) is None
    # 空值透传
    assert clean_description(None) is None
    assert clean_description("普通描述") == "普通描述"


# ---------------------------------------------------------------------------
# 国内站新结构（webData）与 C-code → 国内页数据源
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cn_resistor():
    """CN_17853.html：item.szlcsc.com 新版 webData 结构（C17168 0402 电阻）。"""
    from lcsc2inv.lcsc_client import parse_cn_page

    html = (FIXTURE_DIR / "CN_17853.html").read_text(encoding="utf-8")
    return parse_cn_page(html, page_url="https://item.szlcsc.com/17853.html")


def test_extract_cn_product_id():
    """国际站页 __NEXT_DATA__ 的 webData.productId 即国内站数字 ID。"""
    from lcsc2inv.lcsc_client import extract_cn_product_id

    html = (FIXTURE_DIR / "C17168.html").read_text(encoding="utf-8")
    assert extract_cn_product_id(html) == 17853
    # 无 webData 的页面返回 None
    assert extract_cn_product_id("<html><body>no data</body></html>") is None


def test_parse_cn_page_basic(cn_resistor):
    assert cn_resistor.sku == "C17168"
    assert cn_resistor.mpn == "0402WGF0000TCE"
    # 中文名 / 描述来自 productRecord.productName
    assert cn_resistor.name == "厚膜电阻 0Ω ±1% 62.5mW"
    assert cn_resistor.description == "厚膜电阻 0Ω ±1% 62.5mW"
    assert cn_resistor.brand is not None
    assert cn_resistor.brand.name == "UNI-ROYAL(厚声)"


def test_parse_cn_page_params_and_stock(cn_resistor):
    names = [p.name for p in cn_resistor.additional_properties]
    assert "阻值" in names and "精度" in names and "功率" in names
    assert cn_resistor.get_property("阻值") == "0Ω"
    # 库存映射为 CNY Offer（无价格）
    assert cn_resistor.offer is not None
    assert cn_resistor.offer.inventory_level == 506900
    assert cn_resistor.offer.price_currency == "CNY"


def test_parse_cn_page_images(cn_resistor):
    assert cn_resistor.image_urls
    assert all(u.startswith("https://") for u in cn_resistor.image_urls)
    # 品牌证书图被过滤
    assert all("/certificate/" not in u for u in cn_resistor.image_urls)


def test_parse_cn_page_old_structure_fallback():
    """旧版页面（无 webData）回退到 productDetailsVo / ld+json 解析。"""
    from lcsc2inv.lcsc_client import parse_cn_page

    p = parse_cn_page((FIXTURE_DIR / "CN_360864.html").read_text(encoding="utf-8"))
    assert p.sku == "C386757"


def test_merge_cn_part_prefers_cn_text():
    """中文名 / 描述取国内站，分类 / 品牌 / 价格库存取国际站。"""
    from lcsc2inv.lcsc_client import load_fixture, merge_cn_part, parse_cn_page

    html = (FIXTURE_DIR / "CN_17853.html").read_text(encoding="utf-8")
    cn = parse_cn_page(html)
    intl = load_fixture(FIXTURE_DIR / "C17168.html")
    m = merge_cn_part(cn, intl)

    assert m.sku == "C17168"
    assert m.name == "0402WGF0000TCE"                          # 名称 = MPN
    assert m.description == "厚膜电阻 0Ω ±1% 62.5mW"          # CN 文本优先
    assert m.category == "Resistors/Chip Resistor - Surface Mount"  # 分类用国际站（英文映射表）
    assert m.brand.name == "UNI-ROYAL"                        # 品牌名与已有 Manufacturer 一致
    assert m.package == "0402"                                # 参数取国际站英文参数
    assert m.offer is not None and m.offer.inventory_level    # 价格库存取国际站


def test_fetch_cn_part_merge(tmp_path, monkeypatch, capacitor):
    """fetch_cn_part：C-code → 国际站页取数字 ID → 国内页 → 合并。"""
    from lcsc2inv.config import Settings
    from lcsc2inv.lcsc_client import Fetcher

    settings = Settings(
        lcsc_cache_dir=str(tmp_path), lcsc_cache_enabled=True, lcsc_request_interval=0.0
    )
    f = Fetcher(settings=settings)
    html_intl = (FIXTURE_DIR / "C17168.html").read_text(encoding="utf-8")
    html_cn = (FIXTURE_DIR / "CN_17853.html").read_text(encoding="utf-8")

    def fake_fetch_html(self, url):
        return html_cn if "item.szlcsc.com" in url else html_intl

    monkeypatch.setattr(Fetcher, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(Fetcher, "_search_first_cn_id", lambda self, code: None)
    part = f.fetch_cn_part("C17168")

    assert part.sku == "C17168"
    assert part.description == "厚膜电阻 0Ω ±1% 62.5mW"
    assert part.category == "Resistors/Chip Resistor - Surface Mount"
    # 数字 ID 已写入持久映射
    assert f._cn_numeric_id("C17168") == 17853
    assert (tmp_path / "cn_id_map.json").exists()


def test_fetch_cn_part_falls_back_to_intl(tmp_path, monkeypatch):
    """国内页抓取/解析失败时退回国际站数据，不抛异常。"""
    from lcsc2inv.config import Settings
    from lcsc2inv.lcsc_client import Fetcher, LcscFetchError

    settings = Settings(
        lcsc_cache_dir=str(tmp_path), lcsc_cache_enabled=True, lcsc_request_interval=0.0
    )
    f = Fetcher(settings=settings)
    html_intl = (FIXTURE_DIR / "C17168.html").read_text(encoding="utf-8")

    def fake_fetch_html(self, url):
        if "item.szlcsc.com" in url:
            raise LcscFetchError("国内站被反爬")
        return html_intl

    monkeypatch.setattr(Fetcher, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(Fetcher, "_search_first_cn_id", lambda self, code: None)
    part = f.fetch_cn_part("C17168")
    assert part.sku == "C17168"
    assert part.description and "厚膜" not in part.description  # 国际站英文描述


def test_fetch_cn_part_rejects_sku_mismatch(tmp_path, monkeypatch):
    """国内页 productCode 与 C-code 不一致时退回国际站数据（防御串号）。"""

    from lcsc2inv.config import Settings
    from lcsc2inv.lcsc_client import Fetcher

    settings = Settings(
        lcsc_cache_dir=str(tmp_path), lcsc_cache_enabled=True, lcsc_request_interval=0.0
    )
    f = Fetcher(settings=settings)
    html_intl = (FIXTURE_DIR / "C17168.html").read_text(encoding="utf-8")
    html_cn = (FIXTURE_DIR / "CN_17853.html").read_text(encoding="utf-8")

    # 把国内页的 productCode 改成另一个 C-code
    tampered = html_cn.replace('"productCode":"C17168"', '"productCode":"C999999"')
    assert tampered != html_cn

    def fake_fetch_html(self, url):
        return tampered if "item.szlcsc.com" in url else html_intl

    monkeypatch.setattr(Fetcher, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(Fetcher, "_search_first_cn_id", lambda self, code: None)
    part = f.fetch_cn_part("C17168")
    assert part.sku == "C17168"
    assert part.description != "厚膜电阻 0Ω ±1% 62.5mW"


def test_fetch_cn_code_uses_webdata_parser(tmp_path, monkeypatch):
    """fetch("CN:<id>") 走新版 webData 解析。"""
    from lcsc2inv.config import Settings
    from lcsc2inv.lcsc_client import Fetcher

    settings = Settings(
        lcsc_cache_dir=str(tmp_path), lcsc_cache_enabled=True, lcsc_request_interval=0.0
    )
    f = Fetcher(settings=settings)
    html_cn = (FIXTURE_DIR / "CN_17853.html").read_text(encoding="utf-8")
    monkeypatch.setattr(Fetcher, "_fetch_html", lambda self, url: html_cn)

    part = f.fetch("https://item.szlcsc.com/17853.html")
    assert part.sku == "C17168"
    assert part.name == "厚膜电阻 0Ω ±1% 62.5mW"


def test_search_first_cn_id(tmp_path, monkeypatch):
    """国内站搜索页渲染后取第一个商品链接的数字 ID，并写入持久映射。"""
    import tempfile

    from lcsc2inv.config import Settings
    from lcsc2inv.lcsc_client import Fetcher

    settings = Settings(
        lcsc_cache_dir=str(tempfile.mkdtemp()), lcsc_cache_enabled=True,
        lcsc_request_interval=0.0,
    )
    f = Fetcher(settings=settings)

    page = MagicMock()
    page.content.return_value = (
        '<div class="list"><a href="https://item.szlcsc.com/17853.html">C17168</a>'
        '<a href="https://item.szlcsc.com/99999.html">其他</a></div>'
    )
    browser = MagicMock()
    browser.new_page.return_value = page
    monkeypatch.setattr(Fetcher, "_get_browser", lambda self: browser)

    assert f._search_first_cn_id("C17168") == 17853
    page.close.assert_called_once()

    # 经由 _cn_numeric_id_via_search 后写入映射缓存
    assert f._cn_numeric_id_via_search("C17168") == 17853


def test_fetch_cn_part_without_intl(tmp_path, monkeypatch):
    """国际站完全不可达时：搜索页解析 ID → 纯国内站数据兜底导入。"""
    from lcsc2inv.config import Settings
    from lcsc2inv.lcsc_client import Fetcher, LcscFetchError

    settings = Settings(
        lcsc_cache_dir=str(tmp_path), lcsc_cache_enabled=True, lcsc_request_interval=0.0
    )
    f = Fetcher(settings=settings)
    html_cn = (FIXTURE_DIR / "CN_17853.html").read_text(encoding="utf-8")

    def fake_fetch_html(self, url):
        # 国际站不可达；国内站（item.szlcsc.com）正常
        if "item.szlcsc.com" not in url:
            raise LcscFetchError(f"国际站不可达 url={url}")
        return html_cn

    def fake_search(self, code):
        assert code == "C17168"
        return 17853

    monkeypatch.setattr(Fetcher, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(Fetcher, "_search_first_cn_id", fake_search)

    part = f.fetch_cn_part("C17168")
    assert part.sku == "C17168"
    assert part.description == "厚膜电阻 0Ω ±1% 62.5mW"
    assert part.offer is not None
    assert part.offer.price_currency == "CNY"
    assert part.offer.inventory_level == 506900


def test_merge_cn_part_name_is_mpn():
    """Part 名称 = MPN（器件真实型号）；描述 = 中文规格串拼类目前缀。"""
    from lcsc2inv.lcsc_client import merge_cn_part
    from lcsc2inv.lcsc_models import LCSCPart

    cn = LCSCPart(
        sku="C14857", mpn="CL21C470JBANNNC",
        name="47pF ±5% 50V", description="47pF ±5% 50V",
        cn_category_name="贴片电容",
    )
    intl = LCSCPart(sku="C14857", name="Samsung CL21C470JBANNNC")
    m = merge_cn_part(cn, intl)
    assert m.name == "CL21C470JBANNNC"
    assert m.description == "贴片电容 47pF ±5% 50V"


def test_merge_cn_part_no_duplicate_prefix():
    """品名已含类目词（厚膜电阻…）时不重复拼。"""
    from lcsc2inv.lcsc_client import merge_cn_part
    from lcsc2inv.lcsc_models import LCSCPart

    cn = LCSCPart(
        sku="C17168", name="厚膜电阻 0Ω ±1% 62.5mW",
        description="厚膜电阻 0Ω ±1% 62.5mW", cn_category_name="贴片电阻",
    )
    intl = LCSCPart(sku="C17168")
    m = merge_cn_part(cn, intl)
    assert m.name == "厚膜电阻 0Ω ±1% 62.5mW"


def test_merge_cn_part_filters_placeholder_image():
    """国际站无实图（no_goods_pic 占位）时退回国内站图片。"""
    from lcsc2inv.lcsc_client import merge_cn_part
    from lcsc2inv.lcsc_models import LCSCPart

    cn = LCSCPart(sku="C14857", image_urls=["https://alimg.szlcsc.com/real.jpg"])
    intl = LCSCPart(
        sku="C14857",
        image_urls=["https://static.lcsc.com/feassets/pc/images/product/no_goods_pic6.jpg"],
    )
    m = merge_cn_part(cn, intl)
    assert m.image_urls == ["https://alimg.szlcsc.com/real.jpg"]


def test_parse_cn_page_captures_cn_category(cn_resistor):
    """解析时记录 currentCatalog.catalogName（中文类目）。"""
    assert cn_resistor.cn_category_name == "贴片电阻"
