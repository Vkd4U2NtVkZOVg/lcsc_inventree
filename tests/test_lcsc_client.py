"""针对 `lcsc_client.parse_ldjson` 的离线测试。"""

from __future__ import annotations

from lcsc2inv.lcsc_client import parse_lcsc_code, parse_ldjson, product_url


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