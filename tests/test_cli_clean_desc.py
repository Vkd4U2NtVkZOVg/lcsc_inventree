"""clean-desc 一键清理描述模板文案命令的离线测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from lcsc2inv.cli import cli as cli_group
from lcsc2inv.config import Settings
from lcsc2inv.lcsc_client import LCSC_CN_DESC_BOILERPLATE as B

BOILER_DESC = "R-RJ45R08P-C000连接器，价格￥1.23元。" + B


class FakePart:
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
    FakePart.saved = []
    FakePart.rows = []
    settings = Settings(
        inventree_url="http://localhost",
        inventree_token="x" * 40,
        lcsc_cache_dir=str(tmp_path),
        dry_run=False,
    )
    monkeypatch.setattr("lcsc2inv.cli.get_settings", lambda: settings)
    monkeypatch.setattr("lcsc2inv.cli._connect_inventree", lambda token=None: object())
    monkeypatch.setattr("inventree.part.Part", FakePart)
    return SimpleNamespace(settings=settings)


def _inv_part(pk: int, ipn: str, description: str):
    return SimpleNamespace(pk=pk, IPN=ipn, description=description, keywords="")


class TestCleanDesc:
    def test_commit_cleans_only_matching(self, env):
        FakePart.rows = [
            _inv_part(1, "C386757", BOILER_DESC),
            _inv_part(2, "C2", "某器件，" + B),
            _inv_part(3, "C3", B),          # 整段都是模板 → 清空
            _inv_part(4, "C4", "正常描述"),  # 无模板 → 不动
        ]
        result = CliRunner(env={"COLUMNS": "300"}).invoke(
            cli_group, ["clean-desc", "--commit"]
        )
        assert result.exit_code == 0, result.output
        saved = dict(FakePart.saved)
        assert set(saved) == {1, 2, 3}
        assert saved[1]["description"] == "R-RJ45R08P-C000连接器，价格￥1.23元。"
        assert saved[2]["description"] == "某器件"
        assert saved[3]["description"] == ""
        assert "清理 3" in result.output
        assert "无匹配 1" in result.output

    def test_dry_run_no_writes(self, env):
        FakePart.rows = [_inv_part(1, "C386757", BOILER_DESC)]
        result = CliRunner(env={"COLUMNS": "300"}).invoke(
            cli_group, ["clean-desc", "--dry-run"]
        )
        assert result.exit_code == 0, result.output
        assert FakePart.saved == []
        assert "将清理描述" in result.output

    def test_idempotent(self, env):
        FakePart.rows = [_inv_part(1, "C1", BOILER_DESC)]
        CliRunner(env={"COLUMNS": "300"}).invoke(
            cli_group, ["clean-desc", "--commit"]
        )
        first = list(FakePart.saved)
        # 模拟持久化：list 返回已清理后的描述
        FakePart.rows[0].description = dict(FakePart.saved)[1]["description"]
        result = CliRunner(env={"COLUMNS": "300"}).invoke(
            cli_group, ["clean-desc", "--commit"]
        )
        assert list(FakePart.saved) == first
        assert "清理 0" in result.output

    def test_write_error_counted(self, env, monkeypatch):
        def boom(self, payload):
            raise RuntimeError("api down")

        FakePart.rows = [_inv_part(1, "C1", BOILER_DESC)]
        monkeypatch.setattr(FakePart, "save", boom)
        result = CliRunner(env={"COLUMNS": "300"}).invoke(
            cli_group, ["clean-desc", "--commit"]
        )
        assert result.exit_code == 0, result.output
        assert "失败 1" in result.output
        assert "api down" in result.output
