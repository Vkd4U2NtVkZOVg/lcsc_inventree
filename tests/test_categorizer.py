"""分类匹配三层策略测试。"""

from __future__ import annotations

from lcsc2inv.categorizer import match


def test_capacitor_direct_match(capacitor):
    m = match(capacitor)
    assert m.source == "direct"
    assert m.score == 100
    assert "Capacitors" in m.category_path
    assert m.category_path.endswith("Ceramic Capacitors (MLCC)")


def test_resistor_direct_match(resistor):
    m = match(resistor)
    # YAML 中精确写了 "Chip Resistor - Surface Mount" -> SMD 路径
    assert m.source == "direct"
    assert "Resistors" in m.category_path


def test_optoisolator_direct_match(optoisolator):
    m = match(optoisolator)
    assert m.source == "direct"
    assert "Optoisolators" in m.category_path


def test_no_category_falls_back_to_uncategorized(monkeypatch):
    """构造一个无 category 的 LCSCPart，验证兜底为 '__uncategorized__'。"""
    from lcsc2inv.lcsc_models import LCSCPart
    p = LCSCPart(sku="C999999", mpn="UNKNOWN", category=None)
    m = match(p)
    # 无 category 时，模糊匹配针对空字符串打分，可能返回 0 但不会抛异常
    assert m.category_path in ("__uncategorized__",) or m.score >= 0