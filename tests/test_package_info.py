"""封装（Package）写入 InvenTree 的行为测试：描述后缀、参数、keywords、订单导入透传。

全部离线：writer 用 mock API / mock REST，不发任何 HTTP。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lcsc2inv.inventree_writer import InvenTreeWriter, WriteOptions, WriteResult
from lcsc2inv.lcsc_models import LCSCPart, PropertyValue


def _part(package: str | None = None, description: str = "10k 1% resistor") -> LCSCPart:
    props = [PropertyValue(name="封装", value=package)] if package else []
    return LCSCPart(
        sku="C25804",
        mpn="RC0603FR-0710KL",
        name="10kΩ 0603",
        description=description,
        additional_properties=props,
    )


def _writer() -> InvenTreeWriter:
    w = InvenTreeWriter.__new__(InvenTreeWriter)
    w.api = MagicMock()
    w.settings = MagicMock()
    w.supplier_name = "LCSC Electronics"
    w._mfr_cache = {}
    w._supplier_cache = {}
    w._category_cache = {}
    w._template_cache = {}
    return w


class _RestStub:
    """替换 writer._rest：按序回放 (status, data)，并记录调用。"""

    def __init__(self, responses: list[tuple[int, object]]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, method, path, *, params=None, json=None):
        self.calls.append(
            {"method": method, "path": path, "params": params, "json": json}
        )
        status, data = self.responses.pop(0)
        return status, data

    def of(self, method: str, path: str) -> list[dict]:
        return [c for c in self.calls if c["method"] == method and c["path"] == path]


# ---------------------------------------------------------------------------
# 纯函数：描述后缀 / 封装取值
# ---------------------------------------------------------------------------


class TestPackageSuffix:
    def test_appended(self):
        out = InvenTreeWriter._with_package_suffix("10k 1% resistor", "0603")
        assert out == "10k 1% resistor（封装：0603）"

    def test_dedup_when_already_in_description(self):
        # 描述里已含封装串（大小写不敏感）→ 不重复追加
        assert InvenTreeWriter._with_package_suffix("chip 0603 resistor", "0603") == \
            "chip 0603 resistor"
        assert InvenTreeWriter._with_package_suffix("SOD-123 diode", "sod-123") == \
            "SOD-123 diode"

    def test_empty_description(self):
        assert InvenTreeWriter._with_package_suffix("", "0805") == "封装：0805"

    def test_no_package(self):
        assert InvenTreeWriter._with_package_suffix("abc", None) == "abc"


class TestResolvePackage:
    def test_explicit_wins(self):
        part = _part(package="弯插")
        assert InvenTreeWriter._resolve_package(part, "0805") == "0805"

    def test_falls_back_to_part(self):
        part = _part(package="弯插")
        assert InvenTreeWriter._resolve_package(part, None) == "弯插"

    def test_missing_everywhere(self):
        assert InvenTreeWriter._resolve_package(_part(), "  ") is None


# ---------------------------------------------------------------------------
# 创建路径：_ensure_part 的 payload
# ---------------------------------------------------------------------------


class TestEnsurePartPayload:
    def _run(self, part: LCSCPart, package: str | None):
        w = _writer()
        created = MagicMock()
        created.pk = 42
        with patch.object(w, "_find_part_by_ipn", return_value=None), \
             patch("lcsc2inv.inventree_writer.Part") as PartMock:
            PartMock.create.return_value = created
            pk, is_created = w._ensure_part(
                part, category_pk=7, update=False, dry_run=False, package=package
            )
        assert (pk, is_created) == (42, True)
        return PartMock.create.call_args.args[1]

    def test_description_suffix_and_keywords(self):
        payload = self._run(_part(description="10k 1% resistor"), package="0603")
        assert payload["description"] == "10k 1% resistor（封装：0603）"
        # keywords 也带上封装，方便搜索
        assert "0603" in payload["keywords"].split(",")

    def test_dedup_against_lcsc_description(self):
        payload = self._run(_part(description="10k 1% 0603 resistor"), package="0603")
        assert payload["description"] == "10k 1% 0603 resistor"

    def test_no_package_unchanged(self):
        payload = self._run(_part(), package=None)
        assert payload["description"] == "10k 1% resistor"
        assert "封装" not in payload["description"]


# ---------------------------------------------------------------------------
# 更新路径：update_part_fields
# ---------------------------------------------------------------------------


class TestUpdatePartFieldsPackage:
    def _run(self, part: LCSCPart, *, footprint: str | None = None):
        w = _writer()
        saved: list[dict] = []

        class FakePart:
            def __init__(self, api, pk):
                self.pk = pk
            def save(self, payload):
                saved.append(payload)

        with patch("lcsc2inv.inventree_writer.Part", FakePart):
            w.update_part_fields(
                part, part_pk=1, update_description=True,
                update_image=False, update_keywords=True,
                update_notes=False, update_parameters=False,
                footprint=footprint,
            )
        return saved[0]

    def test_suffix_from_lcsc_data(self):
        payload = self._run(_part(package="弯插", description="RJ45 连接器"))
        assert payload["description"] == "RJ45 连接器（封装：弯插）"

    def test_suffix_from_explicit_footprint(self):
        payload = self._run(_part(description="RJ45 连接器"), footprint="0805")
        assert payload["description"] == "RJ45 连接器（封装：0805）"

    def test_keywords_include_package(self):
        payload = self._run(_part(package="0603"))
        assert "0603" in payload["keywords"].split(",")


# ---------------------------------------------------------------------------
# 参数写入（REST 实现，绕过 SDK 版本门禁；兼容新旧 InvenTree 端点）
# ---------------------------------------------------------------------------


def _writer_new_api() -> InvenTreeWriter:
    w = _writer()
    w._param_style = "new"  # 跳过端点探测
    return w


class TestSetPackageParameter:
    def test_creates_template_then_param(self):
        w = _writer_new_api()
        rest = _RestStub([
            (200, [{"pk": 8, "name": "Tolerance"}]),  # 模板列表：无 Package
            (201, {"pk": 7}),                          # 创建 Package 模板
            (200, []),                                 # Part1 无参数
            (201, {"pk": 9}),                          # 创建参数
            (200, []),                                 # Part2 无参数
            (201, {"pk": 10}),                         # 创建参数
        ])
        w._rest = rest
        assert w.set_package_parameter(part_pk=1, value="0805") is True
        tpl_posts = rest.of("POST", "/api/parameter/template/")
        assert tpl_posts[0]["json"]["name"] == "Package"
        assert tpl_posts[0]["json"]["model_type"] == "part"
        posts = rest.of("POST", "/api/parameter/")
        assert posts[0]["json"] == {
            "model_type": "part", "model_id": 1, "template": 7, "data": "0805",
        }
        # 模板已缓存，重复调用不再请求模板接口
        assert w.set_package_parameter(part_pk=2, value="0603") is True
        assert len(rest.of("GET", "/api/parameter/template/")) == 1

    def test_patch_when_value_differs(self):
        w = _writer_new_api()
        rest = _RestStub([
            (200, [{"pk": 7, "name": "Package"}]),     # 模板已存在
            (200, [{"pk": 5, "template": 7, "data": "old"}]),
            (200, {}),
        ])
        w._rest = rest
        assert w.set_package_parameter(part_pk=1, value="0603") is True
        patches = rest.of("PATCH", "/api/parameter/5/")
        assert patches[0]["json"] == {"data": "0603"}

    def test_noop_when_value_equal(self):
        w = _writer_new_api()
        rest = _RestStub([
            (200, [{"pk": 7, "name": "Package"}]),
            (200, [{"pk": 5, "template": 7, "data": "0805"}]),
        ])
        w._rest = rest
        assert w.set_package_parameter(part_pk=1, value="0805") is True
        assert rest.of("PATCH", "/api/parameter/5/") == []
        assert rest.of("POST", "/api/parameter/") == []

    def test_old_api_style_fallback(self):
        """旧版 InvenTree（/api/part/parameter/，part/value 字段）。"""
        w = _writer()
        rest = _RestStub([
            (404, {"detail": "Not found"}),            # 探测 new 端点 → 失败
            (200, [{"pk": 9, "name": "Other"}]),       # 探测 old 端点 → 成功
            (200, [{"pk": 7, "name": "Package"}]),     # old 模板列表
            (200, [{"pk": 5, "template": 7, "value": "old"}]),
            (200, {}),
        ])
        w._rest = rest
        assert w.set_package_parameter(part_pk=1, value="0603") is True
        assert rest.calls[0]["path"] == "/api/parameter/template/"
        patches = rest.of("PATCH", "/api/part/parameter/5/")
        assert patches[0]["json"] == {"value": "0603"}

    def test_template_query_failure_returns_false(self):
        w = _writer()
        # new/old 端点探测各一次 + _ensure_template 实际查询一次，全部失败
        w._rest = _RestStub([(500, None), (500, None), (500, None)])
        assert w.set_package_parameter(part_pk=1, value="0805") is False

    def test_param_write_failure_raises(self):
        w = _writer_new_api()
        rest = _RestStub([
            (200, [{"pk": 7, "name": "Package"}]),
            (200, []),
            (400, {"detail": "bad"}),
        ])
        w._rest = rest
        with pytest.raises(RuntimeError, match="HTTP 400"):
            w.set_package_parameter(part_pk=1, value="0805")


class TestWriteParametersRest:
    def test_creates_params_from_mapping(self):
        part = LCSCPart(
            sku="C1",
            additional_properties=[PropertyValue(name="Package", value="0805")],
        )
        w = _writer_new_api()
        rest = _RestStub([
            (200, []),                                  # 现有参数为空
            (200, [{"pk": 7, "name": "Package"}]),      # 模板已存在
            (201, {"pk": 9}),                           # 创建参数
        ])
        w._rest = rest
        w._write_parameters(part, category_top=None, category_sub=None, part_pk=5)
        posts = rest.of("POST", "/api/parameter/")
        assert len(posts) == 1
        assert posts[0]["json"] == {
            "model_type": "part", "model_id": 5, "template": 7, "data": "0805",
        }

    def test_updates_only_changed_values(self):
        part = LCSCPart(
            sku="C1",
            additional_properties=[PropertyValue(name="Package", value="0805")],
        )
        w = _writer_new_api()
        rest = _RestStub([
            (200, [{"pk": 90, "template": 7, "data": "0603"}]),  # 已有旧值
            (200, [{"pk": 7, "name": "Package"}]),               # 模板
            (200, {}),                                            # PATCH
        ])
        w._rest = rest
        w._write_parameters(part, category_top=None, category_sub=None, part_pk=5)
        patches = rest.of("PATCH", "/api/parameter/90/")
        assert patches[0]["json"] == {"data": "0805"}
        assert rest.of("POST", "/api/parameter/") == []


class TestCleanKeywords:
    def test_dedup_case_insensitive(self):
        assert InvenTreeWriter._clean_keywords("A,B,a,b ,B") == ["A", "B"]

    def test_comma_inside_package(self):
        raw = "C1,GL5516,Through Hole,P=3.4mm,Through Hole,P=3.4mm"
        assert InvenTreeWriter._clean_keywords(raw) == \
            ["C1", "GL5516", "Through Hole", "P=3.4mm"]

    def test_empty(self):
        assert InvenTreeWriter._clean_keywords("") == []
        assert InvenTreeWriter._clean_keywords(None) == []


class TestGetPackageParameter:
    def test_template_missing_returns_none_without_create(self):
        w = _writer_new_api()
        rest = _RestStub([(200, [])])  # 模板列表为空
        w._rest = rest
        assert w.get_package_parameter(part_pk=1) is None
        assert rest.of("POST", "/api/parameter/") == []  # 只读，不创建
        assert rest.of("POST", "/api/parameter/template/") == []

    def test_returns_existing_value(self):
        w = _writer_new_api()
        rest = _RestStub([
            (200, [{"pk": 7, "name": "Package"}]),
            (200, [{"pk": 5, "template": 7, "data": "0805"}]),
        ])
        w._rest = rest
        assert w.get_package_parameter(part_pk=1) == "0805"

    def test_param_missing_returns_none(self):
        w = _writer_new_api()
        rest = _RestStub([
            (200, [{"pk": 7, "name": "Package"}]),
            (200, []),
        ])
        w._rest = rest
        assert w.get_package_parameter(part_pk=1) is None


# ---------------------------------------------------------------------------
# WriteOptions / WriteResult 兼容
# ---------------------------------------------------------------------------


def test_write_options_footprint_default():
    opts = WriteOptions()
    assert opts.footprint is None
    assert WriteOptions(footprint="0603").footprint == "0603"


def test_write_result_unchanged_shape():
    r = WriteResult(part_pk=1)
    assert r.ok() is True
