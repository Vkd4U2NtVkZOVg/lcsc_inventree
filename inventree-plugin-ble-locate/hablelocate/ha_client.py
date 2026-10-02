"""Home Assistant REST 调用：触发 BLE 标签闪烁。

独立于 Django/InvenTree 的纯函数模块（不 import 任何 InvenTree 依赖），
便于离线测试。所有错误只记日志并返回 (ok, detail)，绝不上抛——
定位失败不应影响 InvenTree 的正常使用。
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger("inventree.plugins.hablelocate")


def normalize_service(service: str) -> tuple[str, str]:
    """把服务名规范化为 (domain, name)。

    接受 `script.ble_tag_locate` 或 `script/ble_tag_locate` 两种写法。
    """
    svc = (service or "").strip().strip("/")
    svc = svc.replace("/", ".", 1)
    domain, _, name = svc.partition(".")
    return domain, name


def trigger_tag(
    ha_url: str,
    token: str,
    service: str,
    tag_id: str,
    seconds: int = 10,
    timeout: float = 5.0,
    extra_data: dict | None = None,
) -> tuple[bool, str]:
    """调用 HA 服务触发标签闪烁。

    Args:
        ha_url: HA 地址（如 http://192.168.11.5:8123）。
        token: HA 长期访问令牌。
        service: 服务名（domain.service 或 domain/service）。
        tag_id: 标签 ID（不透明字符串，原样传给 HA）。
        seconds: 闪烁时长（秒），放进服务数据。
        timeout: HTTP 超时（秒）。
        extra_data: 附加服务数据（可选）。

    Returns:
        (ok, detail)：ok 为 False 时 detail 是给日志看的说明。
    """
    if not (ha_url or "").strip() or not (service or "").strip():
        return False, "HA_URL 或 HA_SERVICE 未配置"
    if not (tag_id or "").strip():
        return False, "标签 ID 为空"

    domain, name = normalize_service(service)
    if not domain or not name:
        return False, f"服务名格式不合法: {service!r}（应为 domain.service）"

    url = f"{ha_url.rstrip('/')}/api/services/{domain}/{name}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {"tag_id": tag_id, "seconds": seconds}
    if extra_data:
        payload.update(extra_data)

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning("HA 调用失败 tag=%s: %s", tag_id, exc)
        return False, f"{type(exc).__name__}: {exc}"

    if resp.status_code >= 400:
        logger.warning(
            "HA 调用返回 %s tag=%s: %s", resp.status_code, tag_id, resp.text[:200]
        )
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"

    logger.info("HA 已触发标签 %s（服务 %s，时长 %ss）", tag_id, service, seconds)
    return True, "ok"
