"""字段映射 + clean_parameter_value 测试。"""

from __future__ import annotations

from lcsc2inv.mapping import clean_parameter_value, to_inventree_parameters


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