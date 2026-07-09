"""Click CLI 入口。

子命令：
- `import <code|url>`：单条导入
- `batch <csv>`：批量导入
- `doctor`：检查 InvenTree 连通 + 权限
- `cache clear`：清理本地缓存
"""

from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path
from typing import Iterable

import click
from rich.console import Console
from rich.table import Table

from lcsc2inv.categorizer import match as categorizer_match
from lcsc2inv.config import get_settings, load_yaml
from lcsc2inv.inventree_writer import InvenTreeWriter, WriteOptions, WriteResult
from lcsc2inv.lcsc_client import (
    Fetcher,
    LcscFetchError,
    default_fetcher,
    parse_lcsc_code,
)
from lcsc2inv.lcsc_models import LCSCPart
from lcsc2inv.mapping import to_inventree_parameters, to_part_notes

console = Console()
logger = logging.getLogger("lcsc2inv")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _connect_inventree(token: str | None = None):
    """构造 InvenTree API 客户端。"""
    from inventree.api import InvenTreeAPI  # 延迟导入（CLI 不一定需要）

    s = get_settings()
    if not s.inventree_url:
        raise click.ClickException("INVENTREE_URL 未设置（请复制 .env.example → .env 并填写）")
    # inventree-python 0.14+ 把参数名从 server 改成了 host；这里兼容两个版本
    kwargs: dict = {"host": s.inventree_url}
    if token or s.inventree_token:
        kwargs["token"] = token or s.inventree_token
    elif s.inventree_username and s.inventree_password:
        kwargs["username"] = s.inventree_username
        kwargs["password"] = s.inventree_password
    else:
        raise click.ClickException(
            "需要 INVENTREE_TOKEN 或 INVENTREE_USERNAME/INVENTREE_PASSWORD"
        )
    # 旧版本用 server=；如果传 host= 报 unexpected kwarg，则回退
    try:
        api = InvenTreeAPI(**kwargs)
    except TypeError:
        kwargs = {("server" if k == "host" else k): v for k, v in kwargs.items()}
        api = InvenTreeAPI(**kwargs)
    return api


def _print_dry_run_preview(part: LCSCPart) -> None:
    cat = categorizer_match(part)
    params = to_inventree_parameters(
        part, category_top=part.category_top, category_sub=part.category_sub
    )
    notes = to_part_notes(part)
    table = Table(title=f"[DRY] {part.sku}  {part.mpn or ''}", show_lines=False)
    table.add_column("field", style="cyan")
    table.add_column("value", style="white")
    table.add_row("name", part.name or "")
    table.add_row("description", (part.description or "")[:80])
    table.add_row("manufacturer", part.manufacturer_name or "")
    table.add_row("category", f"{part.category}  →  {cat.category_path}  ({cat.source}, {cat.score})")
    if part.offer:
        table.add_row("price", f"{part.offer.price} {part.offer.price_currency}")
        table.add_row("stock (LCSC)", str(part.offer.inventory_level or 0))
    table.add_row("datasheet", part.datasheet_url_resolved or "")
    if params:
        param_str = "; ".join(f"{k}={v['value']}" for k, v in params.items())
        table.add_row("parameters", param_str)
    if notes:
        table.add_row("notes", notes.splitlines()[0] + "…")
    console.print(table)


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def _common_dry_run(ctx: click.Context, dry_run: bool | None) -> None:
    """把 group / 子命令级 --dry-run 同步到 settings。"""
    if dry_run is not None:
        s = get_settings()
        s.dry_run = dry_run


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Debug 日志")
@click.option("--dry-run/--commit", "dry_run", default=None,
              help="强制 dry-run 或 commit（默认读 DRY_RUN）")
@click.option("--token", default=None, help="覆盖 INVENTREE_TOKEN")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, dry_run: bool | None, token: str | None) -> None:
    """lcsc2inv — LCSC → InvenTree 自动导入工具。

    \b
    dry-run 用法（三种都支持）：
        lcsc2inv --dry-run import C28323          (group 级选项)
        lcsc2inv import C28323 --dry-run          (子命令级选项)
        DRY_RUN=true lcsc2inv import C28323       (环境变量)
    """
    _setup_logging(verbose)
    _common_dry_run(ctx, dry_run)
    ctx.ensure_object(dict)
    ctx.obj["token"] = token


# 一个共用选项，便于在每个子命令上复用（同样暴露 --dry-run/--commit）
_dry_run_option = click.option(
    "--dry-run/--commit", "dry_run", default=None,
    help="强制 dry-run 或 commit（默认读 DRY_RUN；也可放在子命令前）",
)


@cli.command()
@_dry_run_option
@click.argument("code_or_url")
@click.option("--update/--no-update", default=False, help="强制更新已存在 Part 的描述/备注")
@click.option("--qty", type=int, default=None, help="建 StockItem 时使用的数量")
@click.option("--stock/--no-stock", default=False, help="建 StockItem（默认不建）")
@click.option("--note", default=None, help="附加备注写入 Part.notes")
@click.pass_context
def import_cmd(
    ctx: click.Context,
    dry_run: bool | None,
    code_or_url: str,
    update: bool,
    qty: int | None,
    stock: bool,
    note: str | None,
) -> None:
    """导入单个 LCSC 商品（如 C28323 或 https://www.lcsc.com/product-detail/C28323.html）。

    默认只创建器件，不建 StockItem（库存由用户手工管理）。
    用 --stock 可选建库存。
    """
    _common_dry_run(ctx, dry_run)
    settings = get_settings()
    code = parse_lcsc_code(code_or_url)
    fetcher: Fetcher = default_fetcher(settings)
    try:
        part = fetcher.fetch(code)
    except LcscFetchError as exc:
        raise click.ClickException(f"抓取 LCSC 失败: {exc}") from exc

    if settings.dry_run:
        _print_dry_run_preview(part)
        return

    api = _connect_inventree(token=ctx.obj.get("token"))
    writer = InvenTreeWriter(api, settings)
    opts = WriteOptions(
        update_existing=update,
        create_stock=stock,  # --stock 才建库存（默认 False）
        quantity=qty,
        extra_note=note,
        fetcher=fetcher,  # 复用限速（图片下载与 HTML 抓取共享 1 req/s）
        force_image_upload=update,  # --update 时强制覆盖图片
    )
    result = writer.upsert_part(part, options=opts)
    _print_result(part.sku, result)


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------


@cli.command()
@_dry_run_option
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--update/--no-update", default=False)
@click.option("--stock/--no-stock", default=False, help="建 StockItem（默认不建）")
@click.option("--workers", type=int, default=1, help="并发 worker 数（默认 1 即顺序）")
@click.pass_context
def batch(
    ctx: click.Context,
    dry_run: bool | None,
    csv_path: Path,
    update: bool,
    stock: bool,
    workers: int,
) -> None:
    """从 CSV 批量导入。CSV 必须有 lcsc_code 列，可选 quantity / note 列。

    默认只创建器件，不建 StockItem（库存由用户手工管理）。
    用 --stock 可选建库存。
    """
    _common_dry_run(ctx, dry_run)
    settings = get_settings()
    rows = _read_csv(csv_path)
    if not rows:
        raise click.ClickException(f"CSV 为空: {csv_path}")
    fetcher = default_fetcher(settings)

    if settings.dry_run:
        for r in rows:
            code = r["lcsc_code"]
            try:
                part = fetcher.fetch(code)
            except LcscFetchError as exc:
                console.print(f"[red]{code}: 抓取失败 {exc}[/red]")
                continue
            _print_dry_run_preview(part)
        return

    api = _connect_inventree(token=ctx.obj.get("token"))
    writer = InvenTreeWriter(api, settings)
    if workers > 1:
        _run_batch_threaded(writer, rows, fetcher, update, no_stock, workers)
    else:
        for r in rows:
            _import_one(writer, fetcher, r, update, stock)


def _import_one(
    writer: InvenTreeWriter,
    fetcher: Fetcher,
    row: dict,
    update: bool,
    stock: bool,
) -> None:
    code = row["lcsc_code"]
    qty = int(row["quantity"]) if row.get("quantity") else None
    note = row.get("note") or None
    try:
        part = fetcher.fetch(code)
    except LcscFetchError as exc:
        console.print(f"[red]{code}: 抓取失败 {exc}[/red]")
        return
    opts = WriteOptions(
        update_existing=update,
        create_stock=stock,  # --stock 才建库存（默认 False）
        quantity=qty,
        extra_note=note,
        fetcher=fetcher,
        force_image_upload=update,
    )
    result = writer.upsert_part(part, options=opts)
    _print_result(code, result)


def _run_batch_threaded(
    writer: InvenTreeWriter,
    rows: list[dict],
    fetcher: Fetcher,
    update: bool,
    stock: bool,
    workers: int,
) -> None:
    """简化并发：多 worker 各自 new 一个 fetcher/writer 不可行（writer 共享 API）。"""

    console.print("[yellow]并发模式下仍顺序执行（InvenTree SDK 非线程安全）[/yellow]")
    for r in rows:
        _import_one(writer, fetcher, r, update, no_stock)


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        out: list[dict] = []
        for row in reader:
            code = (row.get("lcsc_code") or "").strip()
            if not code:
                continue
            row["lcsc_code"] = code
            out.append(row)
        return out


def _print_result(code: str, result: WriteResult) -> None:
    if not result.ok():
        msg = "; ".join(result.errors) or "未知错误"
        console.print(f"[red]{code}: 失败 — {msg}[/red]")
        return
    created = ", ".join(k for k, v in result.created.items() if v)
    # 图片状态：上传成功 / 跳过原因
    if result.image_uploaded:
        img_state = "[green]image: uploaded[/green]"
    elif result.image_skipped_reason:
        img_state = f"[dim]image: skipped ({result.image_skipped_reason})[/dim]"
    else:
        img_state = "[dim]image: n/a[/dim]"
    console.print(
        f"[green]{code}: OK[/green]  {result.summary()}  "
        f"{('[created: ' + created + ']') if created else ''}  {img_state}"
    )


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """检查 InvenTree 连通性与权限。"""
    s = get_settings()
    if not s.inventree_url:
        raise click.ClickException("INVENTREE_URL 未设置")
    console.print(f"[bold]InvenTree URL:[/bold] {s.inventree_url}")
    try:
        api = _connect_inventree(token=ctx.obj.get("token"))
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"无法连接：{exc}") from exc
    # 1. /api/ ping
    try:
        from inventree.part import Part  # 探测
        n = len(Part.list(api))
        console.print(f"[green]✓ /api/part/ 可访问，当前 Part 数量: {n}[/green]")
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"/api/part/ 失败: {exc}") from exc
    # 2. supplier
    from inventree.company import Company, SupplierPart
    suppliers = [c for c in Company.list(api) if getattr(c, "is_supplier", False)]
    console.print(f"[green]✓ 供应商数: {len(suppliers)}[/green]")
    console.print(f"[green]✓ SupplierPart 数: {len(SupplierPart.list(api))}[/green]")
    # 3. YAML 检查
    cat = load_yaml("lcsc_categories.yaml")
    fmap = load_yaml("field_map.yaml")
    console.print(f"[green]✓ lcsc_categories.yaml: {len(cat)} 顶级[/green]")
    console.print(f"[green]✓ field_map.yaml: {len(fmap)} 顶级[/green]")
    console.print("[bold green]All checks passed.[/bold green]")


# ---------------------------------------------------------------------------
# cache（平铺到顶层，因为 chain=True 不允许嵌套子组）
# ---------------------------------------------------------------------------


@cli.command("cache-clear")
def cache_clear() -> None:
    """清理本地 LCSC HTML 缓存。"""
    s = get_settings()
    d = s.cache_dir_path
    if not d.exists():
        console.print(f"缓存目录不存在: {d}")
        return
    n = sum(1 for _ in d.iterdir())
    for p in d.iterdir():
        if p.is_file():
            p.unlink()
    console.print(f"已清理 {n} 个文件 ({d})")


@cli.command("cache-ls")
def cache_ls() -> None:
    """列出已缓存的 LCSC 商品。"""
    s = get_settings()
    d = s.cache_dir_path
    if not d.exists():
        console.print(f"缓存目录不存在: {d}")
        return
    for p in sorted(d.glob("*.json")):
        if p.suffix == ".meta.json":
            continue
        console.print(p.name)


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------


# Click 8.1+ 推荐：用 group.command 装饰器；但保留别名以兼容 `lcsc2inv import` 等。
cli.add_command(import_cmd, name="import")  # 显式注册 import
# click>=8.1 的 group 已支持 command name 自动化；这里保险起见再注册一次


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()