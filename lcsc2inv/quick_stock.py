"""快速入库逻辑：Part 匹配 + 库存合并/新建。

专为收货入库场景设计，提供比 InvenTree 自带流程更快的操作体验：
- 智能匹配 Part（IPN → MPN → 模糊搜索）
- 可选合并到同货位已有库存（避免重复条目）
- 自动从 LCSC 抓取创建缺失 Part
- 搜索结果本地缓存（24h TTL）

独立模块（不依赖 Flask），便于测试和复用。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import requests
from inventree.part import Part, PartCategory
from inventree.stock import StockItem, StockLocation
from inventree.company import ManufacturerPart

from lcsc2inv.config import get_settings

logger = logging.getLogger("lcsc2inv.quick_stock")

# 搜索缓存文件
SEARCH_CACHE_FILE = "search_cache.json"
SEARCH_CACHE_TTL = 86400  # 24 小时

# 全量 Part 缓存文件
PARTS_CACHE_FILE = "parts_cache.json"
PARTS_CACHE_TTL = 3600  # 1 小时（更频繁更新）


def _get_search_cache_path() -> Path:
    """获取搜索缓存文件路径。"""
    settings = get_settings()
    return settings.cache_dir_path / SEARCH_CACHE_FILE


def _get_parts_cache_path() -> Path:
    """获取全量 Part 缓存文件路径。"""
    settings = get_settings()
    return settings.cache_dir_path / PARTS_CACHE_FILE


def _load_search_cache() -> dict[str, Any]:
    """加载搜索缓存。"""
    cache_path = _get_search_cache_path()
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_search_cache(cache: dict[str, Any]) -> None:
    """保存搜索缓存（原子写入）。"""
    cache_path = _get_search_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_name(f"{SEARCH_CACHE_FILE}.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, cache_path)


def _get_cached_search(query: str) -> list[dict[str, Any]] | None:
    """从缓存获取搜索结果（如果未过期）。

    Returns:
        缓存的结果列表，或 None（缓存未命中或已过期）
    """
    cache = _load_search_cache()
    entry = cache.get(query)
    if not entry:
        return None
    
    timestamp = entry.get("timestamp", 0)
    if time.time() - timestamp > SEARCH_CACHE_TTL:
        # 缓存已过期，删除
        del cache[query]
        _save_search_cache(cache)
        return None
    
    return entry.get("results")


def _cache_search_results(query: str, results: list[dict[str, Any]]) -> None:
    """缓存搜索结果。"""
    cache = _load_search_cache()
    cache[query] = {
        "results": results,
        "timestamp": time.time(),
    }
    _save_search_cache(cache)


# ---------------------------------------------------------------------------
# 全量 Part 缓存
# ---------------------------------------------------------------------------


def _load_parts_cache() -> dict[str, Any]:
    """加载全量 Part 缓存。"""
    cache_path = _get_parts_cache_path()
    if not cache_path.exists():
        return {"timestamp": 0, "parts": []}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"timestamp": 0, "parts": []}
    except (json.JSONDecodeError, OSError):
        return {"timestamp": 0, "parts": []}


def _save_parts_cache(cache: dict[str, Any]) -> None:
    """保存全量 Part 缓存（原子写入）。"""
    cache_path = _get_parts_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_name(f"{PARTS_CACHE_FILE}.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, cache_path)


def get_parts_cache_status() -> dict[str, Any]:
    """获取 Part 缓存状态。"""
    cache = _load_parts_cache()
    age = time.time() - cache.get("timestamp", 0)
    return {
        "count": len(cache.get("parts", [])),
        "timestamp": cache.get("timestamp", 0),
        "age_seconds": age,
        "is_fresh": age < PARTS_CACHE_TTL,
    }


def refresh_parts_cache() -> dict[str, Any]:
    """全量刷新 Part 缓存。

    从 InvenTree API 获取所有 Part 的基本信息并缓存到本地。
    后续搜索直接使用本地缓存，速度极快。

    Returns:
        {"ok": bool, "count": int, "duration": float}
    """
    start = time.time()
    settings = get_settings()
    parts = []

    try:
        # 使用 REST API 分页获取所有 Part
        url = f"{settings.inventree_url.rstrip('/')}/api/part/"
        headers = {"Authorization": f"Token {settings.inventree_token}"}
        page = 1
        page_size = 500

        while True:
            response = requests.get(
                url,
                headers=headers,
                params={"limit": page_size, "offset": (page - 1) * page_size},
                timeout=30,
            )

            if response.status_code != 200:
                logger.warning("获取 Part 列表失败 (page %d): %d", page, response.status_code)
                break

            data = response.json()
            results = data.get("results", [])
            if not results:
                break

            for item in results:
                parts.append({
                    "pk": item.get("pk"),
                    "name": item.get("name"),
                    "IPN": item.get("IPN"),
                    "description": item.get("description"),
                    "in_stock": item.get("in_stock"),
                    "category": (item.get("category_detail") or {}).get("pathstring") or (item.get("category_detail") or {}).get("name"),
                })

            # 检查是否有下一页
            if data.get("next") is None:
                break
            page += 1

        # 保存缓存
        _save_parts_cache({
            "timestamp": time.time(),
            "parts": parts,
        })

        duration = time.time() - start
        logger.info("Part 缓存已刷新：%d 个 Part，耗时 %.2fs", len(parts), duration)
        return {"ok": True, "count": len(parts), "duration": duration}

    except Exception as exc:
        logger.error("刷新 Part 缓存失败：%s", exc)
        return {"ok": False, "count": 0, "duration": time.time() - start, "error": str(exc)}


def find_part(api, code: str) -> dict[str, Any]:
    """智能匹配 Part：IPN → MPN → 模糊搜索。

    优先使用本地 Part 缓存（毫秒级），缓存未命中时回退到 REST API。

    Args:
        api: InvenTreeAPI 实例
        code: LCSC C-code（如 C28323）或 MPN

    Returns:
        {
            "found": bool,
            "part_pk": int | None,
            "part_name": str | None,
            "ipn": str | None,
            "mpn": str | None,
            "category": str | None,
            "in_stock": float | None,
            "match_type": "ipn" | "mpn" | "search" | None,
        }
    """
    result = {
        "found": False,
        "part_pk": None,
        "part_name": None,
        "ipn": None,
        "mpn": None,
        "category": None,
        "in_stock": None,
        "match_type": None,
    }

    # 尝试使用本地缓存
    parts_cache = _load_parts_cache()
    cache_age = time.time() - parts_cache.get("timestamp", 0)
    
    if cache_age < PARTS_CACHE_TTL and parts_cache.get("parts"):
        # 本地缓存有效，先在本地查找
        logger.debug("使用本地缓存查找 Part: %s", code)
        result = _find_part_in_cache(parts_cache["parts"], code)
        if result["found"]:
            return result
        logger.debug("本地缓存未找到 %s，回退到 API", code)
    
    # 本地缓存未命中或无效，使用 API
    logger.debug("使用 API 查找 Part: %s", code)
    
    # 1. IPN 精确匹配（最快）
    try:
        for p in Part.search(api, search=code, search_in="IPN"):
            if getattr(p, "IPN", None) == code:
                return _part_to_result(p, "ipn")
    except Exception:  # noqa: BLE001
        pass

    # 2. IPN 全表扫描（fallback）
    try:
        for p in Part.list(api):
            if getattr(p, "IPN", None) == code:
                return _part_to_result(p, "ipn")
    except Exception:  # noqa: BLE001
        pass

    # 3. MPN 精确匹配
    try:
        for mp in ManufacturerPart.list(api, search=code):
            if getattr(mp, "MPN", None) == code:
                part_pk = getattr(mp, "part", None)
                if part_pk:
                    try:
                        p = Part(api, part_pk)
                        return _part_to_result(p, "mpn")
                    except Exception:  # noqa: BLE001
                        pass
    except Exception:  # noqa: BLE001
        pass

    # 4. 模糊搜索（名称/描述/关键词）
    try:
        results = Part.search(api, search=code)
        if results:
            p = results[0]
            return _part_to_result(p, "search")
    except Exception:  # noqa: BLE001
        pass

    return result


def _find_part_in_cache(parts: list[dict], code: str) -> dict[str, Any]:
    """在本地 Part 缓存中查找。

    查找顺序：IPN 精确匹配 → 名称模糊匹配 → MPN 模糊匹配
    """
    result = {
        "found": False,
        "part_pk": None,
        "part_name": None,
        "ipn": None,
        "mpn": None,
        "category": None,
        "in_stock": None,
        "match_type": None,
    }
    
    code_lower = code.lower()
    
    # 1. IPN 精确匹配
    for part in parts:
        ipn = part.get("IPN") or ""
        if ipn.lower() == code_lower:
            return {
                "found": True,
                "part_pk": part.get("pk"),
                "part_name": part.get("name"),
                "ipn": ipn,
                "mpn": None,  # 本地缓存不包含 MPN
                "category": part.get("category"),
                "in_stock": float(part["in_stock"]) if isinstance(part.get("in_stock"), (int, float)) else None,
                "match_type": "ipn",
            }
    
    # 2. 名称/描述模糊匹配
    for part in parts:
        name = (part.get("name") or "").lower()
        description = (part.get("description") or "").lower()
        if code_lower in name or code_lower in description:
            return {
                "found": True,
                "part_pk": part.get("pk"),
                "part_name": part.get("name"),
                "ipn": part.get("IPN"),
                "mpn": None,
                "category": part.get("category"),
                "in_stock": float(part["in_stock"]) if isinstance(part.get("in_stock"), (int, float)) else None,
                "match_type": "search",
            }
    
    return result


def _part_to_result(p: Part, match_type: str) -> dict[str, Any]:
    """把 Part 对象转为结果字典。"""
    part_pk = p.pk
    part_name = getattr(p, "name", None)
    ipn = getattr(p, "IPN", None)
    in_stock = getattr(p, "in_stock", None)

    # 获取分类路径
    category = None
    try:
        cat_pk = getattr(p, "category", None)
        if cat_pk:
            cat = PartCategory(p.api, cat_pk)
            category = getattr(cat, "pathstring", None) or getattr(cat, "name", None)
    except Exception:  # noqa: BLE001
        pass

    # 获取 MPN（从 ManufacturerPart）
    mpn = None
    try:
        for mp in ManufacturerPart.list(p.api, part=part_pk):
            mpn = getattr(mp, "MPN", None)
            if mpn:
                break
    except Exception:  # noqa: BLE001
        pass

    return {
        "found": True,
        "part_pk": part_pk,
        "part_name": str(part_name) if part_name else None,
        "ipn": str(ipn) if ipn else None,
        "mpn": str(mpn) if mpn else None,
        "category": str(category) if category else None,
        "in_stock": float(in_stock) if isinstance(in_stock, (int, float)) else None,
        "match_type": match_type,
    }


def get_stock_at_location(
    api, part_pk: int, location_pk: int
) -> dict[str, Any] | None:
    """查询某 Part 在某货位的现有库存。

    Returns:
        {"stock_pk": int, "quantity": float} 或 None（无库存）
    """
    try:
        for item in StockItem.list(api, part=part_pk, location=location_pk):
            if getattr(item, "part", None) == part_pk and getattr(item, "location", None) == location_pk:
                qty = getattr(item, "quantity", None)
                return {
                    "stock_pk": item.pk,
                    "quantity": float(qty) if isinstance(qty, (int, float)) else None,
                }
    except Exception:  # noqa: BLE001
        pass
    return None


def add_or_merge_stock(
    api,
    part_pk: int,
    quantity: int | float,
    location_pk: int | None = None,
    notes: str | None = None,
    merge: bool = True,
) -> dict[str, Any]:
    """入库：merge=True 时合并到同货位已有库存，否则新建 StockItem。

    Returns:
        {
            "stock_pk": int,
            "quantity": float,
            "merged": bool,
            "new_quantity": float,  # 合并后的总数量
        }
    """
    # 尝试合并到已有库存
    if merge and location_pk:
        existing = get_stock_at_location(api, part_pk, location_pk)
        if existing:
            # 通过 REST API 追加数量（InvenTree SDK 不直接支持 add）
            settings = get_settings()
            try:
                response = requests.post(
                    f"{settings.inventree_url.rstrip('/')}/api/stock/add/",
                    json={
                        "items": [{"pk": existing["stock_pk"], "quantity": str(quantity)}],
                        "notes": notes or "快速入库（合并）",
                    },
                    headers={"Authorization": f"Token {settings.inventree_token}"},
                    timeout=20,
                )
                if response.status_code < 400:
                    new_qty = (existing["quantity"] or 0) + quantity
                    return {
                        "stock_pk": existing["stock_pk"],
                        "quantity": quantity,
                        "merged": True,
                        "new_quantity": new_qty,
                    }
            except Exception as exc:  # noqa: BLE001
                logger.warning("合并库存失败，回退到新建: %s", exc)

    # 新建 StockItem
    payload: dict[str, Any] = {"part": part_pk, "quantity": str(quantity)}
    if location_pk:
        payload["location"] = location_pk
    if notes:
        payload["notes"] = notes

    objs = StockItem.create(api, payload)
    if not objs:
        raise RuntimeError("创建 StockItem 失败")

    # inventree-python 0.14+ 返回 list
    stock_pk = objs[0].pk if isinstance(objs, list) else objs.pk
    return {
        "stock_pk": stock_pk,
        "quantity": quantity,
        "merged": False,
        "new_quantity": quantity,
    }


def list_locations(api) -> list[dict[str, Any]]:
    """获取货位列表（用于下拉框）。

    Returns:
        [{"pk": int, "name": str, "path": str}, ...]
    """
    locations = []
    try:
        for loc in StockLocation.list(api):
            pk = loc.pk
            name = getattr(loc, "name", None)
            path = getattr(loc, "pathstring", None) or name
            locations.append({
                "pk": pk,
                "name": str(name) if name else None,
                "path": str(path) if path else None,
            })
    except Exception:  # noqa: BLE001
        pass
    return locations


def search_parts(api, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """模糊搜索 Part（名称/IPN/MPN/描述）。

    优先使用本地 Part 缓存（毫秒级），缓存未命中时回退到 REST API。

    Args:
        api: InvenTreeAPI 实例
        query: 搜索关键词
        limit: 返回结果数量上限

    Returns:
        [{"pk": int, "name": str, "ipn": str, "mpn": str, "description": str, "category": str, "in_stock": float}, ...]
    """
    # 检查搜索缓存
    cached = _get_cached_search(query)
    if cached is not None:
        logger.debug("搜索缓存命中: %s (%d 条)", query, len(cached))
        return cached

    # 尝试使用本地 Part 缓存
    parts_cache = _load_parts_cache()
    cache_age = time.time() - parts_cache.get("timestamp", 0)
    
    if cache_age < PARTS_CACHE_TTL and parts_cache.get("parts"):
        # 本地缓存有效，直接在本地搜索
        logger.debug("使用本地 Part 缓存搜索：%s", query)
        results = _search_local_parts(parts_cache["parts"], query, limit)
        if results:
            _cache_search_results(query, results)
        return results
    
    # 本地缓存无效或为空，使用 REST API
    logger.debug("本地缓存无效，使用 REST API 搜索：%s", query)
    results = _search_remote_parts(api, query, limit)
    if results:
        _cache_search_results(query, results)
    return results


def _search_local_parts(parts: list[dict], query: str, limit: int) -> list[dict[str, Any]]:
    """在本地 Part 缓存中搜索。"""
    query_lower = query.lower()
    results = []
    
    for part in parts:
        if len(results) >= limit:
            break
        
        name = (part.get("name") or "").lower()
        ipn = (part.get("IPN") or "").lower()
        description = (part.get("description") or "").lower()
        
        # 模糊匹配：名称、IPN、描述
        if query_lower in name or query_lower in ipn or query_lower in description:
            results.append({
                "pk": part.get("pk"),
                "name": part.get("name"),
                "ipn": part.get("IPN"),
                "mpn": None,  # 本地缓存不包含 MPN
                "description": part.get("description"),
                "category": part.get("category"),
                "in_stock": float(part["in_stock"]) if isinstance(part.get("in_stock"), (int, float)) else None,
            })
    
    return results


def _search_remote_parts(api, query: str, limit: int) -> list[dict[str, Any]]:
    """使用 REST API 搜索 Part（回退方案）。"""
    results = []
    settings = get_settings()
    
    try:
        url = f"{settings.inventree_url.rstrip('/')}/api/part/"
        headers = {"Authorization": f"Token {settings.inventree_token}"}
        response = requests.get(
            url,
            headers=headers,
            params={"search": query, "limit": limit},
            timeout=10,
        )
        
        if response.status_code != 200:
            return results
        
        data = response.json()
        for item in data.get("results", []):
            part_pk = item.get("pk")
            name = item.get("name")
            ipn = item.get("IPN")
            description = item.get("description")
            in_stock = item.get("in_stock")
            category_detail = item.get("category_detail") or {}
            category = category_detail.get("pathstring") or category_detail.get("name")
            
            # 获取 MPN（需要额外查询）
            mpn = None
            try:
                mpn_url = f"{settings.inventree_url.rstrip('/')}/api/company/manufacturer-part/"
                mpn_resp = requests.get(
                    mpn_url,
                    headers=headers,
                    params={"part": part_pk, "limit": 1},
                    timeout=5,
                )
                if mpn_resp.status_code == 200:
                    mpn_data = mpn_resp.json()
                    mpn_list = mpn_data.get("results", [])
                    if mpn_list:
                        mpn = mpn_list[0].get("MPN")
            except Exception:  # noqa: BLE001
                pass
            
            results.append({
                "pk": part_pk,
                "name": str(name) if name else None,
                "ipn": str(ipn) if ipn else None,
                "mpn": str(mpn) if mpn else None,
                "description": str(description) if description else None,
                "category": str(category) if category else None,
                "in_stock": float(in_stock) if isinstance(in_stock, (int, float)) else None,
            })
    except Exception:  # noqa: BLE001
        pass
    return results
