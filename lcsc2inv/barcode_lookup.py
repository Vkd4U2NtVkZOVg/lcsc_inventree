"""读取 InvenTree 对象上绑定的第三方条码（barcode_data 字段）。

新版 InvenTree 把自定义绑定码直接存在模型字段上（如 `StockItem.barcode_data`），
但 REST API 序列化器只暴露 `barcode_hash`、不暴露 `barcode_data`，因此无法通过
API 读取原文。本工具的备份功能已经依赖 docker.sock 进入 InvenTree 容器，这里
复用同一机制：用只读的 `manage.py shell` 查询字段值，不写入任何数据。

容器内 manage.py 的位置因镜像版本而异（老版 `/home/inventree/InvenTree`，新版
`/home/inventree/src/backend/InvenTree`），所以用 `find` 动态定位。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from lcsc2inv.config import Settings

logger = logging.getLogger("lcsc2inv.barcode_lookup")

# 输出标记：manage.py shell 可能先打印无关提示，只认标记之后的 JSON
_MARKER = "===BARCODE_JSON==="

# 允许查询的模型 → (导入行, 管理器名)；白名单防止拼接任意模型名
_MODELS = {
    "stockitem": ("from stock.models import StockItem", "StockItem"),
}

# ---------------------------------------------------------------------------
# 本地缓存（bound_barcodes.json，存于缓存目录）
# ---------------------------------------------------------------------------

_CACHE_FILENAME = "bound_barcodes.json"
_cache_lock = threading.Lock()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cache_path(settings: Settings) -> Path:
    return settings.cache_dir_path / _CACHE_FILENAME


def load_cache(settings: Settings) -> dict[int, str]:
    """读取本地缓存 {pk: barcode_data}；文件缺失/损坏时返回空 dict。

    save_cache 用临时文件 + os.replace 原子替换，因此并发读不会看到半个文件。
    """
    p = cache_path(settings)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    out: dict[int, str] = {}
    for key, value in entries.items():
        try:
            out[int(key)] = str(value or "")
        except (TypeError, ValueError):
            continue
    return out


def save_cache(
    settings: Settings, cache: dict[int, str], updated_at: str | None = None
) -> None:
    """原子写盘保存缓存（调用方需持有 _cache_lock 或保证单线程写）。"""
    p = cache_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": updated_at or _utcnow_iso(),
        "entries": {str(k): v for k, v in sorted(cache.items())},
    }
    tmp = p.with_name(_CACHE_FILENAME + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def replace_cache(
    settings: Settings, cache: dict[int, str], updated_at: str | None = None
) -> None:
    """整体替换缓存内容（从备份恢复用），保留备份里的更新时间。"""
    with _cache_lock:
        save_cache(settings, dict(cache), updated_at=updated_at)


def cache_put(settings: Settings, pk: int, code: str) -> None:
    """写入/覆盖单个缓存条目（懒加载回填用）。"""
    with _cache_lock:
        cache = load_cache(settings)
        cache[int(pk)] = code
        save_cache(settings, cache)


def cache_info(settings: Settings) -> dict:
    """缓存概况（条数 + 最近更新时间）。"""
    cache = load_cache(settings)
    updated_at = None
    p = cache_path(settings)
    if p.exists():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            updated_at = json.loads(p.read_text(encoding="utf-8")).get("updated_at")
    return {"count": len(cache), "updated_at": updated_at}


# ---------------------------------------------------------------------------
# 容器内批量读取 + 缓存同步
# ---------------------------------------------------------------------------


def _exec_json_query(settings: Settings, code: str) -> list[dict]:
    """进 InvenTree 容器执行只读查询，解析标记后的 JSON 数组。"""
    container_name = settings.inventree_backup_container
    if not container_name:
        raise ValueError("INVENTREE_BACKUP_CONTAINER 未设置，无法读取绑定条码")

    cmd = [
        "sh", "-c",
        'cd "$(dirname "$(find /home/inventree -maxdepth 4 -name manage.py '
        '2>/dev/null | head -1)")" && python manage.py shell -c ' + shlex.quote(code),
    ]

    from lcsc2inv import backup  # 复用 docker client 工厂（测试可注入）

    client = backup._get_docker_client()
    try:
        container = client.containers.get(container_name)
    except Exception as exc:  # noqa: BLE001 — 统一转为 RuntimeError
        raise RuntimeError(f"找不到 InvenTree 容器 {container_name!r}: {exc}") from exc

    exit_code, output = container.exec_run(cmd)
    output_text = (
        output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output)
    )
    if exit_code != 0:
        raise RuntimeError(
            f"容器内查询条码失败（退出码 {exit_code}）:\n{output_text[-500:]}"
        )
    if _MARKER not in output_text:
        raise RuntimeError(f"容器内查询条码输出异常:\n{output_text[-500:]}")
    try:
        rows = json.loads(output_text.split(_MARKER, 1)[1].strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"容器内查询条码返回非 JSON:\n{output_text[-500:]}") from exc
    if not isinstance(rows, list):
        raise RuntimeError("容器内查询条码返回结构异常")
    return rows


def get_bound_barcodes(
    settings: Settings, model_type: str, pks: list[int]
) -> dict[int, str]:
    """批量查询对象绑定的自定义条码。

    Args:
        settings: 运行配置（需要 INVENTREE_BACKUP_CONTAINER 指向 InvenTree 容器）。
        model_type: 目前仅支持 `stockitem`。
        pks: 要查询的对象主键列表。

    Returns:
        {pk: barcode_data}；未绑定自定义条码的对象值为空串。

    Raises:
        ValueError: 未配置容器名或 model_type 不支持。
        RuntimeError: docker 不可用、容器不存在或容器内查询失败。
    """
    if model_type not in _MODELS:
        raise ValueError(f"不支持的 model_type: {model_type!r}")
    if not pks:
        return {}

    import_line, model_name = _MODELS[model_type]
    pk_list = ", ".join(str(int(p)) for p in pks)
    code = (
        "import json\n"
        f"{import_line}\n"
        f"rows = {model_name}.objects.filter(pk__in=[{pk_list}])"
        ".values('pk', 'barcode_data')\n"
        f"print({_MARKER!r})\n"
        "print(json.dumps(list(rows)))\n"
    )

    result: dict[int, str] = {}
    for row in _exec_json_query(settings, code):
        try:
            result[int(row["pk"])] = str(row.get("barcode_data") or "")
        except (KeyError, TypeError, ValueError):
            continue
    return result


def get_all_bound_barcodes(settings: Settings) -> dict[int, str]:
    """一次性读取 InvenTree 中所有非空的自定义绑定条码（单次 exec）。"""
    import_line, model_name = _MODELS["stockitem"]
    code = (
        "import json\n"
        f"{import_line}\n"
        f"rows = {model_name}.objects.exclude(barcode_data='')"
        ".values('pk', 'barcode_data')\n"
        f"print({_MARKER!r})\n"
        "print(json.dumps(list(rows)))\n"
    )
    result: dict[int, str] = {}
    for row in _exec_json_query(settings, code):
        try:
            result[int(row["pk"])] = str(row.get("barcode_data") or "")
        except (KeyError, TypeError, ValueError):
            continue
    return result


def sync_cache(settings: Settings) -> dict:
    """全量镜像同步：以 InvenTree 为准，把全部绑定条码覆盖到本地缓存。

    策略（全量覆盖）：远端有而缓存无 → 新增；两端值不同 → 以远端覆盖；
    缓存有而远端无（已在 InvenTree 解绑）→ 删除。同步完成后缓存与
    InvenTree 完全一致。

    Returns:
        报告 dict：added/updated/removed（明细）、added_count、updated_count、
        removed_count、unchanged_count、remote_count、total_cached、duration_ms。
    """
    started = time.time()
    remote = get_all_bound_barcodes(settings)
    with _cache_lock:
        cache = load_cache(settings)
        added = {pk: c for pk, c in remote.items() if pk not in cache}
        updated = [
            {"pk": pk, "cached": cache[pk], "remote": c}
            for pk, c in remote.items()
            if pk in cache and cache[pk] != c
        ]
        removed = sorted(pk for pk in cache if pk not in remote)
        unchanged = len(remote) - len(added) - len(updated)
        save_cache(settings, dict(remote))  # 镜像：缓存 = 远端
    return {
        "added": [{"pk": k, "code": v} for k, v in sorted(added.items())],
        "updated": updated,
        "removed": removed,
        "added_count": len(added),
        "updated_count": len(updated),
        "removed_count": len(removed),
        "unchanged_count": unchanged,
        "remote_count": len(remote),
        "total_cached": len(remote),
        "duration_ms": int((time.time() - started) * 1000),
    }
