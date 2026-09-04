"""search_provider.py 的单元测试：SearchProvider 接口与 HostSearchProvider 实现。

契约（实现规格第四章）：按 query_id 匹配（不按 query 文本，避免重复查询词歧义）、
校验完整性（缺失条目 → failed）、多余条目忽略、业务模块不感知搜索来源。
"""
import json
from pathlib import Path

import pytest

from search_provider import HostSearchProvider, SearchProvider


def test_search_provider_is_interface():
    provider = SearchProvider()
    with pytest.raises(NotImplementedError):
        provider.fetch([])


def test_fetch_preserves_success_and_failure_entries(tmp_path):
    entries = [
        {"query_id": 1, "query": "q1", "results": [{"title": "t", "url": "https://a/x", "snippet": "s"}],
         "failed": False, "attempts": 1},
        {"query_id": 2, "query": "q2", "results": [], "failed": False, "attempts": 1},
        {"query_id": 3, "query": "q3", "results": [], "failed": True, "attempts": 3, "error": "timeout"},
    ]
    path = tmp_path / "search_results.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    batch = HostSearchProvider(path).fetch([
        {"query_id": 1, "query": "q1"}, {"query_id": 2, "query": "q2"},
        {"query_id": 3, "query": "q3"},
    ])
    assert batch == entries


def test_fetch_matches_by_query_id_not_text(tmp_path):
    """重复查询词歧义：两个 query 文本相同，靠 query_id 区分，互不混淆。"""
    entries = [
        {"query_id": 1, "query": "same text", "results": [{"title": "A", "url": "https://a/1", "snippet": ""}],
         "failed": False, "attempts": 1},
        {"query_id": 2, "query": "same text", "results": [{"title": "B", "url": "https://b/2", "snippet": ""}],
         "failed": False, "attempts": 1},
    ]
    path = tmp_path / "search_results.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    batch = HostSearchProvider(path).fetch([
        {"query_id": 1, "query": "same text"}, {"query_id": 2, "query": "same text"},
    ])
    assert [e["query_id"] for e in batch] == [1, 2]
    assert batch[0]["results"][0]["url"] == "https://a/1"
    assert batch[1]["results"][0]["url"] == "https://b/2"


def test_fetch_missing_entry_marked_failed(tmp_path):
    """搜索结果文件缺失某 query 条目 → 视为 failed（attempts=0，error 说明），不静默丢弃。"""
    entries = [
        {"query_id": 1, "query": "q1", "results": [], "failed": False, "attempts": 1},
    ]
    path = tmp_path / "search_results.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    batch = HostSearchProvider(path).fetch([
        {"query_id": 1, "query": "q1"}, {"query_id": 2, "query": "q2"},
    ])
    assert batch[1] == {"query_id": 2, "query": "q2", "results": [],
                        "failed": True, "attempts": 0,
                        "error": "搜索结果文件缺少该 query 的条目"}


def test_fetch_extra_entries_ignored(tmp_path):
    """文件里多余 query_id（不属于本批 pending）→ 忽略。"""
    entries = [
        {"query_id": 1, "query": "q1", "results": [], "failed": False, "attempts": 1},
        {"query_id": 99, "query": "上一批残留", "results": [], "failed": False, "attempts": 1},
    ]
    path = tmp_path / "search_results.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    batch = HostSearchProvider(path).fetch([{"query_id": 1, "query": "q1"}])
    assert len(batch) == 1
    assert batch[0]["query_id"] == 1


def test_fetch_outputs_in_pending_order(tmp_path):
    """输出按 pending 顺序（与文件条目顺序无关），便于逐 query 判定。"""
    entries = [
        {"query_id": 2, "query": "q2", "results": [], "failed": False, "attempts": 1},
        {"query_id": 1, "query": "q1", "results": [], "failed": False, "attempts": 1},
    ]
    path = tmp_path / "search_results.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    batch = HostSearchProvider(path).fetch([
        {"query_id": 1, "query": "q1"}, {"query_id": 2, "query": "q2"},
    ])
    assert [e["query_id"] for e in batch] == [1, 2]


def test_fetch_invalid_json_raises(tmp_path):
    path = tmp_path / "search_results.json"
    path.write_text("不是JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="搜索结果文件"):
        HostSearchProvider(path).fetch([{"query_id": 1, "query": "q1"}])


def test_fetch_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        HostSearchProvider(tmp_path / "不存在.json").fetch(
            [{"query_id": 1, "query": "q1"}])
