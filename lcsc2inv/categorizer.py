"""LCSC 类目到 InvenTree 类别的三层匹配。

策略：
1. 直映（direct map）：`config/lcsc_categories.yaml` 中精确的 lcsc_category → InvenTree category path
2. 函数过滤（function filter）：对 IC / Power 类，根据 `Function Type` 进一步匹配子类别
3. 模糊兜底（fuzzy match）：用 thefuzz.partial_ratio，未匹配时返回最佳候选项 + 警告

输出：`InvenTreeCategoryMatch`（含类别 path、匹配来源、得分），调用方决定是否接受。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from thefuzz import fuzz

from lcsc2inv.config import get_settings, load_yaml
from lcsc2inv.lcsc_models import LCSCPart

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CategoryMatch:
    """三层匹配结果。

    Attributes:
        category_path: InvenTree 类别路径，如 "Passive/Capacitors/MLCC"。
            顶层 `"__uncategorized__"` 表示未匹配上。
        source: "direct" / "function" / "fuzzy" / "none"
        score: 0-100
        candidates: 所有候选及得分（用于 dry-run 输出）
    """

    category_path: str
    source: str = "none"
    score: int = 0
    candidates: list[tuple[str, int]] = field(default_factory=list)


def _load_direct_map() -> dict[str, list[str]]:
    """加载 `config/lcsc_categories.yaml`，展平为 `lcsc_category -> [path...]`。

    YAML 结构（见配置文件）：
        Capacitors:
            _default: ["Passive", "Capacitors"]
            Ceramic Capacitors: ["Passive", "Capacitors", "Ceramic Capacitors (MLCC)"]
    """
    raw = load_yaml("lcsc_categories.yaml")
    flat: dict[str, list[str]] = {}
    for top, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        default = cfg.get("_default")
        if isinstance(default, list):
            flat[top] = default
        for sub, path in cfg.items():
            if sub == "_default" or not isinstance(path, list):
                continue
            key = f"{top}/{sub}"
            flat[key] = path
    return flat


def _all_paths_from_yaml() -> list[list[str]]:
    """提取所有出现在 YAML 中的类别 path，作为模糊匹配的候选池。"""
    raw = load_yaml("lcsc_categories.yaml")
    out: list[list[str]] = []
    seen: set[str] = set()
    for top, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        for sub, path in cfg.items():
            if isinstance(path, list) and path:
                key = "/".join(path)
                if key not in seen:
                    seen.add(key)
                    out.append(list(path))
        if isinstance(cfg.get("_default"), list):
            key = "/".join(cfg["_default"])
            if key not in seen:
                seen.add(key)
                out.append(list(cfg["_default"]))
    return out


def _function_match(part: LCSCPart, all_paths: list[list[str]]) -> CategoryMatch | None:
    """对 IC / Power Management 等含 Function Type 的子类做第二层匹配。

    触发条件：LCSC `category_sub` 包含 "Power Management" / "Integrated Circuits"，
    且 `additionalProperty` 含 `Function Type`。
    """
    function_type = part.get_property("Function Type")
    if not function_type:
        return None
    ratio_limit = get_settings().category_match_ratio_limit
    best: tuple[int, list[str]] | None = None
    for path in all_paths:
        leaf = path[-1].lower()
        if not leaf:
            continue
        score = fuzz.partial_ratio(function_type.lower(), leaf)
        if score >= ratio_limit and (best is None or score > best[0]):
            best = (score, path)
    if best is None:
        return None
    return CategoryMatch(
        category_path="/".join(best[1]),
        source="function",
        score=best[0],
    )


def _fuzzy_match(part: LCSCPart, all_paths: list[list[str]], *, create_missing_category: bool = False) -> CategoryMatch:
    """兜底：对 LCSC 大类名（如 'Resistors'）做 partial_ratio 匹配。

    Args:
        create_missing_category: True 时，如果 LCSC 分类本地不存在，返回 "__create:<path>" 格式，
            表示调用方应创建该分类。
    """
    ratio_limit = get_settings().category_match_ratio_limit
    candidates: list[tuple[str, int]] = []
    haystack = part.category_top or part.category or part.mpn or part.sku
    haystack_l = haystack.lower() if haystack else ""
    best_path: list[str] | None = None
    best_score = 0
    for path in all_paths:
        leaf = " ".join(path).lower()
        score = fuzz.partial_ratio(haystack_l, leaf) if haystack_l else 0
        candidates.append(("/".join(path), score))
        if score > best_score:
            best_score = score
            best_path = path
    candidates.sort(key=lambda x: x[1], reverse=True)
    if best_path and best_score >= ratio_limit:
        return CategoryMatch(
            category_path="/".join(best_path),
            source="fuzzy",
            score=best_score,
            candidates=candidates[:5],
        )
    # 模糊匹配失败：尝试用 LCSC 的原始分类路径
    if create_missing_category and part.category:
        # LCSC category 通常是 "Top/Sub" 格式，如 "Inductors, Coils, Chokes" 或 "Capacitors/Ceramic Capacitors"
        # 尝试按 "/" 分割，如果有多段则用第一段作为 top
        segments = [s.strip() for s in part.category.split("/")]
        top = segments[0] if segments else part.category
        logger.warning(
            "分类模糊匹配失败：LCSC=%s best=%s limit=%s -> 将创建 InvenTree 分类 '%s'",
            haystack,
            best_score,
            ratio_limit,
            top,
        )
        # 返回特殊格式 "__create:<path>"，inventree_writer 会解析并创建
        # 顶层用 LCSC 的 category_top，其余段拼成完整路径
        lcsc_path = part.category.split("/")
        return CategoryMatch(
            category_path=f"__create:{part.category}",
            source="create-missing",
            score=best_score,
            candidates=candidates[:5],
        )
    logger.warning(
        "分类模糊匹配失败：LCSC=%s best=%s limit=%s",
        haystack,
        best_score,
        ratio_limit,
    )
    return CategoryMatch(
        category_path="__uncategorized__",
        source="none",
        score=best_score,
        candidates=candidates[:5],
    )


def match(part: LCSCPart, *, create_missing_category: bool = False) -> CategoryMatch:
    """对 LCSC 商品做三层匹配，返回最佳 `CategoryMatch`。

    Args:
        create_missing_category: True 时，本地无匹配的 LCSC 分类会返回 "__create:<path>" 格式，
            表示调用方应创建该分类。
    """
    direct_map = _load_direct_map()
    all_paths = _all_paths_from_yaml()

    # Tier 1: direct
    if part.category and part.category in direct_map:
        return CategoryMatch(
            category_path="/".join(direct_map[part.category]),
            source="direct",
            score=100,
        )

    # Tier 2: function (针对 IC / Power 子类)
    fn = _function_match(part, all_paths)
    if fn is not None:
        return fn

    # Tier 3: fuzzy
    return _fuzzy_match(part, all_paths, create_missing_category=create_missing_category)