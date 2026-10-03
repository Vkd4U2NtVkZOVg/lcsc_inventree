"""`lcsc2inv.web` Flask 应用测试（全部离线，用 mock，不打真实 LCSC/InvenTree）。

覆盖所有后端路由（实现见 `lcsc2inv/web.py`，设计见
`.zcode/plans/plan-sess_97ee5802-40e2-4b93-bcf7-15c7c6b6ac5e.md`）：

- `GET  /`                       渲染单页 HTML
- `POST /api/import`             单条导入（含 LCSC 商品 URL 识别）
- `POST /api/batch`              CSV 批量导入（同步）
- `POST /api/preview`            无写入预览（serialized LCSCPart → 类别/参数/备注）
- `POST /api/batch/jobs`         后台批量任务（异步，返回 job_id）
- `GET  /api/batch/jobs/<id>`    轮询任务状态
- `GET  /api/history`、`POST /api/history/clear`  最近导入历史
- `GET  /api/doctor`             连通性 / 配置检查
- `GET  /api/cache`、`POST /api/cache/clear`  缓存管理

关键约定：
- 只 patch 稳定的边界（`web.get_settings` / `web.default_fetcher` /
  `web.build_inventree_api` / `web.InvenTreeWriter`），不依赖路由内部细节。
- 预览 / 历史 / 批量任务测试让真实的分类匹配与字段映射逻辑跑真数据
  （它们只读本地 config + thefuzz，不打网络）。
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lcsc2inv.config import Settings
from lcsc2inv.inventree_writer import WriteResult
from lcsc2inv.lcsc_client import LcscFetchError
from lcsc2inv.web import app

# ---------------------------------------------------------------------------
# 辅助：构造测试 Settings / 夹具
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path, **overrides) -> Settings:
    """用 tmp cache 目录构造 Settings，避免污染用户目录。"""
    base = {
        "inventree_url": "http://inventree.test",
        "inventree_token": "t" * 40,
        "inventree_supplier_name": "LCSC Electronics",
        "lcsc_cache_enabled": True,
        "lcsc_cache_dir": str(tmp_path),
        "lcsc_cache_ttl_days": 7,
        "lcsc_request_interval": 0.0,  # 取消限速等待
        "lcsc_max_retries": 2,
        "lcsc_user_agent": "test-agent",
        "category_match_ratio_limit": 75,
        "dry_run": False,
        "lcsc_upload_image": True,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def client(tmp_path):
    """提供 Flask test_client，并把 get_settings 固定到 tmp cache 目录。"""
    settings = _settings(tmp_path)
    with patch("lcsc2inv.web.get_settings", return_value=settings), app.test_client() as c:
        yield c


@pytest.fixture
def import_patches(capacitor):
    """patch 导入相关边界：fetcher / inventree api / writer。

    默认：`fetcher.fetch` 返回 capacitor；`writer.upsert_part` 返回
    `WriteResult(part_pk=7, created={"part": True})`。
    """
    fetcher = MagicMock()
    fetcher.fetch.return_value = capacitor
    api = MagicMock()
    writer = MagicMock()
    writer.upsert_part.return_value = WriteResult(part_pk=7, created={"part": True})
    with patch("lcsc2inv.web.default_fetcher", return_value=fetcher), \
            patch("lcsc2inv.web.build_inventree_api", return_value=api), \
            patch("lcsc2inv.web.InvenTreeWriter", return_value=writer):
        yield fetcher, api, writer


# ---------------------------------------------------------------------------
# 现有路由：GET /
# ---------------------------------------------------------------------------


class TestIndex:
    def test_index_renders(self, client):
        rv = client.get("/")
        assert rv.status_code == 200
        html = rv.get_data(as_text=True)
        assert "批量" in html or "import" in html.lower()


# ---------------------------------------------------------------------------
# 现有路由：POST /api/import
# ---------------------------------------------------------------------------


class TestImport:
    def test_success(self, client, import_patches):
        fetcher, _api, _writer = import_patches
        rv = client.post("/api/import", json={"code": "C28323"})
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["part_pk"] == 7
        assert data["part_url"] == "http://inventree.test/part/7/"
        fetcher.fetch.assert_called_once_with("C28323")

    def test_missing_code(self, client):
        rv = client.post("/api/import", json={})
        assert rv.status_code == 400
        assert rv.get_json()["ok"] is False

    def test_invalid_code(self, client):
        rv = client.post("/api/import", json={"code": "!!not-a-code!!"})
        assert rv.status_code == 400
        assert rv.get_json()["ok"] is False

    def test_fetch_error(self, client, import_patches):
        fetcher, _api, _writer = import_patches
        fetcher.fetch.side_effect = LcscFetchError("被限流")
        rv = client.post("/api/import", json={"code": "C28323"})
        assert rv.status_code == 502
        assert "抓取" in rv.get_json()["error"]

    def test_upsert_error_returns_502(self, client, import_patches):
        _fetcher, _api, writer = import_patches
        writer.upsert_part.return_value = WriteResult(errors=["写入失败"])
        rv = client.post("/api/import", json={"code": "C28323"})
        assert rv.status_code == 502
        assert rv.get_json()["ok"] is False

    def test_part_url_import_international(self, client, import_patches):
        fetcher, _api, _writer = import_patches
        url = "https://www.lcsc.com/product-detail/C28323.html"
        rv = client.post("/api/import", json={"code": url})
        assert rv.status_code == 200
        # /api/import 把原始输入交给 fetcher.fetch（内部再 parse_lcsc_code），
        # 保留原始 URL 以便 fetch 侧识别国内站 /mro/ 路径
        fetcher.fetch.assert_called_once_with(url)

    def test_part_url_import_cn(self, client, import_patches):
        fetcher, _api, _writer = import_patches
        url = "https://item.szlcsc.com/360864.html"
        rv = client.post("/api/import", json={"code": url})
        assert rv.status_code == 200
        fetcher.fetch.assert_called_once_with(url)

    def test_part_url_none_when_no_base(self, tmp_path, capacitor):
        settings = _settings(tmp_path, inventree_url="")
        fetcher = MagicMock()
        fetcher.fetch.return_value = capacitor
        writer = MagicMock()
        writer.upsert_part.return_value = WriteResult(part_pk=7)
        with patch("lcsc2inv.web.get_settings", return_value=settings), \
                patch("lcsc2inv.web.default_fetcher", return_value=fetcher), \
                patch("lcsc2inv.web.build_inventree_api", return_value=MagicMock()), \
                patch("lcsc2inv.web.InvenTreeWriter", return_value=writer):
            rv = app.test_client().post("/api/import", json={"code": "C28323"})
        assert rv.status_code == 200
        assert rv.get_json()["part_url"] is None

    def test_options_passthrough(self, client, import_patches):
        _fetcher, _api, writer = import_patches
        rv = client.post(
            "/api/import",
            json={
                "code": "C28323",
                "update": True,
                "stock": True,
                "qty": 5,
                "note": "hello",
                "create_missing_category": True,
            },
        )
        assert rv.status_code == 200
        call = writer.upsert_part.call_args
        opts = call.kwargs["options"]
        assert opts.update_existing is True
        assert opts.create_stock is True
        assert opts.quantity == 5
        assert opts.extra_note == "hello"
        assert opts.force_image_upload is True  # --update 语义
        assert call.kwargs["create_missing_category"] is True


# ---------------------------------------------------------------------------
# 现有路由：POST /api/batch（同步）
# ---------------------------------------------------------------------------


def _csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    fields = ["lcsc_code", "quantity", "note"]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: (r.get(k) or "") for k in fields})
    return buf.getvalue().encode("utf-8")


class TestBatch:
    def test_missing_file(self, client):
        rv = client.post("/api/batch", data={})
        assert rv.status_code == 400

    def test_empty_csv(self, client):
        rv = client.post(
            "/api/batch",
            data={"file": (io.BytesIO(b"lcsc_code\n\n"), "x.csv")},
            content_type="multipart/form-data",
        )
        assert rv.status_code == 400

    def test_success(self, client, import_patches):
        _fetcher, _api, _writer = import_patches
        rv = client.post(
            "/api/batch",
            data={"file": (io.BytesIO(_csv_bytes([{"lcsc_code": "C28323"},
                                                  {"lcsc_code": "C191386"}])), "p.csv")},
            content_type="multipart/form-data",
        )
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["total"] == 2
        assert data["ok_count"] == 2
        assert [r["code"] for r in data["results"]] == ["C28323", "C191386"]
        assert all(r["ok"] for r in data["results"])

    def test_row_fetch_error_flagged(self, client, import_patches, optoisolator):
        fetcher, _api, _writer = import_patches

        def _fetch(code):
            if code == "C9999":
                raise LcscFetchError("被限流")
            return optoisolator

        fetcher.fetch.side_effect = _fetch
        rv = client.post(
            "/api/batch",
            data={"file": (io.BytesIO(_csv_bytes([{"lcsc_code": "C28323"},
                                                  {"lcsc_code": "C9999"}])), "p.csv")},
            content_type="multipart/form-data",
        )
        data = rv.get_json()
        assert data["ok"] is True
        assert data["ok_count"] == 1
        results = {r["code"]: r for r in data["results"]}
        assert results["C9999"]["ok"] is False
        assert "抓取失败" in results["C9999"]["error"]


# ---------------------------------------------------------------------------
# 现有路由：GET /api/doctor
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_missing_url_returns_500(self, tmp_path):
        settings = _settings(tmp_path, inventree_url="", inventree_token="")
        with patch("lcsc2inv.web.get_settings", return_value=settings), \
                patch("lcsc2inv.web.build_inventree_api",
                      side_effect=ValueError("INVENTREE_URL 未设置")):
            rv = app.test_client().get("/api/doctor")
        assert rv.status_code == 500
        assert rv.get_json()["ok"] is False

    def test_ok(self, tmp_path):
        settings = _settings(tmp_path)
        api = MagicMock()
        stock_items = []
        for qty in (5, 2, 1.5):
            item = MagicMock()
            item.quantity = qty
            stock_items.append(item)
        with patch("lcsc2inv.web.get_settings", return_value=settings), \
                patch("lcsc2inv.web.build_inventree_api", return_value=api), \
                patch("inventree.part.Part.list", return_value=[]), \
                patch("inventree.company.Company.list", return_value=[]), \
                patch("inventree.stock.StockItem.list", return_value=stock_items):
            rv = app.test_client().get("/api/doctor")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        checks = {c["name"]: c["detail"] for c in data["checks"]}
        assert "INVENTREE_URL" in checks
        assert "Part 数量" in checks
        # 库存统计：条目数 3，总数量 5+2+1.5=8.5
        assert checks["库存条目数"] == "3"
        assert checks["库存总数量"] == "8.5"

    def test_stock_stats_failure_isolated(self, tmp_path):
        """库存统计失败只影响「库存统计」一项，不应拖垮其它检查。"""
        settings = _settings(tmp_path)
        api = MagicMock()
        with patch("lcsc2inv.web.get_settings", return_value=settings), \
                patch("lcsc2inv.web.build_inventree_api", return_value=api), \
                patch("inventree.part.Part.list", return_value=[]), \
                patch("inventree.company.Company.list", return_value=[]), \
                patch("inventree.stock.StockItem.list",
                      side_effect=RuntimeError("stock boom")):
            rv = app.test_client().get("/api/doctor")
        assert rv.status_code == 200
        data = rv.get_json()
        checks = {c["name"]: c for c in data["checks"]}
        assert checks["Part 数量"]["ok"] is True
        assert checks["库存统计"]["ok"] is False
        assert data["ok"] is False

    def test_api_exception_flagged(self, tmp_path):
        settings = _settings(tmp_path)
        api = MagicMock()
        with patch("lcsc2inv.web.get_settings", return_value=settings), \
                patch("lcsc2inv.web.build_inventree_api", return_value=api), \
                patch("inventree.part.Part.list",
                      side_effect=RuntimeError("conn refused")):
            rv = app.test_client().get("/api/doctor")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is False
        names = [c["name"] for c in data["checks"]]
        assert "InvenTree API 访问" in names


# ---------------------------------------------------------------------------
# 现有路由：GET /api/cache、POST /api/cache/clear
# ---------------------------------------------------------------------------


class TestCache:
    def _seed(self, tmp_path: Path, names: list[str]) -> None:
        for n in names:
            (tmp_path / n).write_text("{}", encoding="utf-8")

    def test_list_when_dir_missing(self, client, tmp_path):
        # cache 目录尚未创建
        rv = client.get("/api/cache")
        assert rv.status_code == 200
        assert rv.get_json()["codes"] == []

    def test_list_excludes_meta(self, client, tmp_path):
        self._seed(tmp_path, ["C2.json", "C1.json", "C1.meta.json"])
        rv = client.get("/api/cache")
        data = rv.get_json()
        assert data["codes"] == ["C1.json", "C2.json"]
        assert data["dir"] == str(tmp_path)

    def test_clear(self, client, tmp_path):
        self._seed(tmp_path, ["C1.json", "C2.json"])
        rv = client.post("/api/cache/clear")
        assert rv.status_code == 200
        assert rv.get_json()["cleared"] == 2
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# POST /api/preview —— 无写入预览
# ---------------------------------------------------------------------------


class TestPreview:
    """POST /api/preview：输入 `part`（序列化 LCSCPart）与可选的 `category_data` /
    `mapping_data`，返回类别匹配/参数映射/备注/关键词/回显 part；只读本地 config，
    绝不写 InvenTree（不抓取、不落库）。"""

    def test_preview_success(self, client, capacitor):
        rv = client.post("/api/preview", json={"part": capacitor.model_dump(mode="json")})
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert "category_path" in data["category"]
        assert "source" in data["category"] and "score" in data["category"]
        assert "candidates" in data["category"]
        assert "parameters" in data
        assert "notes" in data
        assert "keywords" in data
        assert data["part"]["sku"] == capacitor.sku

    def test_preview_missing_part(self, client):
        rv = client.post("/api/preview", json={})
        assert rv.status_code == 400
        assert "part" in rv.get_json()["error"]

    def test_preview_part_not_dict(self, client):
        rv = client.post("/api/preview", json={"part": "not-a-dict"})
        assert rv.status_code == 400

    def test_preview_invalid_part(self, client):
        # sku 必须是 str；非法则 pydantic 校验失败 → 400
        rv = client.post("/api/preview", json={"part": {"sku": 123}})
        assert rv.status_code == 400

    def test_preview_invalid_category_data(self, client, capacitor):
        rv = client.post(
            "/api/preview",
            json={"part": capacitor.model_dump(mode="json"), "category_data": "nope"},
        )
        assert rv.status_code == 400

    def test_preview_invalid_mapping_data(self, client, capacitor):
        rv = client.post(
            "/api/preview",
            json={"part": capacitor.model_dump(mode="json"), "mapping_data": 123},
        )
        assert rv.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/history、POST /api/history/clear —— 最近导入历史
# ---------------------------------------------------------------------------


class TestHistory:
    """导入历史持久化到缓存目录 `history.json`；GET 返回新→旧，POST 清空。"""

    def _write(self, tmp_path: Path, entries: list[dict]) -> None:
        (tmp_path / "history.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

    def test_history_read(self, client, tmp_path):
        self._write(
            tmp_path,
            [
                {"type": "single", "code": "C28323", "ok": True,
                 "summary": "part#7", "error": None,
                 "part_url": "http://inventree.test/part/7/", "ts": 1000.0},
                {"type": "single", "code": "C9999", "ok": False,
                 "summary": "boom", "error": "boom",
                 "part_url": None, "ts": 2000.0},
            ],
        )
        rv = client.get("/api/history")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["count"] == 2
        assert len(data["entries"]) == 2
        assert data["entries"][0]["code"] == "C9999"  # 新→旧

    def test_history_empty(self, client):
        rv = client.get("/api/history")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["entries"] == []

    def test_history_clear(self, client, tmp_path):
        self._write(tmp_path, [{"code": "C28323"}])
        rv = client.post("/api/history/clear")
        assert rv.status_code == 200
        assert rv.get_json()["cleared"] is True
        # 清空后文件仍保留，但内容为空列表
        assert json.loads((tmp_path / "history.json").read_text(encoding="utf-8")) == []

    def test_single_import_writes_history(self, client, import_patches, tmp_path):
        _fetcher, _api, _writer = import_patches
        rv = client.post("/api/import", json={"code": "C28323"})
        assert rv.status_code == 200
        p = tmp_path / "history.json"
        assert p.exists()
        entries = json.loads(p.read_text(encoding="utf-8"))
        assert entries[-1]["code"] == "C28323"
        assert entries[-1]["ok"] is True


# ---------------------------------------------------------------------------
# POST /api/batch/jobs、GET /api/batch/jobs/<id> —— 后台批量任务
# ---------------------------------------------------------------------------


class TestBatchJobs:
    """后台批量任务：JSON rows（或 CSV）提交返回 job_id（queued），worker 异步
    执行后轮询到终态（completed/failed），含逐条结果与 fail_count。"""

    def _wait_done(self, client, job_id: str, timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            rv = client.get(f"/api/batch/jobs/{job_id}")
            assert rv.status_code == 200
            body = rv.get_json()
            assert body["ok"] is True
            job = body["job"]
            if job["status"] in ("completed", "failed"):
                return job
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} 超时未完成")

    def test_submit_and_status(self, client, import_patches):
        _fetcher, _api, _writer = import_patches
        rv = client.post(
            "/api/batch/jobs",
            json={"rows": [{"lcsc_code": "C28323"}, {"lcsc_code": "C191386"}]},
        )
        assert rv.status_code == 200
        body = rv.get_json()
        assert body["ok"] is True
        assert body["status"] == "queued"
        assert body["job_id"]

        job = self._wait_done(client, body["job_id"])
        assert job["status"] == "completed"
        assert job["total"] == 2
        assert job["ok_count"] == 2
        assert job["fail_count"] == 0
        assert len(job["results"]) == 2

    def test_submit_via_csv(self, client, import_patches):
        _fetcher, _api, _writer = import_patches
        rv = client.post(
            "/api/batch/jobs",
            data={"file": (io.BytesIO(_csv_bytes([{"lcsc_code": "C28323"}])), "p.csv")},
            content_type="multipart/form-data",
        )
        assert rv.status_code == 200
        body = rv.get_json()
        assert body["ok"] is True
        job = self._wait_done(client, body["job_id"])
        assert job["status"] == "completed"
        assert job["total"] == 1
        assert job["ok_count"] == 1

    def test_row_failure_counted(self, client, import_patches):
        fetcher, _api, _writer = import_patches

        def _fetch(code):
            if code == "C9999":
                raise LcscFetchError("down")
            return MagicMock()

        fetcher.fetch.side_effect = _fetch
        rv = client.post(
            "/api/batch/jobs", json={"rows": [{"lcsc_code": "C9999"}]}
        )
        body = rv.get_json()
        job = self._wait_done(client, body["job_id"])
        assert job["status"] == "completed"
        assert job["total"] == 1
        assert job["ok_count"] == 0
        assert job["fail_count"] == 1
        assert job["results"][0]["ok"] is False

    def test_missing_rows(self, client):
        rv = client.post("/api/batch/jobs", json={})
        assert rv.status_code == 400
        assert rv.get_json()["ok"] is False

    def test_unknown_job_404(self, client):
        rv = client.get("/api/batch/jobs/does-not-exist")
        assert rv.status_code == 404
        assert rv.get_json()["ok"] is False


# ---------------------------------------------------------------------------
# InvenTree 数据库一键备份路由
# ---------------------------------------------------------------------------


class TestBackupRoutes:
    @pytest.fixture
    def bclient(self, tmp_path):
        """备份路由用独立的 Settings：storage 指向 tmp 目录，避免写 /backup。"""
        settings = _settings(
            tmp_path,
            inventree_backup_container="inventree-server",
            inventree_backup_storage=str(tmp_path),
        )
        with patch("lcsc2inv.web.get_settings", return_value=settings), \
                app.test_client() as c:
            yield c

    @pytest.fixture(autouse=True)
    def _reset_backup_state(self):
        """重置模块级备份状态，避免上一个用例遗留的后台线程污染下一个用例。"""
        from lcsc2inv import web as webmod

        with webmod._backup_lock:
            webmod._backup_job.update(
                {
                    "status": "idle",
                    "started_at": None,
                    "finished_at": None,
                    "message": "",
                    "error": "",
                    "result": None,
                }
            )
            webmod._backup_thread = None
        yield

    def _drain(self, bclient):
        """等待当前备份任务结束（用于回收遗留的后台线程）。"""
        for _ in range(50):
            time.sleep(0.05)
            if bclient.get("/api/backup").get_json()["status"] != "running":
                return

    def test_list_empty(self, bclient):
        rv = bclient.get("/api/backup")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["status"] == "idle"
        assert data["snapshots"] == []
        assert data["storage"]

    def test_start_and_complete(self, bclient):
        fake = {
            "snapshot": "20260926-120000",
            "files": ["backup.psql.gz", "media.zip"],
            "exit_code": 0,
            "output_tail": "ok",
        }
        # 后台线程异步执行，patch 必须覆盖到线程真正调用 run_inventree_backup 的时刻
        with patch("lcsc2inv.web.run_inventree_backup", return_value=fake) as m:
            rv = bclient.post("/api/backup")
            assert rv.status_code == 200
            data = rv.get_json()
            assert data["ok"] is True
            # 后台线程完成后再查一次（仍在 patch 作用域内）
            st = {}
            for _ in range(50):
                time.sleep(0.05)
                st = bclient.get("/api/backup").get_json()
                if st["status"] != "running":
                    break
            assert st["status"] == "completed"
            assert st["result"]["snapshot"] == "20260926-120000"
            assert m.call_count == 1

    def test_conflict_while_running(self, bclient):
        def slow(*_a, **_k):
            time.sleep(0.3)
            return {"snapshot": "x", "files": [], "exit_code": 0, "output_tail": ""}

        with patch("lcsc2inv.web.run_inventree_backup", side_effect=slow):
            bclient.post("/api/backup")
            rv = bclient.post("/api/backup")
            assert rv.status_code == 409
            assert rv.get_json()["ok"] is False
            self._drain(bclient)  # 等慢线程结束，避免污染下一个用例

    def test_failure_reported(self, bclient):
        def boom(*_a, **_k):
            raise RuntimeError("docker daemon 不可用")

        # patch 需覆盖后台线程实际执行 run_inventree_backup 的时刻
        with patch("lcsc2inv.web.run_inventree_backup", side_effect=boom):
            bclient.post("/api/backup")
            st = {}
            for _ in range(50):
                time.sleep(0.05)
                st = bclient.get("/api/backup").get_json()
                if st["status"] != "running":
                    break
            assert st["status"] == "failed"
            assert "docker daemon 不可用" in st["error"]

    def test_download_file(self, bclient, tmp_path):
        snap = tmp_path / "20260926-120000"
        snap.mkdir()
        (snap / "db.psql.gz").write_bytes(b"DBDATA")
        rv = bclient.get("/api/backup/20260926-120000/db.psql.gz")
        assert rv.status_code == 200
        assert rv.data == b"DBDATA"
        assert "attachment" in rv.headers.get("Content-Disposition", "")

    def test_download_blocked_outside(self, bclient):
        rv = bclient.get("/api/backup/..%2f..%2f/etc%2fpasswd")
        assert rv.status_code == 404

    # ---- 删除快照 -------------------------------------------------------

    def test_delete_snapshot(self, bclient, tmp_path):
        snap = tmp_path / "20260926-120000"
        (snap / "backup").mkdir(parents=True)
        (snap / "backup" / "db.psql.gz").write_bytes(b"x")
        rv = bclient.delete("/api/backup/20260926-120000")
        assert rv.status_code == 200
        assert rv.get_json()["ok"] is True
        assert not snap.exists()

    def test_delete_missing(self, bclient):
        rv = bclient.delete("/api/backup/20990101-000000")
        assert rv.status_code == 404
        assert rv.get_json()["ok"] is False

    def test_delete_traversal_blocked(self, bclient, tmp_path):
        outside = tmp_path.parent / "evil"
        outside.mkdir(exist_ok=True)
        try:
            (outside / "f.txt").write_text("x")
            rv = bclient.delete("/api/backup/..")
            assert rv.status_code == 400
            assert outside.exists()
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_delete_rejected_while_running(self, bclient):
        from lcsc2inv import web as webmod

        with webmod._backup_lock:
            webmod._backup_job["status"] = "running"
        try:
            rv = bclient.delete("/api/backup/20260926-120000")
            assert rv.status_code == 409
        finally:
            with webmod._backup_lock:
                webmod._backup_job["status"] = "idle"


# ---------------------------------------------------------------------------
# 现有路由：POST /api/stock/decrement（扫码扣减库存）
# ---------------------------------------------------------------------------


class TestStockDecrement:
    """扣减走 InvenTree 的 /api/stock/remove/ 动作（patch 掉 _inventree_request）。"""

    def _patch_request(self, status=200, result=None):
        return patch(
            "lcsc2inv.web._inventree_request",
            return_value=(status, result if result is not None else []),
        )

    def test_decrement_calls_remove_with_default_quantity(self, client):
        with self._patch_request() as m:
            rv = client.post("/api/stock/decrement", json={"stock_pk": 42})
        assert rv.status_code == 200
        assert rv.get_json()["ok"] is True
        method, path, payload = m.call_args.args
        assert method == "POST"
        assert path == "/api/stock/remove/"
        assert payload["items"] == [{"pk": 42, "quantity": "1"}]

    def test_decrement_custom_quantity(self, client):
        with self._patch_request() as m:
            rv = client.post("/api/stock/decrement", json={"stock_pk": 42, "quantity": 5})
        assert rv.status_code == 200
        assert m.call_args.args[2]["items"] == [{"pk": 42, "quantity": "5"}]

    def test_decrement_missing_params(self, client):
        rv = client.post("/api/stock/decrement", json={})
        assert rv.status_code == 400
        assert rv.get_json()["ok"] is False

    def test_decrement_bad_quantity(self, client):
        rv = client.post("/api/stock/decrement", json={"stock_pk": "abc", "quantity": 1})
        assert rv.status_code == 400

    def test_adjust_negative_quantity_uses_add(self, client):
        """负数数量 → 走 /api/stock/add/ 入库（绝对值生效）。"""
        with self._patch_request() as m:
            rv = client.post("/api/stock/decrement",
                             json={"stock_pk": 7, "quantity": -5})
        assert rv.status_code == 200
        method, path, payload = m.call_args.args
        assert path == "/api/stock/add/"
        assert payload["items"] == [{"pk": 7, "quantity": "5"}]

    def test_adjust_zero_rejected(self, client):
        rv = client.post("/api/stock/decrement",
                         json={"stock_pk": 7, "quantity": 0})
        assert rv.status_code == 400
        assert "不能为 0" in rv.get_json()["error"]

    def test_decrement_upstream_error_passed_through(self, client):
        with self._patch_request(status=400, result={"detail": "库存不足"}):
            rv = client.post("/api/stock/decrement", json={"stock_pk": 42})
        assert rv.status_code == 400
        assert rv.get_json()["ok"] is False


# ---------------------------------------------------------------------------
# 现有路由：GET /api/stock/<pk>/barcode-info、/api/stock/<pk>/qrcode.png
# ---------------------------------------------------------------------------


class TestStockBarcodeView:
    """条码查看页：库存信息 + 标准条码数据 + 服务端二维码 PNG。"""

    def test_barcode_info(self, client):
        item = {
            "pk": 7, "quantity": 12.5, "batch": "B1", "serial": None,
            "part_detail": {"name": "R100", "full_name": "R100 10k", "IPN": "C28323"},
            "location_detail": {"name": "A1-3", "pathstring": "仓库/A1-3"},
        }
        with patch("lcsc2inv.web._inventree_request", return_value=(200, item)):
            rv = client.get("/api/stock/7/barcode-info")
        assert rv.status_code == 200
        d = rv.get_json()
        assert d["ok"] is True
        assert d["barcode_data"] == '{"stockitem": 7}'
        assert d["part_name"] == "R100 10k"
        assert d["ipn"] == "C28323"
        assert d["location_name"] == "仓库/A1-3"
        assert d["qrcode_url"].endswith("/qrcode.png")
        assert d["stock_url"].endswith("/stock/item/7/")

    def test_barcode_info_upstream_error(self, client):
        with patch("lcsc2inv.web._inventree_request",
                   return_value=(404, {"detail": "Not found."})):
            rv = client.get("/api/stock/999/barcode-info")
        assert rv.status_code == 404
        assert rv.get_json()["ok"] is False


    def test_barcode_info_location_fallback(self, client):
        """location_detail 为 null 时，单独查货位接口补名字。"""
        item = {"pk": 8, "quantity": 1, "location": 223,
                "part_detail": {"name": "R1"}, "location_detail": None}

        def fake_request(method, path, payload=None):
            if path == "/api/stock/8/":
                return 200, item
            if path == "/api/stock/location/223/":
                return 200, {"pk": 223, "name": "A1-3", "pathstring": "仓库/A1-3"}
            return 404, {}

        with patch("lcsc2inv.web._inventree_request", side_effect=fake_request):
            rv = client.get("/api/stock/8/barcode-info")
        d = rv.get_json()
        assert d["ok"] is True
        assert d["location_name"] == "仓库/A1-3"

    def test_qrcode_png(self, client):
        """qrcode 端点只依赖 pk（标准条码数据），不需要 patch InvenTree。"""
        qrcode = pytest.importorskip("qrcode")
        rv = client.get("/api/stock/7/qrcode.png")
        assert rv.status_code == 200
        assert rv.mimetype == "image/png"
        assert rv.data[:4] == b"\x89PNG"
        assert len(rv.data) > 200


# ---------------------------------------------------------------------------
# 条码查看：绑定条码读取 + 通用二维码端点
# ---------------------------------------------------------------------------


class TestBoundBarcodeView:
    def test_barcode_info_includes_bound_from_cache(self, client, tmp_path):
        """缓存命中：绑定码来自缓存，不进容器。"""
        import json as json_mod
        from lcsc2inv.barcode_lookup import cache_path
        from lcsc2inv.config import Settings

        settings = Settings(lcsc_cache_dir=str(tmp_path))
        cache_path(settings).parent.mkdir(parents=True, exist_ok=True)
        cache_path(settings).write_text(
            json_mod.dumps({"updated_at": None, "entries": {"7": "X198516570"}}),
            encoding="utf-8",
        )
        item = {"pk": 7, "quantity": 1, "part_detail": {"name": "R1"},
                "location_detail": None}
        with patch("lcsc2inv.web._inventree_request", return_value=(200, item)), \
                patch("lcsc2inv.web.barcode_lookup.get_bound_barcodes") as m:
            rv = client.get("/api/stock/7/barcode-info")
        d = rv.get_json()
        assert d["ok"] is True
        assert d["bound_barcode"] == "X198516570"
        assert d["bound_from_cache"] is True
        assert "data=X198516570" in d["bound_qrcode_url"]
        m.assert_not_called()  # 绝不进容器

    def test_barcode_info_cache_miss_no_display(self, client, tmp_path):
        """缓存未命中：不显示绑定码，也绝不进容器查询。"""
        item = {"pk": 7, "quantity": 1, "part_detail": {"name": "R1"},
                "location_detail": None}
        with patch("lcsc2inv.web._inventree_request", return_value=(200, item)), \
                patch("lcsc2inv.web.barcode_lookup.get_bound_barcodes") as m:
            rv = client.get("/api/stock/7/barcode-info")
        d = rv.get_json()
        assert d["ok"] is True
        assert d["bound_barcode"] == ""
        assert d["bound_from_cache"] is False
        assert d["bound_qrcode_url"] is None
        m.assert_not_called()  # 缓存未命中也不回源

    def test_generic_qrcode_png(self, client):
        pytest.importorskip("qrcode")
        rv = client.get("/api/qrcode.png?data=X198516570")
        assert rv.status_code == 200
        assert rv.mimetype == "image/png"
        assert rv.data[:4] == b"\x89PNG"

    def test_generic_qrcode_requires_data(self, client):
        rv = client.get("/api/qrcode.png")
        assert rv.status_code == 400

    def test_generic_qrcode_rejects_long_data(self, client):
        rv = client.get("/api/qrcode.png?data=" + "x" * 2001)
        assert rv.status_code == 400


# ---------------------------------------------------------------------------
# 条码缓存：查看时缓存优先/懒加载回填 + 缓存概况/同步端点
# ---------------------------------------------------------------------------


class TestBarcodeCacheEndpoints:
    def _seed_cache(self, tmp_path, entries: dict):
        import json as json_mod
        from lcsc2inv.barcode_lookup import cache_path
        from lcsc2inv.config import Settings

        settings = Settings(lcsc_cache_dir=str(tmp_path))
        cache_path(settings).parent.mkdir(parents=True, exist_ok=True)
        cache_path(settings).write_text(
            json_mod.dumps({"updated_at": "2026-09-28T00:00:00+00:00",
                            "entries": {str(k): v for k, v in entries.items()}}),
            encoding="utf-8",
        )

    def test_barcode_info_uses_cache_without_exec(self, client, tmp_path):
        self._seed_cache(tmp_path, {7: "CACHED7"})
        item = {"pk": 7, "quantity": 1, "part_detail": {"name": "R1"},
                "location_detail": None}
        with patch("lcsc2inv.web._inventree_request", return_value=(200, item)), \
                patch("lcsc2inv.web.barcode_lookup.get_bound_barcodes") as m:
            rv = client.get("/api/stock/7/barcode-info")
        d = rv.get_json()
        assert d["ok"] is True
        assert d["bound_barcode"] == "CACHED7"
        assert d["bound_from_cache"] is True
        m.assert_not_called()  # 缓存命中，不应进容器

    def test_barcode_info_cache_miss_does_not_write_cache(self, client, tmp_path):
        """缓存未命中时不回写缓存（懒加载回填已移除，只靠「更新缓存」同步）。"""
        item = {"pk": 8, "quantity": 1, "part_detail": {"name": "R2"},
                "location_detail": None}
        with patch("lcsc2inv.web._inventree_request", return_value=(200, item)):
            rv = client.get("/api/stock/8/barcode-info")
        d = rv.get_json()
        assert d["bound_barcode"] == ""
        assert d["bound_from_cache"] is False
        from lcsc2inv.barcode_lookup import load_cache
        from lcsc2inv.config import Settings

        assert load_cache(Settings(lcsc_cache_dir=str(tmp_path))) == {}

    def test_cache_info_endpoint(self, client, tmp_path):
        self._seed_cache(tmp_path, {1: "A", 2: "B"})
        rv = client.get("/api/barcode/cache")
        d = rv.get_json()
        assert d["ok"] is True
        assert d["count"] == 2
        assert d["updated_at"] == "2026-09-28T00:00:00+00:00"

    def test_cache_sync_endpoint(self, client):
        report = {"added": [{"pk": 3, "code": "C3"}], "changed": [],
                  "added_count": 1, "changed_count": 0, "remote_count": 5,
                  "total_cached": 5, "duration_ms": 123}
        with patch("lcsc2inv.web.barcode_lookup.sync_cache", return_value=report):
            rv = client.post("/api/barcode/cache/sync")
        d = rv.get_json()
        assert d["ok"] is True
        assert d["report"]["added_count"] == 1

    def test_cache_sync_error_returns_502(self, client):
        with patch("lcsc2inv.web.barcode_lookup.sync_cache",
                   side_effect=RuntimeError("docker 不可用")):
            rv = client.post("/api/barcode/cache/sync")
        assert rv.status_code == 502
        assert "docker 不可用" in rv.get_json()["error"]


# ---------------------------------------------------------------------------
# 条码缓存：下载备份（JSON/CSV）+ 从备份恢复
# ---------------------------------------------------------------------------


class TestBarcodeCacheBackup:
    def _seed(self, tmp_path, entries: dict, updated_at=None):
        from lcsc2inv.barcode_lookup import cache_path
        from lcsc2inv.config import Settings

        settings = Settings(lcsc_cache_dir=str(tmp_path))
        cache_path(settings).parent.mkdir(parents=True, exist_ok=True)
        import json as json_mod

        cache_path(settings).write_text(
            json_mod.dumps({
                "updated_at": updated_at or "2026-09-28T00:00:00+00:00",
                "entries": {str(k): v for k, v in entries.items()},
            }),
            encoding="utf-8",
        )

    def test_download_json(self, client, tmp_path):
        self._seed(tmp_path, {10: "B10", 2: "A2"})
        rv = client.get("/api/barcode/cache/download?format=json")
        assert rv.status_code == 200
        assert "attachment" in rv.headers.get("Content-Disposition", "")
        assert rv.headers["Content-Disposition"].endswith(".json")
        d = json.loads(rv.get_data(as_text=True))
        assert d["updated_at"] == "2026-09-28T00:00:00+00:00"
        assert d["entries"] == {"2": "A2", "10": "B10"}  # 按 pk 排序

    def test_download_csv(self, client, tmp_path):
        self._seed(tmp_path, {10: "B10", 2: "A,2"})
        rv = client.get("/api/barcode/cache/download?format=csv")
        assert rv.status_code == 200
        assert rv.headers["Content-Disposition"].endswith(".csv")
        text = rv.get_data(as_text=True)
        assert text.startswith("\ufeff")  # BOM，Excel 识别 UTF-8
        lines = text.lstrip("\ufeff").strip().splitlines()
        assert lines[0] == "stock_pk,barcode_data"
        assert lines[1] == "2,\"A,2\""  # 含逗号自动加引号
        assert lines[2] == "10,B10"

    def test_download_empty_cache_404(self, client):
        rv = client.get("/api/barcode/cache/download")
        assert rv.status_code == 404
        assert "缓存为空" in rv.get_json()["error"]

    def test_restore_replaces_cache(self, client, tmp_path):
        self._seed(tmp_path, {99: "OLD"})
        backup_payload = {
            "updated_at": "2026-01-01T00:00:00+00:00",
            "entries": {"7": "X198516570", "8": "X23229957"},
        }
        rv = client.post(
            "/api/barcode/cache/restore",
            data={"file": (io.BytesIO(json.dumps(backup_payload).encode()),
                           "backup.json")},
            content_type="multipart/form-data",
        )
        assert rv.status_code == 200
        assert rv.get_json()["restored"] == 2
        from lcsc2inv.barcode_lookup import cache_info, load_cache
        from lcsc2inv.config import Settings

        settings = Settings(lcsc_cache_dir=str(tmp_path))
        assert load_cache(settings) == {7: "X198516570", 8: "X23229957"}
        # 恢复保留备份文件里的更新时间
        assert cache_info(settings)["updated_at"] == "2026-01-01T00:00:00+00:00"

    def test_restore_invalid_json_400(self, client):
        rv = client.post(
            "/api/barcode/cache/restore",
            data={"file": (io.BytesIO(b"not json"), "b.json")},
            content_type="multipart/form-data",
        )
        assert rv.status_code == 400

    def test_restore_bad_structure_400(self, client):
        rv = client.post(
            "/api/barcode/cache/restore",
            data={"file": (io.BytesIO(json.dumps({"foo": 1}).encode()), "b.json")},
            content_type="multipart/form-data",
        )
        assert rv.status_code == 400
        assert "entries" in rv.get_json()["error"]

    def test_restore_no_entries_400(self, client):
        rv = client.post(
            "/api/barcode/cache/restore",
            data={"file": (io.BytesIO(json.dumps({"entries": {}}).encode()), "b.json")},
            content_type="multipart/form-data",
        )
        assert rv.status_code == 400

    def test_restore_missing_file_400(self, client):
        rv = client.post("/api/barcode/cache/restore", data={})
        assert rv.status_code == 400


# ---------------------------------------------------------------------------
# 绑定自定义条码：POST /api/barcode/bind
# ---------------------------------------------------------------------------


class TestBarcodeBind:
    def test_bind_success_updates_cache(self, client, tmp_path):
        item_req = [("POST", "/api/barcode/link/")]
        with patch("lcsc2inv.web._inventree_request",
                   return_value=(200, {"success": "Barcode assigned"})) as m:
            rv = client.post("/api/barcode/bind",
                             json={"stock_pk": 7, "barcode": "NEWCODE1"})
        assert rv.status_code == 200
        d = rv.get_json()
        assert d["ok"] is True
        assert d["qrcode_url"].endswith("data=NEWCODE1")
        method, path, payload = m.call_args.args
        assert (method, path) == ("POST", "/api/barcode/link/")
        assert payload == {"barcode": "NEWCODE1", "stockitem": 7}
        # 本地缓存已同步
        from lcsc2inv.barcode_lookup import load_cache
        from lcsc2inv.config import Settings

        assert load_cache(Settings(lcsc_cache_dir=str(tmp_path))).get(7) == "NEWCODE1"

    def test_bind_missing_fields_400(self, client):
        rv = client.post("/api/barcode/bind", json={})
        assert rv.status_code == 400
        rv = client.post("/api/barcode/bind", json={"stock_pk": 7})
        assert rv.status_code == 400
        rv = client.post("/api/barcode/bind", json={"stock_pk": "abc", "barcode": "X"})
        assert rv.status_code == 400

    def test_bind_too_long_400(self, client):
        rv = client.post("/api/barcode/bind",
                         json={"stock_pk": 7, "barcode": "x" * 2001})
        assert rv.status_code == 400

    def test_bind_upstream_reject_keeps_cache(self, client, tmp_path):
        """InvenTree 拒绝（如条码已被其它库存占用）→ 错误透传，缓存不动。"""
        with patch("lcsc2inv.web._inventree_request",
                   return_value=(400, {"error": "Existing barcode found"})):
            rv = client.post("/api/barcode/bind",
                             json={"stock_pk": 7, "barcode": "DUP"})
        assert rv.status_code == 400
        assert "Existing barcode found" in json.dumps(rv.get_json()["error"])
        from lcsc2inv.barcode_lookup import load_cache
        from lcsc2inv.config import Settings

        assert load_cache(Settings(lcsc_cache_dir=str(tmp_path))) == {}

    def test_bind_network_error_502(self, client):
        import requests as requests_mod

        with patch("lcsc2inv.web._inventree_request",
                   side_effect=requests_mod.ConnectionError("timeout")):
            rv = client.post("/api/barcode/bind",
                             json={"stock_pk": 7, "barcode": "X"})
        assert rv.status_code == 502


# ---------------------------------------------------------------------------
# 更新现有 Part：POST /api/part/update
# ---------------------------------------------------------------------------


class TestPartUpdate:
    def test_success_passes_flags(self, client, import_patches):
        fetcher, _api, writer = import_patches
        writer.update_part_fields.return_value = {
            "updated_fields": ["description", "keywords"],
            "image": {"uploaded": True, "skipped_reason": None, "url": "http://x"},
            "parameters": None,
            "errors": [],
        }
        rv = client.post("/api/part/update", json={
            "part_pk": 1613, "code": "https://www.lcsc.com/product-detail/C28323.html",
            "name": True, "description": True, "image": True, "keywords": True,
            "notes": False, "parameters": False,
        })
        assert rv.status_code == 200
        d = rv.get_json()
        assert d["ok"] is True
        assert d["part_pk"] == 1613
        assert d["code"] == "C28323"  # URL 已规范化
        fetcher.fetch.assert_called_once_with("https://www.lcsc.com/product-detail/C28323.html")
        kwargs = writer.update_part_fields.call_args.kwargs
        assert kwargs["part_pk"] == 1613
        assert kwargs["update_name"] is True
        assert kwargs["update_description"] is True
        assert kwargs["update_notes"] is False
        assert kwargs["update_parameters"] is False

    def test_parameters_default_true(self, client, import_patches):
        """未勾选/未传 parameters 字段时，默认也写入参数。"""
        _fetcher, _api, writer = import_patches
        writer.update_part_fields.return_value = {
            "updated_fields": ["description"], "image": None,
            "parameters": {"written": True}, "errors": [],
        }
        rv = client.post("/api/part/update", json={"part_pk": 1, "code": "C28323"})
        assert rv.status_code == 200
        assert writer.update_part_fields.call_args.kwargs["update_parameters"] is True

    def test_missing_fields_400(self, client):
        rv = client.post("/api/part/update", json={})
        assert rv.status_code == 400
        rv = client.post("/api/part/update", json={"part_pk": 1})
        assert rv.status_code == 400
        rv = client.post("/api/part/update", json={"part_pk": "abc", "code": "C1"})
        assert rv.status_code == 400

    def test_invalid_code_400(self, client):
        rv = client.post("/api/part/update",
                         json={"part_pk": 1, "code": "!!bad!!"})
        assert rv.status_code == 400

    def test_fetch_error_502(self, client, import_patches):
        fetcher, _api, _writer = import_patches
        fetcher.fetch.side_effect = LcscFetchError("被限流")
        rv = client.post("/api/part/update", json={"part_pk": 1, "code": "C28323"})
        assert rv.status_code == 502

    def test_part_not_found_404(self, client, import_patches):
        _fetcher, _api, writer = import_patches
        writer.update_part_fields.side_effect = ValueError("Part pk=999 不存在")
        rv = client.post("/api/part/update", json={"part_pk": 999, "code": "C28323"})
        assert rv.status_code == 404
        assert "不存在" in rv.get_json()["error"]

    def test_partial_errors_502(self, client, import_patches):
        _fetcher, _api, writer = import_patches
        writer.update_part_fields.return_value = {
            "updated_fields": ["description"], "image": None, "parameters": None,
            "errors": ["参数写入失败: RuntimeError: boom"],
        }
        rv = client.post("/api/part/update", json={"part_pk": 1, "code": "C28323"})
        assert rv.status_code == 502
        d = rv.get_json()
        assert d["ok"] is False
        assert "boom" in d["errors"][0]


# ---------------------------------------------------------------------------
# 立创订单导入：POST /api/order/parse、/api/order/import
# ---------------------------------------------------------------------------


class TestOrderImport:
    def _xls_bytes(self):
        import io as _io
        import xlwt

        buf = _io.BytesIO()
        wb = xlwt.Workbook()
        sh = wb.add_sheet("订单")
        sh.write(1, 0, "订单编号：")
        sh.write(1, 1, "SO777")
        header = ["序号", "商品编号", "品牌", "厂家型号", "封装", "商品名称",
                  "订购数量（修改后）", "发货数量", "商品单价"]
        for c, h in enumerate(header):
            sh.write(17, c, h)
        sh.write(18, 0, 1)
        sh.write(18, 1, "C28323")
        sh.write(18, 2, "Yageo")
        sh.write(18, 3, "RC0603FR-0710KL")
        sh.write(18, 5, "10kΩ 1% 0603")
        sh.write(18, 6, "50个")
        sh.write(18, 8, "￥0.05/个")
        wb.save(buf)
        return buf.getvalue()

    def test_parse_matches_by_ipn(self, client, import_patches):
        fetcher, _api, writer = import_patches
        writer._find_part_by_ipn.return_value = 1613
        with patch("inventree.part.Part") as PartMock:
            PartMock.return_value.name = "10kΩ 0603"
            PartMock.return_value.in_stock = 120
            rv = client.post(
                "/api/order/parse",
                data={"file": (io.BytesIO(self._xls_bytes()), "order.xls")},
                content_type="multipart/form-data",
            )
        assert rv.status_code == 200
        d = rv.get_json()
        assert d["ok"] is True
        assert d["order_no"] == "SO777"
        assert d["counts"]["ipn"] == 1
        row = d["rows"][0]
        assert row["lcsc_code"] == "C28323"
        assert row["match"] == "ipn"
        assert row["part_pk"] == 1613
        assert row["part_name"] == "10kΩ 0603"
        assert row["stock_qty"] == 120

    def test_parse_missing_file_400(self, client):
        rv = client.post("/api/order/parse", data={})
        assert rv.status_code == 400

    def test_import_update_and_stock(self, client, import_patches):
        fetcher, _api, writer = import_patches
        writer.update_part_fields.return_value = {
            "updated_fields": ["description"], "image": None,
            "parameters": None, "errors": [],
        }
        writer.add_stock.return_value = 88
        rv = client.post("/api/order/import", json={
            "order_no": "SO777",
            "rows": [{"lcsc_code": "C28323", "quantity": 50,
                      "part_pk": 1613, "action": "update"}],
            "options": {"description": True, "image": True,
                        "stock": True, "location_pk": "186"},
        })
        assert rv.status_code == 200
        d = rv.get_json()
        assert d["ok"] is True and d["ok_count"] == 1
        kwargs = writer.update_part_fields.call_args.kwargs
        assert kwargs["part_pk"] == 1613
        stock_kwargs = writer.add_stock.call_args.kwargs
        assert stock_kwargs["quantity"] == 50
        assert stock_kwargs["location_pk"] == 186
        assert "SO777" in stock_kwargs["notes"]

    def test_import_create_missing(self, client, import_patches):
        _fetcher, _api, writer = import_patches
        from lcsc2inv.inventree_writer import WriteResult

        writer.upsert_part.return_value = WriteResult(part_pk=99, created={"part": True})
        writer.add_stock.return_value = 89
        rv = client.post("/api/order/import", json={
            "rows": [{"lcsc_code": "C9999999", "quantity": 3, "action": "create"}],
            "options": {"create_missing": True, "stock": True},
        })
        d = rv.get_json()
        assert d["ok"] is True
        assert d["results"][0]["part_pk"] == 99
        assert d["results"][0]["stock_pk"] == 89

    def test_import_skip_rows(self, client, import_patches):
        _fetcher, _api, writer = import_patches
        rv = client.post("/api/order/import", json={
            "rows": [{"lcsc_code": "C1", "action": "skip"}],
            "options": {},
        })
        d = rv.get_json()
        assert d["ok"] is True
        assert d["results"][0]["detail"] == "跳过"
        writer.update_part_fields.assert_not_called()

    def test_import_empty_rows_400(self, client):
        rv = client.post("/api/order/import", json={"rows": []})
        assert rv.status_code == 400

    def test_import_bad_location_400(self, client, import_patches):
        rv = client.post("/api/order/import", json={
            "rows": [{"lcsc_code": "C1", "action": "skip"}],
            "options": {"location_pk": "abc"},
        })
        assert rv.status_code == 400

    def test_footprint_passed_to_update_and_create(self, client, import_patches):
        """订单行解析出的「封装」要透传给 writer（描述后缀 / WriteOptions.footprint）。"""
        _fetcher, _api, writer = import_patches
        writer.update_part_fields.return_value = {
            "updated_fields": ["description"], "image": None,
            "parameters": None, "errors": [],
        }
        # update 路径
        rv = client.post("/api/order/import", json={
            "rows": [{"lcsc_code": "C28323", "quantity": 1, "part_pk": 5,
                      "action": "update", "footprint": "0805"}],
            "options": {"description": True, "image": False, "stock": False},
        })
        assert rv.status_code == 200
        assert writer.update_part_fields.call_args.kwargs["footprint"] == "0805"

        # create 路径
        rv = client.post("/api/order/import", json={
            "rows": [{"lcsc_code": "C9999999", "quantity": 1,
                      "action": "create", "footprint": "SOT-23"}],
            "options": {"create_missing": True, "stock": False},
        })
        assert rv.status_code == 200
        opts = writer.upsert_part.call_args.kwargs["options"]
        assert opts.footprint == "SOT-23"

    def test_footprint_empty_is_none(self, client, import_patches):
        """footprint 空串 → None，不产生空后缀。"""
        _fetcher, _api, writer = import_patches
        writer.update_part_fields.return_value = {
            "updated_fields": ["description"], "image": None,
            "parameters": None, "errors": [],
        }
        rv = client.post("/api/order/import", json={
            "rows": [{"lcsc_code": "C28323", "quantity": 1, "part_pk": 5,
                      "action": "update", "footprint": "  "}],
            "options": {"description": True, "image": False, "stock": False},
        })
        assert rv.status_code == 200
        assert writer.update_part_fields.call_args.kwargs["footprint"] is None
