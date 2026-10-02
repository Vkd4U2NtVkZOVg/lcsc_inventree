"""标签解析：从货位参数 / 库存项与货位的 metadata 里取标签 ID。

纯函数模块（不 import InvenTree 依赖），便于离线测试。
"""

from __future__ import annotations

DEFAULT_TAG_KEY = "TAG_ID"


def resolve_tag(
    item_metadata: dict | None,
    location_metadata: dict | None,
    key: str = DEFAULT_TAG_KEY,
) -> str | None:
    """按「库存项优先，其次货位」的顺序取标签 ID。

    Args:
        item_metadata: 库存项的 metadata 字典（可为 None）。
        location_metadata: 货位的 metadata 字典（可为 None）。
        key: metadata 里存标签 ID 的键名。

    Returns:
        标签 ID 字符串；两边都没有时返回 None。空串视为未设置。
    """
    for meta in (item_metadata, location_metadata):
        value = (meta or {}).get(key)
        if value:
            return str(value).strip()
    return None


def resolve_tag_from_pairs(
    pairs, key: str = DEFAULT_TAG_KEY
) -> str | None:
    """从货位参数列表里按模板名匹配标签 ID。

    Args:
        pairs: [(模板名, 值), ...]，例如 [("TAG_ID", "1234"), ...]。
        key: 参数模板名（大小写不敏感）。

    Returns:
        匹配到的参数值；没有匹配时返回 None。
    """
    wanted = (key or "").strip().lower()
    for name, value in pairs or []:
        if (name or "").strip().lower() == wanted and value:
            return str(value).strip()
    return None


def build_notification(ok: bool, tag: str, detail: str) -> tuple[str, str]:
    """把触发结果格式化为站内通知的 (name, message)。

    message 截断到 120 字符以内（InvenTree 通知文本的建议上限）。
    """
    if ok:
        name = f"BLE 定位成功：标签 {tag}"
        message = "标签已通过 Home Assistant 触发闪烁"
    else:
        name = f"BLE 定位失败：标签 {tag}"
        message = f"触发失败：{detail}"
    return name, message[:120]
