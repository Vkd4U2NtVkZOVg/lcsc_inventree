"""`qr_label.parse_scan_payload`：立创标签 QR / 扫码文本解析。"""

from __future__ import annotations

import pytest

from lcsc2inv.qr_label import parse_scan_payload

# 真实标签样例（2025-04 立创 0402 电阻整盘标签）
LABEL_QR = "{on:SO25040821121,pc:C17168,pm:0402WGF0000TCE,qty:100,mc:,cc:1,pdi:152840923,hp:11}"


class TestLabelQr:
    def test_full_label(self):
        p = parse_scan_payload(LABEL_QR)
        assert p is not None
        assert p.code == "C17168"
        assert p.quantity == 100
        assert p.mpn == "0402WGF0000TCE"
        assert p.order_no == "SO25040821121"

    def test_label_without_qty(self):
        p = parse_scan_payload("{pc:C25804,pm:RC0805FR-0710KL}")
        assert p.code == "C25804"
        assert p.quantity is None
        assert p.mpn == "RC0805FR-0710KL"

    def test_label_with_spaces(self):
        p = parse_scan_payload("  {pc: c17168 , qty: 50 } ")
        assert p.code == "C17168"
        assert p.quantity == 50

    def test_non_digit_qty_ignored(self):
        p = parse_scan_payload("{pc:C17168,qty:abc}")
        assert p.code == "C17168"
        assert p.quantity is None

    def test_brace_but_no_pc(self):
        # InvenTree 绑定码等其他 JSON 类文本不误判
        assert parse_scan_payload('{"stock_pk": 456}') is None

    def test_pc_not_c_code(self):
        assert parse_scan_payload("{pc:hello,qty:1}") is None


class TestPlainCodes:
    def test_bare_code(self):
        assert parse_scan_payload("C17168").code == "C17168"

    def test_lowercase_and_separators(self):
        assert parse_scan_payload("c17168").code == "C17168"
        assert parse_scan_payload("C-17168").code == "C17168"
        assert parse_scan_payload(" C.17168 ").code == "C17168"

    def test_intl_url(self):
        p = parse_scan_payload("https://www.lcsc.com/product-detail/C28323.html")
        assert p.code == "C28323"

    def test_too_short_digits(self):
        # C+3 位不是 LCSC 编码形态，避免误吞货位/库存码
        assert parse_scan_payload("C123") is None


class TestNonPartCodes:
    """非器件编码（货位条码 / 库存码 / 噪声）应返回 None，交回原条码流程。"""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            None,
            "WAREHOUSE-A01",
            "687/32",
            "X152840923",       # 标签底部 Code-128（包装 ID）
            "https://item.szlcsc.com/17853.html",  # 国内站 URL 无法本地映射 C-code
            "inventree://stockitem/456",
        ],
    )
    def test_rejected(self, text):
        assert parse_scan_payload(text) is None
