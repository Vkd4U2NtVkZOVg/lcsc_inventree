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
import re
import webbrowser
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from lcsc2inv.categorizer import match as categorizer_match
from lcsc2inv.client import build_inventree_api
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
    try:
        return build_inventree_api(token=token)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


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
@click.option("--create-missing-category/--no-create-missing-category", default=False,
              help="LCSC 分类本地不存在时自动创建（默认不创建，落在 __uncategorized__）")
@click.pass_context
def import_cmd(
    ctx: click.Context,
    dry_run: bool | None,
    code_or_url: str,
    update: bool,
    qty: int | None,
    stock: bool,
    note: str | None,
    create_missing_category: bool,
) -> None:
    """导入单个 LCSC 商品（如 C28323 或 https://www.lcsc.com/product-detail/C28323.html）。

    默认只创建器件，不建 StockItem（库存由用户手工管理）。
    用 --stock 可选建库存。
    用 --create-missing-category 允许自动创建本地不存在的 LCSC 分类。
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
    result = writer.upsert_part(part, options=opts, create_missing_category=create_missing_category)
    _print_result(code, result, batch_mode=False)


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------


@cli.command()
@_dry_run_option
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--update/--no-update", default=False)
@click.option("--stock/--no-stock", default=False, help="建 StockItem（默认不建）")
@click.option("--create-missing-category/--no-create-missing-category", default=False,
              help="LCSC 分类本地不存在时自动创建（默认不创建）")
@click.option("--workers", type=int, default=1, help="并发 worker 数（默认 1 即顺序）")
@click.pass_context
def batch(
    ctx: click.Context,
    dry_run: bool | None,
    csv_path: Path,
    update: bool,
    stock: bool,
    create_missing_category: bool,
    workers: int,
) -> None:
    """从 CSV 批量导入。CSV 必须有 lcsc_code 列，可选 quantity / note 列。

    默认只创建器件，不建 StockItem（库存由用户手工管理）。
    用 --stock 可选建库存。
    用 --create-missing-category 允许自动创建本地不存在的 LCSC 分类。
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
        _run_batch_threaded(writer, rows, fetcher, update, stock, create_missing_category, workers)
    else:
        for r in rows:
            _import_one(writer, fetcher, r, update, stock, create_missing_category)


def _import_one(
    writer: InvenTreeWriter,
    fetcher: Fetcher,
    row: dict,
    update: bool,
    stock: bool,
    create_missing_category: bool = False,
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
    result = writer.upsert_part(part, options=opts, create_missing_category=create_missing_category)
    _print_result(code, result, batch_mode=True)


def _run_batch_threaded(
    writer: InvenTreeWriter,
    rows: list[dict],
    fetcher: Fetcher,
    update: bool,
    stock: bool,
    create_missing_category: bool,
    workers: int,
) -> None:
    """简化并发：多 worker 各自 new 一个 fetcher/writer 不可行（writer 共享 API）。"""

    console.print("[yellow]并发模式下仍顺序执行（InvenTree SDK 非线程安全）[/yellow]")
    for r in rows:
        _import_one(writer, fetcher, r, update, stock, create_missing_category)


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


def _print_result(code: str, result: WriteResult, *, batch_mode: bool = False) -> None:
    """打印导入结果。
    
    Args:
        code: LCSC C-code
        result: 导入结果
        batch_mode: 是否在批量导入模式下（批量模式下不打开浏览器）
    """
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

    # 抓取完成后打开浏览器（仅在非批量模式下）
    if not batch_mode:
        _open_browser_if_enabled(code, result)


def _open_browser_if_enabled(code: str, result: WriteResult) -> None:
    """根据配置决定是否在浏览器中打开新建 Part 的详情页。"""
    settings = get_settings()
    if not settings.open_browser:
        return

    if not result.ok() or result.part_pk is None:
        return

    try:
        # 构造 Part 详情页 URL：INVENTREE_URL + /part/ + part_pk
        base_url = settings.inventree_url.rstrip("/")
        part_url = f"{base_url}/part/{result.part_pk}/"

        logger.info("准备打开浏览器: %s", part_url)
        # 使用 new=2 在新标签页打开
        webbrowser.open(part_url, new=2)
    except Exception as exc:
        logger.warning("无法打开浏览器: %s", exc)


# ---------------------------------------------------------------------------
# backfill-package（一键回填封装信息）
# ---------------------------------------------------------------------------

# InvenTree Part.IPN 中识别 LCSC 器件：国际站 C28323 / 国内站 CN:360864
_LCSC_IPN_RE = re.compile(r"(?:C\d+|CN:\d+)")


@cli.command("backfill-package")
@_dry_run_option
@click.option("--limit", type=int, default=None, help="只处理前 N 个 Part（测试用）")
@click.pass_context
def backfill_package(
    ctx: click.Context, dry_run: bool | None, limit: int | None
) -> None:
    """为 InvenTree 中所有 LCSC 导入的器件回填封装信息。

    扫描 IPN 为 LCSC 编号（C123456 / CN:123456）的 Part，从 LCSC 重新抓取
    封装数据（1 req/s 限速 + 本地 HTML 缓存，重复执行很快），然后：

    \b
    - 描述尾部追加（封装：xxx）——基于 InvenTree 现有描述修改，不覆盖手工改动，
      描述里已含该封装串时跳过；
    - 写 `Package` 参数（模板不存在则自动创建）；
    - keywords 追加封装串。
    """
    _common_dry_run(ctx, dry_run)
    settings = get_settings()
    api = _connect_inventree(token=ctx.obj.get("token"))
    from inventree.part import Part  # 局部导入便于测试 patch

    writer = InvenTreeWriter(api, settings)
    fetcher = default_fetcher(settings)

    # 1. 扫描 InvenTree，收集 IPN 是 LCSC 编号的 Part
    targets: list[dict[str, Any]] = []
    for p in Part.list(api):
        ipn = (getattr(p, "IPN", None) or "").strip()
        if not _LCSC_IPN_RE.fullmatch(ipn):
            continue
        targets.append(
            {
                "pk": p.pk,
                "ipn": ipn,
                "description": getattr(p, "description", None) or "",
                "keywords": getattr(p, "keywords", None) or "",
            }
        )
    targets.sort(key=lambda t: t["ipn"])
    if limit is not None:
        targets = targets[:limit]
    if not targets:
        console.print("[yellow]没有找到 IPN 为 LCSC 编号的 Part，无可回填。[/yellow]")
        return

    mode = "[DRY] " if settings.dry_run else ""
    console.print(
        f"{mode}共找到 [bold]{len(targets)}[/bold] 个 LCSC 器件，开始回填封装…"
    )

    stats = {"todo": 0, "ok": 0, "no_package": 0, "fetch_failed": 0, "errors": 0}
    failed: list[str] = []
    total = len(targets)
    for i, t in enumerate(targets, 1):
        code = t["ipn"]
        try:
            part = fetcher.fetch(code)
        except LcscFetchError as exc:
            stats["fetch_failed"] += 1
            failed.append(f"{code}: 抓取失败 {exc}")
            console.print(f"[red][{i}/{total}] {code}: 抓取失败 {exc}[/red]")
            continue

        package = part.package
        if not package:
            stats["no_package"] += 1
            console.print(f"[dim][{i}/{total}] {code}: LCSC 无封装数据，跳过[/dim]")
            continue

        # 2. 基于 InvenTree 现有值计算变更（不覆盖手工修改）
        new_desc = InvenTreeWriter._with_package_suffix(t["description"], package)[:250]
        # keywords：封装值可能自带逗号（如 Through Hole,P=3.4mm），
        # 用整体子串判断避免重复追加，并顺带清洗历史重复
        kws = InvenTreeWriter._clean_keywords(t["keywords"])
        if package.lower() not in (t["keywords"] or "").lower():
            kws.append(package)
        new_kw = ",".join(kws)

        payload: dict[str, Any] = {}
        if new_desc != t["description"]:
            payload["description"] = new_desc
        if new_kw != t["keywords"]:
            payload["keywords"] = new_kw

        # 参数现状（只读查询；dry-run 也查，保证预览真实）
        param_cur = writer.get_package_parameter(part_pk=t["pk"])
        param_pending = param_cur != package

        if settings.dry_run:
            stats["todo"] += 1
            if payload and param_pending:
                console.print(
                    f"[cyan][{i}/{total}] {code}: 封装 {package} → "
                    f"描述/keywords + Package 参数 将更新[/cyan]"
                )
            elif payload:
                console.print(
                    f"[cyan][{i}/{total}] {code}: 封装 {package} → "
                    f"仅描述/keywords 将更新[/cyan]"
                )
            elif param_pending:
                console.print(
                    f"[cyan][{i}/{total}] {code}: 封装 {package} → "
                    f"仅补 Package 参数[/cyan]"
                )
            else:
                stats["todo"] -= 1
                stats["ok"] += 1
            continue

        # 3. 写入（幂等：重复执行无副作用）
        try:
            if payload:
                Part(api, t["pk"]).save(payload)
            if param_pending:
                if not writer.set_package_parameter(part_pk=t["pk"], value=package):
                    raise RuntimeError("Package 参数模板不可用（查询/创建失败）")
            if payload or param_pending:
                stats["todo"] += 1
                console.print(
                    f"[green][{i}/{total}] {code}: {package} ✓ "
                    f"{'描述/keywords ' if payload else ''}"
                    f"{'+ Package 参数' if param_pending else ''}已回填[/green]"
                )
            else:
                stats["ok"] += 1
        except Exception as exc:  # noqa: BLE001 — 单个失败不阻断批量
            stats["errors"] += 1
            failed.append(f"{code}: 写入失败 {type(exc).__name__}: {exc}")
            console.print(f"[red][{i}/{total}] {code}: 写入失败 {exc}[/red]")

    console.print(
        f"\n[bold]回填完成[/bold]：需更新 {stats['todo']}、"
        f"已是最新 {stats['ok']}、LCSC 无封装 {stats['no_package']}、"
        f"抓取失败 {stats['fetch_failed']}、写入失败 {stats['errors']}"
    )
    if failed:
        console.print("[red]失败明细（最多显示 20 条）:[/red]")
        for line in failed[:20]:
            console.print(f"  - {line}")


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
    except Exception as exc:
        raise click.ClickException(f"无法连接：{exc}") from exc
    # 1. /api/ ping
    try:
        from inventree.part import Part  # 探测
        n = len(Part.list(api))
        console.print(f"[green]✓ /api/part/ 可访问，当前 Part 数量: {n}[/green]")
    except Exception as exc:
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
