"""InvenTree 数据库一键备份。

通过宿主机挂载进容器的 `docker.sock`，进入 InvenTree 容器执行其原生备份命令
（默认 `invoke backup`，会 dump 整个数据库——含条码/二维码绑定表 `barcode_barcode`
——以及 media 附件），然后把容器内备份目录里本次新生成的文件拷贝到本容器对外暴露
的目录（默认 `/backup`，已在 docker-compose 里 bind-mount 到宿主机）。

设计取舍：
- 不直接连接数据库、不需要 DB 凭据，只依赖「InvenTree 也是宿主机 Docker 部署」这一条件；
- 通过 docker SDK 走 unix socket，避免在镜像里额外装 docker CLI。
"""

from __future__ import annotations

import io
import logging
import shutil
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from lcsc2inv.config import Settings

logger = logging.getLogger("lcsc2inv.backup")

# 测试时替换该工厂即可注入假的 docker client；None 时用真实 docker SDK。
_docker_factory: Callable[[], object] | None = None

# 用于区分「本次备份新生成」的文件，容忍几秒时钟偏移
_MTIME_SKEW_SECONDS = 5


def _get_docker_client() -> object:
    if _docker_factory is not None:
        return _docker_factory()
    import docker  # 延迟导入：仅用备份功能时才需要 docker 依赖

    return docker.from_env()


def set_docker_factory(factory: Callable[[], object] | None) -> None:
    """注入 docker client 工厂（供测试与外部宿主复用）。"""
    global _docker_factory
    _docker_factory = factory


def storage_path(settings: Settings) -> Path:
    """本容器对外暴露的备份目录（自动创建）。"""
    p = Path(settings.inventree_backup_storage).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sanitize_member(name: str) -> Path | None:
    """把 tar 成员名限制在目录内，防路径穿越。"""
    rel = name.lstrip("/")
    parts = Path(rel).parts
    if not parts or ".." in parts:
        return None
    return Path(*parts)


def _extract_new(raw: bytes, dest: Path, min_mtime: float) -> list[str]:
    """从 docker get_archive 的 tar 中抽出本次新生成的文件，返回相对路径。"""
    copied: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            if member.mtime < min_mtime:
                continue
            rel = _sanitize_member(member.name)
            if rel is None:
                logger.warning("跳过越界备份成员: %s", member.name)
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            copied.append(rel.as_posix())
    return copied


def _run_backup_cmd(
    container: object,
    cmd: list[str],
    client: object,
    log_sink: Callable[[str], None] | None,
) -> tuple[int, str]:
    """流式执行容器内备份命令，实时把输出段交给 `log_sink`。

    用 docker 底层 API（exec_create / exec_start / exec_inspect）代替高层
    `exec_run`：`exec_run(stream=True)` 会立刻 inspect 退出码（此时常为 None），
    而这里在消费完输出生成器后再 inspect，能拿到真实退出码，同时逐段回传日志。

    Returns:
        (exit_code, 完整输出文本)。
    """
    api = client.api
    exec_id = api.exec_create(
        container.id, cmd, stdout=True, stderr=True, tty=True
    )["Id"]
    chunks: list[str] = []

    def emit(text: str) -> None:
        chunks.append(text)
        if log_sink is not None:
            try:
                log_sink(text)
            except Exception:  # noqa: BLE001 — 日志回传失败不应中断备份
                logger.debug("log_sink 回传失败", exc_info=True)

    try:
        output = api.exec_start(exec_id, detach=False, tty=True, stream=True)
        for raw in output:
            emit(raw.decode("utf-8", "replace"))
    finally:
        info = api.exec_inspect(exec_id)

    exit_code = (info or {}).get("ExitCode") or 0
    return exit_code, "".join(chunks)


def run_inventree_backup(
    settings: Settings, log_sink: Callable[[str], None] | None = None
) -> dict:
    """执行一次 InvenTree 备份。

    `log_sink(chunk)`：备份命令的每一段输出与关键步骤都会实时回调（Web 用它把
    日志打到网页上）。

    Returns:
        dict: {"snapshot": 目录名, "files": [相对路径], "exit_code": int,
               "output_tail": str}

    Raises:
        ValueError: 未配置容器名时。
        RuntimeError: docker 不可用、容器不存在或备份命令失败时。
    """
    container_name = settings.inventree_backup_container
    if not container_name:
        raise ValueError(
            "INVENTREE_BACKUP_CONTAINER 未设置（请在 .env 填 InvenTree 容器名）"
        )

    cmd = settings.inventree_backup_cmd.split()
    src_dir = settings.inventree_backup_src_dir
    storage = storage_path(settings)
    job_started = time.time()
    snapshot_dir = storage / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    if log_sink is not None:
        log_sink(f"▶ 执行备份命令：{' '.join(cmd)}")

    try:
        client = _get_docker_client()
    except Exception as exc:  # noqa: BLE001 — 汇总为清晰报错
        raise RuntimeError(f"连接 docker daemon 失败（请确认已挂载 docker.sock）: {exc}") from exc

    try:
        container = client.containers.get(container_name)
    except Exception as exc:  # noqa: BLE001 — 找不到容器等
        raise RuntimeError(f"找不到 InvenTree 容器 {container_name!r}: {exc}") from exc

    # 1) 容器内流式执行备份命令（输出实时回传 log_sink）
    logger.info("在容器 %s 内执行备份: %s", container_name, " ".join(cmd))
    exit_code, output_text = _run_backup_cmd(container, cmd, client, log_sink)

    if exit_code != 0:
        raise RuntimeError(
            f"容器内备份命令退出码 {exit_code}:\n{output_text[-2000:]}"
        )

    # 2) 拉取容器内备份目录，只保留本次新生成的文件
    if log_sink is not None:
        log_sink("▶ 备份命令成功，正在拉取容器内产物…")
    archive, _stat = container.get_archive(src_dir)
    raw = b"".join(archive)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    copied = _extract_new(raw, snapshot_dir, job_started - _MTIME_SKEW_SECONDS)

    if not copied:
        raise RuntimeError(
            f"在容器 {src_dir} 内未找到本次生成的备份文件。"
            f"备份命令输出:\n{output_text[-1000:]}"
        )

    if log_sink is not None:
        log_sink(f"▶ 完成：快照 {snapshot_dir.name}，共 {len(copied)} 个文件")

    return {
        "snapshot": snapshot_dir.name,
        "files": sorted(copied),
        "exit_code": exit_code,
        "output_tail": output_text[-2000:],
        "full_output": output_text,  # 完整日志
    }


def list_backups(settings: Settings) -> list[dict]:
    """列出已保存的备份快照（含每个文件的名字/大小/时间）。

    `get_archive` 拉容器内目录时 tar 成员带目录名前缀（如 `backup/xxx.gz`），
    解包后文件嵌套在子目录里，因此这里递归列出，`name` 返回相对快照目录的
    posix 路径（下载路由用 `<path:filename>` 可匹配带斜杠的路径）。
    """
    storage = storage_path(settings)
    snapshots: list[dict] = []
    for d in sorted(storage.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        files = [
            {
                "name": p.relative_to(d).as_posix(),
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
            }
            for p in sorted(d.rglob("*"))
            if p.is_file()
        ]
        if files:
            snapshots.append({"snapshot": d.name, "files": files})
    return snapshots
