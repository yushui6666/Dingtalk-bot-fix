"""门店备件权威表读取与匹配（2026-09-09 备件登记功能）。

数据源：
- data/stores/_store_registry.json —— 工单店名 → 备件表文件名（人工维护的权威映射草稿）。
- data/stores/<门店>.json —— 每店备件权威表，scenes/<主题>/parts[].name+aliases + 工具池。

关键约束（用户口径）：
- 只在该「店 × 主题房」的备件表 + 该店「工具」池里匹配；
- 记录的是规范名 name（不是工程师口头说法）；
- 未命中/无表 → 静默（返回空，不报错）。
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

# 默认数据目录（相对本模块）：dingtalk_script/data/stores
_DEFAULT_STORES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "stores")

_REGISTRY_FILENAME = "_store_registry.json"
_TOOL_SCENE = "工具"


def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _registry_mapping(store_name: str, stores_dir: str) -> Optional[list[str]]:
    """工单店名 → 备件表文件名列表；无映射返回 None。

    支持单文件（字符串）与多文件（列表，如「上海商场08店」= 商场08品牌C+商场08品牌B并集）。
    """
    if not store_name:
        return None
    reg = _load_json(os.path.join(stores_dir, _REGISTRY_FILENAME))
    if not reg:
        return None
    mapping = reg.get("registry", {}).get("stores", {})
    value = mapping.get(store_name)
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v]
    return None


def resolve_store_file(store_name: str, stores_dir: str = _DEFAULT_STORES_DIR) -> Optional[str]:
    """工单 store_name → 备件表文件名；无对应表返回 None。

    多文件映射（并集）取第一个用于单文件场景；
    完整列表见 resolve_store_files。
    """
    files = _registry_mapping(store_name, stores_dir)
    return files[0] if files else None


def resolve_store_files(store_name: str, stores_dir: str = _DEFAULT_STORES_DIR) -> list[str]:
    """工单 store_name → 全部备件表文件名（并集）；无对应表返回 []。"""
    return _registry_mapping(store_name, stores_dir) or []


def _normalize_key(text: str) -> str:
    """主题名归一：去空格/全半角/常见后缀，用于 subject 与场景 key 的模糊匹配。"""
    t = re.sub(r"\s+", "", text or "")
    t = t.replace("（", "(").replace("）", ")")
    t = re.sub(r"(主题|主题房|房间)$", "", t)
    return t


def _scene_parts(store_doc: dict, subject: str) -> dict:
    """取该主题场景的 parts 列表；场景 miss 时为空 用工具池兜底。"""
    scenes = store_doc.get("scenes", {}) or {}
    if not subject:
        return {}
    target_key = _normalize_key(subject)
    # 先精确，再模糊包含匹配
    if target_key in scenes:
        return scenes[target_key].get("parts", []) or []
    for key in scenes:
        if target_key and (target_key in _normalize_key(key) or _normalize_key(key) in target_key):
            return scenes[key].get("parts", []) or []
    return {}


def build_pool(store_name: str, subject: str, stores_dir: str = _DEFAULT_STORES_DIR) -> dict:
    """返回 {规范名: {别名集合}}，= 主题场景 parts + 该店工具池 parts。

    多文件映射（如商场08店 = 品牌C+品牌B）按并集合并：每个文件的
    主题场景 + 各自工具池都进池。店无表 / 主题与工具池都没有 → 空 dict。
    """
    files = resolve_store_files(store_name, stores_dir)
    if not files:
        return {}

    pool: dict[str, set[str]] = {}
    for fname in files:
        doc = _load_json(os.path.join(stores_dir, fname))
        if not doc:
            continue
        # 主题场景 parts
        for p in _scene_parts(doc, subject):
            name = (p.get("name") or "").strip()
            if not name:
                continue
            aliases = {str(a).strip() for a in (p.get("aliases") or []) if str(a).strip()}
            pool.setdefault(name, set()).update(aliases)
        # 工具池 parts（固定并入）
        if _TOOL_SCENE in (doc.get("scenes") or {}):
            for p in doc["scenes"][_TOOL_SCENE].get("parts", []) or []:
                name = (p.get("name") or "").strip()
                if not name:
                    continue
                aliases = {str(a).strip() for a in (p.get("aliases") or []) if str(a).strip()}
                pool.setdefault(name, set()).update(aliases)
    return pool


def canonicalize(word: str, pool: dict) -> Optional[str]:
    """把口头词规范化到池内的规范备件名；不在池内返回 None（静默不记）。"""
    if not word:
        return None
    w = str(word).strip()
    if not w:
        return None
    # 精确规范名
    if w in pool:
        return w
    # 别名 → 规范名
    for canonical, aliases in pool.items():
        if w in aliases:
            return canonical
    return None


def keep_only_matched(raw_parts, pool: dict) -> list[str]:
    """按池过滤并规范化为规范名；全部未命中返回空列表（静默）。

    保持原始顺序，去重。
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in raw_parts or []:
        if isinstance(raw, str):
            canon = canonicalize(raw, pool)
        else:
            canon = None
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def build_vocab(store_name: str, subjects, stores_dir: str = _DEFAULT_STORES_DIR) -> dict:
    """为分类器提示词构造「店×主题」候选备件词表。

    返回 {subject(原文): {规范名: {别名集合}}}——每个主题场景并入该店工具池
    （与执行期 build_pool 同一口径，仅用于辅助模型产出贴合词）。
    多文件覆盖的店（并集）同样生效。店无表 / 全部无命中 → 空 dict。
    """
    if not resolve_store_files(store_name, stores_dir):
        return {}
    vocab: dict[str, dict[str, set[str]]] = {}
    for subj in subjects or []:
        if not subj:
            continue
        if subj in vocab:
            continue
        pool = build_pool(store_name, subj, stores_dir)
        if pool:
            vocab[subj] = pool
    return vocab
