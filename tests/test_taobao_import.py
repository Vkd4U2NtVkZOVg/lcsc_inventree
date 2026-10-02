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
        assert saved[0] == {"name": "新名称", "description": "新描述",
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
