"""快速入库模块测试。

覆盖 `lcsc2inv.quick_stock` 的核心函数和 `lcsc2inv.web` 的快速入库 API 端点：

- `find_part` — IPN/MPN/搜索匹配
- `get_stock_at_location` — 查询指定货位库存
- `add_or_merge_stock` — 合并/新建库存
- `list_locations` — 货位列表
- `GET /api/quick/locations` — 货位列表 API
- `POST /api/quick/lookup` — 智能匹配 API
- `POST /api/quick/stock` — 单条入库 API
- `POST /api/quick/batch` — 批量入库 API
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lcsc2inv.config import Settings
from lcsc2inv.inventree_writer import WriteResult
from lcsc2inv.lcsc_client import LcscFetchError
from lcsc2inv.quick_stock import (
    add_or_merge_stock,
    find_part,
    get_stock_at_location,
    list_locations,
)
from lcsc2inv.web import app

# ---------------------------------------------------------------------------
# 辅助：构造测试 Settings / 夹具
# ---------------------------------------------------------------------------


def _settings(tmp_path, **overrides) -> Settings:
    base = {
        "inventree_url": "http://inventree.test",
        "inventree_token": "t" * 40,
        "inventree_supplier_name": "LCSC Electronics",
        "lcsc_cache_enabled": True,
        "lcsc_cache_dir": str(tmp_path),
        "lcsc_cache_ttl_days": 7,
        "lcsc_request_interval": 0.0,
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
    settings = _settings(tmp_path)
    with patch("lcsc2inv.web.get_settings", return_value=settings), app.test_client() as c:
        yield c


@pytest.fixture
def import_patches(capacitor):
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
# quick_stock 模块单元测试
# ---------------------------------------------------------------------------


class TestFindPart:
    def test_find_by_ipn(self):
        api = MagicMock()
        mock_part = MagicMock()
        mock_part.pk = 123
        mock_part.name = "0805 10KΩ"
        mock_part.IPN = "C28323"
        mock_part.in_stock = 500
        mock_part.category = None

        with patch("lcsc2inv.quick_stock.Part") as MockPart, \
             patch("lcsc2inv.quick_stock.ManufacturerPart") as MockMfrPart:
            MockPart.search.return_value = [mock_part]
            MockMfrPart.list.return_value = []

            result = find_part(api, "C28323")

        assert result["found"] is True
        assert result["part_pk"] == 123
        assert result["match_type"] == "ipn"

    def test_not_found(self):
        api = MagicMock()
        with patch("lcsc2inv.quick_stock.Part") as MockPart, \
             patch("lcsc2inv.quick_stock.ManufacturerPart") as MockMfrPart:
            MockPart.search.return_value = []
            MockPart.list.return_value = []
            MockMfrPart.list.return_value = []

            result = find_part(api, "NOTEXIST")

        assert result["found"] is False
        assert result["part_pk"] is None


class TestGetStockAtLocation:
    def test_found(self):
        api = MagicMock()
        mock_item = MagicMock()
        mock_item.pk = 456
        mock_item.part = 123
        mock_item.location = 5
        mock_item.quantity = 100

        with patch("lcsc2inv.quick_stock.StockItem") as MockStockItem:
            MockStockItem.list.return_value = [mock_item]
            result = get_stock_at_location(api, 123, 5)

        assert result is not None
        assert result["stock_pk"] == 456
        assert result["quantity"] == 100.0

    def test_not_found(self):
        api = MagicMock()
        with patch("lcsc2inv.quick_stock.StockItem") as MockStockItem:
            MockStockItem.list.return_value = []
            result = get_stock_at_location(api, 123, 5)

        assert result is None


class TestAddOrMergeStock:
    def test_new_stock(self):
        api = MagicMock()
        mock_stock = MagicMock()
        mock_stock.pk = 789

        with patch("lcsc2inv.quick_stock.get_stock_at_location", return_value=None), \
             patch("lcsc2inv.quick_stock.StockItem") as MockStockItem:
            MockStockItem.create.return_value = mock_stock

            result = add_or_merge_stock(api, 123, 50, location_pk=5, merge=True)

        assert result["stock_pk"] == 789
        assert result["quantity"] == 50
        assert result["merged"] is False
        assert result["new_quantity"] == 50

    def test_merge_stock(self):
        api = MagicMock()
        existing = {"stock_pk": 456, "quantity": 100.0}
        mock_settings = MagicMock()
        mock_settings.inventree_url = "http://inventree.test"
        mock_settings.inventree_token = "t" * 40

        with patch("lcsc2inv.quick_stock.get_stock_at_location", return_value=existing), \
             patch("lcsc2inv.quick_stock.get_settings", return_value=mock_settings), \
             patch("lcsc2inv.quick_stock.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            result = add_or_merge_stock(api, 123, 50, location_pk=5, merge=True)

        assert result["stock_pk"] == 456
        assert result["quantity"] == 50
        assert result["merged"] is True
        assert result["new_quantity"] == 150.0


class TestListLocations:
    def test_list(self):
        api = MagicMock()
        mock_loc1 = MagicMock()
        mock_loc1.pk = 1
        mock_loc1.name = "仓库A"
        mock_loc1.pathstring = "仓库A"
        mock_loc2 = MagicMock()
        mock_loc2.pk = 2
        mock_loc2.name = "仓库B"
        mock_loc2.pathstring = "仓库B"

        with patch("lcsc2inv.quick_stock.StockLocation") as MockLoc:
            MockLoc.list.return_value = [mock_loc1, mock_loc2]
            result = list_locations(api)

        assert len(result) == 2
        assert result[0]["pk"] == 1
        assert result[0]["name"] == "仓库A"


# ---------------------------------------------------------------------------
# API 端点测试
# ---------------------------------------------------------------------------


class TestQuickLocations:
    def test_success(self, client):
        with patch("lcsc2inv.web.build_inventree_api") as mock_api, \
             patch("lcsc2inv.web.list_locations") as mock_list:
            mock_api.return_value = MagicMock()
            mock_list.return_value = [
                {"pk": 1, "name": "仓库A", "path": "仓库A"},
                {"pk": 2, "name": "仓库B", "path": "仓库B"},
            ]
            rv = client.get("/api/quick/locations")

        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert len(data["locations"]) == 2


class TestQuickLookup:
    def test_found(self, client):
        with patch("lcsc2inv.web.build_inventree_api") as mock_api, \
             patch("lcsc2inv.web.find_part") as mock_find, \
             patch("lcsc2inv.web.get_stock_at_location") as mock_stock:
            mock_api.return_value = MagicMock()
            mock_find.return_value = {
                "found": True,
                "part_pk": 123,
                "part_name": "0805 10KΩ",
                "ipn": "C28323",
                "mpn": "RC0805FR-0710KL",
                "category": "Passive/Resistors",
                "in_stock": 500,
                "match_type": "ipn",
            }
            mock_stock.return_value = {"stock_pk": 456, "quantity": 100}

            rv = client.post("/api/quick/lookup", json={"code": "C28323", "location_pk": 5})

        assert rv.status_code == 200
        data = rv.get_json()
        assert data["found"] is True
        assert data["part_pk"] == 123
        assert data["stock_at_location"]["quantity"] == 100

    def test_missing_code(self, client):
        rv = client.post("/api/quick/lookup", json={})
        assert rv.status_code == 400
        assert rv.get_json()["ok"] is False


class TestQuickStock:
    def test_success_existing_part(self, client, import_patches):
        fetcher, api, writer = import_patches
        with patch("lcsc2inv.web.find_part") as mock_find, \
             patch("lcsc2inv.web.add_or_merge_stock") as mock_add:
            mock_find.return_value = {
                "found": True,
                "part_pk": 123,
                "part_name": "0805 10KΩ",
                "ipn": "C28323",
                "mpn": None,
                "category": None,
                "in_stock": 500,
                "match_type": "ipn",
            }
            mock_add.return_value = {
                "stock_pk": 456,
                "quantity": 100,
                "merged": True,
                "new_quantity": 600,
            }

            rv = client.post("/api/quick/stock", json={
                "code": "C28323",
                "quantity": 100,
                "location_pk": 5,
                "merge": True,
            })

        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["part_pk"] == 123
        assert data["stock_pk"] == 456
        assert data["merged"] is True
        assert data["new_quantity"] == 600

    def test_auto_create_part(self, client, import_patches):
        fetcher, api, writer = import_patches
        with patch("lcsc2inv.web.find_part") as mock_find, \
             patch("lcsc2inv.web.add_or_merge_stock") as mock_add:
            mock_find.side_effect = [
                {"found": False, "part_pk": None, "part_name": None, "ipn": None,
                 "mpn": None, "category": None, "in_stock": None, "match_type": None},
                {"found": True, "part_pk": 7, "part_name": "Cap", "ipn": "C28323",
                 "mpn": None, "category": None, "in_stock": 0, "match_type": "ipn"},
            ]
            mock_add.return_value = {
                "stock_pk": 456,
                "quantity": 100,
                "merged": False,
                "new_quantity": 100,
            }

            rv = client.post("/api/quick/stock", json={
                "code": "C28323",
                "quantity": 100,
                "create_if_missing": True,
            })

        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["created_part"] is True

    def test_missing_code(self, client):
        rv = client.post("/api/quick/stock", json={"quantity": 100})
        assert rv.status_code == 400

    def test_missing_quantity(self, client):
        rv = client.post("/api/quick/stock", json={"code": "C28323"})
        assert rv.status_code == 400

    def test_invalid_quantity(self, client):
        rv = client.post("/api/quick/stock", json={"code": "C28323", "quantity": -5})
        assert rv.status_code == 400


class TestQuickBatch:
    def test_success(self, client, import_patches):
        fetcher, api, writer = import_patches
        with patch("lcsc2inv.web.find_part") as mock_find, \
             patch("lcsc2inv.web.add_or_merge_stock") as mock_add:
            mock_find.return_value = {
                "found": True,
                "part_pk": 123,
                "part_name": "0805 10KΩ",
                "ipn": "C28323",
                "mpn": None,
                "category": None,
                "in_stock": 500,
                "match_type": "ipn",
            }
            mock_add.return_value = {
                "stock_pk": 456,
                "quantity": 100,
                "merged": True,
                "new_quantity": 600,
            }

            rv = client.post("/api/quick/batch", json={
                "rows": [
                    {"code": "C28323", "quantity": 100},
                    {"code": "C191386", "quantity": 50},
                ],
                "location_pk": 5,
                "merge": True,
            })

        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["total"] == 2
        assert data["ok_count"] == 2

    def test_empty_rows(self, client):
        rv = client.post("/api/quick/batch", json={"rows": []})
        assert rv.status_code == 400

    def test_invalid_rows(self, client):
        rv = client.post("/api/quick/batch", json={"rows": "not a list"})
        assert rv.status_code == 400


class TestQuickSearch:
    def test_success(self, client):
        with patch("lcsc2inv.web.build_inventree_api") as mock_api, \
             patch("lcsc2inv.web.search_parts") as mock_search:
            mock_api.return_value = MagicMock()
            mock_search.return_value = [
                {"pk": 123, "name": "0805 10KΩ", "ipn": "C28323", "mpn": "RC0805FR-0710KL",
                 "category": "Passive/Resistors", "in_stock": 500},
                {"pk": 124, "name": "0805 100KΩ", "ipn": "C28324", "mpn": "RC0805FR-07100KL",
                 "category": "Passive/Resistors", "in_stock": 300},
            ]

            rv = client.post("/api/quick/search", json={"query": "10K", "limit": 10})

        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["count"] == 2
        assert len(data["results"]) == 2

    def test_missing_query(self, client):
        rv = client.post("/api/quick/search", json={})
        assert rv.status_code == 400

    def test_empty_query(self, client):
        rv = client.post("/api/quick/search", json={"query": ""})
        assert rv.status_code == 400

    def test_default_limit(self, client):
        with patch("lcsc2inv.web.build_inventree_api") as mock_api, \
             patch("lcsc2inv.web.search_parts") as mock_search:
            mock_api.return_value = MagicMock()
            mock_search.return_value = []

            rv = client.post("/api/quick/search", json={"query": "test"})

        assert rv.status_code == 200
        mock_search.assert_called_once_with(mock_api.return_value, "test", 20)
