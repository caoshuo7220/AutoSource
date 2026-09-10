"""store.py 单元测试（docs/04 存储架构改造：store JSONL + 入库即验 + coverage）。"""
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent / ".claude" / "skills" / "autosource" / "scripts"
sys.path.insert(0, str(SKILL_DIR))

from store import (SOURCE_TYPES, SOURCE_TYPE_ALIASES, append_records,
                   canonicalize_source_type, coverage, load_store, record_search,
                   record_sources, record_knowledge)

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
    return {"name": name, "category_path": category_path, "source_type": "文档",
            "granularity": "合集级", "url": url, "description": "d", "reason": "r"}


class TestCanonicalizeSourceType:
    """封闭词表归一：标准词自映射（幂等）→ 别名映射 → 其他兜底。"""

    def test_canonical_words_self_map(self):
        for t in SOURCE_TYPES:
            assert canonicalize_source_type(t) == t

    def test_alias_words_mapped(self):
        assert canonicalize_source_type("厂商文档") == "文档"
        assert canonicalize_source_type("市场研究") == "报告"
        assert canonicalize_source_type("行业标准") == "标准"
        assert canonicalize_source_type("开源社区") == "社区"
        assert canonicalize_source_type("仓库") == "代码仓库"
        # 2026-09-08 交换机 403 条轮表外词收敛（180413 实测 67 条落「其他」中三条
        # 高频形态词：开放组织=联盟/基金会官网、专利数据库=专利检索平台、技术白皮书=报告）
        assert canonicalize_source_type("开放组织") == "官网"
        assert canonicalize_source_type("专利数据库") == "专利"
        assert canonicalize_source_type("技术白皮书") == "报告"
        # 2026-09-10 交换机 2110 条轮表外词收敛（46 种原始词唯一真表外词）：
        # 开放标准=开放标准体系（如 OCP 开放标准）属标准族
        assert canonicalize_source_type("开放标准") == "标准"

    def test_unknown_word_falls_back_to_other(self):
        assert canonicalize_source_type("没见过的新词") == "其他"

    def test_empty_preserved_as_missing(self):
        """空/空白 = 模型违约未填（残缺）——如实保留空，不落「其他」、不被误报为
        表外词（残缺由 stats「未标注」口径承接，见 postprocess.compute_stats）。"""
        assert canonicalize_source_type("") == ""
        assert canonicalize_source_type(None) == ""
        assert canonicalize_source_type("  ") == ""

    def test_idempotent_on_double_apply(self):
        once = canonicalize_source_type("厂商文档")
        assert canonicalize_source_type(once) == once

    def test_canonicalize_closed_idempotent_on_known_words(self):
        """性质钉住：非空输入闭合于 SOURCE_TYPES 且幂等——词表/别名扩展不破坏不变量。"""
        samples = list(SOURCE_TYPES) + list(SOURCE_TYPE_ALIASES)
        for w in samples:
            once = canonicalize_source_type(w)
            assert once in SOURCE_TYPES
            assert canonicalize_source_type(once) == once


class TestRecordSourcesTypeNormalization:
    """入库即归一：store 只存标准词，原始词留痕 source_type_raw，表外词进 unmapped。"""

    def test_alias_normalized_and_raw_preserved(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc"])
        result = record_sources(store, [source_entry() | {"source_type": "厂商文档"}],
                                evidence, NODES)
        assert result["accepted"] == 1
        assert result["unmapped"] == []
        records, _ = load_store(store)
        assert records[0]["source_type"] == "文档"
        assert records[0]["source_type_raw"] == "厂商文档"

    def test_unmapped_word_falls_back_with_alert(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc", "https://b.com/doc"])
        result = record_sources(store, [
            source_entry() | {"source_type": "某新词"},
            source_entry(url="https://b.com/doc", name="B") | {"source_type": "文档"},
        ], evidence, NODES)
        assert result["accepted"] == 2
        assert result["unmapped"] == ["某新词"]
        records, _ = load_store(store)
        assert records[0]["source_type"] == "其他"
        assert records[0]["source_type_raw"] == "某新词"
        assert records[1]["source_type"] == "文档"
        assert records[1]["source_type_raw"] == "文档"


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


class TestRecordKnowledge:
    """清单核对结果入库（2026-09-08 架构修订：验证结果随验证过程落库，废除
    "会话暂存 + 阶段 5 一次性转写 manifest"——214051 实证漏写 60 个 verified
    字段，手工转写 65 条 JSON 是必然出错的事故类型，且与压缩丢失风险同源）。"""

    def _entry(self, **kw) -> dict:
        e = {"name": "K1", "node": "AI训练GPU", "verified": True,
             "category_path": "算力服务器-GPU服务器-AI训练GPU",
             "source_type": "官方文档", "granularity": "合集级",
             "url": "https://k.com/doc", "description": "kd", "reason": "kr"}
        e.update(kw)
        return e

    def test_verified_entry_accepted_and_normalized(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://k.com/doc"])
        result = record_knowledge(store, [self._entry()], evidence)
        assert result["accepted"] == 1
        assert result["rejected"] == []
        assert result["unmapped"] == []
        records, _ = load_store(store)
        r = records[0]
        assert r["type"] == "knowledge"
        assert r["name"] == "K1"
        assert r["verified"] is True
        assert r["source_type"] == "文档"          # 入库即归一
        assert r["source_type_raw"] == "官方文档"   # 原始词留痕
        assert r["ts"]

    def test_unverified_entry_accepted_without_url_or_evidence(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = tmp_path / "absent.jsonl"       # 未验证项不带 URL，不查证据
        result = record_knowledge(store, [self._entry(
            verified=False, url="", note="未找到官方入口",
            category_path="", source_type="", description="", reason="")],
            evidence)
        assert result["accepted"] == 1
        records, _ = load_store(store)
        assert records[0]["verified"] is False
        assert records[0]["note"] == "未找到官方入口"

    def test_verified_without_url_rejected(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://k.com/doc"])
        result = record_knowledge(store, [self._entry(url="")], evidence)
        assert result["accepted"] == 0
        assert result["rejected"][0]["reason"] == "verified=true 缺 url"

    def test_verified_url_not_in_evidence_rejected(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://other.com/x"])
        result = record_knowledge(store, [self._entry()], evidence)
        assert result["accepted"] == 0
        assert "证据" in result["rejected"][0]["reason"]
        assert not store.exists()

    def test_missing_name_or_node_rejected(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://k.com/doc"])
        result = record_knowledge(store, [self._entry(name="")], evidence)
        assert result["accepted"] == 0
        assert result["rejected"][0]["reason"] == "缺 name/node"
        result = record_knowledge(store, [self._entry(node="")], evidence)
        assert result["accepted"] == 0
        assert result["rejected"][0]["reason"] == "缺 name/node"

    def test_unmapped_type_reported(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://k.com/doc"])
        result = record_knowledge(store, [self._entry(source_type="某新词")], evidence)
        assert result["accepted"] == 1
        assert result["unmapped"] == ["某新词"]
        records, _ = load_store(store)
        assert records[0]["source_type"] == "其他"
        assert records[0]["source_type_raw"] == "某新词"


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

    def test_ts_stamped_on_accepted_rows(self, tmp_path):
        """docs/04 §3.1：store 每行由脚本盖时间戳（模型无时钟）——入库记录必须带 ts。"""
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc"])
        assert record_sources(store, [source_entry()], evidence, NODES)["accepted"] == 1
        records, _ = load_store(store)
        assert records[0].get("ts")


class TestRecordSearch:
    def test_search_batch_appended(self, tmp_path):
        store = tmp_path / "store.jsonl"
        rows = [{"phase": "增量发现", "node": "AI训练GPU", "query": "GPU 排名 数据库",
                 "results": 10, "extracted": 3}]
        assert record_search(store, rows) == 1
        records, _ = load_store(store)
        assert records[0]["type"] == "search"
        assert records[0]["query"] == "GPU 排名 数据库"

    def test_search_verified_field_roundtrip(self, tmp_path):
        """2026-09-01：验证搜索日志拆分 verified/extracted 两字段——store 透传不丢失。"""
        store = tmp_path / "store.jsonl"
        assert record_search(store, [{"phase": "验证搜索", "node": "n", "query": "q",
                                      "results": 10, "extracted": 0,
                                      "verified": True}]) == 1
        records, _ = load_store(store)
        assert records[0]["verified"] is True
        assert records[0]["extracted"] == 0

    def test_search_zero_reason_roundtrip(self, tmp_path):
        """2026-09-09 收尾护栏配套：零提取理由 zero_reason 透传不丢失（哨兵 2/3 数据面）。"""
        store = tmp_path / "store.jsonl"
        assert record_search(store, [{"phase": "增量发现", "node": "AI训练GPU",
                                      "query": "q", "results": 10, "extracted": 0,
                                      "zero_reason": "垃圾域"}]) == 1
        records, _ = load_store(store)
        assert records[0]["zero_reason"] == "垃圾域"

    def test_search_zero_reason_defaults_empty(self, tmp_path):
        """未填 zero_reason 的旧批次行为不变（空串落库，哨兵在收尾按空值拦截）。"""
        store = tmp_path / "store.jsonl"
        assert record_search(store, [{"phase": "增量发现", "node": "AI训练GPU",
                                      "query": "q", "results": 10, "extracted": 0}]) == 1
        records, _ = load_store(store)
        assert records[0]["zero_reason"] == ""

    def test_non_dict_rows_skipped(self, tmp_path):
        store = tmp_path / "store.jsonl"
        assert record_search(store, [{"query": "q"}, "垃圾", None]) == 1

    def test_ts_stamped_on_search_rows(self, tmp_path):
        """docs/04 §3.1：搜索日志行同样由脚本盖时间戳。"""
        store = tmp_path / "store.jsonl"
        assert record_search(store, [{"phase": "增量发现", "node": "AI训练GPU",
                                      "query": "q", "results": 1, "extracted": 0}]) == 1
        records, _ = load_store(store)
        assert records[0].get("ts")


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


class TestGarbageDomainGate:
    """2026-09-10 收录政策收紧：垃圾域/低价值聚合平台入库即拒（GARBAGE_DOMAINS
    平台级名单，与收尾过滤双层——名单放"大平台"，长尾单站由判据①覆盖）。"""

    def test_source_rejected_on_garbage_domain(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://shuma.taobao.com/item/1",
                                             "https://a.com/doc"])
        result = record_sources(store, [
            source_entry(url="https://shuma.taobao.com/item/1", name="G"),
            source_entry(),
        ], evidence, NODES)
        assert result["accepted"] == 1
        assert result["rejected"][0]["reason"] == "垃圾域/低价值聚合平台，不收"
        assert "taobao" in result["rejected"][0]["url"]

    def test_knowledge_rejected_on_garbage_domain(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://zhuanlan.zhihu.com/p/1"])
        result = record_knowledge(store, [{
            "name": "K1", "node": "AI训练GPU", "verified": True,
            "category_path": "算力服务器-GPU服务器-AI训练GPU",
            "source_type": "厂商文档", "granularity": "合集级",
            "url": "https://zhuanlan.zhihu.com/p/1", "description": "d", "reason": "r"}],
            evidence)
        assert result["accepted"] == 0
        assert "垃圾域" in result["rejected"][0]["reason"]

    def test_platform_subdomain_patterns_match(self, tmp_path):
        """平台级名单按子串匹配：blog.csdn.net 命中 csdn、www.jd.com 命中 jd.com。"""
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://blog.csdn.net/x",
                                             "https://www.jd.com/x",
                                             "https://a.com/doc"])
        result = record_sources(store, [
            source_entry(url="https://blog.csdn.net/x", name="B"),
            source_entry(url="https://www.jd.com/x", name="J"),
            source_entry(),
        ], evidence, NODES)
        assert result["accepted"] == 1
        assert len(result["rejected"]) == 2

    def test_clean_domains_unaffected(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc"])
        result = record_sources(store, [source_entry()], evidence, NODES)
        assert result["accepted"] == 1

    def test_aws_docs_not_matched_by_amazon_entry(self, tmp_path):
        """钉桩：docs.aws.amazon.com 是合法厂商文档门户，不得被电商词条误伤。

        子串匹配下 "amazon.com" 会命中它（2026-09-10 提交前审查实测：SOM厂商轮
        交付物中即有该域名）——名单词条须为 www.amazon. 形态，只匹配电商主站。
        """
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://docs.aws.amazon.com/x"])
        result = record_sources(
            store, [source_entry(url="https://docs.aws.amazon.com/x", name="AWS 文档")],
            evidence, NODES)
        assert result["accepted"] == 1
        assert result["rejected"] == []

    def test_verified_false_without_url_not_checked(self, tmp_path):
        """verified=false 不带 URL——无域名可查，不受闸门影响。"""
        store = tmp_path / "store.jsonl"
        result = record_knowledge(store, [{
            "name": "K1", "node": "AI训练GPU", "verified": False,
            "note": "未找到官方入口"}], tmp_path / "no-evidence.jsonl")
        assert result["accepted"] == 1
