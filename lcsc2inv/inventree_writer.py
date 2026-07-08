"""基于 `inventree-python` SDK 的幂等 upsert 写入。

幂等键策略：
- Part: `IPN == LCSC C-code` （Part 名称可能同名；用 IPN 区分）
- Manufacturer: `name`
- Supplier: `name`（默认固定 "LCSC Electronics"）
- ManufacturerPart: `(part, manufacturer, MPN)`
- SupplierPart: `(part, supplier, SKU)`
- SupplierPriceBreak: 按 SupplierPart 替换（先 delete-all 再 add）

执行顺序：
1. 类别 Category（含类别路径递归创建）
2. Manufacturer (get_or_create)
3. Supplier (get_or_create)
4. Part (get_or_create)
5. PartParameter × N（按 template 名 get + 创建/更新值）
6. ManufacturerPart (get_or_create)
7. SupplierPart (get_or_create)
8. SupplierPriceBreak (replace all)
9. StockItem (optional, by quantity)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from inventree.api import InvenTreeAPI
from inventree.company import (
    Company,
    ManufacturerPart,
    SupplierPart,
    SupplierPriceBreak,
)
from inventree.part import (
    Part,
    PartCategory,
    PartParameter,
    PartParameterTemplate,
)
from inventree.stock import StockItem

from lcsc2inv.categorizer import CategoryMatch, match as categorizer_match
from lcsc2inv.config import Settings, get_settings
from lcsc2inv.lcsc_models import LCSCPart
from lcsc2inv.mapping import to_inventree_parameters, to_part_notes

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据类：写操作的"建议"，由 writer 落地
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WriteResult:
    """一次 upsert 的结果摘要。"""

    part_pk: int | None = None
    manufacturer_pk: int | None = None
    supplier_pk: int | None = None
    manufacturer_part_pk: int | None = None
    supplier_part_pk: int | None = None
    stock_item_pk: int | None = None
    created: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.errors and self.part_pk is not None

    def summary(self) -> str:
        return (
            f"part#{self.part_pk} mfr#{self.manufacturer_pk} "
            f"sup#{self.supplier_pk} mp#{self.manufacturer_part_pk} "
            f"sp#{self.supplier_part_pk} stock#{self.stock_item_pk}"
        )


@dataclass(slots=True)
class WriteOptions:
    """单次写入的可选项。"""

    update_existing: bool = False  # True 时更新已存在 Part 的 description/notes/image
    create_stock: bool = True
    quantity: int | None = None
    extra_note: str | None = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class InvenTreeWriter:
    """对 LCSCPart 做幂等 upsert 的高层封装。"""

    def __init__(self, api: InvenTreeAPI, settings: Settings | None = None):
        self.api = api
        self.settings = settings or get_settings()
        self.supplier_name = self.settings.inventree_supplier_name or "LCSC Electronics"
        # 缓存：name -> pk
        self._mfr_cache: dict[str, int] = {}
        self._supplier_cache: dict[str, int] = {}
        self._category_cache: dict[str, int] = {}
        self._template_cache: dict[str, int] = {}

    # ---- public ---------------------------------------------------------

    def upsert_part(
        self,
        part: LCSCPart,
        *,
        options: WriteOptions | None = None,
    ) -> WriteResult:
        """将一个 LCSCPart 完整写入 InvenTree。"""
        opts = options or WriteOptions()
        result = WriteResult()
        if opts.dry_run:
            opts.create_stock = False  # dry-run 永远不建 StockItem
        try:
            cat_match = categorizer_match(part)
            cat_pk = self._ensure_category(cat_match, dry_run=opts.dry_run)
            if cat_pk is None and cat_match.category_path != "__uncategorized__":
                result.errors.append(f"无法创建类别 {cat_match.category_path}")

            mfr_pk = self._ensure_manufacturer(part.manufacturer_name, dry_run=opts.dry_run)
            result.manufacturer_pk = mfr_pk

            sup_pk = self._ensure_supplier(self.supplier_name, dry_run=opts.dry_run)
            result.supplier_pk = sup_pk

            # 4. Part
            part_pk, created = self._ensure_part(
                part,
                category_pk=cat_pk,
                update=opts.update_existing,
                dry_run=opts.dry_run,
            )
            result.part_pk = part_pk
            result.created["part"] = created

            if opts.dry_run or part_pk is None:
                return result

            # 5. PartParameters
            self._write_parameters(
                part,
                category_top=cat_match.category_path.split("/")[0] if cat_match.category_path else None,
                category_sub=(
                    cat_match.category_path.split("/", 2)[1]
                    if cat_match.category_path and "/" in cat_match.category_path
                    else None
                ),
                part_pk=part_pk,
            )

            # 6. ManufacturerPart
            if mfr_pk and part.mpn:
                mp_pk, mp_created = self._ensure_manufacturer_part(
                    part_pk, mfr_pk, part.mpn, dry_run=opts.dry_run
                )
                result.manufacturer_part_pk = mp_pk
                result.created["manufacturer_part"] = mp_created

            # 7. SupplierPart
            sp_pk = None
            if sup_pk:
                sp_pk, sp_created = self._ensure_supplier_part(
                    part_pk=part_pk,
                    supplier_pk=sup_pk,
                    sku=part.lcsc_code,
                    manufacturer_part_pk=result.manufacturer_part_pk,
                    dry_run=opts.dry_run,
                )
                result.supplier_part_pk = sp_pk
                result.created["supplier_part"] = sp_created

                # 8. PriceBreaks（基于 LCSC offers.price 单档 + inventory_level 作 pack qty hint）
                if sp_pk and part.offer and part.offer.price is not None:
                    self._replace_price_breaks(
                        sp_pk,
                        [(1, part.offer.price, part.offer.price_currency)],
                        dry_run=opts.dry_run,
                    )

            # 9. StockItem
            if opts.create_stock and sp_pk:
                qty = opts.quantity or (part.offer.inventory_level if part.offer else None)
                if qty:
                    stock_pk = self._ensure_stock(
                        part_pk=part_pk,
                        supplier_part_pk=sp_pk,
                        quantity=qty,
                        price=part.offer.price if part.offer else None,
                        currency=part.offer.price_currency if part.offer else None,
                        dry_run=opts.dry_run,
                    )
                    result.stock_item_pk = stock_pk

        except Exception as exc:  # noqa: BLE001 — 写入失败时记录详细原因给调用方
            logger.exception("upsert_part 失败 sku=%s", part.sku)
            result.errors.append(f"{type(exc).__name__}: {exc}")
        return result

    # ---- category --------------------------------------------------------

    def _ensure_category(self, match: CategoryMatch, *, dry_run: bool) -> int | None:
        if match.category_path == "__uncategorized__":
            logger.warning("[%s] 未匹配到分类；落到 Uncategorized", match.category_path)
            return None
        if dry_run:
            logger.info("[DRY] category ensure: %s (source=%s score=%d)",
                        match.category_path, match.source, match.score)
            return None
        # 逐层创建
        parent_pk: int | None = None
        pk: int | None = None
        for i, segment in enumerate(match.category_path.split("/")):
            key = f"{parent_pk or ''}/{segment}" if parent_pk else segment
            if key in self._category_cache:
                pk = self._category_cache[key]
                parent_pk = pk
                continue
            pk = self._get_category_by_name(segment, parent_pk)
            if pk is None:
                cat = PartCategory.create(
                    self.api,
                    {"name": segment, "description": "auto-created by lcsc2inventree",
                     "parent": parent_pk},
                )
                pk = cat.pk
            self._category_cache[key] = pk
            parent_pk = pk
        return pk

    def _get_category_by_name(self, name: str, parent_pk: int | None) -> int | None:
        """线性搜索 PartCategory 全表，按 (name, parent) 命中。"""
        for cat in PartCategory.list(self.api):
            if cat.name == name and (cat.parent or None) == (parent_pk or None):
                return cat.pk
        return None

    # ---- manufacturer / supplier ---------------------------------------

    def _ensure_manufacturer(self, name: str | None, *, dry_run: bool) -> int | None:
        if not name:
            return None
        if dry_run:
            return None
        if name in self._mfr_cache:
            return self._mfr_cache[name]
        pk = self._get_company_pk(name, must_be="manufacturer")
        if pk is None:
            obj = Company.create(
                self.api,
                {"name": name, "description": "auto-created by lcsc2inventree",
                 "is_manufacturer": True, "is_supplier": False},
            )
            pk = obj.pk
        self._mfr_cache[name] = pk
        return pk

    def _ensure_supplier(self, name: str, *, dry_run: bool) -> int | None:
        if dry_run:
            return None
        if name in self._supplier_cache:
            return self._supplier_cache[name]
        pk = self._get_company_pk(name, must_be="supplier")
        if pk is None:
            obj = Company.create(
                self.api,
                {"name": name, "description": "auto-created by lcsc2inventree",
                 "is_supplier": True, "is_manufacturer": False},
            )
            pk = obj.pk
        self._supplier_cache[name] = pk
        return pk

    def _get_company_pk(self, name: str, *, must_be: str) -> int | None:
        """搜索现有 Company，按 name + 角色命中。"""
        for c in Company.list(self.api):
            if c.name != name:
                continue
            if must_be == "manufacturer" and not getattr(c, "is_manufacturer", False):
                continue
            if must_be == "supplier" and not getattr(c, "is_supplier", False):
                continue
            return c.pk
        return None

    # ---- part -----------------------------------------------------------

    def _ensure_part(
        self,
        part: LCSCPart,
        *,
        category_pk: int | None,
        update: bool,
        dry_run: bool,
    ) -> tuple[int | None, bool]:
        """创建或更新 Part；返回 (pk, created)。"""
        notes = to_part_notes(part)  # 仅 dry-run 用
        if dry_run:
            logger.info("[DRY] part upsert sku=%s name=%s mpn=%s",
                        part.sku, part.name, part.mpn)
            return None, False

        # 查找已存在 (按 IPN)
        existing_pk = self._find_part_by_ipn(part.sku)
        payload: dict[str, Any] = {
            "name": part.name or part.mpn or part.sku,
            "description": (part.description or "")[:250],
            "IPN": part.sku,
            "keywords": self._build_keywords(part),
            "link": part.page_url,
            "active": True,
            "purchaseable": True,
            "component": True,
            "assembly": False,
        }
        if category_pk:
            payload["category"] = category_pk
        if notes:
            payload["notes"] = notes

        if existing_pk is None:
            obj = Part.create(self.api, payload)
            return obj.pk, True

        # 已存在
        if update:
            Part(self.api, existing_pk).save(payload)
            logger.info("已更新 Part pk=%d sku=%s", existing_pk, part.sku)
        return existing_pk, False

    def _find_part_by_ipn(self, ipn: str) -> int | None:
        """按 IPN 全表扫描找到 Part pk。"""
        # 优先用 search（更快）；fallback 用 list。
        try:
            results = Part.search(self.api, search=ipn, search_in="IPN")
        except Exception:  # noqa: BLE001
            results = None
        if results:
            for p in results:
                if getattr(p, "IPN", None) == ipn:
                    return p.pk
        for p in Part.list(self.api):
            if getattr(p, "IPN", None) == ipn:
                return p.pk
        return None

    @staticmethod
    def _build_keywords(part: LCSCPart) -> str:
        bits: list[str] = [part.sku]
        if part.mpn:
            bits.append(part.mpn)
        if part.manufacturer_name:
            bits.append(part.manufacturer_name)
        if part.category_top:
            bits.append(part.category_top)
        return ",".join(b for b in bits if b)

    # ---- parameters -----------------------------------------------------

    def _write_parameters(
        self,
        part: LCSCPart,
        *,
        category_top: str | None,
        category_sub: str | None,
        part_pk: int,
    ) -> None:
        params = to_inventree_parameters(
            part, category_top=category_top, category_sub=category_sub
        )
        if not params:
            return
        # 1. 找出已存在的 parameter（按 template 名）
        existing: dict[str, PartParameter] = {}
        try:
            for pp in PartParameter.list(self.api, part=part_pk):
                tpl = getattr(pp, "template", None)
                if tpl is None:
                    continue
                tpl_name = getattr(tpl, "name", None) if not isinstance(tpl, int) else None
                if tpl_name:
                    existing[tpl_name] = pp
        except Exception:  # noqa: BLE001
            pass
        for inv_name, body in params.items():
            tpl_pk = self._ensure_template(inv_name)
            if tpl_pk is None:
                continue
            value = body["value"]
            if inv_name in existing:
                existing[inv_name].save({"value": value})
            else:
                PartParameter.create(
                    self.api,
                    {"part": part_pk, "template": tpl_pk, "value": value},
                )

    def _ensure_template(self, name: str) -> int | None:
        """确保 PartParameterTemplate 存在；返回 pk。"""
        if name in self._template_cache:
            return self._template_cache[name]
        # 搜索现有
        try:
            for t in PartParameterTemplate.list(self.api):
                if getattr(t, "name", None) == name:
                    self._template_cache[name] = t.pk
                    return t.pk
        except Exception:  # noqa: BLE001
            pass
        # 创建
        obj = PartParameterTemplate.create(
            self.api,
            {"name": name, "description": "auto-created by lcsc2inventree"},
        )
        self._template_cache[name] = obj.pk
        return obj.pk

    # ---- manufacturer_part / supplier_part -----------------------------

    def _ensure_manufacturer_part(
        self, part_pk: int, mfr_pk: int, mpn: str, *, dry_run: bool
    ) -> tuple[int | None, bool]:
        if dry_run:
            return None, False
        for mp in ManufacturerPart.list(self.api):
            if (
                getattr(mp, "part", None) == part_pk
                and getattr(mp, "manufacturer", None) == mfr_pk
                and getattr(mp, "MPN", None) == mpn
            ):
                return mp.pk, False
        obj = ManufacturerPart.create(
            self.api,
            {"part": part_pk, "manufacturer": mfr_pk, "MPN": mpn},
        )
        return obj.pk, True

    def _ensure_supplier_part(
        self,
        *,
        part_pk: int,
        supplier_pk: int,
        sku: str,
        manufacturer_part_pk: int | None,
        dry_run: bool,
    ) -> tuple[int | None, bool]:
        if dry_run:
            return None, False
        for sp in SupplierPart.list(self.api):
            if (
                getattr(sp, "part", None) == part_pk
                and getattr(sp, "supplier", None) == supplier_pk
                and getattr(sp, "SKU", None) == sku
            ):
                return sp.pk, False
        payload: dict[str, Any] = {
            "part": part_pk,
            "supplier": supplier_pk,
            "SKU": sku,
            "pack_quantity": 1,
            "packaging": "Cut Tape",
        }
        if manufacturer_part_pk:
            payload["manufacturer_part"] = manufacturer_part_pk
        obj = SupplierPart.create(self.api, payload)
        return obj.pk, True

    def _replace_price_breaks(
        self, sp_pk: int, breaks: list[tuple[int, float, str]], *, dry_run: bool
    ) -> None:
        if dry_run or not breaks:
            return
        # 先删除已有
        try:
            for pb in SupplierPriceBreak.list(self.api, part=sp_pk):
                pb.delete()
        except Exception:  # noqa: BLE001
            pass
        for qty, price, currency in breaks:
            SupplierPriceBreak.create(
                self.api,
                {"part": sp_pk, "quantity": qty, "price": str(price),
                 "price_currency": currency},
            )

    # ---- stock ---------------------------------------------------------

    def _ensure_stock(
        self,
        *,
        part_pk: int,
        supplier_part_pk: int,
        quantity: int,
        price: float | None,
        currency: str | None,
        dry_run: bool,
    ) -> int | None:
        if dry_run:
            return None
        # 简化为"创建一条新 StockItem"——用户可手工调拨合并；不做幂等累加
        payload: dict[str, Any] = {
            "part": part_pk,
            "quantity": quantity,
            "supplier_part": supplier_part_pk,
        }
        if price is not None:
            payload["purchase_price"] = str(price)
            payload["purchase_price_currency"] = currency or "USD"
        obj = StockItem.create(self.api, payload)
        return obj.pk