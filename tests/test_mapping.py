"""字段映射 + clean_parameter_value 测试。"""

from __future__ import annotations

from lcsc2inv.lcsc_models import LCSCPart, PropertyValue
from lcsc2inv.mapping import clean_parameter_value, to_inventree_parameters, to_part_notes


def test_clean_basic():
    # "10nF" → "10n"（归一化 SI 单位，符合 Ki-nTree 风格）
    assert clean_parameter_value("10nF") == "10n"
    assert clean_parameter_value("10nF (X7R)") == "10n"
    assert clean_parameter_value("50V") == "50V"
    assert clean_parameter_value("100mW") == "100mW"


def test_clean_units():
    assert clean_parameter_value("10kΩ") == "10k"
    assert clean_parameter_value("10kOhms") == "10k"
    assert clean_parameter_value("4.7MΩ") == "4.7M"
    assert clean_parameter_value("1uF") == "1u"
    assert clean_parameter_value("100nF") == "100n"
    assert clean_parameter_value("22pF") == "22p"


def test_clean_range():
    # 区间取第一个
    assert clean_parameter_value("-40°C ~ 85°C") == "-40°C"


def test_clean_unicode():
    assert clean_parameter_value("±10%") == "±10%"


def test_to_inventree_parameters_capacitor(capacitor):
    params = to_inventree_parameters(
        capacitor, category_top=capacitor.category_top, category_sub=capacitor.category_sub
    )
    # 电容基本量映射到 InvenTree "Value"（Passives 通用）
    assert params["Value"]["value"] == "1u"
    # Tolerance 应清洗为 "±10%"
    assert "Tolerance" in params
    assert params["Tolerance"]["value"] == "±10%"
    # Rated Voltage / Package 应被映射
    assert "Rated Voltage" in params
    assert "Package" in params


def test_to_inventree_parameters_resistor(resistor):
    params = to_inventree_parameters(
        resistor, category_top=resistor.category_top, category_sub=resistor.category_sub
    )
    # Resistance 应映射到 "Value"（Passives 通用）
    assert params["Value"]["value"] == "10k"
    assert params["Value"]["raw"] == "10kΩ"
    # Rated Power
    assert params["Rated Power"]["value"] == "100mW"


def test_to_inventree_parameters_optoisolator(optoisolator):
    params = to_inventree_parameters(
        optoisolator,
        category_top=optoisolator.category_top,
        category_sub=optoisolator.category_sub,
    )
    assert "Isolation Voltage" in params
    assert params["Isolation Voltage"]["value"] == "5kV"
    assert "Operating Temperature" in params


def _cn_part() -> LCSCPart:
    """国内站风格：封装参数名是「封装」，类别是中文（不在 YAML 中）。"""
    return LCSCPart(
        sku="C386757",
        mpn="R-RJ45R08P-C000",
        category="以太网连接器(RJ45 RJ11)",
        additional_properties=[PropertyValue(name="封装", value="弯插")],
    )


def test_package_mapping_accepts_chinese_name():
    """field_map Base 的 Package 候选包含「封装」，国内站参数也能映射。"""
    params = to_inventree_parameters(_cn_part(), category_top=None, category_sub=None)
    assert set(params) == {"Package"}
    assert params["Package"] == {"value": "弯插", "raw": "弯插"}


def test_part_notes_includes_package():
    """Part.notes 应包含封装行，便于人工核对。"""
    notes = to_part_notes(_cn_part())
    assert "- **Package**: 弯插" in notes


def test_part_notes_without_package():
    """没有封装时不输出 Package 行。"""
    part = LCSCPart(sku="C1")
    assert "- **Package**" not in to_part_notes(part)