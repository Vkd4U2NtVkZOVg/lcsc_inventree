"""图片下载 + 上传相关测试（全部离线，使用 mock 不打真实网络）。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from lcsc2inv.config import Settings, get_settings
from lcsc2inv.inventree_writer import WriteOptions, WriteResult
from lcsc2inv.lcsc_client import Fetcher, LcscFetchError, default_fetcher
from lcsc2inv.lcsc_models import LCSCPart


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_part_with_image() -> LCSCPart:
    """构造一个 LCSCPart，含 image_urls[0] = http://example.com/C999.jpg。"""
    # 注：故意写死 url，不用真实的 assets.lcsc.com（避免意外发请求）
    return LCSCPart(
        sku="C999999",
        mpn="MOCK-MPN",
        name="Mock part for image tests",
        description="Mock",
        image_urls=["http://example.com/test/C999999_front.jpg"],
    )


def _make_settings(tmp_path: Path, **overrides) -> Settings:
    """用 tmp_path 作 cache_dir，避免污染用户目录。"""
    defaults = dict(
        inventree_url="http://localhost",
        inventree_token="x" * 40,
        inventree_supplier_name="LCSC Electronics",
        lcsc_cache_enabled=True,
        lcsc_cache_dir=str(tmp_path),
        lcsc_cache_ttl_days=7,
        lcsc_request_interval=0.0,  # 测 throttle 时取消等待
        lcsc_max_retries=2,
        lcsc_user_agent="test",
        category_match_ratio_limit=75,
        dry_run=False,
        lcsc_upload_image=True,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_part_no_image() -> LCSCPart:
    """构造一个 LCSCPart，没有 image_urls。"""
    return LCSCPart(
        sku="C999998",
        mpn="MOCK-MPN",
        name="Mock no-image part",
        image_urls=[],
    )


# ---------------------------------------------------------------------------
# _upload_image 行为测试（mock Part + mock Fetcher）
# ---------------------------------------------------------------------------


class TestUploadImageGating:
    """验证 _upload_image 的开关 / 幂等 / 强制重传 逻辑。"""

    def _writer(self, settings: Settings) -> MagicMock:
        """构造一个最小可用的 writer mock（只保留 _upload_image 需要的字段）。"""
        w = MagicMock()
        w.settings = settings
        return w

    def test_disabled_setting(self, tmp_path: Path):
        """lcsc_upload_image=false → 不上传，记 disabled。"""
        from lcsc2inv.inventree_writer import InvenTreeWriter

        settings = _make_settings(tmp_path, lcsc_upload_image=False)
        writer = InvenTreeWriter.__new__(InvenTreeWriter)
        writer.settings = settings
        result = WriteResult()
        part = _make_part_with_image()

        writer._upload_image(part, part_pk=12345, result=result)

        assert result.image_uploaded is False
        assert result.image_skipped_reason == "disabled"
        assert result.image_url == part.image_urls[0]

    def test_no_image_on_lcsc(self, tmp_path: Path):
        """LCSCPart.image_urls 为空 → 不上传，记 no-image-on-lcsc。"""
        from lcsc2inv.inventree_writer import InvenTreeWriter

        settings = _make_settings(tmp_path)
        writer = InvenTreeWriter.__new__(InvenTreeWriter)
        writer.settings = settings
        result = WriteResult()
        part = _make_part_no_image()

        writer._upload_image(part, part_pk=12345, result=result)

        assert result.image_uploaded is False
        assert result.image_skipped_reason == "no-image-on-lcsc"
        assert result.image_url is None

    def test_already_uploaded(self, tmp_path: Path):
        """Part 已有 image 且非 force → 跳过，记 already-uploaded。"""
        from lcsc2inv.inventree_writer import InvenTreeWriter, Part

        settings = _make_settings(tmp_path)
        writer = InvenTreeWriter.__new__(InvenTreeWriter)
        writer.settings = settings
        writer.api = MagicMock()
        result = WriteResult()
        part = _make_part_with_image()

        # 模拟 Part(api, pk) 直接返回一个已经有 image 的 mock 实例
        existing = MagicMock()
        existing.image = "media/part_images/C999999.jpg"
        original_init = Part.__init__

        def _no_init(self, *args, **kwargs):
            # 给个 image 字段后返回，不发任何 HTTP
            self.image = "media/part_images/C999999.jpg"

        with patch.object(Part, "__init__", _no_init), \
             patch.object(Part, "uploadImage") as m_upload:
            writer._upload_image(part, part_pk=12345, result=result)

        assert result.image_uploaded is False
        assert result.image_skipped_reason == "already-uploaded"
        assert m_upload.called is False

    def test_force_reupload(self, tmp_path: Path):
        """force=True 强制重新上传，即使 Part 已有 image。"""
        from lcsc2inv.inventree_writer import InvenTreeWriter

        settings = _make_settings(tmp_path)
        writer = InvenTreeWriter.__new__(InvenTreeWriter)
        writer.settings = settings
        writer.api = MagicMock()
        result = WriteResult()
        part = _make_part_with_image()

        # download_image 返回一个 fake 本地文件
        local = tmp_path / "C999999.jpg"
        local.write_bytes(b"\xff\xd8\xff\xe0FAKE-JPEG")
        mock_fetcher = MagicMock()
        mock_fetcher.download_image.return_value = local

        # 用 FakePart 替换 lcsc2inv.inventree_writer 命名空间里的 Part
        class FakePart:
            instances: list = []
            def __init__(self, api, pk):
                self.api = api
                self.pk = pk
                self.image = "media/part_images/old.jpg"  # 模拟已有图片
                FakePart.instances.append(self)
            def uploadImage(self, path):
                self.last_uploaded = path
                FakePart.upload_calls.append(path)

        FakePart.upload_calls = []

        with patch("lcsc2inv.inventree_writer.Part", FakePart):
            writer._upload_image(
                part, part_pk=12345, result=result,
                fetcher=mock_fetcher, force=True,
            )

        mock_fetcher.download_image.assert_called_once_with(
            "C999999", part.image_urls[0]
        )
        assert FakePart.upload_calls == [str(local)]
        assert result.image_uploaded is True
        assert result.image_skipped_reason is None
        # 本地缓存文件应被清理
        assert not local.exists()

    def test_download_failure(self, tmp_path: Path):
        """下载失败 → 不上传，记 download-failed。"""
        from lcsc2inv.inventree_writer import InvenTreeWriter
        from inventree.part import Part

        settings = _make_settings(tmp_path)
        writer = InvenTreeWriter.__new__(InvenTreeWriter)
        writer.settings = settings
        writer.api = MagicMock()
        result = WriteResult()
        part = _make_part_with_image()

        mock_fetcher = MagicMock()
        mock_fetcher.download_image.side_effect = LcscFetchError("被限流 HTTP 503")

        # Part 还没有 image（幂等检查通过）
        empty_part = MagicMock(spec=Part)
        empty_part.image = ""
        with patch("inventree.part.Part", return_value=empty_part):
            writer._upload_image(
                part, part_pk=12345, result=result, fetcher=mock_fetcher,
            )

        assert result.image_uploaded is False
        assert result.image_skipped_reason is not None
        assert "download-failed" in result.image_skipped_reason

    def test_upload_failure(self, tmp_path: Path):
        """上传失败（Part.uploadImage 抛异常）→ 记 upload-failed，本地文件清理。"""
        from lcsc2inv.inventree_writer import InvenTreeWriter

        settings = _make_settings(tmp_path)
        writer = InvenTreeWriter.__new__(InvenTreeWriter)
        writer.settings = settings
        writer.api = MagicMock()
        result = WriteResult()
        part = _make_part_with_image()

        local = tmp_path / "C999999.jpg"
        local.write_bytes(b"\xff\xd8\xff\xe0FAKE")
        mock_fetcher = MagicMock()
        mock_fetcher.download_image.return_value = local

        class FlakyPart:
            def __init__(self, api, pk):
                self.image = ""  # 没有现有图片，会走到上传
            def uploadImage(self, path):
                raise RuntimeError("server 500")

        with patch("lcsc2inv.inventree_writer.Part", FlakyPart):
            writer._upload_image(
                part, part_pk=12345, result=result, fetcher=mock_fetcher,
            )

        assert result.image_uploaded is False
        assert "upload-failed" in result.image_skipped_reason
        assert not local.exists()  # 即便失败也清理本地缓存

    def test_dry_run_records_url_without_download(self, tmp_path: Path):
        """dry-run 模式：不下载，只记录 url + skipped。"""
        from lcsc2inv.inventree_writer import InvenTreeWriter

        settings = _make_settings(tmp_path)
        writer = InvenTreeWriter.__new__(InvenTreeWriter)
        writer.settings = settings
        writer.api = MagicMock()
        result = WriteResult()
        part = _make_part_with_image()

        # part_pk=None 模拟 dry-run 提前 return（_upload_image 仍被调用但 part_pk 为 None）
        mock_fetcher = MagicMock()
        writer._upload_image(
            part, part_pk=None, result=result, fetcher=mock_fetcher,
        )

        assert result.image_uploaded is False
        assert result.image_skipped_reason == "dry-run"
        assert result.image_url == part.image_urls[0]
        assert mock_fetcher.download_image.called is False


# ---------------------------------------------------------------------------
# Fetcher.download_image 测试（mock requests）
# ---------------------------------------------------------------------------


class TestDownloadImage:
    """测试下载 + 缓存 + 重试。"""

    def _fetcher(self, tmp_path: Path) -> Fetcher:
        settings = _make_settings(tmp_path, lcsc_request_interval=0.0)
        f = default_fetcher(settings)
        return f

    def test_first_download_writes_cache(self, tmp_path: Path):
        f = self._fetcher(tmp_path)
        url = "http://example.com/test/C111.jpg"
        fake_bytes = b"\xff\xd8\xff\xe0FAKE-JPEG-1"
        with patch.object(f.session, "get") as m_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.content = fake_bytes
            m_get.return_value = resp

            p = f.download_image("C111", url)

        assert p == tmp_path / "images" / "C111.jpg"
        assert p.read_bytes() == fake_bytes
        meta = (tmp_path / "images" / "C111.meta.json")
        assert meta.exists()
        meta_obj = json.loads(meta.read_text(encoding="utf-8"))
        assert "fetched_at" in meta_obj

    def test_second_call_uses_cache(self, tmp_path: Path):
        """缓存命中 → 不发 HTTP 请求。"""
        f = self._fetcher(tmp_path)
        url = "http://example.com/test/C222.jpg"
        # 预填缓存
        img = tmp_path / "images" / "C222.jpg"
        meta = tmp_path / "images" / "C222.meta.json"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"CACHED")
        meta.write_text(json.dumps({"fetched_at": time.time()}), encoding="utf-8")

        with patch.object(f.session, "get") as m_get:
            p = f.download_image("C222", url)

        assert p == img
        assert m_get.called is False

    def test_cache_disabled_always_downloads(self, tmp_path: Path):
        """lcsc_cache_enabled=false → 总是发请求，不读缓存。"""
        settings = _make_settings(tmp_path, lcsc_cache_enabled=False)
        f = default_fetcher(settings)
        url = "http://example.com/test/C333.jpg"
        # 即使预填了缓存也不读
        img = tmp_path / "images" / "C333.jpg"
        meta = tmp_path / "images" / "C333.meta.json"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"OLD")
        meta.write_text(json.dumps({"fetched_at": time.time()}), encoding="utf-8")

        with patch.object(f.session, "get") as m_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b"NEW"
            m_get.return_value = resp
            p = f.download_image("C333", url)

        assert p.read_bytes() == b"NEW"
        assert m_get.called is True

    def test_403_triggers_retry_then_raises(self, tmp_path: Path):
        """HTTP 403 应触发 tenacity 重试，全部失败后抛 LcscFetchError。"""
        settings = _make_settings(tmp_path, lcsc_max_retries=2)
        f = default_fetcher(settings)
        url = "http://example.com/test/C444.jpg"
        with patch.object(f.session, "get") as m_get:
            resp = MagicMock()
            resp.status_code = 403
            m_get.return_value = resp
            with pytest.raises(LcscFetchError, match="被限流"):
                f.download_image("C444", url)
        assert m_get.call_count >= 2  # 至少重试了 max_retries 次

    def test_retry_then_success(self, tmp_path: Path):
        """第一次 503 第二次 200 → 最终成功。"""
        settings = _make_settings(tmp_path, lcsc_max_retries=3)
        f = default_fetcher(settings)
        url = "http://example.com/test/C555.jpg"

        resp_fail = MagicMock(status_code=503)
        resp_ok = MagicMock(status_code=200, content=b"SUCCESS")
        with patch.object(f.session, "get", side_effect=[resp_fail, resp_ok]) as m_get:
            p = f.download_image("C555", url)

        assert p.read_bytes() == b"SUCCESS"
        assert m_get.call_count == 2

    def test_expired_cache_triggers_redownload(self, tmp_path: Path):
        """meta.json 时间戳超过 TTL → 重新下载。"""
        settings = _make_settings(tmp_path, lcsc_cache_ttl_days=1)
        f = default_fetcher(settings)
        url = "http://example.com/test/C666.jpg"
        # 缓存 7 天前的
        img = tmp_path / "images" / "C666.jpg"
        meta = tmp_path / "images" / "C666.meta.json"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"OLD")
        meta.write_text(
            json.dumps({"fetched_at": time.time() - 7 * 86400}),
            encoding="utf-8",
        )

        with patch.object(f.session, "get") as m_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b"FRESH"
            m_get.return_value = resp
            p = f.download_image("C666", url)

        assert p.read_bytes() == b"FRESH"
        assert m_get.called is True


# ---------------------------------------------------------------------------
# WriteResult 数据类
# ---------------------------------------------------------------------------


class TestWriteResultImageFields:
    def test_default_fields(self):
        r = WriteResult()
        assert r.image_uploaded is False
        assert r.image_url is None
        assert r.image_skipped_reason is None

    def test_summary_includes_img_flag(self):
        r = WriteResult(part_pk=1, image_uploaded=True)
        assert "img#1" in r.summary()

        r2 = WriteResult(part_pk=1, image_uploaded=False)
        assert "img#0" in r2.summary()

    def test_options_have_new_fields(self):
        o = WriteOptions()
        assert o.fetcher is None
        assert o.force_image_upload is False

        o2 = WriteOptions(force_image_upload=True)
        assert o2.force_image_upload is True


# ---------------------------------------------------------------------------
# Settings 集成
# ---------------------------------------------------------------------------


class TestSettingsUploadImage:
    def test_default_true(self, monkeypatch, tmp_path):
        """不设置 LCSC_UPLOAD_IMAGE → 默认 True。"""
        monkeypatch.delenv("LCSC_UPLOAD_IMAGE", raising=False)
        monkeypatch.setenv("LCSC_CACHE_DIR", str(tmp_path))
        get_settings(force_reload=True)
        s = get_settings(force_reload=True)
        assert s.lcsc_upload_image is True

    def test_disabled_via_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LCSC_UPLOAD_IMAGE", "false")
        monkeypatch.setenv("LCSC_CACHE_DIR", str(tmp_path))
        get_settings(force_reload=True)
        s = get_settings(force_reload=True)
        assert s.lcsc_upload_image is False

    def test_enabled_via_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LCSC_UPLOAD_IMAGE", "true")
        monkeypatch.setenv("LCSC_CACHE_DIR", str(tmp_path))
        get_settings(force_reload=True)
        s = get_settings(force_reload=True)
        assert s.lcsc_upload_image is True