"""构造 InvenTree API 客户端（CLI 与 Web 共用）。

把连接逻辑从 `cli.py` 抽出来，避免 CLI 与 Web 服务重复实现；兼容
`inventree-python` 新旧版本对参数的命名差异（`host` vs `server`）。
"""

from __future__ import annotations

from inventree.api import InvenTreeAPI

from lcsc2inv.config import Settings, get_settings


def build_inventree_api(
    settings: Settings | None = None, token: str | None = None
) -> InvenTreeAPI:
    """根据配置构造 InvenTree API 客户端。

    Args:
        settings: 运行配置；None 时用 `get_settings()`。
        token: 覆盖 Token；优先于 settings.inventree_token。

    Raises:
        ValueError: 缺少 URL 或认证信息时。
    """
    s = settings or get_settings()
    if not s.inventree_url:
        raise ValueError("INVENTREE_URL 未设置（请复制 .env.example → .env 并填写）")
    # inventree-python 0.14+ 把参数名从 server 改成了 host；这里兼容两个版本
    kwargs: dict = {"host": s.inventree_url}
    if token or s.inventree_token:
        kwargs["token"] = token or s.inventree_token
    elif s.inventree_username and s.inventree_password:
        kwargs["username"] = s.inventree_username
        kwargs["password"] = s.inventree_password
    else:
        raise ValueError(
            "需要 INVENTREE_TOKEN 或 INVENTREE_USERNAME/INVENTREE_PASSWORD"
        )
    # 旧版本用 server=；如果传 host= 报 unexpected kwarg，则回退
    try:
        return InvenTreeAPI(**kwargs)
    except TypeError:
        kwargs = {("server" if k == "host" else k): v for k, v in kwargs.items()}
        return InvenTreeAPI(**kwargs)
