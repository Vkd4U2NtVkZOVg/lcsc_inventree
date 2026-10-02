"""hablelocate 插件测试（离线，不依赖 InvenTree/Django 运行环境）。

覆盖：
- ha_client.trigger_tag：服务名规范化、URL/头/body 组装、鉴权头、
  HTTP 4xx/5xx、网络异常、参数缺失
- tags.resolve_tag：库存项优先、货位回退、空值跳过、自定义键名

注：plugin.py 里的 Django 薄封装（locate_stock_item 等）依赖 InvenTree
运行时，无法离线导入；其逻辑已收敛到上述两个纯函数模块中。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hablelocate"


def _load(name: str):
    """按文件路径加载 hablelocate 包内模块（绕开会拉起 InvenTree 的包 __init__）。"""
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ha_client():
    return _load("ha_client")


@pytest.fixture
def tags():
    return _load("tags")


# ---------------------------------------------------------------------------
# tags.resolve_tag
# ---------------------------------------------------------------------------


class TestResolveTag:
    def test_location_tag_used_when_item_empty(self, tags):
        assert tags.resolve_tag(None, {"ble_tag": "LOC-1"}, key="ble_tag") == "LOC-1"

    def test_item_tag_overrides_location(self, tags):
        assert tags.resolve_tag({"ble_tag": "ITEM-9"}, {"ble_tag": "LOC-1"}, key="ble_tag") == "ITEM-9"

    def test_item_empty_string_falls_back(self, tags):
        assert tags.resolve_tag({"ble_tag": ""}, {"ble_tag": "LOC-1"}, key="ble_tag") == "LOC-1"

    def test_missing_everywhere_returns_none(self, tags):
        assert tags.resolve_tag(None, None) is None
        assert tags.resolve_tag({}, {}) is None
        assert tags.resolve_tag({"other": "x"}, {"other": "y"}) is None

    def test_custom_key(self, tags):
        assert tags.resolve_tag({"tag": "T1"}, {"tag": "T2"}, key="tag") == "T1"

    def test_value_stripped(self, tags):
        assert tags.resolve_tag({"ble_tag": "  TAG-7  "}, None, key="ble_tag") == "TAG-7"


# ---------------------------------------------------------------------------
# ha_client.trigger_tag
# ---------------------------------------------------------------------------


class TestNormalizeService:
    def test_dot_form(self, ha_client):
        assert ha_client.normalize_service("script.ble_tag_locate") == (
            "script", "ble_tag_locate")

    def test_slash_form(self, ha_client):
        assert ha_client.normalize_service("script/ble_tag_locate") == (
            "script", "ble_tag_locate")

    def test_invalid(self, ha_client):
        # domain 原样保留、name 为空 —— trigger_tag 会据此拒绝
        assert ha_client.normalize_service("nosuffix") == ("nosuffix", "")


class TestTriggerTag:
    def test_posts_to_ha_with_auth_and_payload(self, ha_client, monkeypatch):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json, headers=headers, timeout=timeout)
            resp = type("R", (), {"status_code": 200, "text": "[]"})()
            return resp

        monkeypatch.setattr(ha_client.requests, "post", fake_post)

        ok, detail = ha_client.trigger_tag(
            ha_url="http://ha.test:8123/",
            token="tok-1",
            service="script.ble_tag_locate",
            tag_id="TAG-A1",
            seconds=15,
            timeout=3,
        )

        assert ok is True
        assert detail == "ok"
        assert captured["url"] == "http://ha.test:8123/api/services/script/ble_tag_locate"
        assert captured["json"] == {"tag_id": "TAG-A1", "seconds": 15}
        assert captured["headers"] == {"Authorization": "Bearer tok-1"}
        assert captured["timeout"] == 3

    def test_no_token_omits_auth_header(self, ha_client, monkeypatch):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["headers"] = headers
            return type("R", (), {"status_code": 200, "text": "[]"})()

        monkeypatch.setattr(ha_client.requests, "post", fake_post)
        ok, _ = ha_client.trigger_tag(
            ha_url="http://ha.test", token="", service="script.x", tag_id="T"
        )
        assert ok is True
        assert captured["headers"] == {}

    def test_extra_data_merged(self, ha_client, monkeypatch):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return type("R", (), {"status_code": 200, "text": "[]"})()

        monkeypatch.setattr(ha_client.requests, "post", fake_post)
        ha_client.trigger_tag(
            ha_url="http://ha.test", token="t", service="script.x",
            tag_id="T", extra_data={"mode": "fast"},
        )
        assert captured["json"] == {"tag_id": "T", "seconds": 10, "mode": "fast"}

    def test_http_400_reported_not_raised(self, ha_client, monkeypatch):
        def fake_post(url, **kw):
            return type("R", (), {"status_code": 401, "text": "Unauthorized"})()

        monkeypatch.setattr(ha_client.requests, "post", fake_post)
        ok, detail = ha_client.trigger_tag(
            ha_url="http://ha.test", token="bad", service="script.x", tag_id="T"
        )
        assert ok is False
        assert "401" in detail and "Unauthorized" in detail

    def test_network_error_reported_not_raised(self, ha_client, monkeypatch):
        import requests as requests_mod

        def fake_post(url, **kw):
            raise requests_mod.ConnectionError("connect timeout")

        monkeypatch.setattr(ha_client.requests, "post", fake_post)
        ok, detail = ha_client.trigger_tag(
            ha_url="http://ha.test", token="t", service="script.x", tag_id="T"
        )
        assert ok is False
        assert "ConnectionError" in detail

    def test_missing_config_reported(self, ha_client, monkeypatch):
        # monkeypatch 避免「配置缺失」场景真的发请求
        monkeypatch.setattr(
            ha_client.requests, "post",
            lambda *a, **kw: pytest.fail("不应发起请求"))
        ok, detail = ha_client.trigger_tag(ha_url="", token="t",
                                           service="script.x", tag_id="T")
        assert ok is False and "HA_URL" in detail
        ok, detail = ha_client.trigger_tag(ha_url="http://ha.test", token="t",
                                           service="", tag_id="T")
        assert ok is False and "HA_SERVICE" in detail
        ok, detail = ha_client.trigger_tag(ha_url="http://ha.test", token="t",
                                           service="script.x", tag_id="  ")
        assert ok is False and "标签 ID" in detail


# ---------------------------------------------------------------------------
# tags.resolve_tag_from_pairs（货位参数）
# ---------------------------------------------------------------------------


class TestResolveTagFromPairs:
    def test_matches_template_name(self, tags):
        assert tags.resolve_tag_from_pairs([("TAG_ID", "1234")]) == "1234"

    def test_case_insensitive(self, tags):
        assert tags.resolve_tag_from_pairs([("tag_id", "AB1")]) == "AB1"

    def test_skips_other_and_empty(self, tags):
        pairs = [("NAME", "x"), ("TAG_ID", ""), ("TAG_ID", None), ("TAG_ID", "777")]
        assert tags.resolve_tag_from_pairs(pairs) == "777"

    def test_no_match_returns_none(self, tags):
        assert tags.resolve_tag_from_pairs([("NAME", "x")]) is None
        assert tags.resolve_tag_from_pairs([]) is None
        assert tags.resolve_tag_from_pairs(None) is None

    def test_value_stripped(self, tags):
        assert tags.resolve_tag_from_pairs([("TAG_ID", " 42 ")]) == "42"


# ---------------------------------------------------------------------------
# tags.build_notification（站内通知文案）
# ---------------------------------------------------------------------------


class TestBuildNotification:
    def test_success_text(self, tags):
        name, message = tags.build_notification(True, "1234", "")
        assert name == "BLE 定位成功：标签 1234"
        assert "闪烁" in message

    def test_failure_text_truncated(self, tags):
        detail = "x" * 500
        name, message = tags.build_notification(False, "T1", detail)
        assert name == "BLE 定位失败：标签 T1"
        assert message.startswith("触发失败：")
        assert len(message) <= 120
