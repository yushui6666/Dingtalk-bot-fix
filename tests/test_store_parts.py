"""store_parts 模块测试：工单店名→备件表解析、场景/工具池、别名归一、跨店隔离、静默。

TDD 红先行（2026-09-09）：本文件先写，运行见红，再实现 store_parts.py 转绿。
"""
import json

import pytest

import store_parts


@pytest.fixture()
def stores_dir(tmp_path):
    """构造两个门店 + 注册表的临时备件目录。"""
    d = tmp_path / "stores"
    d.mkdir()
    (d / "1_北京商场15店.json").write_text(json.dumps({
        "store": "1.北京商场15店",
        "scenes": {
            "博物馆": {"parts": [
                {"name": "继电器DC12V", "aliases": ["继电器"], "spec": "", "qty": "", "check_time": "", "check_qty": ""},
                {"name": "干簧管", "aliases": [], "spec": "", "qty": "", "check_time": "", "check_qty": ""},
            ]},
            "工具": {"parts": [
                {"name": "万用表", "aliases": ["万能表"], "spec": "", "qty": "", "check_time": "", "check_qty": ""},
            ]},
        },
        "_review": [],
    }))
    (d / "2_天津商场22道店.json").write_text(json.dumps({
        "store": "2.天津商场22道店",
        "scenes": {
            "鬼屋回魂": {"parts": [
                {"name": "磁铁", "aliases": [], "spec": "", "qty": "", "check_time": "", "check_qty": ""},
            ]},
            "工具": {"parts": [
                {"name": "电钻", "aliases": [], "spec": "", "qty": "", "check_time": "", "check_qty": ""},
            ]},
        },
        "_review": [],
    }))
    (d / "_store_registry.json").write_text(json.dumps({
        "registry": {"stores": {
            "北京商场15店": "1_北京商场15店.json",
            "天津商场22道店": "2_天津商场22道店.json",
        }},
        "_review": {},
    }))
    return str(d)


@pytest.fixture()
def union_stores_dir(tmp_path):
    """商场08模式：一个工单店名映射两份备件表（并集）。"""
    d = tmp_path / "stores"
    d.mkdir()
    (d / "1_商场08品牌C.json").write_text(json.dumps({
        "store": "1.商场08品牌C",
        "scenes": {
            "鬼屋回魂": {"parts": [
                {"name": "磁铁", "aliases": [], "spec": "", "qty": "", "check_time": "", "check_qty": ""},
            ]},
            "工具": {"parts": [
                {"name": "万用表", "aliases": [], "spec": "", "qty": "", "check_time": "", "check_qty": ""},
            ]},
        },
        "_review": [],
    }))
    (d / "2_商场08品牌B.json").write_text(json.dumps({
        "store": "2.商场08品牌B",
        "scenes": {
            "博物馆": {"parts": [
                {"name": "继电器DC12V", "aliases": [], "spec": "", "qty": "", "check_time": "", "check_qty": ""},
            ]},
            "工具": {"parts": [
                {"name": "电钻", "aliases": [], "spec": "", "qty": "", "check_time": "", "check_qty": ""},
            ]},
        },
        "_review": [],
    }))
    (d / "_store_registry.json").write_text(json.dumps({
        "registry": {"stores": {
            "上海商场08店": ["1_商场08品牌C.json", "2_商场08品牌B.json"],
        }},
        "_review": {},
    }))
    return str(d)


def test_resolve_store_files_union(union_stores_dir):
    assert store_parts.resolve_store_file("上海商场08店", union_stores_dir) == "1_商场08品牌C.json"
    assert store_parts.resolve_store_files("上海商场08店", union_stores_dir) == [
        "1_商场08品牌C.json", "2_商场08品牌B.json"]


def test_union_pool_merges_both_brand_files(union_stores_dir):
    # 品牌C主题：本品牌场景件 + 两家工具池
    pool = store_parts.build_pool("上海商场08店", "鬼屋回魂", union_stores_dir)
    assert "磁铁" in pool                      # 品牌C场景件
    assert "继电器DC12V" not in pool           # 品牌B场景件（不同品牌主题，不混入）
    assert "万用表" in pool                    # 品牌C工具池
    assert "电钻" in pool                      # 品牌B工具池（工具池并集）
    # 品牌B主题
    pool2 = store_parts.build_pool("上海商场08店", "博物馆", union_stores_dir)
    assert "继电器DC12V" in pool2
    assert "磁铁" not in pool2
    assert "万用表" in pool2 and "电钻" in pool2


def test_union_pool_canonicalize_and_keep(union_stores_dir):
    pool = store_parts.build_pool("上海商场08店", "鬼屋回魂", union_stores_dir)
    assert store_parts.canonicalize("磁铁", pool) == "磁铁"
    assert store_parts.canonicalize("电钻", pool) == "电钻"   # 工具池借件
    kept = store_parts.keep_only_matched(["磁铁", "继电器DC12V", "电钻"], pool)
    assert kept == ["磁铁", "电钻"]            # 品牌B场景件在品牌C主题下被静默丢弃


def test_resolve_store_file_by_registry(stores_dir):
    assert store_parts.resolve_store_file("北京商场15店", stores_dir) == "1_北京商场15店.json"
    assert store_parts.resolve_store_file("天津商场22道店", stores_dir) == "2_天津商场22道店.json"


def test_resolve_store_unknown_returns_none(stores_dir):
    assert store_parts.resolve_store_file("不存在的店", stores_dir) is None


def test_pool_includes_scene_and_tool_parts(stores_dir):
    pool = store_parts.build_pool("北京商场15店", "博物馆", stores_dir)
    # 场景件 + 工具件都进池
    assert "继电器DC12V" in pool
    assert "干簧管" in pool
    assert "万用表" in pool  # 工具池并入
    # 非本店/非本主题的不在池里
    assert "磁铁" not in pool
    assert "电钻" not in pool


def test_canonicalize_exact_and_alias(stores_dir):
    pool = store_parts.build_pool("北京商场15店", "博物馆", stores_dir)
    assert store_parts.canonicalize("继电器DC12V", pool) == "继电器DC12V"
    # 别名「继电器」→ 规范名「继电器DC12V」
    assert store_parts.canonicalize("继电器", pool) == "继电器DC12V"
    # 工具池别名（借来用的工具）
    assert store_parts.canonicalize("万能表", pool) == "万用表"


def test_canonicalize_unknown_returns_none(stores_dir):
    pool = store_parts.build_pool("北京商场15店", "博物馆", stores_dir)
    assert store_parts.canonicalize("随便什么不存在的备件", pool) is None


def test_cross_store_part_never_matches(stores_dir):
    # 用 A 店主题的池，去规范化属于 B 店的件 → 不命中
    pool_a = store_parts.build_pool("北京商场15店", "博物馆", stores_dir)
    assert store_parts.canonicalize("磁铁", pool_a) is None          # B 店鬼屋回魂的件
    assert store_parts.canonicalize("电钻", pool_a) is None          # B 店工具池的件
    pool_b = store_parts.build_pool("天津商场22道店", "鬼屋回魂", stores_dir)
    assert store_parts.canonicalize("磁铁", pool_b) == "磁铁"

    # A 店单里提到 B 店件 → 规范化不到 → 静默不记
    raw = ["继电器DC12V", "磁铁"]
    kept = store_parts.keep_only_matched(raw, pool_a)
    assert kept == ["继电器DC12V"]


def test_scene_miss_falls_back_to_tool_only(stores_dir):
    # factory 主题不存在 → 池只有工具池
    pool = store_parts.build_pool("北京商场15店", "不存在的主题", stores_dir)
    assert "万用表" in pool
    assert "继电器DC12V" not in pool
    assert "干簧管" not in pool


def test_store_missing_returns_empty_pool(stores_dir):
    pool = store_parts.build_pool("无此店", "博物馆", stores_dir)
    assert pool == {}
