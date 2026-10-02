"""环境变量 / YAML 配置加载。

优先级（从高到低）：
1. 显式传入 `config_dir=` 参数
2. 环境变量（已通过 python-dotenv 加载）
3. 项目内置 `config/` 目录

所有配置通过 `get_settings()` 返回的 `@dataclass` 实例访问。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 项目内置 config 目录；优先读 LCSC_CONFIG_DIR 环境变量（Docker 部署时指向挂载/拷贝目录）
_DEFAULT_CONFIG_DIR = Path(
    os.environ.get("LCSC_CONFIG_DIR") or (Path(__file__).resolve().parent.parent / "config")
)


def _to_bool(s: str | bool | None, default: bool = False) -> bool:
    if isinstance(s, bool):
        return s
    if s is None:
        return default
    return s.strip().lower() in {"1", "true", "yes", "y", "on"}


def _expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


@dataclass(slots=True)
class Settings:
    """运行配置：从 .env / YAML / 默认值汇集。"""

    # InvenTree
    inventree_url: str = ""
    inventree_token: str = ""
    inventree_username: str = ""
    inventree_password: str = ""
    inventree_supplier_name: str = "LCSC Electronics"

    # InvenTree 数据库一键备份（依赖宿主机 InvenTree 也是 Docker 部署）
    # 备份时通过挂载的 /var/run/docker.sock 进入该容器执行原生备份命令
    inventree_backup_container: str = ""            # InvenTree 容器名（必需）
    inventree_backup_cmd: str = "invoke backup"     # 容器内备份命令（默认全库+media）
    inventree_backup_src_dir: str = "/home/inventree/data/backup"  # 容器内产物目录
    inventree_backup_storage: str = "/backup"       # 本容器对外暴露目录（bind-mount 到宿主机）

    # LCSC 行为
    lcsc_cache_enabled: bool = True
    lcsc_cache_dir: str = "~/.cache/lcsc2inventree"
    lcsc_cache_ttl_days: int = 7
    lcsc_request_interval: float = 1.0
    lcsc_max_retries: int = 3
    lcsc_user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    # 图片上传：默认开启。Part 已 image 则跳过；--update 强制重传
    lcsc_upload_image: bool = True

    # 抓取完成后是否打开浏览器
    open_browser: bool = False

    # 行为开关
    category_match_ratio_limit: int = 75
    dry_run: bool = False

    # YAML 配置（不存进 dataclass，单独函数读取）
    config_dir: Path = field(default_factory=lambda: _DEFAULT_CONFIG_DIR)

    @property
    def cache_dir_path(self) -> Path:
        return Path(_expand(self.lcsc_cache_dir))


_cached: Settings | None = None


def get_settings(*, force_reload: bool = False) -> Settings:
    """读取 .env 重新构造 Settings；可缓存。"""
    global _cached
    if _cached is not None and not force_reload:
        return _cached

    load_dotenv(override=False)
    s = Settings(
        inventree_url=os.getenv("INVENTREE_URL", "").rstrip("/"),
        inventree_token=os.getenv("INVENTREE_TOKEN", ""),
        inventree_username=os.getenv("INVENTREE_USERNAME", ""),
        inventree_password=os.getenv("INVENTREE_PASSWORD", ""),
        inventree_supplier_name=os.getenv("INVENTREE_SUPPLIER_NAME", "LCSC Electronics"),
        inventree_backup_container=os.getenv("INVENTREE_BACKUP_CONTAINER", ""),
        inventree_backup_cmd=os.getenv("INVENTREE_BACKUP_CMD", "invoke backup"),
        inventree_backup_src_dir=os.getenv(
            "INVENTREE_BACKUP_SRC_DIR", "/home/inventree/data/backup"
        ),
        inventree_backup_storage=os.getenv("INVENTREE_BACKUP_STORAGE", "/backup"),
        lcsc_cache_enabled=_to_bool(os.getenv("LCSC_CACHE_ENABLED"), True),
        lcsc_cache_dir=os.getenv("LCSC_CACHE_DIR", "~/.cache/lcsc2inventree"),
        lcsc_cache_ttl_days=int(os.getenv("LCSC_CACHE_TTL_DAYS", "7")),
        lcsc_request_interval=float(os.getenv("LCSC_REQUEST_INTERVAL", "1.0")),
        lcsc_max_retries=int(os.getenv("LCSC_MAX_RETRIES", "3")),
        lcsc_user_agent=os.getenv(
            "LCSC_USER_AGENT",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        ),
        category_match_ratio_limit=int(os.getenv("CATEGORY_MATCH_RATIO_LIMIT", "75")),
        dry_run=_to_bool(os.getenv("DRY_RUN"), False),
        lcsc_upload_image=_to_bool(os.getenv("LCSC_UPLOAD_IMAGE"), True),
        open_browser=_to_bool(os.getenv("LCSC_OPEN_BROWSER"), False),
    )
    _cached = s
    return s


def load_yaml(name: str, *, config_dir: Path | None = None) -> dict[str, Any]:
    """加载 config_dir 下的 YAML 文件；返回原始 dict。"""
    d = Path(config_dir) if config_dir else _DEFAULT_CONFIG_DIR
    path = d / name
    if not path.exists():
        logger.warning("YAML 不存在: %s", path)
        return {}
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层必须是 dict，实际是 {type(data).__name__}")
    return data
