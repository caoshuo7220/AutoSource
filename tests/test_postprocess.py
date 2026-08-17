"""postprocess.py 与 log_tool.py 的单元测试。"""
import csv
import io
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 将 .claude/skills/autosource 加入 path 以便导入
SKILL_DIR = Path(__file__).parent.parent / ".claude" / "skills" / "autosource"
sys.path.insert(0, str(SKILL_DIR))

from postprocess import check_grounded, deduplicate, leaf_node, run, sanitize_domain

FIXED_NOW = datetime(2026, 8, 13, 18, 30, 45)
BOM = b"\xef\xbb\xbf"


def read_csv_rows(path: Path) -> list[list[str]]:
    """用 csv.reader 读回 CSV（StringIO 保证引号内换行被正确解析为同一字段）。"""
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))


def write_raw(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "raw.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def write_evidence(tmp_path: Path, sources: list[dict]) -> Path:
    """构造证据留痕：把候选 URL 放进模拟的 WebSearch 原始结果里（同 hook 记录格式）。"""
    p = tmp_path / "search_log.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for s in sources:
            if not isinstance(s, dict) or not s.get("url"):
                continue
            payload = {
                "tool_name": "WebSearch",
                "tool_input": {"query": "test"},
                "tool_response": {"results": [{"url": s["url"], "title": s.get("name", "")}]},
            }
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return p


def write_evidence_queries(tmp_path: Path, queries: list[str]) -> Path:
    """构造证据留痕：按实际查询词生成模拟 WebSearch 原始结果（供 journal 校验用）。"""
    p = tmp_path / "search_log.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for q in queries:
            payload = {
                "tool_name": "WebSearch",
                "tool_input": {"query": q},
                "tool_response": {"results": [{"url": f"https://example.com/{abs(hash(q))}"}]},
            }
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return p


def base_data() -> dict:
    return {
        "domain": "算力服务器",
        "nodes": ["AI训练GPU", "图形渲染GPU", "服务器CPU"],
        "model": "test-model",
        "sources": [
            {"name": "A", "category_path": "算力服务器-GPU服务器-AI训练GPU",
             "source_type": "官方文档", "url": "https://a.com/doc", "description": "d1"},
            {"name": "B", "category_path": "算力服务器-图形渲染GPU",
             "source_type": "数据集", "url": "https://b.com/data", "description": "d2"},
        ],
    }


class TestDeduplicate:
    def test_no_duplicates(self):
        sources = [
            {"name": "A", "url": "https://a.com/page1"},
            {"name": "B", "url": "https://b.com/page1"},
        ]
        assert len(deduplicate(sources)) == 2

    def test_removes_duplicate_by_name_and_domain(self):
        sources = [
            {"name": "Same", "url": "https://a.com/page1"},
            {"name": "Same", "url": "https://a.com/page2"},
        ]
        result = deduplicate(sources)
        assert len(result) == 1
        assert result[0]["url"] == "https://a.com/page1"

    def test_keeps_mirror_sites(self):
        sources = [
            {"name": "Same", "url": "https://a.com/data"},
            {"name": "Same", "url": "https://mirror.org/data"},
        ]
        assert len(deduplicate(sources)) == 2

    def test_different_name_same_domain_kept(self):
        sources = [
            {"name": "A", "url": "https://a.com/1"},
            {"name": "B", "url": "https://a.com/2"},
        ]
        assert len(deduplicate(sources)) == 2


class TestLeafNode:
    NODES = ["AI训练GPU", "A-B", "B"]

    def test_exact_match(self):
        assert leaf_node("AI训练GPU", self.NODES) == "AI训练GPU"

    def test_full_path_match(self):
        assert leaf_node("算力服务器-GPU服务器-AI训练GPU", self.NODES) == "AI训练GPU"

    def test_node_name_containing_dash_takes_longest_match(self):
        assert leaf_node("算力服务器-A-B", self.NODES) == "A-B"

    def test_no_match_returns_none(self):
        assert leaf_node("算力服务器-未知节点", self.NODES) is None


class TestSanitizeDomain:
    def test_plain_domain_unchanged(self):
        assert sanitize_domain("算力服务器") == "算力服务器"

    def test_path_hostile_chars_replaced(self):
        assert sanitize_domain("算 力/服:务*器") == "算_力_服_务_器"

    def test_empty_falls_back(self):
        assert sanitize_domain("  ") == "未命名领域"


class TestCheckGrounded:
    def test_url_in_evidence_kept(self):
        sources = [{"url": "https://a.com/doc"}]
        kept, ungrounded = check_grounded(sources, '{"results":[{"url":"https://a.com/doc"}]}')
        assert len(kept) == 1
        assert ungrounded == 0

    def test_url_not_in_evidence_rejected(self):
        sources = [{"url": "https://a.com/doc"}, {"url": "https://fake.com/x"}]
        kept, ungrounded = check_grounded(sources, '{"results":[{"url":"https://a.com/doc"}]}')
        assert len(kept) == 1
        assert ungrounded == 1

    def test_fabricated_url_rejected(self):
        # 证据里完全不存在的 URL（编造域名）必然被拒
        sources = [{"url": "https://fabricated.example/x"}]
        kept, ungrounded = check_grounded(
            sources, '{"results":[{"url":"https://x.com/support/faq/2817"}]}')
        assert kept == []
        assert ungrounded == 1


class TestRun:
    def test_full_pipeline(self, tmp_path):
        data = base_data()
        # 加一条重复 + 一条缺 url 的坏记录 + 一条缺 name 的坏记录
        data["sources"].append(dict(data["sources"][0]))
        data["sources"].append({"name": "X", "category_path": "算力服务器-服务器CPU"})
        data["sources"].append({"url": "https://y.com", "category_path": "算力服务器-服务器CPU"})
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])

        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        outdir = tmp_path / "out" / "算力服务器_2026-08-13-183045"
        assert summary["outdir"] == str(outdir)
        assert summary["total_found"] == 3
        assert summary["removed_duplicates"] == 1
        assert summary["ungrounded"] == 0
        assert summary["kept"] == 2
        assert summary["invalid"] == 2
        assert summary["empty_nodes"] == ["服务器CPU"]

        # raw.json 与证据留痕默认删除
        assert not raw.exists()
        assert not ev.exists()

        # 数据源清单 CSV：BOM + 表头 + 2 行
        source_csv = outdir / "算力服务器_2026-08-13-183045_数据源清单.csv"
        assert source_csv.exists()
        content = source_csv.read_bytes()
        assert content[:3] == BOM
        rows = read_csv_rows(source_csv)
        assert rows[0] == ["数据源名称", "分类路径", "数据源类型", "访问地址", "简要说明"]
        assert len(rows) == 3

        # stats CSV：BOM + 表头 + 每节点一行，空节点列填了无结果节点
        stats_csv = outdir / "算力服务器_2026-08-13-183045_stats.csv"
        assert stats_csv.exists()
        assert stats_csv.read_bytes()[:3] == BOM
        rows = read_csv_rows(stats_csv)
        assert len(rows) == 4  # header + 3 nodes
        by_node = {r[2]: r for r in rows[1:]}
        assert by_node["AI训练GPU"][3] == "1"
        assert by_node["AI训练GPU"][4] == "官方文档:1"
        assert by_node["图形渲染GPU"][3] == "1"
        assert by_node["服务器CPU"][3] == "0"
        assert by_node["服务器CPU"][5] == "服务器CPU"  # 无结果节点列
        assert by_node["AI训练GPU"][0] == "算力服务器"
        assert by_node["AI训练GPU"][1] == "2026-08-13 18:30:45"
        assert by_node["AI训练GPU"][8] == "0"  # 证据校验移除
        assert by_node["AI训练GPU"][9] == "0/0"  # 清单验证（无 knowledge）
        assert by_node["AI训练GPU"][11] == "test-model"
        assert float(by_node["AI训练GPU"][12]) >= 0  # 脚本处理耗时

    def test_keep_raw(self, tmp_path):
        raw = write_raw(tmp_path, base_data())
        ev = write_evidence(tmp_path, base_data()["sources"])
        run(str(raw), out_dir=str(tmp_path / "out"), keep_raw=True, now=FIXED_NOW,
            evidence_log=str(ev))
        assert raw.exists()
        assert ev.exists()

    def test_outdir_collision_gets_suffix(self, tmp_path):
        raw1 = write_raw(tmp_path, base_data())
        ev1 = write_evidence(tmp_path, base_data()["sources"])
        run(str(raw1), out_dir=str(tmp_path / "out"), now=FIXED_NOW, evidence_log=str(ev1))
        raw2 = write_raw(tmp_path, base_data())
        ev2 = write_evidence(tmp_path, base_data()["sources"])
        summary = run(str(raw2), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev2))
        assert summary["outdir"].endswith("算力服务器_2026-08-13-183045_1")

    def test_csv_escaping(self, tmp_path):
        data = base_data()
        data["sources"][0]["name"] = '名称,含"逗号"和引号'
        data["sources"][0]["description"] = "多行\n描述"
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        outdir = Path(summary["outdir"])
        source_csv = next(outdir.glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert rows[1][0] == '名称,含"逗号"和引号'
        assert rows[1][4] == "多行\n描述"

    def test_missing_nodes_field_raises(self, tmp_path):
        data = base_data()
        del data["nodes"]
        raw = write_raw(tmp_path, data)
        try:
            run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW)
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "nodes" in str(e)

    def test_unmatched_source_counted_but_kept_in_csv(self, tmp_path):
        data = base_data()
        data["sources"].append({"name": "U", "category_path": "算力服务器-不存在的节点",
                                "source_type": "技术博客", "url": "https://u.com",
                                "description": "d"})
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["unmatched"] == 1
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert len(rows) == 4  # header + 3 条（未匹配的仍收录）

    def test_all_nodes_empty(self, tmp_path):
        data = base_data()
        data["sources"] = []
        raw = write_raw(tmp_path, data)
        # 无候选时不要求证据留痕
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(tmp_path / "不存在.jsonl"))

        assert summary["kept"] == 0
        assert set(summary["empty_nodes"]) == set(data["nodes"])
        stats_csv = next((Path(summary["outdir"])).glob("*stats.csv"))
        rows = read_csv_rows(stats_csv)
        assert len(rows) == 4  # header + 3 个空节点行

    def test_ungrounded_url_rejected_and_counted(self, tmp_path):
        data = base_data()
        real_sources = [dict(s) for s in data["sources"]]
        data["sources"].append({"name": "Fake", "category_path": "算力服务器-服务器CPU",
                                "source_type": "官方文档", "url": "https://fabricated.example/x",
                                "description": "编造的 URL"})
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, real_sources)  # 证据只含真实 URL，不含编造的
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["total_found"] == 3
        assert summary["ungrounded"] == 1
        assert summary["kept"] == 2
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert len(rows) == 3  # header + 2 条（编造的已被拒绝）
        assert all("fabricated" not in r[3] for r in rows)

    def test_missing_evidence_log_raises(self, tmp_path):
        raw = write_raw(tmp_path, base_data())
        try:
            run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                evidence_log=str(tmp_path / "不存在.jsonl"))
            assert False, "should have raised FileNotFoundError"
        except FileNotFoundError as e:
            assert "证据留痕" in str(e)


class TestKnowledge:
    def _knowledge_item(self, verified=True, **overrides):
        item = {
            "name": "IEEE 802.3 工作组",
            "node": "以太网标准(IEEE 802.3)",
            "verified": verified,
            "category_path": "交换机-核心技术-以太网标准(IEEE 802.3)",
            "source_type": "行业标准",
            "url": "https://www.ieee802.org/3/",
            "description": "IEEE 以太网标准工作组官网",
            "reason": "清单验证通过，官方入口",
        }
        item.update(overrides)
        return item

    def test_verified_items_merged_into_sources(self, tmp_path):
        data = base_data()
        kn = self._knowledge_item()
        data["knowledge"] = [kn, {"name": "难搜机构", "node": "服务器CPU", "verified": False,
                                  "note": "已尽力"}]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"] + [kn])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["list_verified"] == "1/2"
        assert summary["kept"] == 3  # 2 增量 + 1 清单并入
        assert summary["unverified"] == [("难搜机构", "已尽力")]
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        urls = [r[3] for r in rows[1:]]
        assert "https://www.ieee802.org/3/" in urls
        # D1 否定断言：未验证项绝不进 CSV
        assert all(r[0] != "难搜机构" for r in rows)
        # stats CSV 的清单验证单元格
        stats_csv = next((Path(summary["outdir"])).glob("*stats.csv"))
        stats_rows = read_csv_rows(stats_csv)
        assert all(r[9] == "1/2" for r in stats_rows[1:])

    def test_verified_missing_category_path_falls_back_to_node(self, tmp_path):
        data = base_data()
        kn = self._knowledge_item()
        del kn["category_path"]
        data["knowledge"] = [kn]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"] + [kn])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert any(r[0] == "IEEE 802.3 工作组" and r[1] == "以太网标准(IEEE 802.3)"
                   for r in rows)

    def test_verified_missing_url_not_merged(self, tmp_path):
        data = base_data()
        data["knowledge"] = [self._knowledge_item(url="")]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["list_verified"] == "0/1"
        assert summary["incomplete"] == 1
        assert summary["kept"] == 2

    def test_verified_missing_name_counts_incomplete_only(self, tmp_path):
        data = base_data()
        data["knowledge"] = [self._knowledge_item(name="")]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["incomplete"] == 1
        assert summary["unverified"] == []  # 缺 name 的 verified 项不进未验证交接单

    def test_knowledge_missing_tolerated(self, tmp_path):
        data = base_data()
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["knowledge_missing"] is True
        assert summary["list_verified"] == "0/0"
        assert summary["kept"] == 2

    def test_knowledge_wrong_type_tolerated(self, tmp_path):
        data = base_data()
        data["knowledge"] = {"name": "不是列表"}
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["list_verified"] == "0/0"
        assert summary["kept"] == 2

    def test_knowledge_empty_list_tolerated(self, tmp_path):
        data = base_data()
        data["knowledge"] = []
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["knowledge_missing"] is True
        assert summary["list_verified"] == "0/0"

    def test_knowledge_non_dict_entries_not_in_denominator(self, tmp_path):
        data = base_data()
        data["knowledge"] = [self._knowledge_item(), "垃圾条目"]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"] + [data["knowledge"][0]])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["list_verified"] == "1/1"  # 非 dict 不计入分母

    def test_merged_url_must_be_grounded(self, tmp_path):
        data = base_data()
        kn = self._knowledge_item(url="https://fabricated.example/portal")
        data["knowledge"] = [kn]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])  # 证据里没有清单项 URL
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["ungrounded"] == 1
        assert summary["kept"] == 2  # 清单项被证据校验拒绝
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert all("fabricated" not in r[3] for r in rows)


class TestJournal:
    def _journal(self, queries):
        return [{"phase": "验证搜索", "node": "以太网标准(IEEE 802.3)", "query": q,
                 "results": 10, "extracted": 3} for q in queries]

    def test_journal_csv_generated(self, tmp_path):
        data = base_data()
        queries = ["IEEE 802.3 official", "交换机 标准 列表"]
        data["journal"] = self._journal(queries)
        raw = write_raw(tmp_path, data)
        ev = write_evidence_queries(tmp_path, queries + ["test"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["journal_count"] == 2
        journal_csv = next((Path(summary["outdir"])).glob("*搜索日志.csv"))
        assert journal_csv.read_bytes()[:3] == BOM
        rows = read_csv_rows(journal_csv)
        assert rows[0] == ["阶段", "节点", "查询词", "返回链接数", "提取候选数", "证据缺失"]
        assert len(rows) == 3
        assert rows[1][2] == "IEEE 802.3 official"
        assert rows[1][5] == ""  # 证据缺失为空

    def test_journal_query_missing_flagged(self, tmp_path):
        data = base_data()
        data["journal"] = self._journal(["不存在的查询词"])
        raw = write_raw(tmp_path, data)
        ev = write_evidence_queries(tmp_path, ["另一个查询"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        journal_csv = next((Path(summary["outdir"])).glob("*搜索日志.csv"))
        rows = read_csv_rows(journal_csv)
        assert rows[1][5] == "是"

    def test_no_journal_no_csv(self, tmp_path):
        data = base_data()
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["journal_count"] == 0
        assert not list((Path(summary["outdir"])).glob("*搜索日志.csv"))

    def test_journal_non_dict_entries_skipped_and_counted(self, tmp_path):
        data = base_data()
        data["journal"] = self._journal(["IEEE 802.3 official"]) + ["垃圾条目"]
        raw = write_raw(tmp_path, data)
        ev = write_evidence_queries(tmp_path, ["IEEE 802.3 official", "test"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["journal_count"] == 1
        assert summary["journal_skipped"] == 1
        journal_csv = next((Path(summary["outdir"])).glob("*搜索日志.csv"))
        assert len(read_csv_rows(journal_csv)) == 2  # header + 1 行

    def test_journal_only_missing_evidence_raises(self, tmp_path):
        data = base_data()
        data["sources"] = []
        data["journal"] = self._journal(["某查询"])
        raw = write_raw(tmp_path, data)
        try:
            run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                evidence_log=str(tmp_path / "不存在.jsonl"))
            assert False, "should have raised FileNotFoundError"
        except FileNotFoundError as e:
            assert "证据留痕" in str(e)

    def test_journal_with_zero_candidates_aborts(self, tmp_path):
        # H1 失败路径：搜索过（journal 非空）却 0 候选 → 中止，raw.json 保留
        data = base_data()
        data["sources"] = []
        data["journal"] = self._journal(["IEEE 802.3 official"])
        raw = write_raw(tmp_path, data)
        ev = write_evidence_queries(tmp_path, ["IEEE 802.3 official"])
        try:
            run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                evidence_log=str(ev))
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "0" in str(e)
        assert raw.exists()  # 中止时 raw.json 保留
        assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


class TestRunResilience:
    def test_sources_wrong_type_tolerated(self, tmp_path):
        data = base_data()
        data["sources"] = {"不是": "列表"}
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, base_data()["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["sources_broken"] is True
        assert summary["kept"] == 0
        assert summary["empty_nodes"] == data["nodes"]


class TestLogTool:
    def test_appends_hook_payload(self, tmp_path):
        log = tmp_path / "log.jsonl"
        payload = json.dumps(
            {"tool_name": "WebSearch", "tool_input": {"query": "测试"},
             "tool_response": {"results": [{"url": "https://a.com", "title": "结果"}]}},
            ensure_ascii=False)
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "log_tool.py"), str(log)],
            input=payload, capture_output=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["tool_name"] == "WebSearch"
        assert json.loads(lines[0])["tool_response"]["results"][0]["url"] == "https://a.com"

    def test_malformed_input_silently_passes(self, tmp_path):
        log = tmp_path / "log.jsonl"
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "log_tool.py"), str(log)],
            input="not json", capture_output=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0
        assert not log.exists()
