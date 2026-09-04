"""state.py 的单元测试：状态读写、schema 校验、原子写入、树校验与增量写回。"""
import json
from pathlib import Path

import pytest

from state import (StateError, apply_writeback, ensure_angle_in_dims, leaf_names,
                   load_state, mark_angles, new_state, node_map, path_of,
                   sanitize_domain, save_state, validate_tree)

TREE = [
    {"name": "交换机", "parent": "", "terms": ["交换机"], "entities": [],
     "dims": [], "angles": []},
    {"name": "数据中心交换机", "parent": "交换机",
     "terms": ["数据中心交换机"], "entities": [], "dims": ["官方文档"],
     "angles": ["官方文档"]},
    {"name": "园区交换机", "parent": "交换机", "terms": ["园区交换机"],
     "entities": [], "dims": ["厂商文档"], "angles": []},
]


def test_new_state_defaults():
    """--init 后的初始状态：phase=running、三集合为空、pending 空、统计归零。"""
    state = new_state("交换机", TREE)
    assert state["domain"] == "交换机"
    assert state["phase"] == "running"
    assert state["sources"] == []
    assert state["pending_batch"] == {}
    assert state["exploration"]["search_history"] == []
    assert state["exploration"]["gaps"] == []
    assert state["exploration"]["loop_stats"] == {
        "batch_count": 0, "consecutive_no_new": 0, "failed_queries": 0}


def test_new_state_fills_entities_angles():
    """init 输出无 entities/angles，脚本补齐空数组（缺省字段不报错）。"""
    nodes = [{"name": "交换机", "parent": "", "terms": ["交换机"], "dims": []}]
    state = new_state("交换机", nodes)
    assert state["structure"]["nodes"][0]["entities"] == []
    assert state["structure"]["nodes"][0]["angles"] == []


def test_save_load_roundtrip(tmp_path):
    state = new_state("交换机", TREE)
    path = tmp_path / "state.json"
    save_state(path, state)
    assert load_state(path) == state


def test_save_state_atomic_no_tmp_leftover(tmp_path):
    """原子写入：写临时文件 + 原子替换，完成后目录内只有 state.json。"""
    path = tmp_path / "state.json"
    save_state(path, new_state("交换机", TREE))
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_save_state_preserves_content_on_overwrite(tmp_path):
    """覆盖写入整体替换：旧字段不残留。"""
    path = tmp_path / "state.json"
    state = new_state("交换机", TREE)
    state["sources"].append({"name": "旧条目", "url": "https://old.example/"})
    save_state(path, state)
    state2 = new_state("交换机", TREE)
    save_state(path, state2)
    assert load_state(path)["sources"] == []


def test_load_state_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_state(tmp_path / "state.json")


def test_load_state_bad_json_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{不是JSON", encoding="utf-8")
    with pytest.raises(StateError):
        load_state(path)


def test_load_state_bad_shape_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"domain": "交换机"}), encoding="utf-8")
    with pytest.raises(StateError):
        load_state(path)


def test_load_state_bad_phase_raises(tmp_path):
    state = new_state("交换机", TREE)
    state["phase"] = "flying"
    path = tmp_path / "state.json"
    save_state(path, state)
    with pytest.raises(StateError):
        load_state(path)


def test_validate_tree_ok():
    validate_tree(TREE)


def test_validate_tree_single_root():
    """单根约束：第二个 parent="" 的节点拒绝。"""
    nodes = TREE + [{"name": "另一根", "parent": "", "terms": [], "dims": []}]
    with pytest.raises(StateError, match="根节点"):
        validate_tree(nodes)


def test_validate_tree_duplicate_names():
    nodes = TREE + [{"name": "园区交换机", "parent": "交换机", "terms": [],
                     "dims": []}]
    with pytest.raises(StateError, match="重复"):
        validate_tree(nodes)


def test_validate_tree_unknown_parent():
    nodes = TREE + [{"name": "孤儿", "parent": "不存在的节点", "terms": [],
                     "dims": []}]
    with pytest.raises(StateError, match="parent"):
        validate_tree(nodes)


def test_validate_tree_no_root():
    nodes = [{"name": "交换机", "parent": "空", "terms": [], "dims": []}]
    with pytest.raises(StateError, match="根节点"):
        validate_tree(nodes)


def test_leaf_names_excludes_internal():
    assert leaf_names(TREE) == {"数据中心交换机", "园区交换机"}


def test_node_map_and_path_of():
    assert set(node_map(TREE)) == {"交换机", "数据中心交换机", "园区交换机"}
    assert path_of("数据中心交换机", TREE) == "交换机-数据中心交换机"
    assert path_of("交换机", TREE) == "交换机"


def test_apply_writeback_entities_and_terms():
    state = new_state("交换机", TREE)
    result = apply_writeback(
        state,
        new_nodes=[],
        new_entities=[{"name": "SONiC", "kind": "项目", "node": "数据中心交换机"}],
        new_terms=[{"term": "ToR", "node": "数据中心交换机"}],
    )
    assert result["rejected"] == []
    node = node_map(state["structure"]["nodes"])["数据中心交换机"]
    assert node["entities"] == [{"name": "SONiC", "kind": "项目"}]
    assert "ToR" in node["terms"]


def test_apply_writeback_dedup():
    state = new_state("交换机", TREE)
    apply_writeback(
        state, new_nodes=[],
        new_entities=[{"name": "SONiC", "kind": "项目", "node": "数据中心交换机"},
                      {"name": "SONiC", "kind": "项目", "node": "数据中心交换机"}],
        new_terms=[{"term": "ToR", "node": "数据中心交换机"},
                   {"term": "ToR", "node": "数据中心交换机"}],
    )
    node = node_map(state["structure"]["nodes"])["数据中心交换机"]
    assert node["entities"] == [{"name": "SONiC", "kind": "项目"}]
    assert node["terms"].count("ToR") == 1


def test_apply_writeback_new_node_full_fields():
    """new_nodes 作为完整节点加入（含可搜索字段），entities/angles 默认空。"""
    state = new_state("交换机", TREE)
    apply_writeback(
        state,
        new_nodes=[{"name": "无线交换机", "parent": "交换机",
                    "terms": ["wireless switch"], "dims": ["官方文档"]}],
        new_entities=[], new_terms=[],
    )
    node = node_map(state["structure"]["nodes"])["无线交换机"]
    assert node["terms"] == ["wireless switch"]
    assert node["dims"] == ["官方文档"]
    assert node["entities"] == []
    assert node["angles"] == []


def test_apply_writeback_reject_duplicate_node_name():
    state = new_state("交换机", TREE)
    result = apply_writeback(
        state,
        new_nodes=[{"name": "园区交换机", "parent": "交换机", "terms": [],
                    "dims": []}],
        new_entities=[], new_terms=[],
    )
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["reason"]


def test_apply_writeback_reject_unknown_parent():
    state = new_state("交换机", TREE)
    result = apply_writeback(
        state,
        new_nodes=[{"name": "新节点", "parent": "不存在", "terms": [], "dims": []}],
        new_entities=[], new_terms=[],
    )
    assert len(result["rejected"]) == 1


def test_apply_writeback_reject_second_root():
    state = new_state("交换机", TREE)
    result = apply_writeback(
        state,
        new_nodes=[{"name": "另一领域", "parent": "", "terms": [], "dims": []}],
        new_entities=[], new_terms=[],
    )
    assert len(result["rejected"]) == 1
    assert "根节点" in result["rejected"][0]["reason"]


def test_apply_writeback_reject_unknown_node_entity():
    state = new_state("交换机", TREE)
    result = apply_writeback(
        state, new_nodes=[],
        new_entities=[{"name": "X", "kind": "厂商", "node": "不存在的节点"}],
        new_terms=[],
    )
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["name"] == "X"


def test_apply_writeback_reject_bad_entity_kind():
    """kind 枚举契约：非五类取值拒绝（规格第二章 entities.kind 枚举）。"""
    state = new_state("交换机", TREE)
    result = apply_writeback(
        state, new_nodes=[],
        new_entities=[{"name": "X", "kind": "神秘物体", "node": "数据中心交换机"}],
        new_terms=[],
    )
    assert len(result["rejected"]) == 1
    assert "kind" in result["rejected"][0]["reason"]


def test_ensure_angle_in_dims():
    nodes = [{"name": "n", "parent": "", "terms": [], "entities": [],
              "dims": ["官方文档"], "angles": []}]
    assert ensure_angle_in_dims(nodes, "n", "官方文档") is False
    assert ensure_angle_in_dims(nodes, "n", "行业标准") is True
    assert "行业标准" in nodes[0]["dims"]


def test_mark_angles_done_only():
    """--commit 规则：仅 status=done 的 query 其 angle 记入 angles，failed 不记。"""
    nodes = [{"name": "n", "parent": "", "terms": [], "entities": [],
              "dims": ["A", "B"], "angles": []}]
    queries = [
        {"query_id": 1, "node": "n", "angle": "A", "status": "done"},
        {"query_id": 2, "node": "n", "angle": "B", "status": "failed"},
        {"query_id": 3, "node": "n", "angle": "A", "status": "done"},
    ]
    mark_angles(nodes, queries)
    assert nodes[0]["angles"] == ["A"]


def test_sanitize_domain():
    assert sanitize_domain("交换机") == "交换机"
    assert sanitize_domain("AI 算力 服务器") == "AI_算力_服务器"
    assert sanitize_domain('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert sanitize_domain("") == "未命名领域"
    assert len(sanitize_domain("超" * 40)) == 30
