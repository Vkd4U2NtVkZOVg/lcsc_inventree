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
import re
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
)
from inventree.stock import StockItem

from lcsc2inv.categorizer import CategoryMatch, match as categorizer_match
from lcsc2inv.config import Settings, get_settings
from lcsc2inv.lcsc_client import Fetcher, LcscFetchError, default_fetcher
from lcsc2inv.lcsc_models import LCSCPart
from lcsc2inv.mapping import to_inventree_parameters, to_part_notes

logger = logging.getLogger(__name__)

# InvenTree 字段长度限制（common.models.ParameterTemplate.name=100 /
# Parameter.data=500）；超长会 400
PARAM_NAME_MAX = 100
PARAM_VALUE_MAX = 500


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
    # 图片上传状态
    image_uploaded: bool = False
    image_url: str | None = None
    image_skipped_reason: str | None = None
    created: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.errors and self.part_pk is not None

    def summary(self) -> str:
        return (
            f"part#{self.part_pk} mfr#{self.manufacturer_pk} "
            f"sup#{self.supplier_pk} mp#{self.manufacturer_part_pk} "
            f"sp#{self.supplier_part_pk} stock#{self.stock_item_pk} "
            f"img#{int(self.image_uploaded)}"
        )


@dataclass(slots=True)
class WriteOptions:
    """单次写入的可选项。"""

    update_existing: bool = False  # True 时更新已存在 Part 的 description/notes/image
    # 默认 False：只建器件，不建 StockItem（库存应由用户手工管理）
    create_stock: bool = False
    quantity: int | None = None
    extra_note: str | None = None
    dry_run: bool = False
    # 图片上传：复用 CLI 已有的 Fetcher 实例以共享 1 req/s 限速
    fetcher: Fetcher | None = None
    # --update 时为 True，强制覆盖已有图片
    force_image_upload: bool = False
    # 显式指定封装（如订单导入解析出的「封装」列）；优先于 LCSC 数据里的 Package
    footprint: str | None = None


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
        create_missing_category: bool = False,
    ) -> WriteResult:
        """将一个 LCSCPart 完整写入 InvenTree。

        Args:
            create_missing_category: True 时，LCSC 分类在 InvenTree 不存在则自动创建。
        """
        opts = options or WriteOptions()
        result = WriteResult()
        if opts.dry_run:
            opts.create_stock = False  # dry-run 永远不建 StockItem
        try:
            # 使用 create_missing_category 参数调用 categorizer
            cat_match = categorizer_match(part, create_missing_category=create_missing_category)
            cat_pk = self._ensure_category(cat_match, dry_run=opts.dry_run)
            if cat_pk is None and cat_match.category_path != "__uncategorized__":
                result.errors.append(f"无法创建类别 {cat_match.category_path}")

            # 分类映射参数只算一次：写 PartParameter + 描述后缀共用
            mapped = to_inventree_parameters(
                part,
                category_top=(cat_match.category_path.split("/")[0]
                              if cat_match.category_path
                              and not cat_match.category_path.startswith("__")
                              else None),
                category_sub=(
                    cat_match.category_path.split("/", 2)[1]
                    if cat_match.category_path and "/" in cat_match.category_path
                    and not cat_match.category_path.startswith("__")
                    else None
                ),
            )

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
                package=self._resolve_package(part, opts.footprint),
                mapped=mapped,
            )
            result.part_pk = part_pk
            result.created["part"] = created

            # 4b. 图片上传（best-effort；失败不影响后续步骤）
            # 放在 dry-run 守卫之前：dry-run 时仍记录 image_url 让 CLI 展示
            self._upload_image(
                part,
                part_pk=part_pk,
                result=result,
                fetcher=opts.fetcher,
                force=opts.force_image_upload,
            )

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
                mapped=mapped,
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
        # 处理 "__create:<path>" 格式：LCSC 分类本地不存在，需要创建
        if match.category_path.startswith("__create:"):
            raw_path = match.category_path[len("__create:"):]
            logger.info("创建缺失的 InvenTree 分类: %s (来自 LCSC)", raw_path)
            if dry_run:
                logger.info("[DRY] 创建分类: %s", raw_path)
                return None
            # 逐层创建
            parent_pk: int | None = None
            pk: int | None = None
            for segment in raw_path.split("/"):
                segment = segment.strip()
                if not segment:
                    continue
                key = f"{parent_pk or ''}/{segment}" if parent_pk else segment
                if key in self._category_cache:
                    pk = self._category_cache[key]
                    parent_pk = pk
                    continue
                pk = self._get_category_by_name(segment, parent_pk)
                if pk is None:
                    cat = PartCategory.create(
                        self.api,
                        {"name": segment, "description": "auto-created by lcsc2inventree from LCSC",
                         "parent": parent_pk},
                    )
                    pk = cat.pk
                self._category_cache[key] = pk
                parent_pk = pk
            return pk

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
        package: str | None = None,
        mapped: dict[str, dict[str, str]] | None = None,
    ) -> tuple[int | None, bool]:
        """创建或更新 Part；返回 (pk, created)。

        `mapped`：分类映射参数（to_inventree_parameters 结果）——其**值**
        会以 `[参数: v1 | v2 | …]` 形式并入描述，保证全局搜索可命中。
        """
        notes = to_part_notes(part)  # 仅 dry-run 用
        if dry_run:
            logger.info("[DRY] part upsert sku=%s name=%s mpn=%s",
                        part.sku, part.name, part.mpn)
            return None, False

        # 查找已存在 (按 IPN)
        existing_pk = self._find_part_by_ipn(part.sku)
        payload: dict[str, Any] = {
            "name": part.name or part.mpn or part.sku,
            "description": self._with_params_suffix(
                self._with_package_suffix(part.description or "", package),
                self._mapped_values(mapped),
            )[:250],
            "IPN": part.sku,
            "keywords": self._build_keywords(part, package=package),
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

    # ---- image upload --------------------------------------------------

    def _upload_image(
        self,
        part: LCSCPart,
        *,
        part_pk: int | None,
        result: WriteResult,
        fetcher: Fetcher | None = None,
        force: bool = False,
    ) -> None:
        """把 LCSC image_urls[0] 上传到 InvenTree Part.image。

        行为：
        - `lcsc_upload_image=false` → 跳过，记 image_skipped_reason="disabled"
        - Part.image 已有值且非 force → 跳过，记 "already-uploaded"
        - 其它情况：下载到本地缓存 → 调 `Part.uploadImage(path)` → 删本地文件
        - 任何步骤失败：warning log + 记 skip reason；不影响 part_pk / 后续步骤

        复用 `opts.fetcher` 共享 1 req/s 限速；没传则新建一个（限速独立计时）。
        """
        if not self.settings.lcsc_upload_image:
            result.image_url = part.image_urls[0] if part.image_urls else None
            result.image_skipped_reason = "disabled"
            return
        if not part.image_urls:
            result.image_skipped_reason = "no-image-on-lcsc"
            return
        url = part.image_urls[0]
        result.image_url = url

        # dry-run 不下载/不上传，仅记录 url
        if part_pk is None:
            result.image_skipped_reason = "dry-run"
            return

        # 幂等：Part 已有图片且非强制 → 跳过
        if not force:
            try:
                existing = Part(self.api, part_pk)
                if getattr(existing, "image", None):
                    result.image_skipped_reason = "already-uploaded"
                    return
            except Exception as exc:  # noqa: BLE001
                logger.debug("查询 Part.image 失败（将继续尝试上传）pk=%s: %s", part_pk, exc)

        # 下载到本地缓存（限速通过 fetcher 共享）
        f = fetcher or default_fetcher(self.settings)
        try:
            local_path = f.download_image(part.sku, url)
        except (LcscFetchError, Exception) as exc:  # noqa: BLE001
            logger.warning("LCSC image download failed sku=%s: %s", part.sku, exc)
            result.image_skipped_reason = f"download-failed: {type(exc).__name__}"
            return

        # 上传（Part.uploadImage 需要文件路径，SDK 暂不支持 BytesIO）
        try:
            Part(self.api, part_pk).uploadImage(str(local_path))
            result.image_uploaded = True
            result.image_skipped_reason = None
            logger.info("Part image uploaded sku=%s url=%s", part.sku, url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("InvenTree image upload failed sku=%s: %s", part.sku, exc)
            result.image_skipped_reason = f"upload-failed: {type(exc).__name__}"
        finally:
            try:
                local_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _resolve_package(part: LCSCPart, explicit: str | None = None) -> str | None:
        """封装取值：显式指定（如订单导入的「封装」列）优先于 LCSC 参数表。"""
        for cand in (explicit, part.package):
            if cand and str(cand).strip():
                return str(cand).strip()
        return None

    # InvenTree 全局搜索不索引 PartParameter，只搜名称/IPN/描述/关键词——
    # 把映射参数的值并入描述以保证可搜索。已有段用该正则定位替换（幂等）。
    _PARAMS_SUFFIX_RE = re.compile(r"\s*\[参数[：:][^\]]*\]\s*$")

    @classmethod
    def _mapped_values(cls, mapped: dict[str, dict[str, str]] | None) -> dict[str, str]:
        """从 to_inventree_parameters 结果提取 {模板名: 清洗后的值}。"""
        if not mapped:
            return {}
        return {
            str(k): str(v["value"]).strip()
            for k, v in mapped.items() if v.get("value")
        }

    @classmethod
    def _with_params_suffix(
        cls, description: str, params: dict[str, str] | None
    ) -> str:
        """把映射参数的**值**并入描述尾部：`… [参数: 10k | ±1% | 0602]`。

        - 幂等：已有 `[参数: …]` 段整体替换为新值；
        - 描述总长超 250 时从尾部丢弃参数值直到放得下（放不下就不加）。
        """
        base = cls._PARAMS_SUFFIX_RE.sub("", description or "").rstrip()
        values = [str(v).strip() for v in (params or {}).values() if str(v).strip()]
        if not values:
            return base
        budget = 250 - len(base) - 4  # " [参数: " 前后按最小 4 字符余量估计
        while values and budget < len(" | ".join(values)):
            values.pop()
        if not values:
            return base
        return f"{base} [参数: {' | '.join(values)}]"[:250]

    @staticmethod
    def _clean_keywords(raw: str) -> list[str]:
        """keywords 去重清洗：去空白、大小写不敏感去重、保序。

        注意封装值本身可能含逗号（如 `Through Hole,P=3.4mm`），因此 keywords
        必须按逗号切分去重后整体重建，否则同一封装会被重复追加。
        """
        seen: set[str] = set()
        out: list[str] = []
        for k in (raw or "").split(","):
            k = k.strip()
            if not k:
                continue
            key = k.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(k)
        return out

    @staticmethod
    def _with_package_suffix(description: str, package: str | None) -> str:
        """把封装追加到描述尾部，保证列表页/详情页直接可见。

        描述里已包含该封装串时不重复追加。
        """
        if not package:
            return description
        if package.lower() in (description or "").lower():
            return description
        if not (description or "").strip():
            return f"封装：{package}"
        return f"{description}（封装：{package}）"

    @staticmethod
    def _build_keywords(part: LCSCPart, package: str | None = None) -> str:
        bits: list[str] = [part.sku]
        if part.mpn:
            bits.append(part.mpn)
        if part.manufacturer_name:
            bits.append(part.manufacturer_name)
        if part.category_top:
            bits.append(part.category_top)
        pkg = package or part.package
        if pkg:
            bits.append(pkg)
        return ",".join(b for b in bits if b)

    # ---- 更新已有 Part --------------------------------------------------

    def update_part_fields(
        self,
        part: LCSCPart,
        *,
        part_pk: int,
        update_name: bool = False,
        update_description: bool = True,
        update_image: bool = True,
        update_keywords: bool = True,
        update_notes: bool = False,
        update_parameters: bool = False,
        footprint: str | None = None,
        fetcher: Fetcher | None = None,
    ) -> dict:
        """用 LCSC 数据更新**已存在**的 Part（不新建、不动分类/厂商/库存）。

        Args:
            part: 从 LCSC 抓取的商品数据。
            part_pk: 要更新的 InvenTree Part 主键。
            update_*: 各字段开关；图片为强制重传（更新场景的核心诉求）；
                名称默认不更新（避免覆盖自定义命名）。
            footprint: 显式封装（如订单导入的「封装」列）；缺省用 LCSC 数据。

        Returns:
            {
              "updated_fields": [...],   # 成功保存的 Part 字段名
              "image": {"uploaded": bool, "skipped_reason": str|None,
                        "url": str|None} | None,
              "parameters": {"written": True} | None,
              "errors": [str, ...],      # 字段级失败原因（部分成功也算）
            }

        Raises:
            ValueError: part_pk 对应的 Part 不存在或无法访问。
        """
        try:
            obj = Part(self.api, part_pk)
            _ = obj.pk  # 触发加载，尽早暴露不存在的 pk
        except Exception as exc:  # noqa: BLE001 — 统一转为清晰的 ValueError
            raise ValueError(f"Part pk={part_pk} 不存在或无法访问: {exc}") from exc

        out: dict = {
            "updated_fields": [],
            "image": None,
            "parameters": None,
            "errors": [],
        }

        # 国内站页面（szlcsc）的 ld+json 不带参数表：若本次抓到的数据
        # 没有任何参数、而现有 Part 的 keywords 首段是 C-code，则改用该
        # 编号从国际站补抓完整参数，避免描述/参数被降级覆盖
        if not part.additional_properties:
            kw_first = (getattr(obj, "keywords", None) or "").split(",")[0].strip()
            if re.fullmatch(r"C\d+", kw_first):
                try:
                    f = fetcher or default_fetcher(self.settings)
                    richer = f.fetch(kw_first)
                    if richer.additional_properties:
                        logger.info(
                            "国内站数据无参数，改用 %s 从国际站补抓（part_pk=%s）",
                            kw_first, part_pk,
                        )
                        part = richer
                except LcscFetchError as exc:
                    logger.warning("按 %s 补抓国际站参数失败: %s", kw_first, exc)

        package = self._resolve_package(part, footprint)

        # 分类映射参数只算一次：描述后缀 + 参数写入共用
        mapped: dict[str, dict[str, str]] | None = None
        try:
            match = categorizer_match(part, create_missing_category=False)
            path = match.category_path or ""
            top = path.split("/")[0] if path and not path.startswith("__") else None
            sub = path.split("/", 2)[1] if path and "/" in path else None
            mapped = to_inventree_parameters(part, category_top=top, category_sub=sub)
        except Exception:  # noqa: BLE001 — 映射失败不影响其它字段
            mapped = None

        # 1) 简单文本字段（一次 save）
        payload: dict[str, Any] = {}
        if update_name:
            # 与创建时一致的命名逻辑：LCSC 名称 → MPN → SKU
            payload["name"] = part.name or part.mpn or part.sku
        if update_description:
            payload["description"] = self._with_params_suffix(
                self._with_package_suffix(part.description or "", package),
                self._mapped_values(mapped),
            )[:250]
        if update_keywords:
            payload["keywords"] = self._build_keywords(part, package=package)
        if update_notes:
            payload["notes"] = to_part_notes(part)
            payload["link"] = part.page_url
        if payload:
            try:
                obj.save(payload)
                out["updated_fields"] = sorted(payload.keys())
            except Exception as exc:  # noqa: BLE001 — 字段级失败不阻断图片
                out["errors"].append(f"字段保存失败: {type(exc).__name__}: {exc}")

        # 2) 图片（强制重传；best-effort，失败记录 skip reason）
        if update_image:
            img_result = WriteResult()
            self._upload_image(
                part,
                part_pk=part_pk,
                result=img_result,
                fetcher=fetcher,
                force=True,
            )
            out["image"] = {
                "uploaded": img_result.image_uploaded,
                "skipped_reason": img_result.image_skipped_reason,
                "url": img_result.image_url,
            }

        # 3) 参数（按 LCSC 分类映射模板；只写不删）
        if update_parameters:
            try:
                self._write_parameters(
                    part, category_top=None, category_sub=None,
                    part_pk=part_pk, mapped=mapped,
                )
                out["parameters"] = {"written": True}
            except Exception as exc:  # noqa: BLE001 — 参数失败不影响其它字段
                out["errors"].append(f"参数写入失败: {type(exc).__name__}: {exc}")

        return out

    def update_custom_part_fields(
        self,
        *,
        part_pk: int,
        name: str | None = None,
        description: str | None = None,
        notes: str | None = None,
        keywords: str | None = None,
        link: str | None = None,
        parameters: dict[str, str] | None = None,
        image_data: tuple[bytes, str] | None = None,
        update_name: bool = False,
        update_description: bool = True,
        update_image: bool = True,
        update_keywords: bool = True,
        update_notes: bool = False,
        update_parameters: bool = False,
    ) -> dict:
        """用自定义来源（淘宝 mhtml 等）数据更新**已存在**的 Part。

        与 `update_part_fields` 的区别：数据由调用方预算好（不抓 LCSC），
        图片为内存字节（webp 自动转 jpg，强制重传），参数为任意键值对。

        Returns:
            结构同 `update_part_fields`：
            {"updated_fields": [...], "image": {...},
             "parameters": {"written": True}|None, "errors": [...]}

        Raises:
            ValueError: part_pk 不存在或无法访问。
        """
        try:
            obj = Part(self.api, part_pk)
            _ = obj.pk  # 触发加载，尽早暴露不存在的 pk
        except Exception as exc:  # noqa: BLE001 — 统一转为清晰的 ValueError
            raise ValueError(f"Part pk={part_pk} 不存在或无法访问: {exc}") from exc

        out: dict = {
            "updated_fields": [],
            "image": None,
            "parameters": None,
            "errors": [],
        }

        payload: dict[str, Any] = {}
        if update_name and name:
            payload["name"] = name[:100]
        if update_description and description is not None:
            payload["description"] = self._with_params_suffix(
                description, parameters
            )[:250]
        if update_keywords and keywords:
            payload["keywords"] = keywords
        if update_notes:
            if notes:
                payload["notes"] = notes
            if link:
                payload["link"] = link
        if payload:
            try:
                obj.save(payload)
                out["updated_fields"] = sorted(payload.keys())
            except Exception as exc:  # noqa: BLE001 — 字段失败不阻断图片
                out["errors"].append(f"字段保存失败: {type(exc).__name__}: {exc}")

        if update_image:
            if image_data:
                data, ext = image_data
                uploaded, reason = self._upload_image_bytes(
                    part_pk=part_pk, data=data, ext=ext
                )
                out["image"] = {
                    "uploaded": uploaded, "skipped_reason": reason, "url": None,
                }
            else:
                out["image"] = {
                    "uploaded": False, "skipped_reason": "no-image", "url": None,
                }

        if update_parameters:
            written = 0
            for pname, pvalue in (parameters or {}).items():
                if not pname or not pvalue:
                    continue
                try:
                    if self.set_named_parameter(
                        part_pk=part_pk, name=pname, value=str(pvalue)
                    ):
                        written += 1
                except RuntimeError as exc:
                    out["errors"].append(f"参数 {pname}: {exc}")
            out["parameters"] = {"written": True} if written else None

        return out

    # ---- parameters -----------------------------------------------------
    # 参数相关操作直接走 REST API：当前 inventree-python 0.23.2 的
    # MAX_API_VERSION=428 低于服务端 API 530，SDK 的 PartParameter* /
    # PartParameterTemplate* 一律抛 NotImplementedError，静默跳过会导致
    # 所有导入都写不上参数（这正是封装参数缺失的根因）。

    def _rest(
        self, method: str, path: str, *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        """直接调用 InvenTree REST API（绕过 SDK 版本门禁）。

        Returns:
            (status_code, 解析后的 JSON；非 JSON 响应为 None)
        """
        import requests

        resp = requests.request(
            method,
            f"{self.settings.inventree_url.rstrip('/')}{path}",
            params=params,
            json=json,
            headers={"Authorization": f"Token {self.settings.inventree_token}"},
            timeout=20,
        )
        try:
            data = resp.json()
        except ValueError:
            data = None
        return resp.status_code, data

    @staticmethod
    def _rest_results(data: Any) -> list[dict[str, Any]]:
        """兼容分页 dict 与裸 list 两种响应。"""
        if isinstance(data, dict):
            return data.get("results") or []
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def _clean_param_name(name: str) -> str:
        """参数模板名清洗 + 截断（InvenTree 限 100 字符）。"""
        return str(name or "").strip()[:PARAM_NAME_MAX]

    @staticmethod
    def _clean_param_value(value: str) -> str:
        """参数值清洗 + 截断（InvenTree 限 500 字符，超长会 400）。"""
        return str(value or "").replace("\r", "").strip()[:PARAM_VALUE_MAX]

    def _param_api_style(self) -> str:
        """探测参数 API 形态并缓存。

        - 'new'：InvenTree 1.5+（模型迁到 common app，`/api/parameter/`，
          字段 model_type/model_id/data）
        - 'old'：旧版（`/api/part/parameter/`，字段 part/value）
        """
        if getattr(self, "_param_style", None):
            return self._param_style
        for style, probe in (
            ("new", "/api/parameter/template/"),
            ("old", "/api/part/parameter/template/"),
        ):
            code, _ = self._rest("GET", probe, params={"limit": 1})
            if code == 200:
                self._param_style = style
                logger.info("InvenTree 参数 API 形态: %s (%s)", style, probe)
                return style
        # 两个端点都探测失败：按 new 处理，让具体错误在写入处暴露
        self._param_style = "new"
        return self._param_style

    def _param_url(self, kind: str) -> str:
        base = (
            "/api/parameter/" if self._param_api_style() == "new"
            else "/api/part/parameter/"
        )
        return f"{base}template/" if kind == "template" else base

    def _param_link_payload(
        self, part_pk: int, tpl_pk: int, value: str
    ) -> dict[str, Any]:
        if self._param_api_style() == "new":
            return {"model_type": "part", "model_id": part_pk,
                    "template": tpl_pk, "data": value}
        return {"part": part_pk, "template": tpl_pk, "value": value}

    def _list_part_parameters(self, part_pk: int) -> list[dict[str, Any]]:
        """列出一个 Part 的全部参数，归一化为 [{pk, template, value}]。

        `template` 尽量归一化为模板名：`template_detail` 存在时取 name，
        否则保留模板 pk（调用方按「名字或 pk」混合键使用时注意一致性）。
        """
        params: dict[str, Any] = {"limit": 500}
        if self._param_api_style() == "new":
            params.update({"model_type": "part", "model_id": part_pk})
        else:
            params["part"] = part_pk
        code, data = self._rest("GET", self._param_url("param"), params=params)
        if code != 200:
            raise RuntimeError(f"查询 Part 参数失败 HTTP {code}")
        out: list[dict[str, Any]] = []
        for pp in self._rest_results(data):
            if pp.get("template") is None:
                continue
            tpl = pp.get("template_detail") or {}
            out.append({
                "pk": pp["pk"],
                "template": tpl.get("name") or pp["template"],
                "value": pp.get("data", pp.get("value") or ""),
            })
        return out

    def _find_template(self, name: str, *, strict: bool = False) -> int | None:
        """只读查询参数模板 pk。

        strict=True：查询失败抛 RuntimeError（ensure 路径需要区分
        「模板不存在」与「查询失败」，前者才允许创建）。
        strict=False：查询失败返回 None（只读场景）。
        """
        name = self._clean_param_name(name)
        if not name:
            return None
        code, data = self._rest(
            "GET", self._param_url("template"), params={"limit": 1000}
        )
        if code != 200:
            if strict:
                raise RuntimeError(f"查询参数模板失败 HTTP {code}")
            return None
        for t in self._rest_results(data):
            if t.get("name") == name:
                return t["pk"]
        return None

    def _ensure_template(self, name: str) -> int | None:
        """确保参数模板存在（REST 实现，兼容新旧端点）；失败返回 None。"""
        name = self._clean_param_name(name)
        if not name:
            return None
        if name in self._template_cache:
            return self._template_cache[name]
        try:
            pk = self._find_template(name, strict=True)
        except RuntimeError as exc:
            logger.warning("%s name=%s", exc, name)
            return None
        if pk is not None:
            self._template_cache[name] = pk
            return pk
        url = self._param_url("template")
        body: dict[str, Any] = {"name": name,
                                "description": "auto-created by lcsc2inventree"}
        if self._param_api_style() == "new":
            body.update({"units": "", "model_type": "part"})
        code, data = self._rest("POST", url, json=body)
        if code >= 400:
            logger.warning("创建参数模板失败 HTTP %s name=%s: %s", code, name, data)
            return None
        self._template_cache[name] = data["pk"]
        return data["pk"]

    def get_package_parameter(self, *, part_pk: int) -> str | None:
        """读取 Part 当前的 Package 参数值；模板或参数不存在返回 None。

        只读：不会创建模板/参数（与 `_ensure_template` 的区别）。
        `_list_part_parameters` 的 template 已归一化为模板名，直接按名匹配。
        """
        cur = next(
            (pp for pp in self._list_part_parameters(part_pk)
             if pp["template"] == "Package"),
            None,
        )
        return cur["value"] if cur else None

    def _write_parameters(
        self,
        part: LCSCPart,
        *,
        category_top: str | None,
        category_sub: str | None,
        part_pk: int,
        mapped: dict[str, dict[str, str]] | None = None,
    ) -> None:
        if mapped is None:
            mapped = to_inventree_parameters(
                part, category_top=category_top, category_sub=category_sub
            )
        params = self._mapped_values(mapped)
        if not params:
            return
        # {模板名: (parameter_pk, value)} —— _list_part_parameters 的
        # template 已归一化为模板名，按名匹配（与 set_named_parameter 一致）
        existing = {
            pp["template"]: (pp["pk"], pp["value"])
            for pp in self._list_part_parameters(part_pk)
        }
        url = self._param_url("param")
        value_field = "data" if self._param_api_style() == "new" else "value"
        for inv_name, value in params.items():
            inv_name = self._clean_param_name(inv_name)
            value = self._clean_param_value(value)
            if not inv_name or not value:
                continue
            tpl_pk = self._ensure_template(inv_name)
            if tpl_pk is None:
                continue
            cur = existing.get(inv_name)
            if cur is not None:
                if cur[1] == value:
                    continue
                code, data = self._rest(
                    "PATCH", f"{url}{cur[0]}/", json={value_field: value}
                )
                if code >= 400:
                    raise RuntimeError(
                        f"更新参数 {inv_name} 失败 HTTP {code}: {data}"
                    )
            else:
                code, data = self._rest(
                    "POST", url,
                    json=self._param_link_payload(part_pk, tpl_pk, value),
                )
                if code >= 400:
                    raise RuntimeError(
                        f"创建参数 {inv_name} 失败 HTTP {code}: {data}"
                    )

    def set_named_parameter(self, *, part_pk: int, name: str, value: str) -> bool:
        """写入/更新单个命名参数（模板不存在则自动创建），不动其它参数。

        名称/值按 InvenTree 字段限制截断（模板名 100、值 500 字符）。

        Returns:
            True=写入成功或值已一致；False=模板不可用（查询/创建失败）。
        Raises:
            RuntimeError: 参数列表/写入的 REST 调用失败。
        """
        name = self._clean_param_name(name)
        value = self._clean_param_value(value)
        if not name:
            return False
        tpl_pk = self._ensure_template(name)
        if tpl_pk is None:
            return False
        # _list_part_parameters 的 template 已归一化为模板名，按名匹配
        cur = next(
            (pp for pp in self._list_part_parameters(part_pk)
             if pp["template"] == name),
            None,
        )
        if cur is not None and cur["value"] == value:
            return True
        url = self._param_url("param")
        value_field = "data" if self._param_api_style() == "new" else "value"
        if cur is not None:
            code, data = self._rest(
                "PATCH", f"{url}{cur['pk']}/", json={value_field: value}
            )
        else:
            code, data = self._rest(
                "POST", url, json=self._param_link_payload(part_pk, tpl_pk, value)
            )
        if code >= 400:
            raise RuntimeError(f"写入参数 {name} 失败 HTTP {code}: {data}")
        return True

    def set_package_parameter(self, *, part_pk: int, value: str) -> bool:
        """只写入/更新单个 `Package` 参数（回填封装用，不动其它参数）。"""
        return self.set_named_parameter(part_pk=part_pk, name="Package", value=value)

    # ---- 通用 upsert（淘宝导入等非 LCSC 来源）---------------------------

    TAOBAO_SUPPLIER_DEFAULT = "淘宝"

    def _upload_image_bytes(
        self, *, part_pk: int, data: bytes, ext: str
    ) -> tuple[bool, str | None]:
        """把内存图片字节上传为 Part.image，返回 (uploaded, skip_reason)。

        webp 一律转 jpg（InvenTree 对 webp 兼容性不稳）；其余格式原样上传。
        """
        path = self.settings.cache_dir_path / f"upload_part{part_pk}{ext}"
        try:
            if ext == ".webp":
                try:
                    from io import BytesIO

                    from PIL import Image

                    img = Image.open(BytesIO(data))
                    if img.mode not in ("RGB", "L"):
                        img = img.convert("RGB")
                    path = path.with_suffix(".jpg")
                    img.save(path, "JPEG", quality=90)
                except ImportError:
                    path.write_bytes(data)  # 无 Pillow：原样尝试
            else:
                path.write_bytes(data)
            Part(self.api, part_pk).uploadImage(str(path))
            return True, None
        except Exception as exc:  # noqa: BLE001 — 图片失败不影响器件写入
            logger.warning("图片上传失败 part_pk=%s: %s", part_pk, exc)
            return False, f"upload-failed: {type(exc).__name__}"
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    def upsert_custom_part(
        self,
        *,
        ipn: str,
        name: str,
        description: str = "",
        notes: str | None = None,
        keywords: str | None = None,
        link: str | None = None,
        category_pk: int | None = None,
        manufacturer_name: str | None = None,
        supplier_name: str | None = None,
        sku: str | None = None,
        price: float | None = None,
        currency: str = "CNY",
        parameters: dict[str, str] | None = None,
        image_data: tuple[bytes, str] | None = None,
        dry_run: bool = False,
        update_existing: bool = False,
        create_stock: bool = False,
        quantity: int | None = None,
        location_pk: int | None = None,
    ) -> WriteResult:
        """通用自定义器件 upsert（淘宝导入等非 LCSC 来源）。

        - IPN 幂等：已存在且 update_existing=False → 直接返回（不改动）
        - 价格：supplier_name + sku 建 SupplierPart + 单档价格
        - parameters：{模板名: 值}，模板按需自动创建
        - image_data：(字节, 扩展名)，webp 自动转 jpg
        - create_stock + quantity：建一笔 StockItem（可指定货位，备注含采购价）
        """
        result = WriteResult()
        existing_pk = self._find_part_by_ipn(ipn)
        if existing_pk is not None and not update_existing:
            result.part_pk = existing_pk
            result.created["part"] = False
            result.image_skipped_reason = "exists-no-update"
            return result

        if dry_run:
            logger.info(
                "[DRY] custom upsert ipn=%s name=%s existing=%s",
                ipn, name, existing_pk,
            )
            return result

        mfr_pk = self._ensure_manufacturer(manufacturer_name, dry_run=False)
        result.manufacturer_pk = mfr_pk
        sup_pk = self._ensure_supplier(supplier_name or self.TAOBAO_SUPPLIER_DEFAULT,
                                       dry_run=False)
        result.supplier_pk = sup_pk

        payload: dict[str, Any] = {
            "name": (name or ipn)[:100],
            # 参数值并入描述（全局搜索可按品牌/型号等命中）
            "description": self._with_params_suffix(
                description or "", parameters
            )[:250],
            "IPN": ipn,
            "active": True,
            "purchaseable": True,
            "component": True,
            "assembly": False,
        }
        if keywords:
            payload["keywords"] = keywords
        if link:
            payload["link"] = link
        if category_pk:
            payload["category"] = category_pk
        if notes:
            payload["notes"] = notes

        try:
            if existing_pk is None:
                obj = Part.create(self.api, payload)
                part_pk = obj.pk
                result.created["part"] = True
            else:
                Part(self.api, existing_pk).save(payload)
                part_pk = existing_pk
                result.created["part"] = False
            result.part_pk = part_pk

            # 图片（best-effort）
            if image_data and part_pk:
                data, ext = image_data
                uploaded, reason = self._upload_image_bytes(
                    part_pk=part_pk, data=data, ext=ext
                )
                result.image_uploaded = uploaded
                result.image_skipped_reason = reason

            # 参数（逐个写，单个失败不影响其余）
            for pname, pvalue in (parameters or {}).items():
                if not pname or not pvalue:
                    continue
                try:
                    self.set_named_parameter(
                        part_pk=part_pk, name=pname, value=str(pvalue)
                    )
                except RuntimeError as exc:
                    result.errors.append(f"参数 {pname}: {exc}")

            # SupplierPart + 单档价格
            if sup_pk and part_pk:
                sp_pk, sp_created = self._ensure_supplier_part(
                    part_pk=part_pk,
                    supplier_pk=sup_pk,
                    sku=sku or ipn,
                    manufacturer_part_pk=None,
                    dry_run=False,
                )
                result.supplier_part_pk = sp_pk
                result.created["supplier_part"] = sp_created
                if sp_pk and price is not None:
                    self._replace_price_breaks(
                        sp_pk, [(1, price, currency)], dry_run=False
                    )
                # 库存（可选；要求已建 SupplierPart 才能挂采购价）
                if create_stock and sp_pk and quantity and quantity > 0:
                    result.stock_item_pk = self._ensure_stock(
                        part_pk=part_pk,
                        supplier_part_pk=sp_pk,
                        quantity=quantity,
                        price=price,
                        currency=currency,
                        dry_run=False,
                        location_pk=location_pk,
                        notes="淘宝导入",
                    )
        except Exception as exc:  # noqa: BLE001 — 统一收集到 errors
            logger.exception("upsert_custom_part 失败 ipn=%s", ipn)
            result.errors.append(f"{type(exc).__name__}: {exc}")
        return result

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

    def add_stock(
        self,
        *,
        part_pk: int,
        quantity: int,
        location_pk: int | None = None,
        notes: str | None = None,
    ) -> int:
        """为已有 Part 新建一笔 StockItem（订单入库用），返回 stock_pk。

        与 `_ensure_stock` 不同：不要求 supplier_part，可指定货位。
        """
        payload: dict[str, Any] = {"part": part_pk, "quantity": str(quantity)}
        if location_pk:
            payload["location"] = location_pk
        if notes:
            payload["notes"] = notes
        return StockItem.create(self.api, payload).pk

    def _ensure_stock(
        self,
        *,
        part_pk: int,
        supplier_part_pk: int,
        quantity: int,
        price: float | None,
        currency: str | None,
        dry_run: bool,
        location_pk: int | None = None,
        notes: str | None = None,
    ) -> int | None:
        if dry_run:
            return None
        # 简化为"创建一条新 StockItem"——用户可手工调拨合并；不做幂等累加
        payload: dict[str, Any] = {
            "part": part_pk,
            "quantity": quantity,
            "supplier_part": supplier_part_pk,
        }
        if location_pk:
            payload["location"] = location_pk
        if notes:
            payload["notes"] = notes
        if price is not None:
            payload["purchase_price"] = str(price)
            payload["purchase_price_currency"] = currency or "USD"
        # StockItem.create 在 inventree-python 0.14+ 返回 list（即使单条创建）
        objs = StockItem.create(self.api, payload)
        if not objs:
            return None
        return objs[0].pk
