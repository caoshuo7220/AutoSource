"""store.py 单元测试（docs/04 存储架构改造：store JSONL + 入库即验 + coverage）。"""
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent / ".claude" / "skills" / "autosource" / "scripts"
sys.path.insert(0, str(SKILL_DIR))

from store import (SOURCE_TYPES, SOURCE_TYPE_ALIASES, STORE_FIELDS, append_records,
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
                 category_path="算力服务器-GPU服务器-AI训练GPU",
                 source_type="文档") -> dict:
    return {"name": name, "category_path": category_path, "source_type": source_type,
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
        """性质回归断言：非空输入闭合于 SOURCE_TYPES 且幂等——词表/别名扩展不破坏不变量。"""
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
    "会话暂存 + 阶段 6 一次性转写 manifest"——214051 实证漏写 60 个 verified
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

    def test_name_not_in_manifest_rejected_with_suggestion(self, tmp_path):
        """2026-09-17：名称对账前移到入库时（manifest 与 store 同目录）。

        收尾折叠按名精确对账，名称写错原本要等 finalize 才点名——115926 实证：
        模型把「HPE Aruba Networking 技术文档门户」写成"……技术文档库"，收尾漏录
        9 项、整轮返工。"""
        store = tmp_path / "store.jsonl"
        (tmp_path / "manifest.json").write_text(json.dumps(
            {"nodes": NODES,
             "knowledge": [{"name": "HPE Aruba Networking 技术文档门户",
                            "node": "AI训练GPU"}]}, ensure_ascii=False), encoding="utf-8")
        evidence = write_evidence(tmp_path, ["https://k.com/doc"])
        result = record_knowledge(
            store, [self._entry(name="HPE Aruba Networking 技术文档库")], evidence)
        assert result["accepted"] == 0
        reason = result["rejected"][0]["reason"]
        assert "不在 manifest 声明清单中" in reason
        assert "最相近的是「HPE Aruba Networking 技术文档门户」" in reason

    def test_no_manifest_skips_name_check(self, tmp_path):
        """manifest 不存在时不校验名称——阶段 1 的行前清单核对先于 manifest 写入，
        厂商名单本来就是 manifest 的来源（115926 实测：首个 record_knowledge 落库
        在 manifest 写盘之前）。"""
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://k.com/doc"])
        result = record_knowledge(store, [self._entry(name="任意名称")], evidence)
        assert result["accepted"] == 1

    def test_name_matching_is_verbatim(self, tmp_path):
        """2026-09-22 统一为逐字口径：入库校验与收尾对账共用同一名称集合。

        此前入库侧对声明集 strip、收尾侧按原文比对——同一差异会"入库放过、
        收尾判未核对"；统一逐字后差异在入库当场点名（与 URL 逐字校验同纪律）。
        """
        store = tmp_path / "store.jsonl"
        (tmp_path / "manifest.json").write_text(json.dumps(
            {"nodes": NODES,
             "knowledge": [{"name": "机构乙 ", "node": "AI训练GPU"}]},
            ensure_ascii=False), encoding="utf-8")
        evidence = write_evidence(tmp_path, ["https://k.com/doc"])
        result = record_knowledge(store, [self._entry(name="机构乙")], evidence)
        assert result["accepted"] == 0
        assert "不在 manifest 声明清单中" in result["rejected"][0]["reason"]
        result = record_knowledge(store, [self._entry(name="机构乙 ")], evidence)
        assert result["accepted"] == 1

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

    def test_ungrounded_url_hint_points_at_same_host_form(self, tmp_path):
        """2026-09-17：拒收消息给出"留痕里同一主机出现过的写法"——模型照抄即可改对。

        115926 实证：只报"不在留痕中"时模型反复试错无效，最后去读 evidence.py 源码
        才定位到差异（http/https 写法、`**` 剥离）。"""
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc/index.html"])
        result = record_sources(store, [source_entry(url="https://a.com/other")],
                                evidence, NODES)
        assert result["accepted"] == 0
        reason = result["rejected"][0]["reason"]
        assert "留痕里同一主机出现过的写法" in reason
        assert "https://a.com/doc/index.html" in reason

    def test_ungrounded_url_hint_says_host_absent(self, tmp_path):
        """留痕里该主机一条都没有 → 说明这条 URL 未被搜到，不能凭记忆写。"""
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc"])
        result = record_sources(store, [source_entry(url="https://ghost.com/x")],
                                evidence, NODES)
        assert result["accepted"] == 0
        assert "没有该主机的任何 URL" in result["rejected"][0]["reason"]

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

    def test_category_path_unmatched_node_rejected(self, tmp_path):
        """2026-09-10 入库即验扩展：category_path 必须匹配声明节点。

        172015 轮模型填成"节点名/条目名"（如 `工业以太网交换机/Westermo 官网`），
        108 条全部归不到节点、stats 每节点 0 —— 整份交付物的节点维度报废。
        错格式当场拒绝并提示正确写法。
        """
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc"])
        bad = source_entry()
        bad["category_path"] = "算力服务器/A 文档"
        result = record_sources(store, [bad], evidence, NODES)
        assert result["accepted"] == 0
        assert "节点名本身" in result["rejected"][0]["reason"]
        ok = source_entry(category_path="无线网-Wi-Fi")
        assert record_sources(store, [ok], evidence, NODES)["accepted"] == 1

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

    def test_search_zero_reason_roundtrip(self, tmp_path):
        """2026-09-09 收尾护栏配套：零提取理由 zero_reason 透传不丢失（校验 2/3 数据面）。"""
        store = tmp_path / "store.jsonl"
        assert record_search(store, [{"phase": "增量发现", "node": "AI训练GPU",
                                      "query": "q", "results": 10, "extracted": 0,
                                      "zero_reason": "垃圾域"}]) == 1
        records, _ = load_store(store)
        assert records[0]["zero_reason"] == "垃圾域"

    def test_search_zero_reason_defaults_empty(self, tmp_path):
        """未填 zero_reason 的旧批次行为不变（空串落库，校验在收尾按空值拦截）。"""
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

    def test_pending_lists_declared_names_without_knowledge_record(self, tmp_path):
        """pending = manifest 声明名 − 已有 knowledge 记录的名（2026-09-21 补）。

        阶段 5/6 要判"两栏清单项全部核对完成"，此前只能自己去 store.jsonl 里 grep
        （180104 轮实测 9 次）。verified 真假都算核对完成——定案后一律落 record_knowledge。
        """
        store = self._seed(tmp_path, [source_entry()], [])
        (tmp_path / "manifest.json").write_text(json.dumps(
            {"nodes": NODES, "vendors": [{"name": "厂商甲", "node": "AI训练GPU"}],
             "knowledge": [{"name": "机构乙", "node": "AI训练GPU"},
                           {"name": "机构丙", "node": "图形渲染GPU"}]},
            ensure_ascii=False), encoding="utf-8")
        cov = {c["node"]: c for c in coverage(store, NODES)}
        assert cov["AI训练GPU"]["pending"] == ["厂商甲", "机构乙"]
        assert cov["图形渲染GPU"]["pending"] == ["机构丙"]

        # 定案落库（verified=false 也算核对完成）——对应项从 pending 消失
        record_knowledge(store, [{"name": "厂商甲", "node": "AI训练GPU",
                                  "verified": False, "note": "未找到官网"}],
                         tmp_path / "no_such_evidence.jsonl")
        record_knowledge(store, [{"name": "机构丙", "node": "图形渲染GPU",
                                  "verified": False, "note": "疑似无效机构"}],
                         tmp_path / "no_such_evidence.jsonl")
        cov = {c["node"]: c for c in coverage(store, NODES)}
        assert cov["AI训练GPU"]["pending"] == ["机构乙"]
        assert cov["图形渲染GPU"]["pending"] == []

    def test_pending_empty_without_manifest(self, tmp_path):
        """阶段 0-2 之前的行前清单核对没有 manifest——pending 给空表，不报错。"""
        store = self._seed(tmp_path, [source_entry()], [])
        assert coverage(store, NODES)[0]["pending"] == []

    def test_types_distribution_per_node(self, tmp_path):
        """docs/07 §五.3：薄弱判定的"体裁/来源维度单一"需要分布数据。下放后
        阶段 5 是独立代理、拿不到"刚提取的内容"，分布改由脚本从 store 算。"""
        store = self._seed(
            tmp_path,
            [source_entry(source_type="官网"),
             source_entry(url="https://a.com/2", name="A2", source_type="官网"),
             source_entry(url="https://a.com/3", name="A3", source_type="文献")],
            [],
        )
        cov = {c["node"]: c for c in coverage(store, NODES)}
        assert cov["AI训练GPU"]["types"] == {"官网": 2, "文献": 1}
        # 无活动节点给空分布——"没有"与"单一"要能分开
        assert cov["图形渲染GPU"]["types"] == {}


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

    def test_scheme_less_garbage_url_rejected(self, tmp_path):
        """无协议写法不得绕过垃圾域闸门（2026-09-18 实证：_domain 对无协议 URL 返回
        空串，is_garbage_domain('') 恒为假——115926/143431 两轮各有 50+ 条无协议
        URL 走的正是这条路径，闸门形同虚设）。"""
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["zhuanlan.zhihu.com/p/1", "https://a.com/doc"])
        result = record_sources(store, [
            source_entry(url="zhuanlan.zhihu.com/p/1", name="G"),
            source_entry(),
        ], evidence, NODES)
        assert result["accepted"] == 1
        assert "垃圾域" in result["rejected"][0]["reason"]

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
        """回归断言：docs.aws.amazon.com 是合法厂商文档门户，不得被电商词条误伤。

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


class TestFailureEvents:
    """失败事件流（2026-09-23）：拒收除了返回给模型，还落一行到 run_dir/rejects.jsonl。

    模型可见的返回体保持不变（键集合仍是 index/name/url/reason）；code 与 actor 只进
    事件流。事件数是**事件数**、不是被拒条目数——同一批重传会重复记录（重复本身是信号）。
    """

    def test_rejection_writes_event_and_keeps_payload_shape(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://shuma.taobao.com/item/1",
                                             "https://a.com/doc"])
        result = record_sources(store, [
            source_entry(url="https://shuma.taobao.com/item/1", name="G"),
            source_entry(),
        ], evidence, NODES)
        assert result["accepted"] == 1
        assert set(result["rejected"][0]) == {"index", "name", "url", "reason"}
        events = [json.loads(line) for line in
                  (tmp_path / "rejects.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [e["code"] for e in events] == ["GARBAGE_DOMAIN"]
        assert events[0]["actor"] == "model"
        assert events[0]["name"] == "G"
        assert "taobao" in events[0]["url"]

    def test_knowledge_rejection_event_carries_url(self, tmp_path):
        """record_knowledge 的拒收事件也要带上 url（2026-09-23 修）。

        缺陷现场：160530 轮的 42 条事件里 19 条 url 为空，其中 18 条恰是最需要看 URL 的
        `URL_NOT_IN_EVIDENCE`——闭包把 url 写死成空串，看不出是哪个 URL 被拒。
        """
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://k.com/doc"])
        result = record_knowledge(store, [
            {"name": "K1", "node": NODES[0], "verified": True,
             "url": "https://other.com/x", "note": "n"}], evidence)
        assert result["accepted"] == 0
        events = [json.loads(line) for line in
                  (tmp_path / "rejects.jsonl").read_text(encoding="utf-8").splitlines()]
        assert events[0]["code"] == "URL_NOT_IN_EVIDENCE"
        assert events[0]["url"] == "https://other.com/x"

    def test_no_rejection_no_event_file(self, tmp_path):
        """零拒收不建文件（append_records 空批不建）——免得每个运行目录都多一个空文件。"""
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://a.com/doc"])
        result = record_sources(store, [source_entry()], evidence, NODES)
        assert result["accepted"] == 1
        assert not (tmp_path / "rejects.jsonl").exists()


class TestRecordShapeContract:
    """契约见证（接口契约第 1 批）：三种 store 行的键集合 = STORE_FIELDS 声明。

    写方新增/删除字段而不更新声明时本测试当场红——形状变更必须是自觉动作。
    """

    def test_writers_emit_declared_fields(self, tmp_path):
        store = tmp_path / "store.jsonl"
        evidence = write_evidence(tmp_path, ["https://k.com/doc"])
        record_sources(store, [source_entry(url="https://k.com/doc")], evidence, NODES)
        record_search(store, [{"phase": "增量", "node": NODES[0], "query": "q",
                               "results": 1, "extracted": 0, "zero_reason": "已收"}])
        record_knowledge(store, [{
            "name": "K1", "node": NODES[0], "verified": True,
            "category_path": "算力服务器-GPU服务器-AI训练GPU",
            "source_type": "文档", "granularity": "合集级",
            "url": "https://k.com/doc", "description": "d", "reason": "r"}],
            evidence)
        records, _ = load_store(store)
        by_type: dict[str, set] = {}
        for r in records:
            by_type.setdefault(r["type"], set()).update(r.keys())
        assert by_type == {t: set(fields) for t, fields in STORE_FIELDS.items()}
