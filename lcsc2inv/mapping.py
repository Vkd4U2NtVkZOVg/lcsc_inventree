"""字段映射 + 参数值清洗。

核心函数：
- `clean_parameter_value(value)`：去掉括号注释、单位归一化（沿用 Ki-nTree 的 part_tools.clean_parameter_value 思路）
- `to_inventree_parameters(part, category_top)`：根据 `field_map.yaml` 把 LCSC `additionalProperty[]`
  转换为 `{template_name: clean_value}`，供 inventree_writer 写 PartParameter
"""

from __future__ import annotations

import re
from typing import Any

from lcsc2inv.config import load_yaml
from lcsc2inv.lcsc_models import LCSCPart

# 通用单位映射（Ki-nTree style；用于把 `Resistance: 10kΩ` 改成 `Resistance: 10k`）
_UNIT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 电阻：k / kilo / kΩ / kOhm / kOhms -> k（顺带消化 Ω / ohm 后缀）
    # 注意：alternation 用左到右最长优先，先放长的（ohms 在前）
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:k|kilo)\s*(?:Ω|ohms|ohms?|ohm)?", re.IGNORECASE), r"\1k"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:M|meg)\s*(?:Ω|ohms|ohms?|ohm)?", re.IGNORECASE), r"\1M"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:Ω|ohms|ohms?|ohm)?", re.IGNORECASE), r"\1"),
    # 电容：pF / nF / uF / μF -> p / n / u
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:pF|picoF|pf)", re.IGNORECASE), r"\1p"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:nF|nanoF|nf)", re.IGNORECASE), r"\1n"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:uF|microF|μF|uf)", re.IGNORECASE), r"\1u"),
    # 电压 / 电流 / 功率：保留数值与单位
    (re.compile(r"(\d+(?:\.\d+)?)\s*(mV|millivolt)", re.IGNORECASE), r"\1mV"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(mA|milliamp)", re.IGNORECASE), r"\1mA"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(mW|milliwatt)", re.IGNORECASE), r"\1mW"),
]

_PAREN_TRAIL_RE = re.compile(r"\s*\([^)]*\)\s*$")  # "10nF (X7R)" -> "10nF"
# 量纲前缀可包含任意非数字字符，例如 "°C"、"mA"、"V"、"W"
_RANGE_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)([^\d~]*)[~～]\s*(-?\d+(?:\.\d+)?)(.*)$")
_DIM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:mm|millimeter|毫米|mm)", re.IGNORECASE)


def clean_parameter_value(value: str) -> str:
    """清洗 LCSC 参数值为 InvenTree 友好形式。

    步骤：
    1. 去除尾部括号注释：`"10nF (X7R)"` → `"10nF"`
    2. 尝试单位归一化：`"10kΩ"` → `"10k"`
    3. 区间取最低值（保留量纲）：`"-40°C ~ 85°C"` → `"-40°C"`
    4. 全角符号统一
    """
    if not value:
        return ""
    s = value.strip()
    s = s.replace("Ω", "Ω").replace("℃", "°C")  # 显式 noop，便于将来替换
    # 括号
    s = _PAREN_TRAIL_RE.sub("", s).strip()
    # 区间
    m = _RANGE_RE.match(s)
    if m:
        s = f"{m.group(1)}{m.group(2)}".strip()
    # 单位归一化
    for pat, repl in _UNIT_PATTERNS:
        s = pat.sub(repl, s)
    return s.strip()


def _walk_chain(
    category_sub: str | None,
    category_top: str | None,
    rules: dict[str, Any],
) -> list[str]:
    """按 parent 链路展开 YAML key 顺序。

    返回的顺序即字段映射优先级（先父后子，子可覆盖父）。
    """
    seen: set[str] = set()
    chain: list[str] = []
    queue: list[str] = [c for c in (category_sub, category_top, "Base") if c]
    while queue:
        key = queue.pop(0)
        if key in seen:
            continue
        seen.add(key)
        chain.append(key)
        cfg = rules.get(key)
        if isinstance(cfg, dict):
            parents = cfg.get("parent")
            if isinstance(parents, list):
                queue.extend(p for p in parents if p)
            elif isinstance(parents, str) and parents:
                queue.append(parents)
    return chain


def to_inventree_parameters(
    part: LCSCPart,
    *,
    category_top: str | None = None,
    category_sub: str | None = None,
) -> dict[str, dict[str, str]]:
    """根据 `config/field_map.yaml` 把 LCSC 规格参数映射为 InvenTree 参数。

    YAML 语义（key=InvenTree 模板名, value=LCSC 字段名候选列表）：
        Resistors:
            parent: [Passives]
            Value:      [Resistance, "Resistance Value"]
            Rated Power:["Power (Watts)", "Power Rating", "Rated Power"]

    Returns:
        {invenree_template_name: {"value": cleaned, "raw": original}}
    """
    rules = load_yaml("field_map.yaml")
    chain = _walk_chain(category_sub, category_top, rules)

    # 第一阶段：确定每个 InvenTree 模板对应的 LCSC 字段（取链路上第一个存在的候选）
    inv_to_lcsc: dict[str, str] = {}
    for key in chain:
        cfg = rules.get(key)
        if not isinstance(cfg, dict):
            continue
        for inv_name, lcsc_candidates in cfg.items():
            if inv_name == "parent" or inv_name in inv_to_lcsc:
                continue
            if not isinstance(lcsc_candidates, list) or not lcsc_candidates:
                continue
            for lcsc_name in lcsc_candidates:
                if part.get_property(lcsc_name):
                    inv_to_lcsc[inv_name] = lcsc_name
                    break

    # 第二阶段：取值 + 清洗
    out: dict[str, dict[str, str]] = {}
    for inv_name, lcsc_name in inv_to_lcsc.items():
        raw = part.get_property(lcsc_name)
        if raw is None:
            continue
        cleaned = clean_parameter_value(raw)
        if cleaned:
            out[inv_name] = {"value": cleaned, "raw": raw}
    return out


def to_part_notes(
    part: LCSCPart,
    *,
    quantity: int | None = None,
    extra_note: str | None = None,
) -> str:
    """构造 Part.notes Markdown 内容（包含 datasheet + 来源 + 可选 BOM 备注）。"""
    lines: list[str] = []
    if part.datasheet_url_resolved:
        lines.append(f"- **Datasheet**: <{part.datasheet_url_resolved}>")
    if part.page_url:
        lines.append(f"- **LCSC**: <{part.page_url}>")
    if part.image_urls:
        lines.append(f"- **Image**: <{part.image_urls[0]}>")
    if part.offer and part.offer.inventory_level is not None:
        lines.append(f"- **LCSC stock**: {part.offer.inventory_level}")
    if quantity is not None:
        lines.append(f"- **BOM quantity**: {quantity}")
    if extra_note:
        lines.append(f"- {extra_note}")
    return "\n".join(lines) if lines else ""