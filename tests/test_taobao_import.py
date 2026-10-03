"""淘宝 mhtml 导入测试：解析模块（合成 MHTML）+ Web 端点（mock 边界）。"""

from __future__ import annotations

import io
import json
from email import encoders
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

import pytest

from lcsc2inv.inventree_writer import WriteResult
from lcsc2inv.lcsc_models import LCSCPart
from lcsc2inv.taobao_import import parse_mhtml, _image_ext
from lcsc2inv.web import app

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-data" * 8

MAIN_URL = "https://item.taobao.com/item.htm?id=111222333&skuId=999"

HTML_FULL = """<!doctype html><html><head><title>测试商品-淘宝网</title></head><body>
<div class="mainTitle--AbCdEf">测试电阻 0805 10kΩ</div>
<div class="shopName--ShOp1">测试店铺旗舰店</div>
<div class="highlightPrice--P1"><span class="symbol--S1">￥</span><span class="text--T1">2.6</span></div>
<div>
  <div class="valueItem--V1 isSelected--Sel hasImg--H">
    <span class="valueItemText--VT">50*50*18mm款</span>
    <img src="https://img.alicdn.com/img/sku1.jpg_.webp"></div>
  <div class="valueItem--V1 hasImg--H">
    <span class="valueItemText--VT">55*55*10mm款</span>
    <img src="https://img.alicdn.com/img/sku2.jpg_.webp"></div>
</div>
<div>
  <div class="generalParamsInfoItem--G">
    <span class="generalParamsInfoItemTitle--GT">品牌</span>
    <span class="generalParamsInfoItemSubTitle--GS">利冷科</span></div>
  <div class="generalParamsInfoItem--G">
    <span class="generalParamsInfoItemTitle--GT">型号</span>
    <span class="generalParamsInfoItemSubTitle--GS">01</span></div>
  <div class="emphasisParamsInfoItem--E">
    <span class="emphasisParamsInfoItemTitle--ET">10kg</span>
    <span class="emphasisParamsInfoItemSubTitle--ES">单层承重</span></div>
</div>
<div>
  <img class="thumbnailPic--TP" src="https://img.alicdn.com/img/g1.jpg_q50.jpg_.webp">
  <img class="thumbnailPic--TP" src="https://img.alicdn.com/img/g1.jpg_q50.jpg_.webp">
  <img class="thumbnailPic--TP" src="https://img.alicdn.com/img/g2.png_q50.jpg_.webp">
</div>
</body></html>"""

HTML_NO_SKU = HTML_FULL.replace(
    '<div class="valueItem--V1 isSelected--Sel hasImg--H">', "<div>"
).replace('<div class="valueItem--V1 hasImg--H">', "<div>").replace(
    "测试电阻 0805 10kΩ", ""  # 无 mainTitle → 回退 <title>
)


def _build_mhtml(html: str, url: str = MAIN_URL,
                 images: dict[str, bytes] | None = None) -> bytes:
    """手工构造 Blink 风格 MHTML（QP 主文档 + base64 内嵌图）。"""
    import base64 as _b64
    import quopri

    boundary = "----MultipartBoundary--TestBoundary----"
    head = "\r\n".join([
        "From: <Saved by Blink>",
        f"Snapshot-Content-Location: {url}",
        "MIME-Version: 1.0",
        f'Content-Type: multipart/related; type="text/html"; boundary="{boundary}"',
        "", "",
    ])
    body: list[str] = [
        f"--{boundary}",
        f"Content-Location: {url}",
        "Content-Type: text/html; charset=utf-8",
        "Content-Transfer-Encoding: quoted-printable",
        "",
        quopri.encodestring(html.encode("utf-8")).decode("ascii"),
    ]
    for loc, data in (images or {}).items():
        body += [
            f"--{boundary}",
            f"Content-Location: {loc}",
            "Content-Type: image/png",
            "Content-Transfer-Encoding: base64",
            "",
            _b64.b64encode(data).decode("ascii"),
        ]
    body.append(f"--{boundary}--")
    return (head + "\r\n".join(body) + "\r\n").encode("utf-8")

IMAGES = {
    "https://img.alicdn.com/img/sku1.jpg_.webp": PNG_BYTES,
    "https://img.alicdn.com/img/g1.jpg_q50.jpg_.webp": PNG_BYTES,
    "https://img.alicdn.com/img/g2.png_q50.jpg_.webp": PNG_BYTES,
}


# ---------------------------------------------------------------------------
# 解析模块
# ---------------------------------------------------------------------------


class TestParseMhtml:
    def test_full(self):
        it = parse_mhtml(_build_mhtml(HTML_FULL, images=IMAGES),
                         source_filename="01-test.mhtml")
        assert it.title == "测试电阻 0805 10kΩ"
        assert it.shop == "测试店铺旗舰店"
        assert it.price == 2.6
        assert it.item_id == "111222333"
        assert it.sku_id == "999"
        assert it.selected_sku == "50*50*18mm款"
        assert len(it.skus) == 2
        assert ("品牌", "利冷科") in it.params
        assert ("型号", "01") in it.params
        # 重点参数是反的：SubTitle=名称，Title=值
        assert ("单层承重", "10kg") in it.params
        # 图集去重保序
        assert it.gallery == ["https://img.alicdn.com/img/g1.jpg_q50.jpg_.webp",
                              "https://img.alicdn.com/img/g2.png_q50.jpg_.webp"]
        # 主图 = 选中 SKU 的图；字节从内嵌资源取
        assert it.main_image_url() == "https://img.alicdn.com/img/sku1.jpg_.webp"
        eb = it.embedded_bytes(it.main_image_url())
        assert eb is not None and eb[0] == PNG_BYTES and eb[1] == ".png"
        assert it.default_ipn() == "TB111222333"
        assert "50*50*18mm款" in it.default_description()

    def test_no_sku_and_title_fallback(self):
        it = parse_mhtml(_build_mhtml(HTML_NO_SKU, images=IMAGES))
        assert it.skus == []
        assert it.selected_sku is None
        # mainTitle 被清空 → 回退 <title> 并去掉「-淘宝网」
        assert it.title == "测试商品"
        assert it.main_image_url() == it.gallery[0]

    def test_image_bytes_missing_returns_none(self):
        it = parse_mhtml(_build_mhtml(HTML_FULL))  # 无内嵌图片
        assert it.embedded_bytes(it.main_image_url()) is None

    def test_invalid_file(self):
        with pytest.raises(ValueError):
            parse_mhtml(b"this is not mhtml")
        with pytest.raises(ValueError):
            parse_mhtml(b"")

    def test_image_ext_magic(self):
        assert _image_ext(PNG_BYTES, "x") == ".png"
        assert _image_ext(b"\xff\xd8\xff\xe0xxx", "x") == ".jpg"
        assert _image_ext(b"RIFF0000WEBPVP8X", "x") == ".webp"
        assert _image_ext(b"????", "a/b/c.jpeg") == ".jpeg"


# ---------------------------------------------------------------------------
# Web 端点
# ---------------------------------------------------------------------------


@pytest.fixture
def taobao_writer(tmp_path):
    """client + 淘宝导入相关 mock（settings/api/writer）。"""
    from tests.test_web import _settings

    settings = _settings(tmp_path)
    writer = MagicMock()
    writer.upsert_custom_part.return_value = WriteResult(
        part_pk=7, created={"part": True}, image_uploaded=True
    )
    with patch("lcsc2inv.web.get_settings", return_value=settings), \
            patch("lcsc2inv.web.build_inventree_api", return_value=MagicMock()), \
            patch("lcsc2inv.web.InvenTreeWriter", return_value=writer), \
            app.test_client() as c:
        yield c, writer


class TestTaobaoEndpoints:
    def _upload(self, client, html=HTML_FULL, images=None):
        data = _build_mhtml(html, images=IMAGES if images is None else images)
        return client.post(
            "/api/taobao/parse",
            data={"files": (io.BytesIO(data), "01-test.mhtml")},
            content_type="multipart/form-data",
        )

    def test_parse_ok(self, taobao_writer):
        client, _ = taobao_writer
        rv = self._upload(client)
        assert rv.status_code == 200
        d = rv.get_json()
        assert d["ok"] is True
        item = d["items"][0]
        assert item["ok"] is True
        assert item["title"] == "测试电阻 0805 10kΩ"
        assert item["shop"] == "测试店铺旗舰店"
        assert item["price"] == 2.6
        assert item["selected_sku"] == "50*50*18mm款"
        assert item["ipn"] == "TB111222333"
        assert item["image_available"] is True
        assert item["token"]

    def test_parse_bad_file(self, taobao_writer):
        client, _ = taobao_writer
        rv = client.post(
            "/api/taobao/parse",
            data={"files": (io.BytesIO(b"garbage"), "x.mhtml")},
            content_type="multipart/form-data",
        )
        assert rv.status_code == 200
        d = rv.get_json()
        assert d["ok"] is False
        assert d["items"][0]["ok"] is False

    def test_parse_no_file_400(self, taobao_writer):
        client, _ = taobao_writer
        rv = client.post("/api/taobao/parse", data={})
        assert rv.status_code == 400

    def test_import_creates_part(self, taobao_writer):
        client, writer = taobao_writer
        writer.upsert_custom_part.return_value = WriteResult(
            part_pk=7, created={"part": True}, image_uploaded=True, stock_item_pk=88,
        )
        token = self._upload(client).get_json()["items"][0]["token"]
        rv = client.post("/api/taobao/import", json={
            "rows": [{"token": token, "ipn": "TB111222333",
                      "name": "测试电阻", "price": "3.5", "qty": "5"}],
            "category_id": "12",
            "create_manufacturer": True,
            "location_id": "8",
        })
        assert rv.status_code == 200
        d = rv.get_json()
        assert d["ok"] is True and d["ok_count"] == 1
        kwargs = writer.upsert_custom_part.call_args.kwargs
        assert kwargs["ipn"] == "TB111222333"
        assert kwargs["name"] == "测试电阻"
        assert kwargs["price"] == 3.5
        assert kwargs["category_pk"] == 12
        assert kwargs["manufacturer_name"] == "利冷科"
        assert kwargs["parameters"].get("品牌") == "利冷科"
        assert kwargs["image_data"] is not None  # 内嵌图片字节
        assert kwargs["currency"] == "CNY"
        assert "淘宝" in kwargs["keywords"].split(",")
        # 库存创建参数
        assert kwargs["create_stock"] is True
        assert kwargs["quantity"] == 5
        assert kwargs["location_pk"] == 8
        out = d["results"][0]
        assert out["ok"] is True and out["part_pk"] == 7
        assert out["part_url"]
        assert out["stock_pk"] == 88
        assert out["stock_url"].endswith("/stock/item/88/")

    def test_import_qty_invalid_is_none(self, taobao_writer):
        client, writer = taobao_writer
        token = self._upload(client).get_json()["items"][0]["token"]
        rv = client.post("/api/taobao/import", json={
            "rows": [{"token": token, "qty": "abc"}],
        })
        assert rv.status_code == 200
        kwargs = writer.upsert_custom_part.call_args.kwargs
        assert kwargs["create_stock"] is False
        assert kwargs["quantity"] is None

    def test_import_binds_stock_barcode(self, taobao_writer):
        client, writer = taobao_writer
        writer.upsert_custom_part.return_value = WriteResult(
            part_pk=7, created={"part": True}, stock_item_pk=88,
        )
        token = self._upload(client).get_json()["items"][0]["token"]
        with patch("lcsc2inv.web._inventree_request",
                   return_value=(200, {})) as m_req:
            rv = client.post("/api/taobao/import", json={
                "rows": [{"token": token, "qty": "10",
                          "stock_barcode": "TB-BARCODE-001"}],
            })
        assert rv.status_code == 200
        d = rv.get_json()
        out = d["results"][0]
        assert out["stock_barcode_assigned"] is True
        assert out["stock_barcode"] == "TB-BARCODE-001"
        m_req.assert_called_once_with(
            "POST", "/api/barcode/link/",
            {"barcode": "TB-BARCODE-001", "stockitem": 88},
        )

    def test_import_barcode_binding_failure_reported(self, taobao_writer):
        client, writer = taobao_writer
        writer.upsert_custom_part.return_value = WriteResult(
            part_pk=7, created={"part": True}, stock_item_pk=88,
        )
        token = self._upload(client).get_json()["items"][0]["token"]
        with patch("lcsc2inv.web._inventree_request",
                   return_value=(400, {"error": "Existing barcode found"})):
            rv = client.post("/api/taobao/import", json={
                "rows": [{"token": token, "qty": "10",
                          "stock_barcode": "DUP"}],
            })
        out = rv.get_json()["results"][0]
        assert out["stock_barcode_assigned"] is False
        assert "Existing barcode found" in json.dumps(out["stock_barcode_error"])
        # 条码失败不影响整行成功
        assert out["ok"] is True

    def test_import_barcode_without_stock_rejected(self, taobao_writer):
        client, writer = taobao_writer
        token = self._upload(client).get_json()["items"][0]["token"]
        with patch("lcsc2inv.web._inventree_request") as m_req:
            rv = client.post("/api/taobao/import", json={
                "rows": [{"token": token, "stock_barcode": "X1"}],
            })
        out = rv.get_json()["results"][0]
        assert out["stock_barcode_assigned"] is False
        assert "未创建库存" in out["stock_barcode_error"]
        m_req.assert_not_called()

    def test_part_update_from_taobao_mhtml(self, taobao_writer):
        """单条导入页「更新现有 Part」的淘宝 mhtml 分支（multipart）。"""
        client, writer = taobao_writer
        writer.update_custom_part_fields.return_value = {
            "updated_fields": ["description", "keywords"],
            "image": {"uploaded": True, "skipped_reason": None, "url": None},
            "parameters": {"written": True},
            "errors": [],
        }
        data = _build_mhtml(HTML_FULL, images=IMAGES)
        rv = client.post(
            "/api/part/update",
            data={"part_pk": "1613", "description": "true", "parameters": "true",
                  "file": (io.BytesIO(data), "01-test.mhtml")},
            content_type="multipart/form-data",
        )
        assert rv.status_code == 200
        d = rv.get_json()
        assert d["ok"] is True and d["part_pk"] == 1613
        assert d["part_url"]
        kwargs = writer.update_custom_part_fields.call_args.kwargs
        assert kwargs["part_pk"] == 1613
        assert kwargs["name"] == "测试电阻 0805 10kΩ"
        assert kwargs["update_name"] is False          # 默认不动名称
        assert kwargs["update_description"] is True
        assert kwargs["update_parameters"] is True
        assert kwargs["image_data"] is not None        # 内嵌图片字节
        assert kwargs["parameters"].get("品牌") == "利冷科"

    def test_part_update_taobao_bad_pk_400(self, taobao_writer):
        client, _ = taobao_writer
        data = _build_mhtml(HTML_FULL)
        rv = client.post(
            "/api/part/update",
            data={"part_pk": "abc", "file": (io.BytesIO(data), "x.mhtml")},
            content_type="multipart/form-data",
        )
        assert rv.status_code == 400

    def test_part_update_taobao_bad_file_400(self, taobao_writer):
        client, _ = taobao_writer
        rv = client.post(
            "/api/part/update",
            data={"part_pk": "5", "file": (io.BytesIO(b"garbage"), "x.mhtml")},
            content_type="multipart/form-data",
        )
        assert rv.status_code == 400

    def test_part_update_taobao_part_missing_404(self, taobao_writer):
        client, writer = taobao_writer
        writer.update_custom_part_fields.side_effect = ValueError(
            "Part pk=999 不存在或无法访问"
        )
        data = _build_mhtml(HTML_FULL)
        rv = client.post(
            "/api/part/update",
            data={"part_pk": "999", "file": (io.BytesIO(data), "x.mhtml")},
            content_type="multipart/form-data",
        )
        assert rv.status_code == 404
        assert "不存在" in rv.get_json()["error"]

    def test_import_disk_fallback_after_eviction(self, taobao_writer):
        """内存 LRU 被驱逐/服务重启后，导入可从磁盘暂存重新解析。"""
        client, writer = taobao_writer
        token = self._upload(client).get_json()["items"][0]["token"]
        from lcsc2inv import web as web_mod

        # 原始 mhtml 已落盘（含元信息）
        assert (web_mod._taobao_disk_dir() / f"{token}.mhtml").exists()
        # 模拟内存驱逐
        with web_mod._taobao_cache_lock:
            web_mod._taobao_cache.clear()
        rv = client.post("/api/taobao/import", json={
            "rows": [{"token": token, "ipn": "TB111222333"}],
        })
        d = rv.get_json()
        assert d["results"][0]["ok"] is True
        kwargs = writer.upsert_custom_part.call_args.kwargs
        # 重新解析的内容与首次一致
        assert kwargs["name"] == "测试电阻 0805 10kΩ"

    def test_parse_sweeps_stale_disk_uploads(self, taobao_writer):
        """超过 TTL（48h）的磁盘暂存在下次解析时被清理。"""
        client, _ = taobao_writer
        import os as _os
        import time as _time

        from lcsc2inv import web as web_mod

        d = web_mod._taobao_disk_dir()
        old = d / "tb99990.mhtml"
        old.write_bytes(b"x")
        stale = _time.time() - 49 * 3600
        _os.utime(old, (stale, stale))
        fresh = d / "tb99991.mhtml"
        fresh.write_bytes(b"x")
        self._upload(client)
        assert not old.exists()
        assert fresh.exists()

    def test_import_skip_and_missing_token(self, taobao_writer):
        client, writer = taobao_writer
        rv = client.post("/api/taobao/import", json={
            "rows": [{"token": "tb999999", "action": "create"},
                     {"token": "x", "action": "skip"}],
        })
        d = rv.get_json()
        # token 无效 → 失败；skip → 成功跳过
        assert d["results"][0]["ok"] is False
        assert d["results"][1]["ok"] is True
        assert d["results"][1]["detail"] == "跳过"
        writer.upsert_custom_part.assert_not_called()

    def test_import_empty_rows_400(self, taobao_writer):
        client, _ = taobao_writer
        rv = client.post("/api/taobao/import", json={"rows": []})
        assert rv.status_code == 400

    def test_import_no_brand_no_manufacturer(self, taobao_writer):
        client, writer = taobao_writer
        html = HTML_FULL.replace(">利冷科<", ">无品牌<")
        token = self._upload(client, html=html).get_json()["items"][0]["token"]
        rv = client.post("/api/taobao/import", json={
            "rows": [{"token": token}], "create_manufacturer": True,
        })
        assert rv.status_code == 200
        kwargs = writer.upsert_custom_part.call_args.kwargs
        assert kwargs["manufacturer_name"] is None  # 「无品牌」不建厂商
        # 但品牌仍写为参数
        assert kwargs["parameters"].get("品牌") == "无品牌"


# ---------------------------------------------------------------------------
# writer 层：upsert_custom_part 的库存创建
# ---------------------------------------------------------------------------


class TestUpsertCustomPartStock:
    def _writer(self):
        from lcsc2inv.inventree_writer import InvenTreeWriter

        w = InvenTreeWriter.__new__(InvenTreeWriter)
        w.api = MagicMock()
        w.settings = MagicMock()
        w.supplier_name = "LCSC Electronics"
        w._mfr_cache, w._supplier_cache = {}, {}
        w._category_cache, w._template_cache = {}, {}
        return w

    def test_creates_stock_with_location(self):
        w = self._writer()
        stock_calls: list[dict] = []
        with patch.object(w, "_find_part_by_ipn", return_value=None), \
             patch.object(w, "_ensure_manufacturer", return_value=None), \
             patch.object(w, "_ensure_supplier", return_value=11), \
             patch.object(w, "_ensure_supplier_part", return_value=(22, True)), \
             patch.object(w, "_replace_price_breaks"), \
             patch.object(w, "_ensure_stock",
                          side_effect=lambda **kw: (stock_calls.append(kw), 99)[1]), \
             patch("lcsc2inv.inventree_writer.Part") as PartMock:
            PartMock.create.return_value = MagicMock(pk=7)
            r = w.upsert_custom_part(
                ipn="TB1", name="n", price=2.6, currency="CNY",
                create_stock=True, quantity=5, location_pk=8,
            )
        assert r.ok() is True
        assert r.stock_item_pk == 99
        assert stock_calls[0]["part_pk"] == 7
        assert stock_calls[0]["supplier_part_pk"] == 22
        assert stock_calls[0]["quantity"] == 5
        assert stock_calls[0]["location_pk"] == 8
        assert stock_calls[0]["price"] == 2.6

    def test_no_stock_without_qty(self):
        w = self._writer()
        with patch.object(w, "_find_part_by_ipn", return_value=None), \
             patch.object(w, "_ensure_manufacturer", return_value=None), \
             patch.object(w, "_ensure_supplier", return_value=11), \
             patch.object(w, "_ensure_supplier_part", return_value=(22, True)), \
             patch.object(w, "_replace_price_breaks"), \
             patch.object(w, "_ensure_stock") as m_stock, \
             patch("lcsc2inv.inventree_writer.Part") as PartMock:
            PartMock.create.return_value = MagicMock(pk=7)
            r = w.upsert_custom_part(
                ipn="TB1", name="n", create_stock=True, quantity=None,
            )
        assert r.ok() is True
        assert r.stock_item_pk is None
        m_stock.assert_not_called()


# ---------------------------------------------------------------------------
# writer 层：update_custom_part_fields（淘宝数据刷新已有 Part）
# ---------------------------------------------------------------------------


class TestUpdateCustomPartFields:
    def _writer(self):
        from lcsc2inv.inventree_writer import InvenTreeWriter

        w = InvenTreeWriter.__new__(InvenTreeWriter)
        w.api = MagicMock()
        w.settings = MagicMock()
        w.supplier_name = "LCSC Electronics"
        w._mfr_cache, w._supplier_cache = {}, {}
        w._category_cache, w._template_cache = {}, {}
        return w

    def test_updates_chosen_fields_params_and_image(self):
        w = self._writer()
        saved: list[dict] = []

        class FakePart:
            def __init__(self, api, pk):
                self.pk = pk
            def save(self, payload):
                saved.append(payload)

        with patch("lcsc2inv.inventree_writer.Part", FakePart), \
             patch.object(w, "_upload_image_bytes",
                          return_value=(True, None)) as m_img, \
             patch.object(w, "set_named_parameter",
                          return_value=True) as m_param:
            r = w.update_custom_part_fields(
                part_pk=5, name="新名称", description="新描述", notes="备注",
                keywords="淘宝,店", link="https://item.taobao.com/x",
                parameters={"品牌": "XF"}, image_data=(PNG_BYTES, ".png"),
                update_name=True, update_notes=True, update_parameters=True,
            )
        assert saved[0] == {"name": "新名称", "description": "新描述 [参数: XF]",
                            "keywords": "淘宝,店", "notes": "备注",
                            "link": "https://item.taobao.com/x"}
        assert r["updated_fields"] == sorted(saved[0].keys())
        assert r["image"] == {"uploaded": True, "skipped_reason": None, "url": None}
        m_img.assert_called_once_with(part_pk=5, data=PNG_BYTES, ext=".png")
        m_param.assert_called_once_with(part_pk=5, name="品牌", value="XF")
        assert r["parameters"] == {"written": True}

    def test_part_missing_raises_valueerror(self):
        w = self._writer()

        class Boom:
            def __init__(self, api, pk):
                raise RuntimeError("no such part")

        with patch("lcsc2inv.inventree_writer.Part", Boom):
            with pytest.raises(ValueError, match="不存在"):
                w.update_custom_part_fields(part_pk=1, update_description=False)

    def test_image_disabled_and_no_params(self):
        w = self._writer()
        saved: list[dict] = []

        class FakePart:
            def __init__(self, api, pk):
                self.pk = pk
            def save(self, payload):
                saved.append(payload)

        with patch("lcsc2inv.inventree_writer.Part", FakePart):
            r = w.update_custom_part_fields(
                part_pk=5, description="只改描述", update_image=False,
            )
        assert saved[0] == {"description": "只改描述"}
        assert r["image"] is None
        assert r["parameters"] is None
        assert r["errors"] == []


# ---------------------------------------------------------------------------
# 参数名/值清洗截断（InvenTree 限模板名 100、值 500）
# ---------------------------------------------------------------------------


def _writer_new_api():
    """构造跳过端点探测的 writer（new API 风格，REST 可注入）。"""
    from lcsc2inv.inventree_writer import InvenTreeWriter

    w = InvenTreeWriter.__new__(InvenTreeWriter)
    w.api = MagicMock()
    w.settings = MagicMock()
    w.supplier_name = "LCSC Electronics"
    w._mfr_cache, w._supplier_cache = {}, {}
    w._category_cache, w._template_cache = {}, {}
    w._param_style = "new"
    return w


class _RestStub:
    """替换 writer._rest：按序回放 (status, data)，并记录调用。"""

    def __init__(self, responses):
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


class TestParamSanitize:
    def test_long_value_truncated_to_500(self):
        w = _writer_new_api()
        rest = _RestStub([
            (200, [{"pk": 7, "name": "颜色分类"}]),
            (200, []),
            (201, {"pk": 9}),
        ])
        w._rest = rest
        long_value = "颜" * 600
        assert w.set_named_parameter(part_pk=1, name="颜色分类",
                                     value=long_value) is True
        posts = rest.of("POST", "/api/parameter/")
        assert len(posts[0]["json"]["data"]) == 500

    def test_long_name_truncated_to_100(self):
        w = _writer_new_api()
        rest = _RestStub([
            (200, []),            # 模板列表：不存在
            (201, {"pk": 7}),     # 创建模板
            (200, []),            # 无参数
            (201, {"pk": 9}),
        ])
        w._rest = rest
        assert w.set_named_parameter(part_pk=1, name="名" * 150, value="v") is True
        tpl = rest.of("POST", "/api/parameter/template/")[0]["json"]
        assert len(tpl["name"]) == 100

    def test_value_strips_carriage_returns(self):
        w = _writer_new_api()
        rest = _RestStub([
            (200, [{"pk": 7, "name": "T"}]),
            (200, []),
            (201, {"pk": 9}),
        ])
        w._rest = rest
        w.set_named_parameter(part_pk=1, name="T", value="a\r\nb\r\n")
        posts = rest.of("POST", "/api/parameter/")
        assert posts[0]["json"]["data"] == "a\nb"

    def test_empty_name_returns_false_without_api_calls(self):
        w = _writer_new_api()
        rest = _RestStub([])
        w._rest = rest
        assert w.set_named_parameter(part_pk=1, name="  ", value="v") is False
        assert rest.calls == []


class TestUpsertCustomPartDescSuffix:
    def test_description_includes_params(self):
        """upsert_custom_part 创建时参数值并入描述（可搜索）。"""
        from lcsc2inv.inventree_writer import InvenTreeWriter

        w = InvenTreeWriter.__new__(InvenTreeWriter)
        w.api = MagicMock()
        w.settings = MagicMock()
        w.supplier_name = "LCSC Electronics"
        w._mfr_cache, w._supplier_cache = {}, {}
        w._category_cache, w._template_cache = {}, {}
        with patch.object(w, "_find_part_by_ipn", return_value=None), \
             patch.object(w, "_ensure_manufacturer", return_value=None), \
             patch.object(w, "_ensure_supplier", return_value=11), \
             patch.object(w, "_ensure_supplier_part", return_value=(22, True)), \
             patch.object(w, "_replace_price_breaks"), \
             patch.object(w, "set_named_parameter", return_value=True), \
             patch("lcsc2inv.inventree_writer.Part") as PartMock:
            PartMock.create.return_value = MagicMock(pk=7)
            r = w.upsert_custom_part(
                ipn="TB1", name="麦片", description="降噪硅麦",
                parameters={"品牌": "XF", "型号": "3729"},
                image_data=None,
            )
        assert r.ok() is True
        payload = PartMock.create.call_args.args[1]
        assert payload["description"] == "降噪硅麦 [参数: XF | 3729]"


class TestUpdateRefetchOnCnData:
    def test_cn_page_data_refetches_intl_via_keywords(self):
        """更新时抓到无参数的国内站数据 → 按 keywords 首段 C-code 补抓国际站。"""
        from types import SimpleNamespace as NS

        from lcsc2inv.inventree_writer import InvenTreeWriter
        from lcsc2inv.lcsc_models import PropertyValue

        w = InvenTreeWriter.__new__(InvenTreeWriter)
        w.api = MagicMock()
        w.settings = MagicMock()
        w.supplier_name = "LCSC Electronics"
        w._mfr_cache, w._supplier_cache = {}, {}
        w._category_cache, w._template_cache = {}, {}

        cn_part = LCSCPart(
            sku="CN:44240",
            description="0402WGF2433TCE贴片电阻，价格￥0.0051元。",
            additional_properties=[PropertyValue(name="封装", value="0402")],
        )  # 国内站：ld+json 无参数表，仅解析器注入的封装一条
        intl_part = LCSCPart(
            sku="C43249",
            additional_properties=[
                PropertyValue(name="Resistance", value="243kΩ"),
                PropertyValue(name="Tolerance", value="±1%"),
            ],
        )  # 国际站参数更全（2 条 > 国内站 1 条）才会被采用
        saved: list[dict] = []

        class FakePart:
            def __init__(self, api, pk):
                self.pk = pk
                self.keywords = "C43249,0402WGF2433TCE,UNI-ROYAL"
            def save(self, payload):
                saved.append(payload)

        fetcher = MagicMock()
        fetcher.fetch.return_value = intl_part

        with patch("lcsc2inv.inventree_writer.Part", FakePart), \
             patch("lcsc2inv.inventree_writer.categorizer_match",
                   return_value=NS(category_path="Passive/Resistors/X")) as m_cat, \
             patch("lcsc2inv.inventree_writer.to_inventree_parameters",
                   return_value={"Value": {"value": "243k", "raw": "243kΩ"}}) as m_map, \
             patch("lcsc2inv.inventree_writer.default_fetcher",
                   return_value=fetcher) as m_df:
            w.update_part_fields(
                cn_part, part_pk=1462, update_description=True,
                update_image=False, update_keywords=False,
                fetcher=None,  # 无 fetcher → 应自动建一个补抓
            )
        # 用 keywords 首段 C43249 补抓（CN 数据只有 1 条注入的封装属性）
        fetcher.fetch.assert_called_once_with("C43249")
        assert m_df.called
        # 参数映射用补抓结果；描述正文保留本次数据源的内容
        assert m_cat.call_args.args[0] is intl_part
        assert m_map.call_args.args[0] is intl_part
        assert "0402WGF2433TCE贴片电阻" in saved[0]["description"]
        assert "[参数: 243k]" in saved[0]["description"]


class TestUpdateAntiDowngrade:
    """首次更新遇 LCSC 限流：补抓重试 + 不用残缺参数覆盖描述。"""

    def _writer_with_keywords_part(self):
        from lcsc2inv.inventree_writer import InvenTreeWriter

        w = InvenTreeWriter.__new__(InvenTreeWriter)
        w.api = MagicMock()
        w.settings = MagicMock()
        w.supplier_name = "LCSC Electronics"
        w._mfr_cache, w._supplier_cache = {}, {}
        w._category_cache, w._template_cache = {}, {}
        return w

    def test_refetch_retries_then_succeeds(self):
        from types import SimpleNamespace as NS

        from lcsc2inv.lcsc_client import LcscFetchError
        from lcsc2inv.lcsc_models import PropertyValue

        w = self._writer_with_keywords_part()
        cn_part = LCSCPart(
            sku="CN:44240",
            additional_properties=[PropertyValue(name="封装", value="0402")],
        )
        intl_part = LCSCPart(
            sku="C43249",
            additional_properties=[
                PropertyValue(name="Resistance", value="243kΩ"),
                PropertyValue(name="Tolerance", value="±1%"),
            ],
        )
        saved: list[dict] = []

        class FakePart:
            def __init__(self, api, pk):
                self.pk = pk
                self.keywords = "C43249,MPN,品牌"
                self.description = "旧描述 [参数: 243k | ±1%]"
            def save(self, payload):
                saved.append(payload)

        fetcher = MagicMock()
        # 前两次限流，第三次成功
        fetcher.fetch.side_effect = [
            LcscFetchError("被限流 HTTP 403"),
            LcscFetchError("被限流 HTTP 403"),
            intl_part,
        ]

        with patch("lcsc2inv.inventree_writer.Part", FakePart), \
             patch("lcsc2inv.inventree_writer.categorizer_match",
                   return_value=NS(category_path="Passive/Resistors/X")), \
             patch("lcsc2inv.inventree_writer.to_inventree_parameters",
                   return_value={"Value": {"value": "243k", "raw": "243kΩ"}}), \
             patch("lcsc2inv.inventree_writer.time.sleep"), \
             patch("lcsc2inv.inventree_writer.default_fetcher",
                   return_value=fetcher):
            w.update_part_fields(
                cn_part, part_pk=1, update_description=True, update_image=False,
            )
        assert fetcher.fetch.call_count == 3  # 重试后成功
        # 成功拿到完整参数 → 描述正常更新
        assert "[参数: 243k]" in saved[0]["description"]

    def test_no_downgrade_when_refetch_fails(self):
        """补抓始终失败 → 保留原描述（参数段更丰富），不用残缺数据覆盖。"""
        from types import SimpleNamespace as NS

        from lcsc2inv.lcsc_client import LcscFetchError
        from lcsc2inv.lcsc_models import PropertyValue

        w = self._writer_with_keywords_part()
        cn_part = LCSCPart(
            sku="CN:44240",
            additional_properties=[PropertyValue(name="封装", value="0402")],
        )
        saved: list[dict] = []

        class FakePart:
            def __init__(self, api, pk):
                self.pk = pk
                self.keywords = "C43249,MPN,品牌"
                # 现有描述参数段有 2 个值
                self.description = "旧描述 [参数: 243k | ±1%]"
            def save(self, payload):
                saved.append(payload)

        fetcher = MagicMock()
        fetcher.fetch.side_effect = LcscFetchError("被限流 HTTP 403")

        with patch("lcsc2inv.inventree_writer.Part", FakePart), \
             patch("lcsc2inv.inventree_writer.categorizer_match",
                   return_value=NS(category_path="__uncategorized__")), \
             patch("lcsc2inv.inventree_writer.to_inventree_parameters",
                   return_value={}), \
             patch("lcsc2inv.inventree_writer.time.sleep"), \
             patch("lcsc2inv.inventree_writer.default_fetcher",
                   return_value=fetcher):
            w.update_part_fields(
                cn_part, part_pk=1, update_description=True, update_image=False,
            )
        # 描述未被降级覆盖
        assert saved == [] or "description" not in saved[0]
        assert fetcher.fetch.call_count == 3  # 重试了 3 次


class TestImportRefetchIntl:
    def test_import_cn_part_refetches_params(self):
        """国内站链接新导入：自动按 sku（C-code）补抓国际站参数。"""
        from lcsc2inv.inventree_writer import InvenTreeWriter, WriteOptions
        from lcsc2inv.lcsc_models import PropertyValue

        w = _writer_new_api()
        cn_part = LCSCPart(
            sku="C2998180",
            description="FRC0402F3571TS贴片电阻，价格￥0.0064元。",
            additional_properties=[PropertyValue(name="封装", value="0402")],
        )
        intl_part = LCSCPart(
            sku="C2998180",
            category="Resistors/Chip Resistor - Surface Mount",
            additional_properties=[
                PropertyValue(name="Package", value="0402"),
                PropertyValue(name="Resistance", value="3.57kΩ"),
                PropertyValue(name="Tolerance", value="±1%"),
            ],
        )
        fetcher = MagicMock()
        # 首次抓取（国内站）发生在 upsert_part 之前；此处补抓直接返回国际站数据
        fetcher.fetch.side_effect = [intl_part]

        wp_calls: list = []

        def _fake_wp(*a, **kw):
            wp_calls.append(kw.get("mapped"))

        param_written: list[tuple[str, str]] = []

        def _fake_set(self, *, part_pk, name, value):
            param_written.append((name, value))
            return True

        with patch.object(w, "_find_part_by_ipn", return_value=None), \
             patch.object(w, "_ensure_category", return_value=7), \
             patch.object(w, "_ensure_manufacturer", return_value=None), \
             patch.object(w, "_ensure_supplier", return_value=11), \
             patch.object(w, "_ensure_supplier_part", return_value=(22, True)), \
             patch.object(w, "_replace_price_breaks"), \
             patch.object(w, "_write_parameters", _fake_wp), \
             patch.object(InvenTreeWriter, "set_named_parameter", _fake_set), \
             patch("lcsc2inv.inventree_writer.Part") as PartMock:
            PartMock.create.return_value = MagicMock(pk=99)
            r = w.upsert_part(
                cn_part, options=WriteOptions(fetcher=fetcher),
            )
        assert r.ok() is True, r.errors
        # 补抓了一次（用 sku 的 C-code）
        fetcher.fetch.assert_called_once_with("C2998180")
        # 描述含国际站参数值
        payload = PartMock.create.call_args.args[1]
        assert "[参数: 3.57k | ±1% | 0402]" in payload["description"]
        # _write_parameters 收到含 Value 的映射
        assert wp_calls and any(
            m and m.get("Value", {}).get("value") == "3.57k" for m in wp_calls
        )
