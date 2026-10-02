"""立创商城订单 .xls 解析（国内版「订单详情」导出格式）。

文件为老式 BIFF .xls（xlrd 解析），结构特点：
- 第一个 sheet 内先有订单信息头（含「订单编号：SOxxxx」）；
- 之后是「商品明细列表」段：一行表头（含「商品编号」列），其下为数据行；
- 大订单可能分多个 sheet / 同 sheet 多段，因此扫描**全部 sheet**，
  每遇到一行含「商品编号」的表头就解析其下数据，直到非数据行。

输出行字段：lcsc_code（商品编号 C-code）、mpn（厂家型号）、brand（品牌）、
description（商品名称）、footprint（封装）、quantity（订购数量，整数）、
unit_price（商品单价，浮点，可能为 None）。

纯解析模块（不依赖 InvenTree / Flask），便于离线测试。
"""

from __future__ import annotations

import re
from pathlib import Path

import xlrd

# 表头列名 → 输出字段（列名做去空格精确匹配；订单导出版本间可能有细微差异）
_HEADER_ALIASES = {
    "商品编号": "lcsc_code",
    "品牌": "brand",
    "厂家型号": "mpn",
    "封装": "footprint",
    "商品名称": "description",
    "商品描述": "description",  # 部分版本叫商品描述
    "商品单价": "unit_price",
}
_QTY_HEADERS = ("订购数量（修改后）", "订购数量", "购买数量")

_CCODE_RE = re.compile(r"^C\d+$")
_NUM_RE = re.compile(r"[\d.]+")
_SO_RE = re.compile(r"SO\d+")


def _cell_str(sheet, r: int, c: int) -> str:
    if c >= sheet.ncols:
        return ""
    value = sheet.cell_value(r, c)
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value).strip()


def _parse_qty(text: str) -> int | None:
    """'20个' / '20' → 20；无法解析返回 None。"""
    nums = _NUM_RE.findall(text or "")
    return int(float(nums[0])) if nums else None


def _parse_price(text: str) -> float | None:
    """'￥0.744500/个' → 0.7445；'-' / 空 → None。"""
    text = (text or "").replace("￥", "").replace("¥", "")
    nums = _NUM_RE.findall(text)
    return float(nums[0]) if nums else None


def _find_header_row(sheet, start: int) -> tuple[int, dict[str, int]] | None:
    """在 sheet 里从 start 行起找「商品编号」表头行，返回 (行号, 列映射)。"""
    for r in range(start, sheet.nrows):
        cols: dict[str, int] = {}
        for c in range(sheet.ncols):
            text = _cell_str(sheet, r, c).replace(" ", "")
            if not text:
                continue
            if text in _HEADER_ALIASES and _HEADER_ALIASES[text] not in cols:
                cols[_HEADER_ALIASES[text]] = c
            for qty_name in _QTY_HEADERS:
                if text.startswith(qty_name) and "quantity" not in cols:
                    cols["quantity"] = c
        if "lcsc_code" in cols and "quantity" in cols:
            return r, cols
    return None


def parse_order_xls(data: bytes) -> dict:
    """解析立创订单 .xls 字节流。

    Returns:
        {"order_no": str|None,
         "rows": [{"lcsc_code": str, "mpn": str, "brand": str,
                   "description": str, "footprint": str,
                   "quantity": int, "unit_price": float|None}, ...]}

    Raises:
        ValueError: 文件不是合法 .xls，或找不到商品明细表头。
    """
    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as exc:  # noqa: BLE001 — 统一为清晰报错
        raise ValueError(f"无法解析 .xls 文件: {exc}") from exc

    rows: list[dict] = []
    order_no: str | None = None

    for sheet in book.sheets():
        # 订单号：任意 sheet 里「订单编号：」旁的 SO 号
        if order_no is None:
            for r in range(min(sheet.nrows, 12)):
                for c in range(sheet.ncols):
                    if "订单编号" in _cell_str(sheet, r, c):
                        hit = _SO_RE.search(_cell_str(sheet, r, c + 1))
                        if hit:
                            order_no = hit.group(0)
                        break

        # 明细段可能有多处（同一 sheet 分段 / 多 sheet）
        search_from = 0
        while search_from < sheet.nrows:
            header = _find_header_row(sheet, search_from)
            if header is None:
                break
            header_row, cols = header
            r = header_row + 1
            while r < sheet.nrows:
                code = _cell_str(sheet, r, cols["lcsc_code"])
                if not _CCODE_RE.match(code):
                    break  # 数据段结束（空行/汇总行/下一个表头）
                qty = _parse_qty(_cell_str(sheet, r, cols["quantity"]))
                if qty is None or qty <= 0:
                    r += 1
                    continue
                price_col = cols.get("unit_price")
                rows.append(
                    {
                        "lcsc_code": code,
                        "mpn": _cell_str(sheet, r, cols["mpn"]) if "mpn" in cols else "",
                        "brand": _cell_str(sheet, r, cols["brand"]) if "brand" in cols else "",
                        "description": (
                            _cell_str(sheet, r, cols["description"])
                            if "description" in cols else ""
                        ),
                        "footprint": (
                            _cell_str(sheet, r, cols["footprint"])
                            if "footprint" in cols else ""
                        ),
                        "quantity": qty,
                        "unit_price": (
                            _parse_price(_cell_str(sheet, r, price_col))
                            if price_col is not None else None
                        ),
                    }
                )
                r += 1
            search_from = r + 1  # 跳过当前段，继续找下一段/续表

    if not rows:
        raise ValueError("文件里没有找到商品明细（未匹配到「商品编号」表头或数据行）")

    return {"order_no": order_no, "rows": rows}


def parse_order_file(path: str | Path) -> dict:
    """从文件路径解析订单（便捷封装）。"""
    return parse_order_xls(Path(path).read_bytes())
