"""`lcsc2inv.order_import` 测试（离线，xlwt 动态构造订单 .xls fixture）。

覆盖：
- 标准订单解析：订单号、字段提取（数量"20个"、单价"￥0.744500/个"）
- 多段明细（同 sheet 两个表头段）、跨 sheet 续表
- 非法文件 / 无明细表头 → ValueError
- 数据行中断（空行/汇总行）后不再误读
"""

from __future__ import annotations

import io

import pytest
import xlwt

from lcsc2inv.order_import import parse_order_xls


def _write_row(sheet, r: int, values: list):
    for c, v in enumerate(values):
        sheet.write(r, c, v)


def _make_order_xls(rows: list[list], *, order_no: str = "SO1234567890",
                    second_sheet_rows: list[list] | None = None) -> bytes:
    """构造一个仿立创订单 .xls：信息头 + 明细表头 + 数据行。"""
    buf = io.BytesIO()
    wb = xlwt.Workbook()
    sh = wb.add_sheet("立创商城订单详情")
    _write_row(sh, 1, ["订单编号：", order_no])
    header = ["序号", "商品编号", "品牌", "厂家型号", "封装", "商品名称",
              "订购数量（修改后）", "发货数量", "是否不发此货", "毛重（kg）",
              "商品单价", "商品金额"]
    _write_row(sh, 17, header)
    for i, row in enumerate(rows):
        _write_row(sh, 18 + i, [i + 1] + row)
    if second_sheet_rows is not None:
        sh2 = wb.add_sheet("续表")
        _write_row(sh2, 17, header)
        for i, row in enumerate(second_sheet_rows):
            _write_row(sh2, 18 + i, [i + 1] + row)
    wb.save(buf)
    return buf.getvalue()


def _item_row(code="C47327171", mpn="APS0420M3R3A", qty="20个", price="￥0.744500/个"):
    return [code, "Coilank(驰兴电感)", mpn, "SMD,4.8x4.2mm", "3.3uH ±20% 4.05A",
            qty, qty, "-", 0.0002, price, "￥14.89"]


class TestParseOrder:
    def test_standard_order(self):
        data = _make_order_xls([_item_row(), _item_row(code="C2842189", mpn="XC32M4")])
        result = parse_order_xls(data)
        assert result["order_no"] == "SO1234567890"
        assert len(result["rows"]) == 2
        first = result["rows"][0]
        assert first["lcsc_code"] == "C47327171"
        assert first["mpn"] == "APS0420M3R3A"
        assert first["brand"] == "Coilank(驰兴电感)"
        assert first["description"] == "3.3uH ±20% 4.05A"
        assert first["footprint"] == "SMD,4.8x4.2mm"
        assert first["quantity"] == 20
        assert first["unit_price"] == pytest.approx(0.7445)

    def test_multi_section_same_sheet(self):
        """同 sheet 两段明细（中间隔汇总行）：两段都应被解析。"""
        rows = [_item_row(), ["-- 汇总 --", "", "", "", "", "", "", "", "", "", ""]]
        data = _make_order_xls(rows, second_sheet_rows=[_item_row(code="C9999999")])
        result = parse_order_xls(data)
        codes = [r["lcsc_code"] for r in result["rows"]]
        assert codes == ["C47327171", "C9999999"]

    def test_qty_variants(self):
        data = _make_order_xls([_item_row(qty="3个", price="￥1.05/个")])
        row = parse_order_xls(data)["rows"][0]
        assert row["quantity"] == 3
        assert row["unit_price"] == pytest.approx(1.05)

    def test_invalid_file(self):
        with pytest.raises(ValueError, match="无法解析"):
            parse_order_xls(b"not an xls file")

    def test_no_detail_section(self):
        buf = io.BytesIO()
        wb = xlwt.Workbook()
        sh = wb.add_sheet("空订单")
        sh.write(0, 0, "这里没有任何明细")
        wb.save(buf)
        with pytest.raises(ValueError, match="没有找到商品明细"):
            parse_order_xls(buf.getvalue())
