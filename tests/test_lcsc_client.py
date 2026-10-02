"""针对 `lcsc_client.parse_ldjson` 的离线测试。"""

from __future__ import annotations

from pathlib import Path

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