"""InvenTree 定位插件：通过 Home Assistant 触发 BLE 标签 LED 闪烁。

部署：放进 InvenTree 自定义插件目录（INVENTREE_PLUGIN_DIR），plugins.txt
加包名 `hablelocate`，重启后在管理界面启用并配置 HA 地址 / 令牌。
"""

from __future__ import annotations

import logging

from plugin import InvenTreePlugin
from plugin.mixins import LocateMixin, SettingsMixin

from . import ha_client
from .tags import DEFAULT_TAG_KEY, build_notification, resolve_tag, resolve_tag_from_pairs

logger = logging.getLogger("inventree.plugins.hablelocate")


def _int_setting(value, default: int) -> int:
    """宽容解析整型设置：坏值回退默认，不抛异常。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class HaBleLocatePlugin(LocateMixin, SettingsMixin, InvenTreePlugin):
    """Home Assistant BLE 标签定位插件。

    定位库存项 → 找到其货位 → 读 metadata 里的标签 ID → 调 HA 服务闪烁。
    """

    NAME = "HaBleLocate"
    SLUG = "hablelocate"
    TITLE = "BLE 标签定位（Home Assistant 桥接）"
    DESCRIPTION = "定位库存 / 货位时通过 Home Assistant 触发 BLE 标签 LED 闪烁"
    VERSION = "0.1.0"
    AUTHOR = "lcsc2inventree contributors"

    SETTINGS = {
        "HA_URL": {
            "name": "Home Assistant 地址",
            "description": "HA 的访问地址，如 http://192.168.11.5:8123",
            "default": "http://homeassistant.local:8123",
            "required": True,
        },
        "HA_TOKEN": {
            "name": "HA 长期访问令牌",
            "description": "在 HA 个人资料页底部创建「长期访问令牌」后粘贴到这里",
            "default": "",
            "protected": True,
        },
        "HA_SERVICE": {
            "name": "HA 服务名",
            "description": "触发的服务（domain.service），对应你 HA 里的标签闪烁脚本",
            "default": "script.ble_tag_locate",
        },
        "TAG_METADATA_KEY": {
            "name": "标签键名",
            "description": "货位参数模板名（推荐，如 TAG_ID）；"
                           "同时兼容库存项/货位 metadata 的同名键",
            "default": DEFAULT_TAG_KEY,
        },
        "BLINK_SECONDS": {
            "name": "闪烁秒数",
            "description": "传给 HA 的闪烁时长（秒）",
            "default": 10,
            "validator": int,
        },
        "REQUEST_TIMEOUT": {
            "name": "请求超时（秒）",
            "description": "调用 HA 的 HTTP 超时",
            "default": 5,
            "validator": int,
        },
    }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _metadata_key(self) -> str:
        return (self.get_setting("TAG_METADATA_KEY") or DEFAULT_TAG_KEY).strip()

    def _location_tag(self, location, key: str) -> str | None:
        """货位标签：先查参数模板（如 TAG_ID），没有再退回 metadata。"""
        try:
            pairs = [
                (
                    getattr(getattr(p, "template", None), "name", None),
                    getattr(p, "data", None),
                )
                for p in location.get_parameters()
            ]
        except Exception:  # noqa: BLE001 — 参数读取失败退回 metadata
            logger.warning(
                "读取货位 %s 参数失败", getattr(location, "pk", "?"), exc_info=True
            )
            pairs = []
        tag = resolve_tag_from_pairs(pairs, key=key)
        if tag:
            return tag
        return resolve_tag(None, getattr(location, "metadata", None), key=key)

    def _trigger(self, tag: str, instance=None) -> None:
        """调 HA 触发标签，并把真实结果写进 InvenTree 站内通知。

        通知失败只记日志，绝不上抛（任务系统里失败会重试/刷屏）。
        """
        ok, detail = ha_client.trigger_tag(
            ha_url=self.get_setting("HA_URL") or "",
            token=self.get_setting("HA_TOKEN") or "",
            service=self.get_setting("HA_SERVICE") or "",
            tag_id=tag,
            seconds=_int_setting(self.get_setting("BLINK_SECONDS"), 10),
            timeout=_int_setting(self.get_setting("REQUEST_TIMEOUT"), 5),
        )
        if not ok:
            logger.warning("标签 %s 触发失败: %s", tag, detail)
        self._notify_result(ok, tag, detail, instance)

    def _notify_result(self, ok: bool, tag: str, detail: str, instance=None) -> None:
        """把定位的真实结果发到 InvenTree 右上角铃铛（UI 站内通知）。

        前端「已请求项目位置」的 toast 是接口受理提示，真实执行结果
        只能通过通知系统回传；这里发给所有活跃员工用户。
        """
        try:
            from common.notifications import trigger_notification
            from django.contrib.auth import get_user_model
            from plugin.builtin.integration.core_notifications import (
                InvenTreeUINotifications,
            )

            users = list(
                get_user_model().objects.filter(is_active=True, is_staff=True)
            )
            if not users:
                logger.info("无活跃员工用户，跳过结果通知")
                return

            name, message = build_notification(ok, tag, detail)
            trigger_notification(
                instance,  # 通知可点击跳转到该库存/货位
                "hablelocate.locate_result",
                context={"name": name, "message": message},
                targets=users,
                delivery_methods={InvenTreeUINotifications},
                check_recent=False,  # 每次定位都应回传结果，不做 24h 去重
            )
            logger.info("定位结果通知已发送（%s：%s）", "成功" if ok else "失败", tag)
        except Exception:  # noqa: BLE001 — 通知失败不影响定位主流程
            logger.warning("发送定位结果通知失败", exc_info=True)

    # ------------------------------------------------------------------
    # LocateMixin 实现
    # ------------------------------------------------------------------

    def locate_stock_item(self, item_pk):
        """定位库存项：不在库跳过；标签 ID 取库存项 metadata，缺省回退货位。"""
        from stock.models import StockItem  # Django 环境内延迟导入

        try:
            item = StockItem.objects.get(pk=item_pk)
        except StockItem.DoesNotExist:
            logger.warning("StockItem pk=%s 不存在，跳过定位", item_pk)
            return

        if not item.in_stock:
            logger.info("库存 %s 不在库（已出货/耗尽），跳过定位", item_pk)
            return

        key = self._metadata_key()
        location = item.location
        # 优先级：库存项 metadata → 货位参数（TAG_ID）→ 货位 metadata
        tag = resolve_tag(
            item_metadata=getattr(item, "metadata", None),
            location_metadata=None,
            key=key,
        )
        if not tag and location is not None:
            tag = self._location_tag(location, key)
        if not tag:
            logger.info(
                "库存 %s（货位 %s）的 metadata 未设置 %s，跳过定位",
                item_pk, getattr(location, "pk", None), key,
            )
            return

        logger.info("定位库存 %s：触发标签 %s", item_pk, tag)
        self._trigger(tag, instance=item)

    def locate_stock_location(self, location_pk):
        """定位货位：读货位 metadata 的标签 ID 并触发。"""
        from stock.models import StockLocation

        try:
            location = StockLocation.objects.get(pk=location_pk)
        except StockLocation.DoesNotExist:
            logger.warning("StockLocation pk=%s 不存在，跳过定位", location_pk)
            return

        key = self._metadata_key()
        tag = self._location_tag(location, key)
        if not tag:
            logger.info("货位 %s 未配置标签（参数 %s / metadata 均无），跳过定位", location_pk, key)
            return

        logger.info("定位货位 %s：触发标签 %s", location_pk, tag)
        self._trigger(tag, instance=location)
