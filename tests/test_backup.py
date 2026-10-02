"""`lcsc2inv.backup` 测试（离线，用假 docker client 模拟 docker.sock 交互）。

覆盖：
- run_inventree_backup：成功拷贝本次新文件、流式日志回传、容器不存在、命令失败、
  无新文件、连不上 daemon
- list_backups：列出快照与文件大小
- _extract_new：按 mtime 过滤 + 路径穿越防护
- 未配置容器名 → ValueError
"""

from __future__ import annotations

import io
import tarfile
import time
from pathlib import Path

import pytest

from lcsc2inv import backup
from lcsc2inv.config import Settings


def make_settings(tmp_path: Path, *, container: str = "inventree-server") -> Settings:
    return Settings(
        inventree_backup_container=container,
        inventree_backup_cmd="invoke backup",
        inventree_backup_src_dir="/home/inventree/data/backup",
        inventree_backup_storage=str(tmp_path),
    )


def make_tar(files: list[tuple[str, bytes, float]]) -> bytes:
    """构造一个内存 tar，每条 = (名字, 内容, mtime)。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data, mtime in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = mtime
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeApi:
    """模拟 docker 底层 APIClient（exec_create/exec_start/exec_inspect）。"""

    def __init__(
        self,
        *,
        exit_code: int = 0,
        chunks: list[bytes] | None = None,
        inspect_exit_code: int | None = None,
    ):
        self.exit_code = exit_code
        self.chunks = list(chunks or [])
        self.inspect_exit_code = inspect_exit_code
        self.exec_id = "exec-1"
        self.create_calls: list[tuple[str, list[str]]] = []
        self.start_calls: list[str] = []

    def exec_create(self, container_id, cmd, **kw):
        self.create_calls.append((container_id, list(cmd)))
        return {"Id": self.exec_id}

    def exec_start(self, exec_id, **kw):
        self.start_calls.append(exec_id)
        return iter(self.chunks)

    def exec_inspect(self, exec_id):
        code = self.inspect_exit_code if self.inspect_exit_code is not None else self.exit_code
        return {"ExitCode": code}


class FakeContainer:
    def __init__(self, *, api: FakeApi | None = None, tar_data: bytes = b""):
        self.id = "ctr-123"
        self.api = api or FakeApi()
        self.tar_data = tar_data

    def get_archive(self, path):  # noqa: F841
        return iter([self.tar_data]), {"size": len(self.tar_data)}


class FakeDocker:
    def __init__(
        self,
        containers: dict[str, FakeContainer],
        api: FakeApi | None = None,
        missing_raises: Exception | None = None,
    ):
        self._containers = containers
        self.api = api or FakeApi()
        self._missing_raises = missing_raises

    @property
    def containers(self):
        return self

    def get(self, name: str):
        if name in self._containers:
            return self._containers[name]
        if self._missing_raises:
            raise self._missing_raises
        raise RuntimeError(f"container {name} not found")


@pytest.fixture(autouse=True)
def _reset_factory():
    backup.set_docker_factory(None)
    yield
    backup.set_docker_factory(None)


def _stub(fake: FakeDocker):
    backup.set_docker_factory(lambda: fake)


def test_run_success(tmp_path):
    now = time.time()
    tar = make_tar(
        [
            ("backup-inventree-20260926.psql.gz", b"DBDUMP", now),
            ("media-backup-20260926.zip", b"MEDIA", now),
            ("old-20200101.psql.gz", b"OLD", now - 100000),  # 应被过滤
        ]
    )
    api = FakeApi(exit_code=0, chunks=[b"backup ok\n"])
    container = FakeContainer(api=api, tar_data=tar)
    _stub(FakeDocker({"inventree-server": container}, api))

    result = backup.run_inventree_backup(make_settings(tmp_path))

    assert result["exit_code"] == 0
    assert api.create_calls == [("ctr-123", ["invoke", "backup"])]
    snapshot_dir = tmp_path / result["snapshot"]
    assert (snapshot_dir / "backup-inventree-20260926.psql.gz").read_bytes() == b"DBDUMP"
    assert (snapshot_dir / "media-backup-20260926.zip").read_bytes() == b"MEDIA"
    # 旧文件被 mtime 过滤，不进入本次快照
    assert not (snapshot_dir / "old-20200101.psql.gz").exists()
    assert set(result["files"]) == {
        "backup-inventree-20260926.psql.gz",
        "media-backup-20260926.zip",
    }


def test_run_streams_log(tmp_path):
    """备份命令的每一段输出与关键步骤都应实时回调 log_sink。"""
    now = time.time()
    tar = make_tar([("backup-inventree-20260926.psql.gz", b"DBDUMP", now)])
    api = FakeApi(exit_code=0, chunks=[b"line1\n", b"line2\n"])
    container = FakeContainer(api=api, tar_data=tar)
    _stub(FakeDocker({"inventree-server": container}, api))

    lines: list[str] = []
    result = backup.run_inventree_backup(make_settings(tmp_path), log_sink=lines.append)

    assert any("line1" in line for line in lines), lines
    assert any("line2" in line for line in lines), lines
    assert any("执行备份命令" in line for line in lines), lines
    assert any("拉取容器内产物" in line for line in lines), lines
    assert any("完成" in line for line in lines), lines
    # 完整输出应包含所有命令输出段
    assert "line1\nline2\n" in result["full_output"]


def test_missing_container_name(tmp_path):
    s = make_settings(tmp_path, container="")
    with pytest.raises(ValueError, match="INVENTREE_BACKUP_CONTAINER"):
        backup.run_inventree_backup(s)


def test_container_not_found(tmp_path):
    _stub(FakeDocker({}, missing_raises=RuntimeError("no such container")))
    with pytest.raises(RuntimeError, match="找不到 InvenTree 容器"):
        backup.run_inventree_backup(make_settings(tmp_path))


def test_command_failed(tmp_path):
    api = FakeApi(exit_code=1, chunks=[b"boom"])
    container = FakeContainer(api=api)
    _stub(FakeDocker({"inventree-server": container}, api))
    with pytest.raises(RuntimeError, match="退出码 1"):
        backup.run_inventree_backup(make_settings(tmp_path))


def test_no_new_files(tmp_path):
    tar = make_tar([("old-20200101.psql.gz", b"OLD", time.time() - 100000)])
    api = FakeApi(exit_code=0)
    _stub(FakeDocker({"inventree-server": FakeContainer(api=api, tar_data=tar)}, api))
    with pytest.raises(RuntimeError, match="未找到本次生成的备份文件"):
        backup.run_inventree_backup(make_settings(tmp_path))


def test_daemon_unreachable(tmp_path):
    def boom():
        raise OSError("connect failed")

    backup.set_docker_factory(boom)
    with pytest.raises(RuntimeError, match="连接 docker daemon 失败"):
        backup.run_inventree_backup(make_settings(tmp_path))


def test_path_traversal_blocked(tmp_path):
    now = time.time()
    tar = make_tar(
        [
            ("../../evil.psql.gz", b"EVIL", now),  # 应被跳过
            ("ok.psql.gz", b"OK", now),
        ]
    )
    api = FakeApi(exit_code=0)
    _stub(FakeDocker({"inventree-server": FakeContainer(api=api, tar_data=tar)}, api))
    result = backup.run_inventree_backup(make_settings(tmp_path))
    assert result["files"] == ["ok.psql.gz"]
    assert not (tmp_path.parent / "evil.psql.gz").exists()


def test_list_backups(tmp_path):
    # 手工构造两个快照（含 get_archive 解包产生的嵌套子目录）
    a = tmp_path / "20260926-100000"
    b = tmp_path / "20260925-100000"
    (a / "backup").mkdir(parents=True)
    b.mkdir()
    (a / "backup" / "db.psql.gz").write_bytes(b"x" * 10)
    (a / "media.zip").write_bytes(b"y")
    (b / "db.psql.gz").write_bytes(b"z")
    (tmp_path / "stray.txt").write_text("not a snapshot dir")  # 顶层文件，应被忽略

    snaps = backup.list_backups(make_settings(tmp_path))

    assert [s["snapshot"] for s in snaps] == ["20260926-100000", "20260925-100000"]
    first = snaps[0]
    sizes = {f["name"]: f["size"] for f in first["files"]}
    # name 是相对快照目录的 posix 路径，嵌套子目录保留前缀
    assert sizes == {"backup/db.psql.gz": 10, "media.zip": 1}
