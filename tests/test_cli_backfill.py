"""backfill-package 一键回填封装命令的离线测试（CliRunner + mock）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lcsc2inv.cli import cli as cli_group
from lcsc2inv.config import Settings
from lcsc2inv.inventree_writer import InvenTreeWriter
from lcsc2inv.lcsc_client import LcscFetchError, load_fixture
from lcsc2inv.lcsc_models import LCSCPart, PropertyValue

FIXTURE_DIR = __file__.rsplit("/", 2)[0] + "/tests/fixtures"


def _settings(tmp_path, dry_run: bool) -> Settings:
    return Settings(
        inventree_url="http://localhost",
        inventree_token="x" * 40,
        lcsc_cache_dir=str(tmp_path),
        lcsc_request_interval=0.0,
        dry_run=dry_run,
    )


def _inv_part(pk: int, ipn: str, description: str = "", keywords: str = ""):
    return SimpleNamespace(pk=pk, IPN=ipn, description=description, keywords=keywords)


class FakePart:
    """替换 inventree.part.Part：list 返回预置对象，save 记录 payload。"""

    saved: list[tuple[int, dict]] = []
    rows: list = []

    def __init__(self, api, pk):
        self.pk = pk

    def save(self, payload):
        FakePart.saved.append((self.pk, payload))

    @classmethod
    def list(cls, api):
        return list(cls.rows)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """patch CLI 边界：settings / api / fetcher / Part / set_package_parameter。"""
    settings = _settings(tmp_path, dry_run=False)
    monkeypatch.setattr("lcsc2inv.cli.get_settings", lambda: settings)
    monkeypatch.setattr("lcsc2inv.cli._connect_inventree", lambda token=None: object())
    FakePart.saved = []
    FakePart.rows = []

    fetcher_calls: list[str] = []
    parts_by_code: dict[str, LCSCPart] = {}

    def _fetch(code):
        fetcher_calls.append(code)
        if code not in parts_by_code:
            raise LcscFetchError(f"无数据 {code}")
        return parts_by_code[code]

    fake_fetcher = SimpleNamespace(fetch=_fetch)

    monkeypatch.setattr("lcsc2inv.cli.default_fetcher", lambda s: fake_fetcher)
    monkeypatch.setattr("inventree.part.Part", FakePart)

    param_calls: list[tuple[int, str, str]] = []  # (part_pk, name, value)
    param_values: dict[int, str] = {}  # pk -> 已存在的 Package 参数值

    def _fake_set_named(self, *, part_pk, name, value):
        param_calls.append((part_pk, name, value))
        return True

    monkeypatch.setattr(InvenTreeWriter, "set_named_parameter", _fake_set_named)
    # 分类匹配 + 参数现状（ctx 参数对比用）：默认 Part 无任何参数
    monkeypatch.setattr(
        "lcsc2inv.cli.categorizer_match",
        lambda part, **kw: SimpleNamespace(category_path="__uncategorized__"),
    )
    monkeypatch.setattr(
        InvenTreeWriter, "_list_part_parameters",
        lambda self, part_pk: (
            [{"pk": 0, "template": "Package", "value": param_values[part_pk]}]
            if part_pk in param_values else []
        ),
    )

    return SimpleNamespace(
        settings=settings,
        parts_by_code=parts_by_code,
        fetcher_calls=fetcher_calls,
        param_calls=param_calls,
        param_values=param_values,
    )


def _c_part(code: str, package: str | None, description: str = "desc") -> LCSCPart:
    props = [PropertyValue(name="Package", value=package)] if package else []
    return LCSCPart(sku=code, mpn="MPN-" + code, description=description,
                    additional_properties=props)


class TestBackfillPackage:
    def test_dry_run_no_writes(self, env, tmp_path):
        FakePart.rows = [
            _inv_part(1, "C111111", description="10k 1% resistor", keywords="C111111"),
        ]
        env.parts_by_code["C111111"] = _c_part("C111111", "0603", "10k 1% resistor")

        result = CliRunner(env={"COLUMNS": "300"}).invoke(cli_group, ["backfill-package", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "C111111" in result.output
        assert "0603" in result.output
        assert FakePart.saved == []  # dry-run 不写
        assert env.param_calls == []

    def test_commit_updates_desc_keywords_param(self, env):
        FakePart.rows = [
            _inv_part(1, "C111111", description="10k 1% resistor", keywords="C111111"),
            _inv_part(2, "C28323", description="1uF cap 0805", keywords="C28323,0805"),
        ]
        env.parts_by_code["C111111"] = _c_part("C111111", "0603", "10k 1% resistor")
        # 描述与 keywords 已含 0805 → 描述/keywords 无需变更
        env.parts_by_code["C28323"] = load_fixture(FIXTURE_DIR + "/C28323.html")

        result = CliRunner(env={"COLUMNS": "300"}).invoke(cli_group, ["backfill-package", "--commit"])
        assert result.exit_code == 0, result.output

        saved = dict(FakePart.saved)
        # C111111：描述加后缀（封装 + 参数值）+ keywords 追加封装
        assert saved[1]["description"] == "10k 1% resistor（封装：0603） [参数: 0603]"
        assert saved[1]["keywords"] == "C111111,0603"
        # C28323：描述新增参数后缀（Base 链路 mapped 值）
        assert saved[2]["description"] == "1uF cap 0805 [参数: 1u | ±10% | 50V | 0805]"
        # Package 参数两者都写
        assert (1, "Package", "0603") in env.param_calls
        assert (2, "Package", "0805") in env.param_calls
        assert "写入失败 0" in result.output

    def test_comma_package_keyword_dedup(self, env):
        """封装值自带逗号时不得重复追加，且清洗历史重复。"""
        pkg = "Through Hole,P=3.4mm"
        FakePart.rows = [
            _inv_part(1, "C10077",
                      description="540nm Photoresistor " + pkg,
                      keywords=f"C10077,GL5516,JCHL,{pkg},{pkg},{pkg}"),
        ]
        env.parts_by_code["C10077"] = _c_part("C10077", pkg, "desc")
        # 参数已存在且一致 → 参数不动
        env.param_values[1] = pkg

        result = CliRunner(env={"COLUMNS": "300"}).invoke(cli_group, ["backfill-package", "--commit"])
        assert result.exit_code == 0, result.output
        saved = dict(FakePart.saved)
        assert saved[1]["keywords"] == "C10077,GL5516,JCHL," + pkg  # 去重后只留一份
        assert env.param_calls == []

    def test_up_to_date_no_writes(self, env):
        """描述/keywords/参数全部已就绪 → 计入「已是最新」，零写入。"""
        FakePart.rows = [
            _inv_part(1, "C111111",
                      description="res（封装：0603） [参数: 0603]",
                      keywords="C111111,0603"),
        ]
        env.parts_by_code["C111111"] = _c_part("C111111", "0603", "res")
        env.param_values[1] = "0603"

        result = CliRunner(env={"COLUMNS": "300"}).invoke(cli_group, ["backfill-package", "--commit"])
        assert result.exit_code == 0, result.output
        assert FakePart.saved == []
        assert env.param_calls == []
        assert "已是最新 1" in result.output
        assert "需更新 0" in result.output

    def test_cn_ipn_and_dedup(self, env):
        """CN: 前缀 IPN 也处理；描述已含封装时不再追加。"""
        FakePart.rows = [
            # InvenTree 描述已含「弯插」→ 不加后缀，仅补参数
            _inv_part(3, "CN:360864", description="RJ45 弯插连接器",
                      keywords="C386757,弯插"),
            # 描述不含封装 → 追加后缀 + keywords
            _inv_part(4, "CN:999999",
                      description="RJ45 连接器 [参数: 弯插]", keywords="C1"),
        ]
        env.parts_by_code["CN:360864"] = load_fixture(FIXTURE_DIR + "/CN_360864.html")
        env.parts_by_code["CN:999999"] = load_fixture(FIXTURE_DIR + "/CN_360864.html")

        result = CliRunner(env={"COLUMNS": "300"}).invoke(cli_group, ["backfill-package", "--commit"])
        assert result.exit_code == 0, result.output
        saved = dict(FakePart.saved)
        assert saved[3]["description"] == "RJ45 弯插连接器 [参数: 弯插]"
        # 描述预置了参数段已收敛 → 仅 keywords 变更
        assert saved[4] == {"keywords": "C1,弯插"}
        assert saved[4]["keywords"] == "C1,弯插"
        assert (3, "Package", "弯插") in env.param_calls
        assert (4, "Package", "弯插") in env.param_calls

    def test_no_package_and_fetch_failure_counted(self, env):
        FakePart.rows = [
            _inv_part(1, "C222222", description="x"),
            _inv_part(2, "C333333", description="y"),
        ]
        env.parts_by_code["C222222"] = _c_part("C222222", None)  # 无封装
        # C333333 不在 parts_by_code → 抓取失败

        result = CliRunner(env={"COLUMNS": "300"}).invoke(cli_group, ["backfill-package", "--commit"])
        assert result.exit_code == 0, result.output
        assert "无封装数据" in result.output
        assert "抓取失败" in result.output
        assert "LCSC 无封装 1" in result.output
        assert "抓取失败 1" in result.output
        assert FakePart.saved == []

    def test_non_lcsc_ipn_skipped(self, env):
        FakePart.rows = [
            _inv_part(1, "RES-0805", description="自建件"),
            _inv_part(2, "ABC123", description="自建件"),
        ]
        result = CliRunner(env={"COLUMNS": "300"}).invoke(cli_group, ["backfill-package", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "没有找到" in result.output
        assert env.fetcher_calls == []

    def test_limit_option(self, env):
        FakePart.rows = [
            _inv_part(i, f"C{i:06d}", description="x") for i in range(1, 6)
        ]
        for i in range(1, 6):
            env.parts_by_code[f"C{i:06d}"] = _c_part(f"C{i:06d}", "0603")

        result = CliRunner(env={"COLUMNS": "300"}).invoke(
            cli_group, ["backfill-package", "--commit", "--limit", "2"]
        )
        assert result.exit_code == 0, result.output
        assert "共找到 2 个" in result.output
        assert len(env.param_calls) == 2

    def test_write_error_counted(self, env, monkeypatch):
        FakePart.rows = [_inv_part(1, "C111111", description="x")]

        def _boom(self, *, part_pk, name, value):
            raise RuntimeError("api down")

        monkeypatch.setattr(InvenTreeWriter, "set_named_parameter", _boom)
        env.parts_by_code["C111111"] = _c_part("C111111", "0603")

        result = CliRunner(env={"COLUMNS": "300"}).invoke(cli_group, ["backfill-package", "--commit"])
        assert result.exit_code == 0, result.output
        assert "写入失败 1" in result.output
        assert "api down" in result.output
