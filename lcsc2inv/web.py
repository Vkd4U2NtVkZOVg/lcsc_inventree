"""Flask Web 界面：通过 HTTP 复用 `lcsc2inventree` 的全部核心逻辑。

功能：
- `GET  /`                    渲染单页 HTML
- `POST /api/import`          单条导入（JSON 体，字段同 CLI 的 import 选项）
- `POST /api/batch`           CSV 批量导入（multipart 上传文件）
- `POST /api/preview`         无写入预览（serialized LCSCPart + 可选 category/mapping 数据）
- `POST /api/batch/jobs`      后台批量任务（JSON rows 或 CSV，异步返回 job_id）
- `GET  /api/batch/jobs/<id>` 轮询任务状态（completed/failed，含逐条结果与重试数据）
- `GET  /api/history`         查看最近导入历史（持久化到缓存目录 history.json）
- `POST /api/history/clear`   清空历史
- `GET  /api/doctor`          InvenTree 连通性与配置检查
- `GET  /api/cache`           列出已缓存商品
- `POST /api/cache/clear`     清理缓存
- `GET  /api/quick/locations` 货位列表（快速入库下拉框）
- `POST /api/quick/lookup`    智能匹配 Part + 查询当前库存
- `POST /api/quick/stock`     单条快速入库（匹配/创建 Part + 入库）
- `POST /api/quick/batch`     批量快速入库（队列提交）

并发说明：InvenTree SDK 非线程安全，且 LCSC 有 1 req/s 限速，因此所有写操作
通过一个全局锁串行执行；后台批量任务由一个 worker 线程排队逐个执行。部署时
建议用 gunicorn 单 worker。
"""

from __future__ import annotations

import contextlib
import csv
import io
import itertools
import json
import logging
import os
import secrets
import shutil
import threading
from collections import OrderedDict

import requests
import time
from datetime import datetime, timezone
from pathlib import Path

from urllib.parse import quote

from flask import Flask, jsonify, render_template, request, send_file
from pydantic import ValidationError

from lcsc2inv import barcode_lookup
from lcsc2inv.backup import list_backups, run_inventree_backup, storage_path
from lcsc2inv.order_import import parse_order_xls
from lcsc2inv.categorizer import match as categorizer_match
from lcsc2inv.client import build_inventree_api
from lcsc2inv.config import Settings, get_settings, load_yaml
from lcsc2inv.inventree_writer import InvenTreeWriter, WriteOptions, WriteResult
from lcsc2inv.lcsc_client import (
    Fetcher,
    LcscFetchError,
    default_fetcher,
    parse_lcsc_code,
)
from lcsc2inv.lcsc_models import LCSCPart
from lcsc2inv.mapping import to_inventree_parameters, to_part_notes
from lcsc2inv.quick_stock import (
    add_or_merge_stock,
    find_part,
    get_stock_at_location,
    list_locations,
    search_parts,
)
from lcsc2inv.taobao_import import TaobaoItem, parse_mhtml

logger = logging.getLogger("lcsc2inv.web")

app = Flask(__name__)

# 全局串行锁：避免 InvenTree SDK / LCSC 限速在多线程下互相踩踏
_write_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 最近历史（持久化到缓存目录 history.json；写入用独立锁保证线程安全）
# ---------------------------------------------------------------------------

HISTORY_FILENAME = "history.json"
HISTORY_MAX_ENTRIES = 20
_history_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 后台批量任务注册表 + 队列 worker
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_jobs_cond = threading.Condition(_jobs_lock)

# ---------------------------------------------------------------------------
# InvenTree 数据库一键备份（后台线程，串行；结果与批量任务分开跟踪）
# ---------------------------------------------------------------------------

_backup_lock = threading.Lock()
_backup_thread: threading.Thread | None = None
_backup_job: dict = {
    "status": "idle",        # idle | running | completed | failed
    "started_at": None,
    "finished_at": None,
    "message": "",
    "error": "",
    "result": None,
    "log": [],               # 备份命令的实时输出行（前端增量渲染）
}


def _backup_log_append(line: str) -> None:
    """把一行备份日志追加进 _backup_job['log']（备份 worker 线程调用）。"""
    with _backup_lock:
        _backup_job["log"].append(line)


def _backup_status_snapshot() -> dict:
    with _backup_lock:
        return dict(_backup_job)


def _run_backup_worker() -> None:
    """在后台线程执行一次 InvenTree 备份并更新 _backup_job。

    「running」状态已在 api_backup_start 里同步置位，这里只负责收尾。
    """
    global _backup_thread
    s = _get_settings()
    try:
        result = run_inventree_backup(s, log_sink=_backup_log_append)
        n = len(result["files"])
        with _backup_lock:
            _backup_job.update(
                {
                    "status": "completed",
                    "finished_at": _utcnow_iso(),
                    "message": f"备份完成：{result['snapshot']}，共 {n} 个文件",
                    "error": "",
                    "result": result,
                }
            )
    except Exception as exc:  # noqa: BLE001 — 任务级兜底，统一写进 error
        logger.exception("InvenTree 备份失败")
        with _backup_lock:
            _backup_job.update(
                {
                    "status": "failed",
                    "finished_at": _utcnow_iso(),
                    "message": "",
                    "error": f"{type(exc).__name__}: {exc}",
                    "result": None,
                }
            )
            _backup_job["log"].append(f"❌ {type(exc).__name__}: {exc}")
    finally:
        with _backup_lock:
            _backup_thread = None
_job_worker: threading.Thread | None = None

def _get_settings() -> Settings:
    return get_settings()


def _get_fetcher() -> Fetcher:
    return default_fetcher(_get_settings())


def _write_result_to_dict(result: WriteResult) -> dict:
    s = get_settings()
    base = (s.inventree_url or "").rstrip("/")
    part_url = f"{base}/part/{result.part_pk}/" if (base and result.part_pk) else None
    return {
        "part_pk": result.part_pk,
        "part_url": part_url,
        "manufacturer_pk": result.manufacturer_pk,
        "supplier_pk": result.supplier_pk,
        "manufacturer_part_pk": result.manufacturer_part_pk,
        "supplier_part_pk": result.supplier_part_pk,
        "stock_item_pk": result.stock_item_pk,
        "image_uploaded": result.image_uploaded,
        "image_url": result.image_url,
        "image_skipped_reason": result.image_skipped_reason,
        "created": result.created,
        "errors": result.errors,
        "ok": result.ok(),
        "summary": result.summary(),
    }


# ---------------------------------------------------------------------------
# 最近历史：持久化 JSON 到缓存目录
# ---------------------------------------------------------------------------


def _history_path() -> Path:
    return _get_settings().cache_dir_path / HISTORY_FILENAME


def _history_load() -> list[dict]:
    p = _history_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _history_save(entries: list[dict]) -> None:
    """临时文件替换写入，避免并发/重启时产生半个 JSON。"""
    p = _history_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{HISTORY_FILENAME}.tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _history_record(entry: dict) -> None:
    """追加一条历史记录；只保留最近的 HISTORY_MAX_ENTRIES 条。"""
    entry.setdefault("ts", time.time())
    with _history_lock:
        entries = _history_load()
        entries.append(entry)
        if len(entries) > HISTORY_MAX_ENTRIES:
            del entries[: len(entries) - HISTORY_MAX_ENTRIES]
        try:
            _history_save(entries)
        except OSError as exc:
            logger.warning("写入历史失败: %s", exc)


def _record_history(
    *,
    code: str | None,
    part_pk: int | None,
    ok: bool,
    summary: str,
    error: str | None,
    part_url: str | None,
    entry_type: str = "single",
) -> None:
    """记录一条导入历史（单条/批量/后台任务共用）。"""
    _history_record(
        {
            "type": entry_type,
            "code": code,
            "part_pk": part_pk,
            "ok": ok,
            "summary": summary,
            "error": error,
            "part_url": part_url,
            "result": {
                "part_pk": part_pk,
                "part_url": part_url,
                "ok": ok,
                "summary": summary,
            },
        }
    )


def _part_url(part_pk: int | None) -> str | None:
    s = _get_settings()
    base = (s.inventree_url or "").rstrip("/")
    return f"{base}/part/{part_pk}/" if (base and part_pk) else None


def _stock_url(stock_pk: int | None) -> str | None:
    s = _get_settings()
    base = (s.inventree_url or "").rstrip("/")
    return f"{base}/stock/item/{stock_pk}/" if (base and stock_pk) else None


# ---------------------------------------------------------------------------
# 后台批量任务
# ---------------------------------------------------------------------------

_JOBS_MAX = 50  # 进程内保留的任务数量上限，超出后丢弃最早的已完成任务


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_job_id() -> str:
    return secrets.token_hex(6)


def _ensure_job_worker() -> None:
    """惰性启动单个后台 worker，串行处理排队任务。"""
    global _job_worker
    with _jobs_lock:
        if _job_worker is None or not _job_worker.is_alive():
            _job_worker = threading.Thread(
                target=_job_worker_loop, daemon=True, name="lcsc2inv-batch-worker"
            )
            _job_worker.start()


def _job_worker_loop() -> None:
    while True:
        with _jobs_cond:
            while not any(j["status"] == "queued" for j in _jobs.values()):
                _jobs_cond.wait()
            job = next(j for j in _jobs.values() if j["status"] == "queued")
            job["status"] = "running"
            job["started_at"] = _utcnow_iso()
        try:
            _run_job(job)
        except Exception as exc:  # noqa: BLE001 — 任务级兜底，写入 error 便于重试
            logger.exception("batch job %s 失败", job["job_id"])
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            job["finished_at"] = _utcnow_iso()
            with _jobs_lock:
                # 限制进程内任务数量，只丢弃最早的已结束任务
                finished = [j for j in _jobs.values()
                            if j["status"] in ("completed", "failed")]
                if len(_jobs) > _JOBS_MAX and finished:
                    _jobs.pop(min(finished, key=lambda j: j["finished_at"])["job_id"], None)
            with _jobs_cond:
                _jobs_cond.notify_all()
            _record_history(
                code=None,
                part_pk=None,
                ok=job["status"] == "completed"
                and job.get("fail_count", 0) == 0,
                summary=f"job {job['job_id']} {job['status']}: "
                        f"{job.get('ok_count')}/{job.get('total')} ok",
                error=job.get("error"),
                part_url=None,
                entry_type="batch",
            )


def _run_job(job: dict) -> None:
    """在 `_write_lock` 下执行批量任务并填充结果（失败写入 error，可重试）。"""
    inp = job["input"]
    rows = inp["rows"]
    fetcher = _get_fetcher()
    with _write_lock:
        try:
            api = build_inventree_api()
        except ValueError as exc:
            job["error"] = str(exc)
            job["status"] = "failed"
            return
        writer = InvenTreeWriter(api, _get_settings())
        results: list[dict] = []
        update = bool(inp.get("update", False))
        stock = bool(inp.get("stock", False))
        create_missing = bool(inp.get("create_missing_category", False))
        for row in rows:
            code = row["lcsc_code"]
            job["current_code"] = code
            qty = int(row["quantity"]) if row.get("quantity") else None
            note = row.get("note") or None
            try:
                part = fetcher.fetch(code)
            except LcscFetchError as exc:
                results.append({"code": code, "ok": False, "error": f"抓取失败: {exc}"})
                continue
            opts = WriteOptions(
                update_existing=update,
                create_stock=stock,
                quantity=qty,
                extra_note=note,
                fetcher=fetcher,
                force_image_upload=update,
            )
            result = writer.upsert_part(
                part, options=opts, create_missing_category=create_missing
            )
            d = _write_result_to_dict(result)
            d["code"] = code
            results.append(d)
        job["results"] = results
        job["ok_count"] = sum(1 for r in results if r.get("ok"))
        job["fail_count"] = sum(1 for r in results if not r.get("ok"))
        job["total"] = len(results)
        job["status"] = "completed"


def _job_to_payload(job: dict) -> dict:
    """返回任务的轮询视图；`input` 保留原始提交数据以便失败后原样重试。"""
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "type": job["type"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "total": job["total"],
        "ok_count": job.get("ok_count"),
        "fail_count": job.get("fail_count"),
        "current_code": job.get("current_code"),
        "results": job.get("results", []),
        "error": job.get("error"),
        "input": job.get("input"),
    }


@app.route("/")
def index() -> str:
    """渲染单页界面。"""
    return render_template("index.html")


@app.route("/api/import", methods=["POST"])
def api_import():
    """单条 LCSC 代码/URL 导入。

    请求体（JSON）：
        code: str           必填，C-code 或商品 URL
        update: bool        是否更新已存在 Part
        stock: bool         是否创建 StockItem
        qty: int|None       建 StockItem 数量
        note: str|None      附加备注
        create_missing_category: bool   是否自动创建缺失分类
    """
    data = request.get_json(silent=True) or {}
    code_or_url = (data.get("code") or "").strip()
    if not code_or_url:
        return jsonify({"ok": False, "error": "缺少 code 参数"}), 400

    update = bool(data.get("update", False))
    stock = bool(data.get("stock", False))
    qty = data.get("qty")
    note = data.get("note") or None
    create_missing = bool(data.get("create_missing_category", False))

    try:
        code = parse_lcsc_code(code_or_url)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    fetcher = _get_fetcher()
    try:
        part = fetcher.fetch(code_or_url)
    except LcscFetchError as exc:
        err = f"抓取 LCSC 失败: {exc}"
        _record_history(code=code, part_pk=None, ok=False, summary=err,
                        error=err, part_url=None)
        return jsonify({"ok": False, "error": err}), 502

    with _write_lock:
        try:
            api = build_inventree_api()
        except ValueError as exc:
            err = str(exc)
            _record_history(code=code, part_pk=None, ok=False, summary=err,
                            error=err, part_url=None)
            return jsonify({"ok": False, "error": err}), 500

        writer = InvenTreeWriter(api, _get_settings())
        opts = WriteOptions(
            update_existing=update,
            create_stock=stock,
            quantity=qty,
            extra_note=note,
            fetcher=fetcher,
            force_image_upload=update,
        )
        result = writer.upsert_part(
            part, options=opts, create_missing_category=create_missing
        )

    result_dict = _write_result_to_dict(result)
    stock_barcode = (data.get("stock_barcode") or "").strip()
    if stock and stock_barcode and result.stock_item_pk:
        try:
            status_code, barcode_result = _inventree_request(
                "POST",
                "/api/barcode/link/",
                {"barcode": stock_barcode, "stockitem": result.stock_item_pk},
            )
        except requests.RequestException as exc:
            status_code, barcode_result = 502, str(exc)
        result_dict["stock_barcode"] = stock_barcode
        result_dict["stock_barcode_assigned"] = status_code < 400
        if status_code >= 400:
            result_dict["stock_barcode_error"] = barcode_result
    _record_history(
        code=code,
        part_pk=result.part_pk,
        ok=result.ok(),
        summary=result.summary(),
        error=None if result.ok() else "; ".join(result.errors),
        part_url=result_dict["part_url"],
    )
    if not result.ok():
        return jsonify(result_dict), 502
    return jsonify(result_dict)


def _form_bool(value, default: bool = False) -> bool:
    """multipart 表单里的布尔字段（"true"/"1"/"on"）。"""
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "on", "yes", "是")


def _part_update_from_taobao():
    """multipart 分支：用淘宝 .mhtml 数据更新已存在 Part（/api/part/update）。"""
    f = request.files.get("file")
    try:
        part_pk = int(request.form.get("part_pk", ""))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "缺少或非法 part_pk"}), 400
    try:
        item = parse_mhtml(f.read(), source_filename=f.filename)
    except ValueError as exc:
        return jsonify({"ok": False, "error": f"解析 mhtml 失败: {exc}"}), 400

    image_url = item.main_image_url()
    image_data = item.embedded_bytes(image_url) if image_url else None
    params = {n: v for n, v in item.params}
    flags = dict(
        update_name=_form_bool(request.form.get("name")),
        update_description=_form_bool(request.form.get("description"), True),
        update_image=_form_bool(request.form.get("image"), True),
        update_keywords=_form_bool(request.form.get("keywords"), True),
        update_notes=_form_bool(request.form.get("notes")),
        update_parameters=_form_bool(request.form.get("parameters")),
    )

    with _write_lock:
        try:
            api = build_inventree_api()
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        writer = InvenTreeWriter(api, _get_settings())
        try:
            result = writer.update_custom_part_fields(
                part_pk=part_pk,
                name=item.default_name(),
                description=item.default_description(),
                notes=_taobao_notes(item, item.price, item.selected_sku),
                keywords=",".join(
                    k for k in ("淘宝", item.shop or "", item.item_id or "") if k
                ),
                link=item.url,
                parameters=params,
                image_data=image_data,
                **flags,
            )
        except ValueError as exc:
            err = str(exc)
            _record_history(code=item.default_ipn(), part_pk=part_pk, ok=False,
                            summary=err, error=err, part_url=None,
                            entry_type="taobao_update")
            return jsonify({"ok": False, "error": err}), 404

    ok = not result.get("errors")
    summary = (
        f"淘宝更新 {part_pk}: {','.join(result['updated_fields']) or '无字段'}"
        f"{' +图片' if result.get('image') else ''}"
        f"{' +参数' if result.get('parameters') else ''}"
    )
    _record_history(
        code=item.default_ipn(),
        part_pk=part_pk,
        ok=ok,
        summary=summary,
        error="; ".join(result["errors"]) or None,
        part_url=_part_url(part_pk),
        entry_type="taobao_update",
    )
    resp = {"ok": ok, "part_pk": part_pk, "code": item.default_ipn(),
            "part_url": _part_url(part_pk), **result}
    return jsonify(resp), (200 if ok else 502)


@app.route("/api/part/update", methods=["POST"])
def api_part_update():
    """用 LCSC 最新数据更新**已存在**的 Part（不新建）。

    支持两种数据源：
    - JSON 体 + code：LCSC 数据（原有流程）
    - multipart + file(.mhtml)：淘宝页面数据（本地解析，图片取内嵌字节）

    请求体（JSON）：
        part_pk: int        必填，InvenTree Part 主键
        code: str           必填，LCSC C-code 或商品 URL
        name: bool          更新名称（默认 false，避免覆盖自定义命名）
        description: bool   更新描述（默认 true）
        image: bool         强制重传图片（默认 true）
        keywords: bool      更新关键词（默认 true）
        notes: bool         更新备注+LCSC 链接（默认 false）
        parameters: bool    写入分类映射参数（默认 false）

    multipart 表单字段（淘宝源）：
        part_pk: 数字       必填
        file: .mhtml        必填
        name/description/image/keywords/notes/parameters: "true"/"false"

    不动的字段：分类、厂商/供应商、价格、库存。
    """
    if request.files.get("file"):
        return _part_update_from_taobao()

    data = request.get_json(silent=True) or {}
    try:
        part_pk = int(data["part_pk"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "缺少或非法 part_pk"}), 400
    code_or_url = (data.get("code") or "").strip()
    if not code_or_url:
        return jsonify({"ok": False, "error": "缺少 code（LCSC 代码或 URL）"}), 400
    try:
        code = parse_lcsc_code(code_or_url)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    fetcher = _get_fetcher()
    try:
        part = fetcher.fetch(code_or_url)
    except LcscFetchError as exc:
        err = f"抓取 LCSC 失败: {exc}"
        _record_history(code=code, part_pk=part_pk, ok=False, summary=err,
                        error=err, part_url=None, entry_type="update")
        return jsonify({"ok": False, "error": err}), 502

    with _write_lock:
        try:
            api = build_inventree_api()
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        writer = InvenTreeWriter(api, _get_settings())
        try:
            result = writer.update_part_fields(
                part,
                part_pk=part_pk,
                update_name=bool(data.get("name", False)),
                update_description=bool(data.get("description", True)),
                update_image=bool(data.get("image", True)),
                update_keywords=bool(data.get("keywords", True)),
                update_notes=bool(data.get("notes", False)),
                update_parameters=bool(data.get("parameters", False)),
                fetcher=fetcher,
            )
        except ValueError as exc:
            err = str(exc)
            _record_history(code=code, part_pk=part_pk, ok=False, summary=err,
                            error=err, part_url=None, entry_type="update")
            return jsonify({"ok": False, "error": err}), 404

    ok = not result.get("errors")
    summary = (
        f"更新 {part_pk}: {','.join(result['updated_fields']) or '无字段'}"
        f"{' +图片' if result.get('image') else ''}"
        f"{' +参数' if result.get('parameters') else ''}"
    )
    _record_history(
        code=code,
        part_pk=part_pk,
        ok=ok,
        summary=summary,
        error="; ".join(result["errors"]) or None,
        part_url=_part_url(part_pk),
        entry_type="update",
    )
    resp = {"ok": ok, "part_pk": part_pk, "code": code, "part_url": _part_url(part_pk),
            **result}
    return jsonify(resp), (200 if ok else 502)


def _find_part_by_mpn(api, mpn: str) -> int | None:
    """按 MPN 精确匹配 ManufacturerPart → part pk；找不到返回 None。"""
    try:
        from inventree.company import ManufacturerPart

        for mp in ManufacturerPart.list(api, search=mpn):
            if getattr(mp, "MPN", None) == mpn:
                return getattr(mp, "part", None)
    except Exception:  # noqa: BLE001 — 匹配失败视为未找到
        return None
    return None


@app.route("/api/order/parse", methods=["POST"])
def api_order_parse():
    """解析立创订单 .xls，并逐行匹配 InvenTree Part。

    匹配顺序：IPN == 商品编号(C-code) → ManufacturerPart.MPN → 未找到。
    每行附现有库存数量（in_stock），便于决定是否入库。
    """
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"ok": False, "error": "缺少 file 字段（.xls 订单文件）"}), 400
    try:
        parsed = parse_order_xls(f.read())
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    with _write_lock:
        try:
            api = build_inventree_api()
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        writer = InvenTreeWriter(api, _get_settings())
        rows = []
        for row in parsed["rows"]:
            match_status, part_pk = "none", None
            pk = writer._find_part_by_ipn(row["lcsc_code"])
            if pk:
                match_status, part_pk = "ipn", pk
            elif row["mpn"]:
                pk = _find_part_by_mpn(api, row["mpn"])
                if pk:
                    match_status, part_pk = "mpn", pk
            part_name, stock_qty, part_url = None, None, None
            if part_pk:
                part_url = _part_url(part_pk)
                try:
                    from inventree.part import Part

                    p = Part(api, part_pk)
                    name = getattr(p, "name", None)
                    part_name = str(name) if name else None
                    sq = getattr(p, "in_stock", None)
                    stock_qty = float(sq) if isinstance(sq, (int, float)) else None
                except Exception:  # noqa: BLE001 — 详情获取失败不影响匹配结果
                    pass
            rows.append(
                {**row, "match": match_status, "part_pk": part_pk,
                 "part_name": part_name, "stock_qty": stock_qty,
                 "part_url": part_url}
            )

    counts = {k: sum(1 for r in rows if r["match"] == k) for k in ("ipn", "mpn", "none")}
    return jsonify(
        {"ok": True, "order_no": parsed["order_no"], "total": len(rows),
         "counts": counts, "rows": rows}
    )


@app.route("/api/order/import", methods=["POST"])
def api_order_import():
    """执行订单导入：逐行「更新已有 Part / 新建 Part」+ 按订单数量入库。

    请求体（JSON）：
        rows: [{lcsc_code, quantity, part_pk?, action: "update"|"create"|"skip"}]
        options: {
          name/description/image/keywords/notes/parameters: bool  # 更新字段开关
          stock: bool                    # 入库（默认 true）
          location_pk: int|null          # 入库默认货位（可空）
          create_missing: bool           # 未匹配行自动新建（默认 true）
        }
        order_no: str|null               # 仅用于历史记录备注
    """
    data = request.get_json(silent=True) or {}
    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        return jsonify({"ok": False, "error": "rows 为空"}), 400

    opts_in = data.get("options") or {}
    update_fields = {
        "name": bool(opts_in.get("name", False)),
        "description": bool(opts_in.get("description", True)),
        "image": bool(opts_in.get("image", True)),
        "keywords": bool(opts_in.get("keywords", True)),
        "notes": bool(opts_in.get("notes", False)),
        "parameters": bool(opts_in.get("parameters", False)),
    }
    do_stock = bool(opts_in.get("stock", True))
    create_missing = bool(opts_in.get("create_missing", True))
    location_pk = opts_in.get("location_pk")
    if location_pk not in (None, ""):
        try:
            location_pk = int(location_pk)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "location_pk 必须为数字"}), 400
    else:
        location_pk = None
    order_no = (data.get("order_no") or "").strip() or None

    fetcher = _get_fetcher()
    results: list[dict] = []
    with _write_lock:
        try:
            api = build_inventree_api()
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        writer = InvenTreeWriter(api, _get_settings())

        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            code = (row.get("lcsc_code") or "").strip()
            action = (row.get("action") or "skip").strip()
            out: dict = {"lcsc_code": code, "action": action, "ok": False}
            try:
                if action == "skip":
                    out["detail"] = "跳过"
                    out["ok"] = True
                    results.append(out)
                    continue

                code = parse_lcsc_code(code)
                part = fetcher.fetch(code)
                # 订单 .xls 里的「封装」列：LCSC 数据缺封装时用它兜底
                row_footprint = (row.get("footprint") or "").strip() or None

                if action == "update":
                    part_pk = int(row.get("part_pk"))
                    upd = writer.update_part_fields(
                        part, part_pk=part_pk, fetcher=fetcher,
                        footprint=row_footprint, **update_fields
                    )
                    out["part_pk"] = part_pk
                    out["detail"] = upd
                    out["ok"] = not upd.get("errors")
                elif action == "create":
                    if not create_missing:
                        out["detail"] = "未匹配到 Part 且未开启自动新建"
                        results.append(out)
                        continue
                    wr = writer.upsert_part(
                        part,
                        options=WriteOptions(
                            update_existing=True,
                            fetcher=fetcher,
                            force_image_upload=update_fields["image"],
                            footprint=row_footprint,
                        ),
                    )
                    out["part_pk"] = wr.part_pk
                    out["detail"] = {"summary": wr.summary(), "errors": wr.errors}
                    out["ok"] = wr.ok()
                else:
                    out["detail"] = f"未知 action: {action}"
                    results.append(out)
                    continue

                # 入库：按订单数量新建一笔 StockItem
                quantity = row.get("quantity")
                if do_stock and out.get("ok") and out.get("part_pk") and quantity:
                    qty = int(quantity)
                    stock_note = f"立创订单 {order_no} 入库" if order_no else "立创订单入库"
                    out["stock_pk"] = writer.add_stock(
                        part_pk=out["part_pk"],
                        quantity=qty,
                        location_pk=location_pk,
                        notes=stock_note,
                    )
            except Exception as exc:  # noqa: BLE001 — 单行失败不阻断后续行
                out["detail"] = f"{type(exc).__name__}: {exc}"
            results.append(out)

    ok_count = sum(1 for r in results if r.get("ok"))
    summary = f"订单 {order_no or ''}: {ok_count}/{len(results)} 行成功".strip()
    _record_history(
        code=None, part_pk=None, ok=ok_count == len(results) and bool(results),
        summary=summary, error=None, part_url=None, entry_type="order",
    )
    return jsonify(
        {"ok": bool(results) and ok_count == len(results), "total": len(results),
         "ok_count": ok_count, "results": results}
    )


# ---------------------------------------------------------------------------
# 淘宝 mhtml 导入
# ---------------------------------------------------------------------------

# 解析结果服务端缓存（token -> TaobaoItem），LRU 上限；导入时按 token 取图片字节
_taobao_cache: "OrderedDict[str, TaobaoItem]" = OrderedDict()
_taobao_cache_lock = threading.Lock()
_taobao_seq = itertools.count(1)
TAOBAO_CACHE_MAX = 12
TAOBAO_MAX_FILES = 20


def _taobao_cache_put(item: TaobaoItem) -> str:
    token = f"tb{next(_taobao_seq)}"
    with _taobao_cache_lock:
        _taobao_cache[token] = item
        while len(_taobao_cache) > TAOBAO_CACHE_MAX:
            _taobao_cache.popitem(last=False)
    return token


def _taobao_notes(item: TaobaoItem, price: float | None, sku_text: str | None) -> str:
    lines: list[str] = []
    if item.url:
        lines.append(f"- **淘宝**: [链接]({item.url})")
    if item.shop:
        lines.append(f"- **店铺**: {item.shop}")
    if price is not None:
        lines.append(f"- **价格**: ¥{price}")
    if sku_text:
        lines.append(f"- **规格**: {sku_text}")
    if item.source_filename:
        lines.append(f"- **来源文件**: {item.source_filename}")
    return "\n".join(lines)


@app.route("/api/taobao/parse", methods=["POST"])
def api_taobao_parse():
    """解析上传的淘宝商品页 .mhtml（可多选），返回预览数据与缓存令牌。

    不做任何 InvenTree 写入；图片字节保留在服务端缓存中，
    /api/taobao/import 按 token 取用（离线可用，不回源 alicdn）。
    """
    files = request.files.getlist("files")
    if not files and request.files.get("file"):
        files = [request.files["file"]]
    files = files[:TAOBAO_MAX_FILES]
    if not files:
        return jsonify({"ok": False, "error": "缺少 file(s) 字段（.mhtml 上传）"}), 400

    items = []
    for f in files:
        if not f or not f.filename:
            continue
        try:
            item = parse_mhtml(f.read(), source_filename=f.filename)
        except ValueError as exc:
            items.append({"ok": False, "filename": f.filename, "error": str(exc)})
            continue
        token = _taobao_cache_put(item)
        img_url = item.main_image_url()
        items.append({
            "ok": True,
            "token": token,
            "filename": item.source_filename,
            "url": item.url,
            "item_id": item.item_id,
            "sku_id": item.sku_id,
            "title": item.title,
            "shop": item.shop,
            "price": item.price,
            "selected_sku": item.selected_sku,
            "skus": [{"text": s.text, "image_url": s.image_url,
                      "selected": s.selected} for s in item.skus],
            "params": [[n, v] for n, v in item.params],
            "gallery": item.gallery[:12],
            "image_available": bool(
                img_url and item.embedded_bytes(img_url)
            ),
            "image_url": img_url,
            "ipn": item.default_ipn(),
            "name": item.default_name(),
            "description": item.default_description(),
        })
    return jsonify({"ok": bool(items) and any(i.get("ok") for i in items),
                    "items": items})


@app.route("/api/taobao/import", methods=["POST"])
def api_taobao_import():
    """按解析预览创建/更新器件。

    请求体（JSON）：
        category_id: int|null      可选，InvenTree 分类 ID（全部行共用）
        create_manufacturer: bool  用「品牌」参数创建厂商公司（默认 true）
        supplier_name: str|null    供应商名（默认「淘宝」）
        location_id: int|null      可选，入库货位 ID（全部行共用）
        rows: [{
            token: str             /api/taobao/parse 返回的令牌
            ipn: str               器件 IPN（默认 TB<itemid>）
            name: str              器件名称（默认标题）
            price: float|null      覆盖价格（缺省用页面高亮价）
            sku_text: str|null     选定规格（缺省用页面选中项）
            image_url: str|null    主图 URL（缺省选中 SKU 图 / 图集首图）
            qty: int|null          入库数量（>0 时创建 StockItem）
            action: "create"|"skip"
            update_existing: bool  IPN 已存在时更新（默认 false）
        }]
    """
    data = request.get_json(silent=True) or {}
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        return jsonify({"ok": False, "error": "rows 为空"}), 400

    category_id = data.get("category_id")
    if category_id not in (None, ""):
        try:
            category_id = int(category_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "category_id 必须为数字"}), 400
    else:
        category_id = None
    location_id = data.get("location_id")
    if location_id not in (None, ""):
        try:
            location_id = int(location_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "location_id 必须为数字"}), 400
    else:
        location_id = None
    create_mfr = bool(data.get("create_manufacturer", True))
    supplier_name = (data.get("supplier_name") or "").strip() or None

    results: list[dict] = []
    with _write_lock:
        try:
            api = build_inventree_api()
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        writer = InvenTreeWriter(api, _get_settings())

        for row in rows:
            if not isinstance(row, dict):
                continue
            token = (row.get("token") or "").strip()
            ipn_in = (row.get("ipn") or "").strip()
            out: dict = {"token": token, "ipn": ipn_in, "ok": False}
            if (row.get("action") or "create") == "skip":
                out["ok"] = True
                out["detail"] = "跳过"
                results.append(out)
                continue
            with _taobao_cache_lock:
                item = _taobao_cache.get(token)
            if item is None:
                out["error"] = "解析结果已过期，请重新上传 mhtml"
                results.append(out)
                continue

            ipn = ipn_in or item.default_ipn()
            out["ipn"] = ipn
            name = (row.get("name") or "").strip() or item.default_name()
            price = row.get("price")
            if price in (None, ""):
                price = item.price
            try:
                price = float(price) if price not in (None, "") else None
            except (TypeError, ValueError):
                price = None
            sku_text = (row.get("sku_text") or "").strip() or item.selected_sku
            image_url = (row.get("image_url") or "").strip() or item.main_image_url()
            image_data = item.embedded_bytes(image_url) if image_url else None
            qty_in = row.get("qty")
            try:
                qty = int(qty_in) if qty_in not in (None, "", "0", 0) else None
            except (TypeError, ValueError):
                qty = None

            params = {n: v for n, v in item.params}
            mfr_name = None
            if create_mfr:
                brand = (params.get("品牌") or "").strip()
                if brand and brand not in ("无品牌", "无", "N/A", "其它/其他"):
                    mfr_name = brand
            keywords = ",".join(
                k for k in ("淘宝", item.shop or "", item.item_id or "") if k
            )
            result = writer.upsert_custom_part(
                ipn=ipn,
                name=name,
                description=item.default_description(),
                notes=_taobao_notes(item, price, sku_text),
                keywords=keywords,
                link=item.url,
                category_pk=category_id,
                manufacturer_name=mfr_name,
                supplier_name=supplier_name,
                sku=f"TB{item.item_id}" if item.item_id else ipn,
                price=price,
                currency="CNY",
                parameters=params,
                image_data=image_data,
                update_existing=bool(row.get("update_existing", False)),
                create_stock=qty is not None,
                quantity=qty,
                location_pk=location_id,
            )
            out["ok"] = result.ok()
            out["part_pk"] = result.part_pk
            out["detail"] = result.summary()
            out["image_uploaded"] = result.image_uploaded
            out["image_skipped_reason"] = result.image_skipped_reason
            out["part_url"] = _part_url(result.part_pk) if result.part_pk else None
            out["stock_pk"] = result.stock_item_pk
            out["stock_url"] = _stock_url(result.stock_item_pk)

            # 条码绑定（建了库存且填写了条码时；机制同 /api/import）
            stock_barcode = (row.get("stock_barcode") or "").strip()
            if stock_barcode and result.stock_item_pk:
                out["stock_barcode"] = stock_barcode
                try:
                    status_code, barcode_result = _inventree_request(
                        "POST",
                        "/api/barcode/link/",
                        {"barcode": stock_barcode, "stockitem": result.stock_item_pk},
                    )
                except requests.RequestException as exc:
                    status_code, barcode_result = 502, str(exc)
                out["stock_barcode_assigned"] = status_code < 400
                if status_code >= 400:
                    out["stock_barcode_error"] = barcode_result
            elif stock_barcode:
                out["stock_barcode"] = stock_barcode
                out["stock_barcode_assigned"] = False
                out["stock_barcode_error"] = "该行未创建库存（入库数为空），无法绑定条码"
            if result.errors:
                out["error"] = "; ".join(result.errors)
            _record_history(
                code=ipn,
                part_pk=result.part_pk,
                ok=result.ok(),
                summary=f"淘宝导入: {result.summary()}",
                error="; ".join(result.errors) or None,
                part_url=_part_url(result.part_pk) if result.part_pk else None,
                entry_type="taobao",
            )
            results.append(out)

    ok_count = sum(1 for r in results if r.get("ok"))
    return jsonify({"ok": bool(results) and ok_count == len(results),
                    "total": len(results), "ok_count": ok_count,
                    "results": results})


# ---------------------------------------------------------------------------
# 快速入库
# ---------------------------------------------------------------------------


@app.route("/api/quick/locations", methods=["GET"])
def api_quick_locations():
    """获取货位列表（快速入库下拉框用）。"""
    try:
        api = build_inventree_api()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    locations = list_locations(api)
    return jsonify({"ok": True, "locations": locations})


@app.route("/api/quick/cache-status", methods=["GET"])
def api_quick_cache_status():
    """获取 Part 缓存状态。"""
    from lcsc2inv.quick_stock import get_parts_cache_status
    status = get_parts_cache_status()
    return jsonify({"ok": True, **status})


@app.route("/api/quick/refresh-cache", methods=["POST"])
def api_quick_refresh_cache():
    """手动刷新 Part 缓存。"""
    from lcsc2inv.quick_stock import refresh_parts_cache
    result = refresh_parts_cache()
    if result.get("ok"):
        return jsonify({"ok": True, **result})
    return jsonify({"ok": False, "error": result.get("error", "刷新失败")}), 500


@app.route("/api/quick/search", methods=["POST"])
def api_quick_search():
    """模糊搜索 Part（名称/IPN/MPN/描述）。

    请求体（JSON）：
        query: str          必填，搜索关键词
        limit: int          可选，返回结果数量上限（默认 20）
    """
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"ok": False, "error": "缺少 query 参数"}), 400

    limit = data.get("limit", 20)
    try:
        limit = int(limit)
        if limit <= 0 or limit > 100:
            raise ValueError
    except (TypeError, ValueError):
        limit = 20

    try:
        api = build_inventree_api()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    results = search_parts(api, query, limit)
    return jsonify({"ok": True, "query": query, "count": len(results), "results": results})


@app.route("/api/quick/lookup", methods=["POST"])
def api_quick_lookup():
    """智能匹配 Part + 查询当前库存。

    请求体（JSON）：
        code: str           必填，C-code 或 MPN
        location_pk: int    可选，查询该货位的现有库存
    """
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "缺少 code 参数"}), 400

    location_pk = data.get("location_pk")
    if location_pk not in (None, ""):
        try:
            location_pk = int(location_pk)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "location_pk 必须为数字"}), 400
    else:
        location_pk = None

    try:
        api = build_inventree_api()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    result = find_part(api, code)
    if result["found"] and location_pk:
        result["stock_at_location"] = get_stock_at_location(api, result["part_pk"], location_pk)
    else:
        result["stock_at_location"] = None

    result["lcsc_code"] = code
    return jsonify(result)


@app.route("/api/quick/stock", methods=["POST"])
def api_quick_stock():
    """单条快速入库：匹配/创建 Part + 入库。

    请求体（JSON）：
        code: str                   可选，C-code 或 MPN（与 part_pk 二选一）
        part_pk: int                可选，直接指定 Part ID（与 code 二选一）
        quantity: int|float         必填，入库数量
        location_pk: int            可选，货位
        merge: bool                 是否合并到已有库存（默认 true）
        create_if_missing: bool     未找到时是否自动从 LCSC 创建（默认 true）
        notes: str                  可选，备注
    """
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    part_pk_input = data.get("part_pk")

    if not code and not part_pk_input:
        return jsonify({"ok": False, "error": "缺少 code 或 part_pk 参数"}), 400

    quantity = data.get("quantity")
    if quantity in (None, ""):
        return jsonify({"ok": False, "error": "缺少 quantity 参数"}), 400
    try:
        quantity = float(quantity)
        if quantity <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "quantity 必须为正数"}), 400

    location_pk = data.get("location_pk")
    if location_pk not in (None, ""):
        try:
            location_pk = int(location_pk)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "location_pk 必须为数字"}), 400
    else:
        location_pk = None

    merge = bool(data.get("merge", True))
    create_if_missing = bool(data.get("create_if_missing", True))
    notes = (data.get("notes") or "").strip() or None

    with _write_lock:
        try:
            api = build_inventree_api()
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

        # 1. 查找 Part（优先使用 part_pk，否则通过 code 查找）
        result = {"found": False, "part_pk": None, "part_name": None, "ipn": None,
                  "mpn": None, "category": None, "in_stock": None, "match_type": None}
        created_part = False

        if part_pk_input:
            # 直接使用 part_pk
            try:
                part_pk_input = int(part_pk_input)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "part_pk 必须为整数"}), 400

            # 从 InvenTree 获取 Part 信息
            try:
                from inventree.part import Part
                p = Part(api, part_pk_input)
                result = {
                    "found": True,
                    "part_pk": p.pk,
                    "part_name": getattr(p, "name", None),
                    "ipn": getattr(p, "IPN", None),
                    "mpn": None,
                    "category": None,
                    "in_stock": getattr(p, "in_stock", None),
                    "match_type": "pk",
                }
            except Exception as exc:
                return jsonify({"ok": False, "error": f"Part #{part_pk_input} 不存在: {exc}"}), 404
        else:
            # 通过 code 查找
            result = find_part(api, code)

        # 2. 未找到且允许自动创建
        part_pk = result["part_pk"]
        if not part_pk and create_if_missing and code:
            try:
                parsed_code = parse_lcsc_code(code)
            except ValueError:
                return jsonify({
                    "ok": False,
                    "error": f"未找到 Part，且 '{code}' 不是合法的 LCSC 代码，无法自动创建",
                    "found": False,
                }), 404

            fetcher = _get_fetcher()
            try:
                lcsc_part = fetcher.fetch(code)
            except LcscFetchError as exc:
                return jsonify({
                    "ok": False,
                    "error": f"抓取 LCSC 失败: {exc}",
                    "found": False,
                }), 502

            writer = InvenTreeWriter(api, _get_settings())
            opts = WriteOptions(
                update_existing=False,
                create_stock=False,
                fetcher=fetcher,
                force_image_upload=False,
            )
            write_result = writer.upsert_part(
                lcsc_part, options=opts, create_missing_category=True
            )
            if not write_result.ok():
                return jsonify({
                    "ok": False,
                    "error": f"创建 Part 失败: {'; '.join(write_result.errors)}",
                    "found": False,
                }), 502
            part_pk = write_result.part_pk
            result["part_pk"] = part_pk
            created_part = True

        if not part_pk:
            return jsonify({
                "ok": False,
                "error": f"未找到 Part '{code}'，且未开启自动创建",
                "found": False,
            }), 404

        # 3. 入库
        try:
            stock_result = add_or_merge_stock(
                api,
                part_pk=part_pk,
                quantity=quantity,
                location_pk=location_pk,
                notes=notes,
                merge=merge,
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"入库失败: {exc}"}), 500

    # 4. 记录历史
    _record_history(
        code=code,
        part_pk=part_pk,
        ok=True,
        summary=f"快速入库: +{quantity} → {stock_result['new_quantity']}",
        error=None,
        part_url=_part_url(part_pk),
        entry_type="quick",
    )

    return jsonify({
        "ok": True,
        "part_pk": part_pk,
        "part_name": result["part_name"],
        "stock_pk": stock_result["stock_pk"],
        "quantity": stock_result["quantity"],
        "merged": stock_result["merged"],
        "new_quantity": stock_result["new_quantity"],
        "created_part": created_part,
        "part_url": _part_url(part_pk),
    })


@app.route("/api/quick/batch", methods=["POST"])
def api_quick_batch():
    """批量快速入库：队列提交。

    请求体（JSON）：
        rows: [{code, quantity, notes?}, ...]
        location_pk: int            可选，默认货位
        merge: bool                 是否合并（默认 true）
        create_if_missing: bool     未找到时是否自动创建（默认 true）
    """
    data = request.get_json(silent=True) or {}
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        return jsonify({"ok": False, "error": "rows 为空或格式错误"}), 400

    location_pk = data.get("location_pk")
    if location_pk not in (None, ""):
        try:
            location_pk = int(location_pk)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "location_pk 必须为数字"}), 400
    else:
        location_pk = None

    merge = bool(data.get("merge", True))
    create_if_missing = bool(data.get("create_if_missing", True))

    results = []
    with _write_lock:
        try:
            api = build_inventree_api()
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

        for row in rows:
            if not isinstance(row, dict):
                continue
            code = (row.get("code") or "").strip()
            quantity = row.get("quantity")
            notes = (row.get("notes") or "").strip() or None

            out = {"code": code, "ok": False}
            if not code or quantity in (None, ""):
                out["error"] = "缺少 code 或 quantity"
                results.append(out)
                continue

            try:
                quantity = float(quantity)
                if quantity <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                out["error"] = "quantity 必须为正数"
                results.append(out)
                continue

            # 查找 Part
            result = find_part(api, code)
            part_pk = result["part_pk"]
            created_part = False

            if not part_pk and create_if_missing:
                try:
                    parse_lcsc_code(code)
                except ValueError:
                    out["error"] = f"未找到 Part，且 '{code}' 不是合法的 LCSC 代码"
                    results.append(out)
                    continue

                fetcher = _get_fetcher()
                try:
                    lcsc_part = fetcher.fetch(code)
                except LcscFetchError as exc:
                    out["error"] = f"抓取 LCSC 失败: {exc}"
                    results.append(out)
                    continue

                writer = InvenTreeWriter(api, _get_settings())
                opts = WriteOptions(
                    update_existing=False,
                    create_stock=False,
                    fetcher=fetcher,
                    force_image_upload=False,
                )
                write_result = writer.upsert_part(
                    lcsc_part, options=opts, create_missing_category=True
                )
                if not write_result.ok():
                    out["error"] = f"创建 Part 失败: {'; '.join(write_result.errors)}"
                    results.append(out)
                    continue
                part_pk = write_result.part_pk
                created_part = True

            if not part_pk:
                out["error"] = f"未找到 Part '{code}'"
                results.append(out)
                continue

            # 入库
            try:
                stock_result = add_or_merge_stock(
                    api,
                    part_pk=part_pk,
                    quantity=quantity,
                    location_pk=location_pk,
                    notes=notes,
                    merge=merge,
                )
                out.update({
                    "ok": True,
                    "part_pk": part_pk,
                    "part_name": result["part_name"],
                    "stock_pk": stock_result["stock_pk"],
                    "quantity": stock_result["quantity"],
                    "merged": stock_result["merged"],
                    "new_quantity": stock_result["new_quantity"],
                    "created_part": created_part,
                })
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"入库失败: {exc}"

            results.append(out)

    ok_count = sum(1 for r in results if r.get("ok"))
    _record_history(
        code=None,
        part_pk=None,
        ok=ok_count == len(results) and bool(results),
        summary=f"快速批量入库: {ok_count}/{len(results)} 成功",
        error=None,
        part_url=None,
        entry_type="quick_batch",
    )

    return jsonify({
        "ok": ok_count == len(results) and bool(results),
        "total": len(results),
        "ok_count": ok_count,
        "results": results,
    })


@app.route("/api/preview", methods=["POST"])
def api_preview():
    """无写入预览：给定 serialized LCSCPart + 可选 category/mapping 数据，
    返回类别匹配、参数映射、备注与关键词；不做任何 InvenTree 写入。

    请求体（JSON）：
        part: dict               必填，序列化的 LCSCPart
        category_data: dict|None 可选，覆盖 `lcsc_categories.yaml`
        mapping_data: dict|None  可选，覆盖 `field_map.yaml`
    """
    data = request.get_json(silent=True) or {}
    raw_part = data.get("part")
    if raw_part is not None:
        if not isinstance(raw_part, dict):
            return jsonify({"ok": False, "error": "part 字段不合法"}), 400
        try:
            part = LCSCPart.model_validate(raw_part)
        except ValidationError as exc:
            return jsonify({"ok": False, "error": f"part 字段不合法: {exc}"}), 400
    else:
        code_or_url = (data.get("code") or "").strip()
        if not code_or_url:
            return jsonify({"ok": False, "error": "缺少 code 或 part 参数"}), 400
        try:
            code = parse_lcsc_code(code_or_url)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        try:
            part = _get_fetcher().fetch(code_or_url)
        except LcscFetchError as exc:
            return jsonify({"ok": False, "error": f"抓取 LCSC 失败: {exc}"}), 502

    category_data = data.get("category_data")
    mapping_data = data.get("mapping_data")
    if category_data is not None and not isinstance(category_data, dict):
        return jsonify({"ok": False, "error": "category_data 必须是 dict"}), 400
    if mapping_data is not None and not isinstance(mapping_data, dict):
        return jsonify({"ok": False, "error": "mapping_data 必须是 dict"}), 400

    cat_match = categorizer_match(
        part,
        create_missing_category=False,
        category_data=category_data,
    )

    # 与 writer 一致：用 InvenTree 类别路径的前两段作为 field_map 的 category_top/sub
    inv_top = None
    inv_sub = None
    if cat_match.category_path and not cat_match.category_path.startswith("__"):
        seg = cat_match.category_path.split("/")
        inv_top = seg[0] if seg else None
        inv_sub = seg[1] if len(seg) > 1 else None

    parameters = to_inventree_parameters(
        part, category_top=inv_top, category_sub=inv_sub, mapping_data=mapping_data
    )

    return jsonify(
        {
            "ok": True,
            "category": {
                "category_path": cat_match.category_path,
                "source": cat_match.source,
                "score": cat_match.score,
                "candidates": [list(c) for c in cat_match.candidates],
            },
            "parameters": parameters,
            "notes": to_part_notes(part),
            "keywords": InvenTreeWriter._build_keywords(part),
            "code": part.sku,
            "source": "LCSC 商品数据",
            "part": part.model_dump(mode="json"),
        }
    )


def _parse_csv_upload() -> tuple[list[dict], dict]:
    """解析 /api/batch 与 /api/batch/jobs 共用的 multipart CSV 上传。

    Returns:
        (rows, opts)：rows 为 [{lcsc_code, quantity, note}]，opts 为导入选项。

    Raises:
        ValueError: 缺少 file 或 CSV 无有效 lcsc_code 行。
    """
    f = request.files.get("file")
    if f is None or not f.filename:
        raise ValueError("缺少 file 字段（CSV 上传）")
    text = f.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    for row in reader:
        code = (row.get("lcsc_code") or "").strip()
        if not code:
            continue
        rows.append(
            {
                "lcsc_code": code,
                "quantity": row.get("quantity") or None,
                "note": row.get("note") or None,
            }
        )
    if not rows:
        raise ValueError("CSV 为空或缺少 lcsc_code 列")
    opts = {
        "update": bool(request.form.get("update", False)),
        "stock": bool(request.form.get("stock", False)),
        "create_missing_category": bool(request.form.get("create_missing_category", False)),
    }
    return rows, opts


@app.route("/api/batch", methods=["POST"])
def api_batch():
    """CSV 批量导入。

    上传字段：file  (CSV 文件，需含 lcsc_code 列，可选 quantity / note)
    """
    try:
        rows, opts = _parse_csv_upload()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    update = opts["update"]
    stock = opts["stock"]
    create_missing = opts["create_missing_category"]

    fetcher = _get_fetcher()
    results = []
    with _write_lock:
        try:
            api = build_inventree_api()
        except ValueError as exc:
            err = str(exc)
            _record_history(code=None, part_pk=None, ok=False, summary=err,
                            error=err, part_url=None)
            return jsonify({"ok": False, "error": err}), 500
        writer = InvenTreeWriter(api, _get_settings())
        for row in rows:
            code = row["lcsc_code"]
            qty = int(row["quantity"]) if row["quantity"] else None
            note = row["note"] or None
            try:
                part = fetcher.fetch(code)
            except LcscFetchError as exc:
                results.append({"code": code, "ok": False, "error": f"抓取失败: {exc}"})
                continue
            opts2 = WriteOptions(
                update_existing=update,
                create_stock=stock,
                quantity=qty,
                extra_note=note,
                fetcher=fetcher,
                force_image_upload=update,
            )
            result = writer.upsert_part(
                part, options=opts2, create_missing_category=create_missing
            )
            d = _write_result_to_dict(result)
            d["code"] = code
            results.append(d)

    ok_count = sum(1 for r in results if r.get("ok"))
    _record_history(
        code=None,
        part_pk=None,
        ok=len(results) > 0 and ok_count == len(results),
        summary=f"{ok_count}/{len(results)} ok",
        error=None,
        part_url=None,
    )
    return jsonify({"ok": True, "total": len(results), "ok_count": ok_count,
                    "results": results})


@app.route("/api/batch/jobs", methods=["POST"])
def api_batch_jobs_create():
    """创建后台批量任务，立即返回 job_id（queued）；worker 异步执行后可轮询。

    请求体（JSON）：{"rows": [{lcsc_code, quantity?, note?}], update?, stock?,
                    create_missing_category?}
    或 multipart：file (CSV) + update / stock / create_missing_category
    """
    data = request.get_json(silent=True) or {}
    raw_rows = data.get("rows")
    if isinstance(raw_rows, list) and raw_rows:
        rows: list[dict] = []
        for r in raw_rows:
            if not isinstance(r, dict):
                continue
            code = (r.get("lcsc_code") or "").strip()
            if not code:
                continue
            rows.append({"lcsc_code": code, "quantity": r.get("quantity"),
                         "note": r.get("note")})
        if not rows:
            return jsonify({"ok": False, "error": "rows 为空或缺少 lcsc_code"}), 400
        opts = {
            "update": bool(data.get("update", False)),
            "stock": bool(data.get("stock", False)),
            "create_missing_category": bool(data.get("create_missing_category", False)),
        }
    else:
        # 兼容 multipart CSV 上传（与 /api/batch 一致）
        try:
            rows, opts = _parse_csv_upload()
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    job: dict = {
        "job_id": _new_job_id(),
        "status": "queued",
        "type": "batch",
        "created_at": _utcnow_iso(),
        "started_at": None,
        "finished_at": None,
        "total": len(rows),
        "ok_count": None,
        "fail_count": None,
        "current_code": None,
        "results": [],
        "error": None,
        "input": {
            "rows": rows,
            "update": bool(opts["update"]),
            "stock": bool(opts["stock"]),
            "create_missing_category": bool(opts["create_missing_category"]),
        },
    }
    with _jobs_lock:
        _jobs[job["job_id"]] = job
    _ensure_job_worker()
    with _jobs_cond:
        _jobs_cond.notify_all()
    return jsonify({"ok": True, "status": "queued", "job_id": job["job_id"]})


@app.route("/api/batch/jobs/<job_id>", methods=["GET"])
def api_batch_jobs_get(job_id):
    """轮询后台任务状态；入队/运行/完成/失败，含逐条结果与可重试的 input。"""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return jsonify({"ok": False, "error": f"未找到 job: {job_id}"}), 404
    return jsonify({"ok": True, "job": _job_to_payload(job)})


def _inventree_request(method: str, path: str, payload: dict | None = None):
    """用服务器配置的 InvenTree Token 调用 API，避免把 Token 暴露给浏览器。"""
    settings = _get_settings()
    if not settings.inventree_url or not settings.inventree_token:
        raise ValueError("缺少 INVENTREE_URL 或 INVENTREE_TOKEN")
    response = requests.request(
        method,
        f"{settings.inventree_url.rstrip('/')}{path}",
        json=payload,
        headers={"Authorization": f"Token {settings.inventree_token}"},
        timeout=20,
    )
    try:
        data = response.json()
    except ValueError:
        data = {"detail": response.text}
    return response.status_code, data


def _pk_of(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, dict):
        for key in ("pk", "id"):
            found = _pk_of(value.get(key))
            if found is not None:
                return found
    return None


@app.route("/api/barcode/scan", methods=["POST"])
def api_barcode_scan():
    """识别 InvenTree 库存或货位条码。"""
    barcode = ((request.get_json(silent=True) or {}).get("barcode") or "").strip()
    if not barcode:
        return jsonify({"ok": False, "error": "缺少 barcode"}), 400
    try:
        status_code, data = _inventree_request("POST", "/api/barcode/", {"barcode": barcode})
    except (ValueError, requests.RequestException) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    if status_code >= 400:
        return jsonify({"ok": False, "error": data}), status_code

    if "stockitem" in data:
        item = data["stockitem"]
        stock_pk = _pk_of(item)
        quantity = item.get("quantity") if isinstance(item, dict) else None
        if stock_pk is None or quantity in (None, ""):
            status_code, item = _inventree_request("GET", f"/api/stock/{stock_pk}/")
            if status_code >= 400:
                return jsonify({"ok": False, "error": item}), status_code
            stock_pk = _pk_of(item)
            quantity = item.get("quantity")
        return jsonify({"ok": True, "kind": "stockitem", "pk": stock_pk,
                        "quantity": quantity, "object": item})
    if "stocklocation" in data:
        location = data["stocklocation"]
        return jsonify({"ok": True, "kind": "stocklocation", "pk": _pk_of(location),
                        "object": location})
    return jsonify({"ok": False, "error": "条码不是库存项或货位", "result": data}), 422


@app.route("/api/stock/transfer", methods=["POST"])
def api_stock_transfer():
    """把一笔库存的全部数量转移到目标货位。"""
    data = request.get_json(silent=True) or {}
    try:
        stock_pk = int(data["stock_pk"])
        location_pk = int(data["location_pk"])
        quantity = str(data["quantity"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "缺少 stock_pk、location_pk 或 quantity"}), 400
    payload = {
        "location": location_pk,
        "notes": data.get("notes") or "Web barcode transfer",
        "items": [{"pk": stock_pk, "quantity": quantity}],
    }
    try:
        status_code, result = _inventree_request("POST", "/api/stock/transfer/", payload)
    except (ValueError, requests.RequestException) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    if status_code >= 400:
        return jsonify({"ok": False, "error": result}), status_code
    return jsonify({"ok": True, "result": result})


@app.route("/api/stock/count", methods=["POST"])
def api_stock_count():
    """把一笔库存的数量直接设置为盘点后的数量。"""
    data = request.get_json(silent=True) or {}
    try:
        stock_pk = int(data["stock_pk"])
        quantity = str(data["quantity"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "缺少 stock_pk 或 quantity"}), 400
    payload = {
        "notes": data.get("notes") or "Web barcode stock count",
        "items": [{"pk": stock_pk, "quantity": quantity}],
    }
    try:
        status_code, result = _inventree_request("POST", "/api/stock/count/", payload)
    except (ValueError, requests.RequestException) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    if status_code >= 400:
        return jsonify({"ok": False, "error": result}), status_code
    return jsonify({"ok": True, "result": result})


@app.route("/api/stock/decrement", methods=["POST"])
def api_stock_decrement():
    """库存增减：把一笔库存的数量调整指定数量（带符号）。

    - quantity > 0：扣减（POST /api/stock/remove/，出库跟踪记录）；
    - quantity < 0：增加（POST /api/stock/add/，入库跟踪记录），按绝对值生效；
    - quantity == 0 或缺失：400。
    """
    data = request.get_json(silent=True) or {}
    try:
        stock_pk = int(data["stock_pk"])
        qty = int(data.get("quantity", 1))
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "缺少 stock_pk 或数量不是整数"}), 400
    if qty == 0:
        return jsonify({"ok": False, "error": "数量不能为 0"}), 400
    if qty > 0:
        path, item_qty = "/api/stock/remove/", qty
    else:
        path, item_qty = "/api/stock/add/", -qty
    payload = {
        "notes": data.get("notes") or "Web barcode stock adjust",
        "items": [{"pk": stock_pk, "quantity": str(item_qty)}],
    }
    try:
        status_code, result = _inventree_request("POST", path, payload)
    except (ValueError, requests.RequestException) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    if status_code >= 400:
        return jsonify({"ok": False, "error": result}), status_code
    return jsonify({"ok": True, "result": result})


@app.route("/api/stock/<int:pk>/barcode-info", methods=["GET"])
def api_stock_barcode_info(pk: int):
    """库存项信息 + 其 InvenTree 标准条码数据（前端「条码查看」页用）。

    这版 InvenTree 没有读取绑定条码原文的 GET 接口，但 `{"stockitem": <pk>}`
    是 InvenTree 的标准库存码格式，可直接被 POST /api/barcode/ 反查
    （已实测），因此二维码始终绘制该标准数据。

    自定义绑定码只从本地缓存读取（由「更新缓存」全量同步生成），
    本接口绝不进容器查询，保证响应速度恒定。
    """
    try:
        status, item = _inventree_request("GET", f"/api/stock/{pk}/")
    except (ValueError, requests.RequestException) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    if status >= 400:
        return jsonify({"ok": False, "error": item}), status

    part_detail = item.get("part_detail") or {}
    location_detail = item.get("location_detail") or {}
    location_name = (
        location_detail.get("pathstring") or location_detail.get("name") or ""
    )
    # 部分 InvenTree 版本的 stock 详情不带 location_detail，单独查一次货位名
    if not location_name and item.get("location"):
        try:
            st, loc = _inventree_request(
                "GET", f"/api/stock/location/{item['location']}/"
            )
            if st < 400 and isinstance(loc, dict):
                location_name = loc.get("pathstring") or loc.get("name") or ""
        except (ValueError, requests.RequestException):
            pass  # 货位名获取失败不影响主体信息

    # 自定义绑定条码：只读本地缓存（由「更新缓存」全量同步生成），不进容器，
    # 保证查看速度恒定；缓存中没有就不显示，用户可点「更新缓存」补齐
    cache = barcode_lookup.load_cache(_get_settings())
    bound = cache.get(pk, "")
    bound_from_cache = pk in cache

    s = _get_settings()
    base = (s.inventree_url or "").rstrip("/")
    return jsonify(
        {
            "ok": True,
            "pk": pk,
            "barcode_data": json.dumps({"stockitem": pk}),
            "part_name": part_detail.get("full_name") or part_detail.get("name") or "",
            "part_description": part_detail.get("description") or "",
            "ipn": part_detail.get("IPN") or "",
            "quantity": item.get("quantity"),
            "batch": item.get("batch") or "",
            "serial": item.get("serial") or "",
            "in_stock": item.get("in_stock"),
            "location_name": location_name,
            "bound_barcode": bound,
            "bound_from_cache": bound_from_cache,
            "bound_qrcode_url": (
                f"/api/qrcode.png?data={quote(bound)}" if bound else None
            ),
            "stock_url": f"{base}/stock/item/{pk}/" if base else None,
            "qrcode_url": f"/api/stock/{pk}/qrcode.png",
        }
    )


def _qr_png_response(data: str, download_name: str):
    """把字符串绘制成二维码 PNG 并作为图片响应返回。"""
    import qrcode  # 延迟导入：仅条码查看功能需要

    img = qrcode.make(data, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", download_name=download_name)


@app.route("/api/barcode/cache", methods=["GET"])
def api_barcode_cache_info():
    """自定义条码缓存概况（条数 + 最近更新时间）。"""
    return jsonify({"ok": True, **barcode_lookup.cache_info(_get_settings())})


@app.route("/api/barcode/cache/sync", methods=["POST"])
def api_barcode_cache_sync():
    """全量镜像同步：以 InvenTree 为准，把全部绑定条码覆盖到本地缓存
    （新增/更新/删除三向对齐）。同步为阻塞请求（单次 exec 约 1~3 秒），
    返回增量报告供前端展示日志。
    """
    try:
        report = barcode_lookup.sync_cache(_get_settings())
    except Exception as exc:  # noqa: BLE001 — 转成 JSON 错误给前端
        logger.warning("条码缓存同步失败: %s", exc)
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
    return jsonify({"ok": True, "report": report})


@app.route("/api/barcode/cache/download", methods=["GET"])
def api_barcode_cache_download():
    """下载条码缓存备份（?format=json 原始备份可再导入恢复；csv 供 Excel 查看）。"""
    fmt = (request.args.get("format") or "json").lower()
    s = _get_settings()
    cache = barcode_lookup.load_cache(s)
    if not cache:
        return jsonify({"ok": False, "error": "缓存为空，请先「更新缓存」"}), 404
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["stock_pk", "barcode_data"])
        for pk, code in sorted(cache.items()):
            writer.writerow([pk, code])
        data = io.BytesIO(("\ufeff" + buf.getvalue()).encode("utf-8"))
        return send_file(
            data, mimetype="text/csv", as_attachment=True,
            download_name=f"bound-barcodes-{stamp}.csv",
        )
    payload = {
        "updated_at": barcode_lookup.cache_info(s).get("updated_at"),
        "entries": {str(k): v for k, v in sorted(cache.items())},
    }
    data = io.BytesIO(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    )
    return send_file(
        data, mimetype="application/json", as_attachment=True,
        download_name=f"bound-barcodes-{stamp}.json",
    )


@app.route("/api/barcode/cache/restore", methods=["POST"])
def api_barcode_cache_restore():
    """从上传的 JSON 备份恢复条码缓存（整体替换当前缓存）。

    恢复后如需对齐远端，可再点「更新缓存」做增量同步。
    """
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"ok": False, "error": "缺少 file 字段（JSON 备份文件）"}), 400
    try:
        data = json.loads(f.read().decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        return jsonify({"ok": False, "error": f"文件不是合法 JSON: {exc}"}), 400
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return jsonify({"ok": False, "error": "JSON 结构不对：缺少 entries 对象"}), 400
    restored: dict[int, str] = {}
    for key, value in entries.items():
        try:
            restored[int(key)] = str(value or "")
        except (TypeError, ValueError):
            continue
    if not restored:
        return jsonify({"ok": False, "error": "备份文件中没有有效条目"}), 400
    barcode_lookup.replace_cache(_get_settings(), restored, data.get("updated_at"))
    return jsonify({"ok": True, "restored": len(restored)})


@app.route("/api/barcode/bind", methods=["POST"])
def api_barcode_bind():
    """把自定义条码绑定到库存项（InvenTree POST /api/barcode/link/）。

    绑定成功后同步写入本地条码缓存，查看页立即生效（保持「只读缓存」策略）。
    """
    data = request.get_json(silent=True) or {}
    try:
        stock_pk = int(data["stock_pk"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "缺少或非法 stock_pk"}), 400
    barcode = (data.get("barcode") or "").strip()
    if not barcode:
        return jsonify({"ok": False, "error": "缺少 barcode"}), 400
    if len(barcode) > 2000:
        return jsonify({"ok": False, "error": "条码内容过长（>2000 字符）"}), 400

    try:
        status_code, result = _inventree_request(
            "POST", "/api/barcode/link/",
            {"barcode": barcode, "stockitem": stock_pk},
        )
    except (ValueError, requests.RequestException) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    if status_code >= 400:
        return jsonify({"ok": False, "error": result}), status_code

    # 绑定成功 → 同步本地缓存
    barcode_lookup.cache_put(_get_settings(), stock_pk, barcode)
    return jsonify(
        {
            "ok": True,
            "stock_pk": stock_pk,
            "barcode": barcode,
            "qrcode_url": f"/api/qrcode.png?data={quote(barcode)}",
        }
    )


@app.route("/api/stock/<int:pk>/qrcode.png", methods=["GET"])
def api_stock_qrcode_png(pk: int):
    """把库存项的 InvenTree 标准条码数据绘制成二维码 PNG（服务端生成）。"""
    try:
        return _qr_png_response(
            json.dumps({"stockitem": pk}), f"stockitem-{pk}-qrcode.png"
        )
    except ImportError as exc:
        return jsonify({"ok": False, "error": f"缺少 qrcode 依赖: {exc}"}), 502


@app.route("/api/qrcode.png", methods=["GET"])
def api_qrcode_png():
    """把任意条码数据（?data=）绘制成二维码 PNG（自定义绑定码用）。"""
    data = (request.args.get("data") or "").strip()
    if not data:
        return jsonify({"ok": False, "error": "缺少 data 参数"}), 400
    if len(data) > 2000:
        return jsonify({"ok": False, "error": "条码数据过长（>2000 字符）"}), 400
    try:
        return _qr_png_response(data, "barcode-qrcode.png")
    except ImportError as exc:
        return jsonify({"ok": False, "error": f"缺少 qrcode 依赖: {exc}"}), 502


@app.route("/api/history", methods=["GET"])
def api_history():
    """列出最近的导入历史（新→旧）。"""
    with _history_lock:
        entries = _history_load()
    return jsonify({"ok": True, "count": len(entries), "entries": list(
        reversed(entries))})


@app.route("/api/history/clear", methods=["POST"])
def api_history_clear():
    """清空历史：写入空列表（保留文件）。"""
    with _history_lock:
        try:
            _history_save([])
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "cleared": True})


@app.route("/api/doctor", methods=["GET"])
def api_doctor():
    """检查 InvenTree 连通性与配置。"""
    s = _get_settings()
    checks = []
    checks.append(("INVENTREE_URL", bool(s.inventree_url), s.inventree_url))
    has_token = bool(s.inventree_token or (s.inventree_username and s.inventree_password))
    checks.append(("认证配置", has_token, ""))

    with _write_lock:
        try:
            api = build_inventree_api()
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks]}), 500
        try:
            from inventree.company import Company
            from inventree.part import Part

            n_parts = len(Part.list(api))
            suppliers = [c for c in Company.list(api) if getattr(c, "is_supplier", False)]
            checks.append(("Part 数量", True, str(n_parts)))
            checks.append(("供应商数量", True, str(len(suppliers))))
        except Exception as exc:
            checks.append(("InvenTree API 访问", False, f"{type(exc).__name__}: {exc}"))

        # 库存统计：条目数 + 总数量（单独 try，失败不影响上面的检查结果）
        try:
            from inventree.stock import StockItem

            stock_items = StockItem.list(api)
            total_qty = 0.0
            for it in stock_items:
                with contextlib.suppress(TypeError, ValueError):
                    total_qty += float(it.quantity or 0)
            if float(total_qty).is_integer():
                qty_str = str(int(total_qty))
            else:
                qty_str = f"{total_qty:.2f}".rstrip("0").rstrip(".")
            checks.append(("库存条目数", True, str(len(stock_items))))
            checks.append(("库存总数量", True, qty_str))
        except Exception as exc:
            checks.append(("库存统计", False, f"{type(exc).__name__}: {exc}"))

    # YAML 配置
    cat = load_yaml("lcsc_categories.yaml")
    fmap = load_yaml("field_map.yaml")
    checks.append(("lcsc_categories.yaml", True, f"{len(cat)} 顶级"))
    checks.append(("field_map.yaml", True, f"{len(fmap)} 顶级"))

    return jsonify(
        {
            "ok": all(c[1] for c in checks),
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        }
    )


@app.route("/api/cache", methods=["GET"])
def api_cache_list():
    """列出已缓存商品。"""
    s = _get_settings()
    d = s.cache_dir_path
    if not d.exists():
        return jsonify({"ok": True, "codes": []})
    codes = sorted(
        p.name for p in d.glob("*.json") if not p.name.endswith(".meta.json")
    )
    return jsonify({"ok": True, "codes": codes, "dir": str(d)})


@app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    """清理缓存目录。"""
    s = _get_settings()
    d = s.cache_dir_path
    if not d.exists():
        return jsonify({"ok": True, "cleared": 0})
    n = 0
    for p in d.iterdir():
        if p.is_file():
            p.unlink()
            n += 1
    return jsonify({"ok": True, "cleared": n})


# ---------------------------------------------------------------------------
# InvenTree 数据库一键备份
# ---------------------------------------------------------------------------


@app.route("/api/backup", methods=["POST"])
def api_backup_start():
    """一键备份：后台线程在 InvenTree 容器内执行原生备份并拷贝到 /backup。"""
    global _backup_thread
    with _backup_lock:
        if _backup_job["status"] == "running":
            return jsonify({"ok": False, "error": "已有备份任务正在进行"}), 409
        s = _get_settings()
        # 同步置位，避免「启动后立刻再查」读到旧状态
        _backup_job.update(
            {
                "status": "running",
                "started_at": _utcnow_iso(),
                "finished_at": None,
                "message": f"进入容器 {s.inventree_backup_container or '?'} "
                            f"执行 {s.inventree_backup_cmd} …",
                "error": "",
                "result": None,
                "log": [],
            }
        )
        thread = threading.Thread(
            target=_run_backup_worker, daemon=True, name="lcsc2inv-backup"
        )
        _backup_thread = thread
        thread.start()
    return jsonify({"ok": True, **_backup_status_snapshot()})


@app.route("/api/backup", methods=["GET"])
def api_backup_list():
    """查询备份状态 + 已保存的快照列表。"""
    s = _get_settings()
    payload = _backup_status_snapshot()
    payload["storage"] = str(storage_path(s))
    payload["snapshots"] = list_backups(s)
    return jsonify(payload)


@app.route("/api/backup/<snapshot>/<path:filename>", methods=["GET"])
def api_backup_download(snapshot: str, filename: str):
    """下载某个快照里的备份文件（受限于 /backup 目录内）。"""
    s = _get_settings()
    base = storage_path(s).resolve()
    target = (base / snapshot / filename).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    return send_file(target, as_attachment=True, download_name=target.name)


@app.route("/api/backup/<snapshot>", methods=["DELETE"])
def api_backup_delete(snapshot: str):
    """删除一个备份快照（整目录，含其下全部文件）。

    - 备份进行中时拒绝删除（避免删到正在写入的快照）；
    - 快照名不允许包含路径分隔符，且必须直接位于备份存储目录内。
    """
    if _backup_status_snapshot()["status"] == "running":
        return jsonify({"ok": False, "error": "备份正在进行中，请稍后再删除"}), 409
    if "/" in snapshot or "\\" in snapshot or snapshot in (".", ".."):
        return jsonify({"ok": False, "error": "非法的快照名"}), 400
    s = _get_settings()
    base = storage_path(s).resolve()
    target = (base / snapshot).resolve()
    if target == base or not target.is_relative_to(base) or not target.is_dir():
        return jsonify({"ok": False, "error": "快照不存在"}), 404
    try:
        shutil.rmtree(target)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"删除失败: {exc}"}), 500
    return jsonify({"ok": True, "deleted": snapshot})


def main() -> None:
    """本地调试用：`python -m lcsc2inv.web`。生产用 gunicorn 单 worker。"""
    logging.basicConfig(level=logging.INFO)
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=False)


if __name__ == "__main__":
    main()
