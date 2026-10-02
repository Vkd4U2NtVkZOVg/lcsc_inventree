"""`lcsc2inv.barcode_lookup` 测试（离线，用假 docker client 模拟容器内查询）。

覆盖：
- get_bound_barcodes：成功解析标记后的 JSON、未绑定返回空串、批量、
  未配置容器名、exec 失败、输出无标记、非 JSON、model_type 白名单
- 命令是只读查询（manage.py shell -c），且动态定位 manage.py
"""

from __future__ import annotations

import pytest

from lcsc2inv import backup, barcode_lookup
from lcsc2inv.config import Settings


def make_settings(tmp_path, *, container: str = "inventree-server") -> Settings:
    return Settings(
        inventree_backup_container=container,
        inventree_backup_storage=str(tmp_path),
        lcsc_cache_dir=str(tmp_path),  # 缓存文件也隔离到 tmp，避免污染真实缓存
    )


class FakeContainer:
    def __init__(self, *, exit_code: int = 0, output: bytes = b""):
        self.exit_code = exit_code
        self.output = output
        self.last_cmd: list[str] = []

    def exec_run(self, cmd, **kw):  # noqa: F841
        self.last_cmd = list(cmd)
        return self.exit_code, self.output


class FakeDocker:
    def __init__(self, containers: dict[str, FakeContainer]):
        self._containers = containers

    @property
    def containers(self):
        return self

    def get(self, name: str):
        if name in self._containers:
            return self._containers[name]
        raise RuntimeError(f"container {name} not found")


@pytest.fixture(autouse=True)
def _reset_factory():
    backup.set_docker_factory(None)
    yield
    backup.set_docker_factory(None)


def _shell_output(rows: list[dict]) -> bytes:
    import json

    return (
        b"113 objects imported automatically (use -v 2 for details).\n"
        + barcode_lookup._MARKER.encode()
        + b"\n"
        + json.dumps(rows).encode()
        + b"\n"
    )


def test_success(tmp_path):
    container = FakeContainer(
        output=_shell_output([{"pk": 2358, "barcode_data": "X198516570"}])
    )
    backup.set_docker_factory(lambda: FakeDocker({"inventree-server": container}))

    result = barcode_lookup.get_bound_barcodes(
        make_settings(tmp_path), "stockitem", [2358]
    )

    assert result == {2358: "X198516570"}
    # 命令：sh -c 定位 manage.py 并执行只读 shell 查询
    assert container.last_cmd[0] == "sh"
    script = container.last_cmd[2]
    assert "manage.py" in script and "shell" in script
    assert "StockItem.objects.filter" in script


def test_unbound_returns_empty_string(tmp_path):
    container = FakeContainer(output=_shell_output([{"pk": 1, "barcode_data": None}]))
    backup.set_docker_factory(lambda: FakeDocker({"inventree-server": container}))

    result = barcode_lookup.get_bound_barcodes(make_settings(tmp_path), "stockitem", [1])

    assert result == {1: ""}


def test_batch(tmp_path):
    container = FakeContainer(
        output=_shell_output(
            [
                {"pk": 1, "barcode_data": "A1"},
                {"pk": 2, "barcode_data": None},
                {"pk": 3, "barcode_data": "C3"},
            ]
        )
    )
    backup.set_docker_factory(lambda: FakeDocker({"inventree-server": container}))

    result = barcode_lookup.get_bound_barcodes(
        make_settings(tmp_path), "stockitem", [1, 2, 3]
    )

    assert result == {1: "A1", 2: "", 3: "C3"}


def test_empty_pks_short_circuit(tmp_path):
    assert barcode_lookup.get_bound_barcodes(make_settings(tmp_path), "stockitem", []) == {}


def test_missing_container_name(tmp_path):
    with pytest.raises(ValueError, match="INVENTREE_BACKUP_CONTAINER"):
        barcode_lookup.get_bound_barcodes(
            make_settings(tmp_path, container=""), "stockitem", [1]
        )


def test_unsupported_model_type(tmp_path):
    with pytest.raises(ValueError, match="不支持的 model_type"):
        barcode_lookup.get_bound_barcodes(make_settings(tmp_path), "part", [1])


def test_exec_failure(tmp_path):
    container = FakeContainer(exit_code=1, output=b"boom")
    backup.set_docker_factory(lambda: FakeDocker({"inventree-server": container}))
    with pytest.raises(RuntimeError, match="退出码 1"):
        barcode_lookup.get_bound_barcodes(make_settings(tmp_path), "stockitem", [1])


def test_output_without_marker(tmp_path):
    container = FakeContainer(output=b"something unexpected")
    backup.set_docker_factory(lambda: FakeDocker({"inventree-server": container}))
    with pytest.raises(RuntimeError, match="输出异常"):
        barcode_lookup.get_bound_barcodes(make_settings(tmp_path), "stockitem", [1])


def test_invalid_json(tmp_path):
    container = FakeContainer(
        output=barcode_lookup._MARKER.encode() + b"\nnot-json\n"
    )
    backup.set_docker_factory(lambda: FakeDocker({"inventree-server": container}))
    with pytest.raises(RuntimeError, match="非 JSON"):
        barcode_lookup.get_bound_barcodes(make_settings(tmp_path), "stockitem", [1])


# ---------------------------------------------------------------------------
# 本地缓存 + 全量同步
# ---------------------------------------------------------------------------


import json as _json


def _seed_cache(settings, entries: dict[int, str]):
    barcode_lookup.save_cache(settings, entries)


def test_cache_roundtrip(tmp_path):
    s = make_settings(tmp_path)
    assert barcode_lookup.load_cache(s) == {}
    _seed_cache(s, {2358: "X1", 7: ""})
    assert barcode_lookup.load_cache(s) == {2358: "X1", 7: ""}
    info = barcode_lookup.cache_info(s)
    assert info["count"] == 2
    assert info["updated_at"]


def test_cache_corrupt_file(tmp_path):
    s = make_settings(tmp_path)
    barcode_lookup.cache_path(s).write_text("not json", encoding="utf-8")
    assert barcode_lookup.load_cache(s) == {}
    assert barcode_lookup.cache_info(s)["count"] == 0


def test_cache_put_lazy_fill(tmp_path):
    s = make_settings(tmp_path)
    barcode_lookup.cache_put(s, 9, "ABC")
    assert barcode_lookup.load_cache(s) == {9: "ABC"}


def _fake_all(rows: list[dict]) -> FakeContainer:
    import json as json_mod

    out = (
        barcode_lookup._MARKER.encode()
        + b"\n"
        + json_mod.dumps(rows).encode()
        + b"\n"
    )
    return FakeContainer(output=out)


def test_get_all_bound_barcodes(tmp_path):
    container = _fake_all(
        [{"pk": 1, "barcode_data": "A1"}, {"pk": 2, "barcode_data": "B2"}]
    )
    backup.set_docker_factory(lambda: FakeDocker({"inventree-server": container}))

    result = barcode_lookup.get_all_bound_barcodes(make_settings(tmp_path))

    assert result == {1: "A1", 2: "B2"}
    script = container.last_cmd[2]
    # 全量查询：shlex.quote 会转义引号，只断言关键片段存在
    assert "exclude" in script and "barcode_data" in script


def test_sync_cache_mirror(tmp_path):
    """全量镜像：新增/更新/删除都以远端为准，缓存与 InvenTree 完全一致。"""
    s = make_settings(tmp_path)
    _seed_cache(s, {1: "A1", 2: "OLD", 9: "Z9"})
    container = _fake_all(
        [
            {"pk": 1, "barcode_data": "A1"},          # 未变化
            {"pk": 2, "barcode_data": "CHANGED"},     # 远端变化 → 覆盖
            {"pk": 3, "barcode_data": "C3"},          # 新码 → 新增
            # pk=9 远端没有（已解绑）→ 从缓存删除
        ]
    )
    backup.set_docker_factory(lambda: FakeDocker({"inventree-server": container}))

    report = barcode_lookup.sync_cache(s)

    assert report["added_count"] == 1
    assert report["added"] == [{"pk": 3, "code": "C3"}]
    assert report["updated_count"] == 1
    assert report["updated"] == [{"pk": 2, "cached": "OLD", "remote": "CHANGED"}]
    assert report["removed_count"] == 1
    assert report["removed"] == [9]
    assert report["unchanged_count"] == 1
    assert report["remote_count"] == 3
    assert report["total_cached"] == 3
    assert report["duration_ms"] >= 0
    # 缓存 = 远端（镜像），pk=9 已被删除
    assert barcode_lookup.load_cache(s) == {1: "A1", 2: "CHANGED", 3: "C3"}


def test_sync_cache_empty_first_run(tmp_path):
    """首次同步：缓存为空，远端全部视为新码。"""
    s = make_settings(tmp_path)
    container = _fake_all([{"pk": 5, "barcode_data": "X5"}])
    backup.set_docker_factory(lambda: FakeDocker({"inventree-server": container}))

    report = barcode_lookup.sync_cache(s)

    assert report["added_count"] == 1
    assert report["total_cached"] == 1
    assert barcode_lookup.load_cache(s) == {5: "X5"}
