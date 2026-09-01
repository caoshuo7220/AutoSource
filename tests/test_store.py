"""store.py 单元测试（docs/05 存储架构改造：store JSONL + 入库即验 + coverage）。"""
import json
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).parent.parent / ".claude" / "skills" / "autosource" / "scripts"
sys.path.insert(0, str(SKILL_DIR))

from store import append_records, coverage, load_store, record_search, record_sources

NODES = ["AI训练GPU", "图形渲染GPU", "无线网-Wi-Fi"]


def write_evidence(tmp_path: Path, urls: list[str]) -> Path:
    """构造证据留痕：把候选 URL 放进模拟的 WebSearch 原始结果里（同 hook 记录格式）。"""
    p = tmp_path / "search_log.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for u in urls:
            payload = {"tool_name": "WebSearch", "tool_input": {"query": "test"},
                       "tool_response": {"results": [{"url": u}]}}
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return p


def source_entry(url="https://a.com/doc", name="A",
                 category_path="算力服务器-GPU服务器-AI训练GPU") -> dict:
    return {"name": name, "category_path": category_path, "source_type": "官方文档",
            "granularity": "合集级", "url": url, "description": "d", "reason": "r"}


class TestAppendLoad:
    def test_append_then_load_roundtrip(self, tmp_path):
        store = tmp_path / "store.jsonl"
        rows = [{"type": "source", "url": "https://a.com/1"}, {"type": "search", "query": "q"}]
        assert append_records(store, rows) == 2
        records, bad = load_store(store)
        assert bad == 0
        assert [r["url"] for r in records if r["type"] == "source"] == ["https://a.com/1"]

    def test_bad_line_skipped_and_counted(self, tmp_path):
        store = tmp_path / "store.jsonl"
        store.write_text('{"type": "source", "url": "https://a.com/1"}\n损坏的行\n', encoding="utf-8")
        records, bad = load_store(store)
        assert bad == 1
        assert len(records) == 1

    def test_empty_batch_does_not_create_file(self, tmp_path):
        store = tmp_path / "store.jsonl"
        assert append_records(store, []) == 0
        assert not store.exists()


class TestRecordSources:
    def test_valid_entry_accepted_and_persisted(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc"])
        result = record_sources(store, [source_entry()], evidence, NODES)
        assert result["accepted"] == 1
        assert result["skipped"] == 0
        assert result["rejected"] == []
        records, _ = load_store(store)
        assert records[0]["name"] == "A"
        assert records[0]["url"] == "https://a.com/doc"

    def test_url_not_in_evidence_rejected_with_reason(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://other.com/x"])
        result = record_sources(store, [source_entry()], evidence, NODES)
        assert result["accepted"] == 0
        assert len(result["rejected"]) == 1
        assert "证据" in result["rejected"][0]["reason"]
        assert result["rejected"][0]["index"] == 0
        assert not store.exists()

    def test_missing_name_rejected(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc"])
        bad = source_entry()
        bad["name"] = ""
        result = record_sources(store, [bad], evidence, NODES)
        assert result["accepted"] == 0
        assert result["rejected"][0]["reason"] == "缺 name/url"

    def test_missing_url_rejected(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc"])
        bad = source_entry()
        bad["url"] = ""
        result = record_sources(store, [bad], evidence, NODES)
        assert result["accepted"] == 0
        assert result["rejected"][0]["reason"] == "缺 name/url"

    def test_invalid_granularity_normalized_to_collection(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc"])
        e = source_entry()
        e["granularity"] = "站点级"
        result = record_sources(store, [e], evidence, NODES)
        assert result["accepted"] == 1
        records, _ = load_store(store)
        assert records[0]["granularity"] == "合集级"

    def test_duplicate_exact_url_skipped(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc"])
        first = record_sources(store, [source_entry()], evidence, NODES)
        second = record_sources(store, [source_entry(name="A2")], evidence, NODES)
        assert first["accepted"] == 1
        assert second["skipped"] == 1
        assert second["accepted"] == 0
        records, _ = load_store(store)
        assert len(records) == 1

    def test_batch_partial_rejection_does_not_block_rest(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc"])
        good = source_entry()
        bad = source_entry(url="https://absent.com/x", name="B")
        result = record_sources(store, [bad, good], evidence, NODES)
        assert result["accepted"] == 1
        assert len(result["rejected"]) == 1
        assert result["rejected"][0]["index"] == 0
        records, _ = load_store(store)
        assert records[0]["name"] == "A"

    def test_missing_evidence_log_rejects_with_clear_reason(self, tmp_path):
        store = tmp_path / "store.jsonl"
        missing = tmp_path / "no_such_log.jsonl"
        result = record_sources(store, [source_entry()], missing, NODES)
        assert result["accepted"] == 0
        assert "证据留痕" in result["rejected"][0]["reason"]

    def test_node_field_derived_from_category_path(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc", "https://b.com/doc"])
        plain = source_entry()
        dashed = source_entry(url="https://b.com/doc", name="B",
                              category_path="算力服务器-配套生态-无线网-Wi-Fi")
        result = record_sources(store, [plain, dashed], evidence, NODES)
        assert result["accepted"] == 2
        records, _ = load_store(store)
        assert records[0]["node"] == "AI训练GPU"
        assert records[1]["node"] == "无线网-Wi-Fi"


class TestRecordSearch:
    def test_search_batch_appended(self, tmp_path):
        store = tmp_path / "store.jsonl"
        rows = [{"phase": "增量发现", "node": "AI训练GPU", "query": "GPU 排名 数据库",
                 "results": 10, "extracted": 3}]
        assert record_search(store, rows) == 1
        records, _ = load_store(store)
        assert records[0]["type"] == "search"
        assert records[0]["query"] == "GPU 排名 数据库"

    def test_non_dict_rows_skipped(self, tmp_path):
        store = tmp_path / "store.jsonl"
        assert record_search(store, [{"query": "q"}, "垃圾", None]) == 1


class TestCoverage:
    def _seed(self, tmp_path, sources, searches):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, [s["url"] for s in sources])
        record_sources(store, sources, evidence, NODES)
        record_search(store, searches)
        return store

    def test_missing_computed(self, tmp_path):
        store = self._seed(
            tmp_path,
            [source_entry(), source_entry(url="https://a.com/2", name="A2"),
             source_entry(url="https://a.com/3", name="A3")],
            [{"phase": "增量发现", "node": "AI训练GPU", "query": "q1", "results": 10, "extracted": 5}],
        )
        cov = {c["node"]: c for c in coverage(store, NODES)}
        assert cov["AI训练GPU"]["recorded"] == 3
        assert cov["AI训练GPU"]["extracted"] == 5
        assert cov["AI训练GPU"]["missing"] == 2
        # 无活动节点也列出、缺口为 0
        assert cov["图形渲染GPU"]["recorded"] == 0
        assert cov["图形渲染GPU"]["missing"] == 0

    def test_extracted_zero_means_no_missing(self, tmp_path):
        store = self._seed(
            tmp_path,
            [source_entry()],
            [{"phase": "增量发现", "node": "AI训练GPU", "query": "q1", "results": 0, "extracted": 0}],
        )
        cov = {c["node"]: c for c in coverage(store, NODES)}
        assert cov["AI训练GPU"]["missing"] == 0
