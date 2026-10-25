"""解析立创（LCSC）物料标签上的二维码 / 条码文本。

标签 QR 内容为紧凑键值对（无引号无空格）::

    {on:SO25040821121,pc:C17168,pm:0402WGF0000TCE,qty:100,mc:,cc:1,pdi:152840923,hp:11}

字段含义：
- on  订单号（SO 开头）
- pc  产品编码，即 LCSC C-code ← 扫码入库最关心的一格
- pm  原厂型号（MPN）
- qty 包装数量
- pdi 产品/包装内部 ID（与底部 Code-128 条码 `X<pdi>` 对应）

除标签格式外，也接受裸 C-code 和两种商品页 URL，方便扫码枪 / 手输。
其余文本（货位条码、库存码等）返回 None，交回原有条码流程处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 标签 QR：花括号包裹的 key:value 列表
_LABEL_RE = re.compile(r"^\s*\{\s*(.*)\s*\}\s*$", re.DOTALL)
_PAIR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^,}]*)")
_C_CODE_RE = re.compile(r"^C(\d{4,9})$", re.IGNORECASE)
_INTL_URL_RE = re.compile(r"lcsc\.com/product-detail/C(\d+)\.html", re.IGNORECASE)


@dataclass(slots=True)
class ScanPayload:
    """一次扫码解析出的有效载荷；quantity 仅标签格式才有。"""

    code: str                      # C-code，如 "C17168"
    quantity: int | None = None    # 标签 qty（包装数量），非标签来源为 None
    mpn: str | None = None         # 原厂型号（标签 pm 字段）
    order_no: str | None = None    # 订单号（标签 on 字段）

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "quantity": self.quantity,
            "mpn": self.mpn,
            "order_no": self.order_no,
        }


def parse_scan_payload(text: str | None) -> ScanPayload | None:
    """把扫码得到的文本解析为 `ScanPayload`；无法识别时返回 None。

    >>> parse_scan_payload('{on:SO25040821121,pc:C17168,pm:0402WGF0000TCE,qty:100}')
    ScanPayload(code='C17168', quantity=100, mpn='0402WGF0000TCE', order_no='SO25040821121')
    >>> parse_scan_payload('c17168').code
    'C17168'
    >>> parse_scan_payload('https://www.lcsc.com/product-detail/C17168.html').code
    'C17168'
    >>> parse_scan_payload('WAREHOUSE-A01') is None
    True
    """
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None

    # 1. 标签 QR 格式
    if raw.startswith("{"):
        m = _LABEL_RE.match(raw)
        if m:
            fields = {k: v.strip() for k, v in _PAIR_RE.findall(m.group(1))}
            pc = fields.get("pc", "")
            if _C_CODE_RE.match(pc):
                qty = None
                q = fields.get("qty", "")
                if q.isdigit():
                    qty = int(q)
                return ScanPayload(
                    code=pc.upper(),
                    quantity=qty,
                    mpn=fields.get("pm") or None,
                    order_no=fields.get("on") or None,
                )
        return None

    # 2. 商品页 URL
    m = _INTL_URL_RE.search(raw)
    if m:
        return ScanPayload(code=f"C{m.group(1)}")

    # 3. 裸 C-code（容许 C-17168 / c 17168 等分隔符）
    compact = re.sub(r"^C[-._/ ]+", "C", raw, flags=re.IGNORECASE).replace(" ", "").upper()
    if _C_CODE_RE.match(compact):
        return ScanPayload(code=compact)

    return None
