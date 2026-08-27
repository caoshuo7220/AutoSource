"""postprocess.py 与 log_tool.py 的单元测试。"""
import csv
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

# 将 .claude/skills/autosource/scripts 加入 path 以便导入
SKILL_DIR = Path(__file__).parent.parent / ".claude" / "skills" / "autosource" / "scripts"
sys.path.insert(0, str(SKILL_DIR))

from postprocess import (check_grounded, check_granularity, count_multilang_groups,
                         deduplicate, default_evidence_log, finalize_report,
                         leaf_node, prepare_run_dir, query_in_evidence, run,
                         sanitize_domain, slice_evidence, strip_citation_anchors)

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


def write_evidence_queries(tmp_path: Path, queries: list[str],
                           sources: list[dict] | None = None) -> Path:
    """构造证据留痕：按实际查询词生成模拟 WebSearch 原始结果（供 journal 校验用）。

    sources 可选：把候选 URL 一并写入留痕，保证本轮是干净运行
    （否则候选会被证据校验拒绝，方案 C 下将不生成输出目录）。
    """
    p = tmp_path / "search_log.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for q in queries:
            payload = {
                "tool_name": "WebSearch",
                "tool_input": {"query": q},
                "tool_response": {"results": [{"url": f"https://example.com/{abs(hash(q))}"}]},
            }
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        for s in sources or []:
            if not isinstance(s, dict) or not s.get("url"):
                continue
            payload = {
                "tool_name": "WebSearch",
                "tool_input": {"query": "test"},
                "tool_response": {"results": [{"url": s["url"], "title": s.get("name", "")}]},
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
        kept, rejected = check_grounded(sources, '{"results":[{"url":"https://a.com/doc"}]}')
        assert len(kept) == 1
        assert len(rejected) == 0

    def test_url_not_in_evidence_rejected(self):
        sources = [{"url": "https://a.com/doc"}, {"url": "https://fake.com/x"}]
        kept, rejected = check_grounded(sources, '{"results":[{"url":"https://a.com/doc"}]}')
        assert len(kept) == 1
        assert len(rejected) == 1

    def test_fabricated_url_rejected(self):
        # 证据里完全不存在的 URL（编造域名）必然被拒
        sources = [{"url": "https://fabricated.example/x"}]
        kept, rejected = check_grounded(
            sources, '{"results":[{"url":"https://x.com/support/faq/2817"}]}')
        assert kept == []
        assert len(rejected) == 1


class TestCheckGroundedBoundaries:
    """边界匹配:候选 URL 必须是留痕中的完整 URL,截短为父路径/裸域名不放行。"""

    def test_exact_url_in_json_evidence_kept(self):
        evidence = '{"results":[{"url":"https://a.com/doc"}]}'
        kept, rejected = check_grounded([{"url": "https://a.com/doc"}], evidence)
        assert len(kept) == 1
        assert rejected == []

    def test_path_prefix_truncation_rejected(self):
        # 留痕是深链,候选截短为父路径 → 拒绝(旧子串匹配会放行)
        evidence = '{"results":[{"url":"https://a.com/doc/123"}]}'
        kept, rejected = check_grounded([{"url": "https://a.com/doc"}], evidence)
        assert kept == []
        assert len(rejected) == 1

    def test_bare_domain_truncation_rejected(self):
        evidence = '{"results":[{"url":"https://a.com/news/123"}]}'
        kept, rejected = check_grounded([{"url": "https://a.com"}], evidence)
        assert kept == []
        assert len(rejected) == 1

    def test_extension_prefix_truncation_rejected(self):
        # 截短到扩展名前(doc vs doc.html)同样拒绝
        evidence = '{"results":[{"url":"https://a.com/manual.pdf"}]}'
        kept, rejected = check_grounded([{"url": "https://a.com/manual"}], evidence)
        assert kept == []
        assert len(rejected) == 1

    def test_full_url_in_free_text_kept(self):
        evidence = "see https://a.com/doc for details"
        kept, rejected = check_grounded([{"url": "https://a.com/doc"}], evidence)
        assert len(kept) == 1
        assert rejected == []

    def test_numeric_fragment_truncation_kept(self):
        # 截短到纯数字引用锚点之前不再拒绝（2026-08-27 交换机 274 轮 10 条实证）
        evidence = '{"results":[{"url":"https://a.com/doc.pdf#2#1"}]}'
        kept, rejected = check_grounded([{"url": "https://a.com/doc.pdf"}], evidence)
        assert len(kept) == 1
        assert rejected == []

    def test_word_fragment_truncation_kept(self):
        # 截短到文字片段之前（#top）不再拒绝——片段不改变资源主体
        evidence = '{"results":[{"url":"https://a.com/page#top"}]}'
        kept, rejected = check_grounded([{"url": "https://a.com/page"}], evidence)
        assert len(kept) == 1
        assert rejected == []

    def test_query_truncation_rejected(self):
        # 去 query 参数仍拒绝（?page=2 会改变内容）
        evidence = '{"results":[{"url":"https://a.com/list?page=2"}]}'
        kept, rejected = check_grounded([{"url": "https://a.com/list"}], evidence)
        assert kept == []
        assert len(rejected) == 1


class TestStripCitationAnchors:
    def test_double_digit_anchors_stripped(self):
        assert strip_citation_anchors("https://a.com/doc.pdf#3#1") == "https://a.com/doc.pdf"

    def test_single_anchor_stripped(self):
        assert strip_citation_anchors("https://a.com/list#1") == "https://a.com/list"

    def test_word_anchor_kept(self):
        assert strip_citation_anchors("https://a.com/page#content") == "https://a.com/page#content"

    def test_no_fragment_unchanged(self):
        assert strip_citation_anchors("https://a.com/doc") == "https://a.com/doc"

    def test_query_then_anchor(self):
        assert strip_citation_anchors("https://a.com/s?x=1#2") == "https://a.com/s?x=1"

class TestCheckGranularity:
    """粒度声明归一化与计数（不拒绝——规则二已按用户决策移除）。"""

    def test_single_level_counted(self):
        sources = [{"name": "标准文件", "granularity": "单篇级",
                    "url": "https://x.com/std/TTAF-290.pdf"}]
        single, missing = check_granularity(sources)
        assert single == 1
        assert missing == 0

    def test_document_url_no_longer_rejected(self):
        # 规则二已移除：文档形态 URL 不再被拒绝
        sources = [{"name": "X 文档中心", "granularity": "合集级",
                    "url": "https://x.com/manual.pdf"}]
        single, missing = check_granularity(sources)
        assert single == 0
        assert missing == 0

    def test_missing_granularity_defaults_to_collection(self):
        sources = [{"name": "X 文档中心", "url": "https://x.com/manual.pdf"}]
        single, missing = check_granularity(sources)
        assert single == 0
        assert missing == 1
        assert sources[0]["granularity"] == "合集级"

    def test_collection_portal_url_kept(self):
        sources = [{"name": "X 文档中心", "granularity": "合集级",
                    "url": "https://x.com/documentation"}]
        single, missing = check_granularity(sources)
        assert single == 0
        assert missing == 0
        assert sources[0]["granularity"] == "合集级"

    def test_site_level_normalized_to_collection(self):
        # 2026-08-21：站点级并入合集级——存量"站点级"按非法值归一化（计入缺声明）
        sources = [{"name": "垂直门户", "granularity": "站点级",
                    "url": "https://portal.com/"}]
        single, missing = check_granularity(sources)
        assert single == 0
        assert missing == 1
        assert sources[0]["granularity"] == "合集级"


class TestMultilangAudit:
    """多语言版本审计：同域名剥语言码路径段后相同的 URL 组计数（不自动合并）。"""

    def _source(self, url, name="X"):
        return {"name": name, "category_path": "算力服务器-服务器CPU",
                "source_type": "官方文档", "granularity": "单篇级",
                "url": url, "description": "d", "reason": "r"}

    def test_locale_variants_grouped(self):
        sources = [
            self._source("https://support.apple.com/zh-cn/122240", "规格页中文"),
            self._source("https://support.apple.com/en-nz/122240", "规格页英文"),
            self._source("https://support.apple.com/ru-ru/122240", "规格页俄文"),
            self._source("https://other.com/guide", "无关来源"),
        ]
        groups, excess = count_multilang_groups(sources)
        assert groups == 1
        assert excess == 2

    def test_non_locale_paths_not_grouped(self):
        sources = [
            self._source("https://a.com/api/v1", "API"),
            self._source("https://a.com/docs/start", "Start"),
        ]
        groups, excess = count_multilang_groups(sources)
        assert groups == 0
        assert excess == 0

    def test_different_netloc_not_grouped(self):
        # zh.wikipedia 与 en.wikipedia 是不同内容的站点，不按语言版本合并
        sources = [
            self._source("https://zh.wikipedia.org/wiki/IPad"),
            self._source("https://en.wikipedia.org/wiki/IPad"),
        ]
        groups, excess = count_multilang_groups(sources)
        assert groups == 0
        assert excess == 0

    def test_stats_column_and_stdout(self, tmp_path):
        data = base_data()
        data["sources"] = [
            self._source("https://support.apple.com/zh-cn/122240", "规格页中文"),
            self._source("https://support.apple.com/en-nz/122240", "规格页英文"),
            self._source("https://support.apple.com/ru-ru/122240", "规格页俄文"),
        ]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        import io
        from contextlib import redirect_stdout
        from postprocess import _print_summary
        buf = io.StringIO()
        with redirect_stdout(buf):
            summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                          evidence_log=str(ev))
            _print_summary(summary)
        assert summary["multilang_groups"] == 1
        assert summary["multilang_excess"] == 2
        assert "多语言版本并存" in buf.getvalue()  # stdout 提示保留，stats 列已删


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
        assert rows[0] == ["数据源名称", "分类路径", "数据源类型", "粒度", "访问地址",
                           "简要说明", "来源搜索"]
        assert len(rows) == 3

        # stats CSV：BOM + 表头 + 每节点一行 + 末尾总计行（纯清单统计，无运行级重复列）
        stats_csv = outdir / "算力服务器_2026-08-13-183045_stats.csv"
        assert stats_csv.exists()
        assert stats_csv.read_bytes()[:3] == BOM
        rows = read_csv_rows(stats_csv)
        assert len(rows) == 5  # header + 3 nodes + 总计
        by_node = {r[0]: r for r in rows[1:] if r[0] != "总计"}
        assert by_node["AI训练GPU"][1] == "1"
        assert by_node["AI训练GPU"][2] == "官方文档:1"
        assert by_node["图形渲染GPU"][1] == "1"
        assert by_node["服务器CPU"][1] == "0"
        assert by_node["服务器CPU"][2] == ""
        assert rows[-1][0] == "总计"
        assert rows[-1][1] == "2"  # 最终收录 = 候选数求和
        assert rows[-1][2] == "官方文档:1; 数据集:1"
        # stats 列集合钉死：纯清单统计表（领域/时间戳在文件名、模型在 manifest、健康指标在 stdout）
        assert rows[0] == ["分类节点", "候选数", "体裁分布"]

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
        assert rows[1][5] == "多行\n描述"

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
        assert len(rows) == 5  # header + 3 个空节点行 + 总计行

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
        # 被拒条目不进清单、不单独成文件；明细在 stdout、计数在 stats；
        # 运行到此结束，raw/留痕正常清理
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert len(rows) == 3  # header + 2 条（编造的被拒）
        assert all("fabricated" not in r[4] for r in rows)
        assert not list(Path(summary["outdir"]).glob("*被拒记录*"))
        assert not raw.exists()
        assert not ev.exists()

    def test_document_url_no_longer_rejected_in_run(self, tmp_path):
        # 规则二已移除：PDF 形态 URL 照常收录
        data = base_data()
        data["sources"].append({"name": "M 手册体系", "granularity": "合集级",
                                "category_path": "算力服务器-服务器CPU",
                                "source_type": "技术手册", "url": "https://m.com/manual.pdf",
                                "description": "单份 PDF", "reason": "x"})
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["kept"] == 3
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert any("manual.pdf" in r[4] for r in rows)

    def test_single_level_kept_counted_and_in_csv(self, tmp_path):
        data = base_data()
        data["sources"].append({"name": "团体标准", "granularity": "单篇级",
                                "category_path": "算力服务器-服务器CPU",
                                "source_type": "行业标准", "url": "https://s.com/std.pdf",
                                "description": "团体标准文件",
                                "reason": "团体标准文件，无合集可替代"})
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["kept"] == 3
        assert summary["single_count"] == 1
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert any(r[0] == "团体标准" and r[3] == "单篇级" for r in rows)

    def test_knowledge_item_with_document_url_kept(self, tmp_path):
        # 规则二已移除：清单项验证到单份 PDF 也照常收录
        data = base_data()
        kn = {"name": "M 文档中心", "node": "服务器CPU", "verified": True,
              "category_path": "算力服务器-服务器CPU",
              "source_type": "官方文档", "url": "https://m.com/manual.pdf",
              "description": "单份 PDF", "reason": "清单验证通过"}
        data["knowledge"] = [kn]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"] + [kn])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["kept"] == 3

    def test_knowledge_item_explicit_single_level_kept(self, tmp_path):
        data = base_data()
        kn = {"name": "国标文件", "node": "服务器CPU", "verified": True,
              "granularity": "单篇级",
              "category_path": "算力服务器-服务器CPU",
              "source_type": "行业标准", "url": "https://m.com/gb.pdf",
              "description": "标准全文", "reason": "标准文件，无合集可替代"}
        data["knowledge"] = [kn]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"] + [kn])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["kept"] == 3
        assert summary["single_count"] == 1

    def test_citation_anchor_stripped_in_csv(self, tmp_path):
        # 证据链仍严格逐字：带引用锚点的 URL 照抄通过校验；
        # 输出 CSV 时尾部 #数字 锚点被脚本确定性剥离
        data = base_data()
        data["sources"].append({"name": "C", "category_path": "算力服务器-服务器CPU",
                                "source_type": "知识库", "granularity": "合集级",
                                "url": "https://c.com/list#1",
                                "description": "d", "reason": "r"})
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["ungrounded"] == 0
        assert summary["kept"] == 3
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert any(r[0] == "C" and r[4] == "https://c.com/list" for r in rows)

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
                                  "note": "未找到官方入口"}]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"] + [kn])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["list_verified"] == "1/2"
        assert summary["kept"] == 3  # 2 增量 + 1 清单并入
        assert summary["unverified"] == [("难搜机构", "未找到官方入口")]
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        urls = [r[4] for r in rows[1:]]
        assert "https://www.ieee802.org/3/" in urls
        # D1 否定断言：未验证项绝不进 CSV
        assert all(r[0] != "难搜机构" for r in rows)

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

    def test_knowledge_extra_contract_fields_tolerated(self, tmp_path):
        """契约外字段（如 expected_type）不阻断并入——搜索层直接落盘 raw.json 后，
        条目上可能残留内部字段；脚本只取契约字段，多余字段忽略。"""
        data = base_data()
        kn = self._knowledge_item()
        kn["expected_type"] = "官方文档"
        data["knowledge"] = [kn]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"] + [kn])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["list_verified"] == "1/1"
        assert summary["kept"] == 3
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        merged_row = next(r for r in rows[1:] if r[0] == "IEEE 802.3 工作组")
        assert merged_row[2] == "行业标准"

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
        assert summary["kept"] == 2  # 清单项被证据校验拒绝，不进清单
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert all("fabricated" not in r[4] for r in rows)
        assert not list(Path(summary["outdir"]).glob("*被拒记录*"))
        assert not raw.exists()  # 无修正重跑环节，临时文件正常清理


class TestQueryEvidence:
    """journal 查询词精确比对:截短/改写一律视为证据缺失(旧子串匹配会放行)。"""

    def test_exact_query_in_payload_passes(self):
        evidence = '{"tool_name":"WebSearch","tool_input":{"query":"IEEE 802.3 official"}}'
        assert query_in_evidence("IEEE 802.3 official", evidence) is True

    def test_truncated_query_not_in_evidence(self):
        evidence = '{"tool_name":"WebSearch","tool_input":{"query":"IEEE 802.3 official"}}'
        assert query_in_evidence("IEEE", evidence) is False

    def test_rewritten_query_not_in_evidence(self):
        evidence = '{"tool_name":"WebSearch","tool_input":{"query":"交换机 标准 列表"}}'
        assert query_in_evidence("交换机 标准", evidence) is False

    def test_malformed_lines_skipped(self):
        assert query_in_evidence("IEEE", "not json\n") is False


class TestJournal:
    def _journal(self, queries):
        return [{"phase": "验证搜索", "node": "以太网标准(IEEE 802.3)", "query": q,
                 "results": 10, "extracted": 3} for q in queries]

    def test_journal_csv_generated(self, tmp_path):
        data = base_data()
        queries = ["IEEE 802.3 official", "交换机 标准 列表"]
        data["journal"] = self._journal(queries)
        raw = write_raw(tmp_path, data)
        ev = write_evidence_queries(tmp_path, queries + ["test"], data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["journal_count"] == 2
        journal_csv = next((Path(summary["outdir"]) / "intermediate").glob("*搜索日志.csv"))
        assert journal_csv.read_bytes()[:3] == BOM
        rows = read_csv_rows(journal_csv)
        assert rows[0] == ["阶段", "节点", "查询词", "返回链接数", "提取候选数", "证据缺失"]
        assert len(rows) == 3
        assert rows[1][2] == "IEEE 802.3 official"
        assert rows[1][5] == "否"  # 证据缺失列：未缺失显式填否

    def test_journal_query_missing_flagged(self, tmp_path):
        data = base_data()
        data["journal"] = self._journal(["不存在的查询词"])
        raw = write_raw(tmp_path, data)
        ev = write_evidence_queries(tmp_path, ["另一个查询"], data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        journal_csv = next((Path(summary["outdir"]) / "intermediate").glob("*搜索日志.csv"))
        rows = read_csv_rows(journal_csv)
        assert rows[1][5] == "是"

    def test_no_journal_no_csv(self, tmp_path):
        data = base_data()
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["journal_count"] == 0
        assert not list((Path(summary["outdir"]) / "intermediate").glob("*搜索日志.csv"))

    def test_journal_non_dict_entries_skipped_and_counted(self, tmp_path):
        data = base_data()
        data["journal"] = self._journal(["IEEE 802.3 official"]) + ["垃圾条目"]
        raw = write_raw(tmp_path, data)
        ev = write_evidence_queries(tmp_path, ["IEEE 802.3 official", "test"],
                                    data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["journal_count"] == 1
        assert summary["journal_skipped"] == 1
        journal_csv = next((Path(summary["outdir"]) / "intermediate").glob("*搜索日志.csv"))
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

    def test_journal_truncated_query_flagged(self, tmp_path):
        # 实际只搜过完整查询词，journal 里截短的查询词必须标"证据缺失"
        data = base_data()
        data["journal"] = self._journal(["IEEE"])
        raw = write_raw(tmp_path, data)
        ev = write_evidence_queries(tmp_path, ["IEEE 802.3 official"], data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        journal_csv = next((Path(summary["outdir"]) / "intermediate").glob("*搜索日志.csv"))
        rows = read_csv_rows(journal_csv)
        assert rows[1][5] == "是"


class TestSliceEvidence:
    """证据留痕切片：只保留本运行查询词命中的行（会话级 → 运行级）。"""

    def _line(self, query: str, tool_input_query: bool = True):
        payload = {"tool_name": "WebSearch", "tool_response": {"query": query, "results": []}}
        if tool_input_query:
            payload["tool_input"] = {"query": query}
        return json.dumps(payload, ensure_ascii=False)

    def test_keeps_matching_query_lines(self):
        evidence = "\n".join([self._line("q1"), self._line("q2")]) + "\n"
        sliced, kept, skipped = slice_evidence(evidence, {"q1"})
        assert kept == 1
        assert skipped == 0
        assert '"q1"' in sliced and '"q2"' not in sliced

    def test_drops_unrelated_lines(self):
        evidence = self._line("其他运行查询") + "\n"
        sliced, kept, skipped = slice_evidence(evidence, {"q1"})
        assert kept == 0
        assert sliced == ""

    def test_malformed_lines_skipped_counted(self):
        evidence = "not json\n" + self._line("q1") + "\n"
        sliced, kept, skipped = slice_evidence(evidence, {"q1"})
        assert kept == 1
        assert skipped == 1

    def test_tool_response_query_fallback(self):
        evidence = self._line("q1", tool_input_query=False) + "\n"
        sliced, kept, skipped = slice_evidence(evidence, {"q1"})
        assert kept == 1


class TestArchives:
    """run bundle 归档：交付物在根、intermediate/ 子目录、manifest 校验和。"""

    def _evidence_mixed(self, tmp_path, run_queries, sources):
        """本运行查询行 + 无关查询行 + 损坏行 + 候选 URL 行（test 查询）。"""
        lines = []
        for q in run_queries:
            lines.append(json.dumps(
                {"tool_name": "WebSearch", "tool_input": {"query": q},
                 "tool_response": {"query": q, "results": []}}, ensure_ascii=False))
        lines.append(json.dumps(
            {"tool_name": "WebSearch", "tool_input": {"query": "无关查询"},
             "tool_response": {"results": []}}, ensure_ascii=False))
        lines.append("not json")
        for s in sources:
            lines.append(json.dumps(
                {"tool_name": "WebSearch", "tool_input": {"query": "test"},
                 "tool_response": {"results": [{"url": s["url"], "title": s.get("name", "")}]}},
                ensure_ascii=False))
        p = tmp_path / "search_log.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_success_run_archives_structured_bundle(self, tmp_path):
        data = base_data()
        queries = ["IEEE 802.3 official", "交换机 标准 列表"]
        data["journal"] = [
            {"phase": "验证搜索", "node": "服务器CPU", "query": q, "results": 5, "extracted": 1}
            for q in queries]
        raw = write_raw(tmp_path, data)
        raw_text = raw.read_text(encoding="utf-8")
        ev = self._evidence_mixed(tmp_path, queries, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        outdir = Path(summary["outdir"])
        intermediate = outdir / "intermediate"
        # 输入快照内容等于 raw.json（会话临时文件已被清理，先存原文再比对）
        assert (intermediate / "raw_input.json").read_text(encoding="utf-8") == raw_text
        # 切片：只保留本运行查询行（无关行/损坏行/候选行全部排除）
        sliced = (intermediate / "evidence_log.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(sliced) == 2
        assert all(not l.startswith("not json") and "无关查询" not in l for l in sliced)
        assert summary["evidence_slice_kept"] == 2
        assert summary["evidence_slice_skipped"] == 1
        # 无 manifest（已按用户决策删除）；搜索日志在 intermediate/（本测试结果数组为空，无溯源行）
        assert not (outdir / "manifest.json").exists()
        assert (intermediate / "算力服务器_2026-08-13-183045_搜索日志.csv").exists()
        assert not (intermediate / "算力服务器_2026-08-13-183045_溯源.csv").exists()
        # 脚本在根目录只写两个交付物（数据源清单 + stats；分析报告.md 由模型收尾时写入）
        root_files = {p.name for p in outdir.iterdir() if p.is_file()}
        assert root_files == {"算力服务器_2026-08-13-183045_数据源清单.csv",
                              "算力服务器_2026-08-13-183045_stats.csv"}
        # 会话临时文件仍按现状删除
        assert not raw.exists()
        assert not ev.exists()

    def test_no_journal_no_evidence_archive(self, tmp_path):
        data = base_data()
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        outdir = Path(summary["outdir"])
        assert not (outdir / "intermediate" / "evidence_log.jsonl").exists()
        assert not list((outdir / "intermediate").glob("*搜索日志.csv"))

    def test_rejected_entries_not_in_bundle(self, tmp_path):
        data = base_data()
        real_sources = [dict(s) for s in data["sources"]]
        data["sources"].append({"name": "Fake", "category_path": "算力服务器-服务器CPU",
                                "source_type": "官方文档", "url": "https://fabricated.example/x",
                                "description": "编造的 URL"})
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, real_sources)
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        # 被拒仍产出完整 bundle（不落被拒文件）；raw/留痕正常清理
        assert summary["ungrounded"] == 1
        outdir = Path(summary["outdir"])
        assert not list(outdir.glob("*被拒记录*"))
        assert (outdir / "intermediate" / "raw_input.json").exists()
        assert not raw.exists()
        assert not ev.exists()


class TestLineage:
    """数据血缘：搜索结果与收录的对应（正查/反查）+ 清单"来源搜索"列。"""

    def _evidence_results(self, tmp_path, queries_results: dict[str, list[str]]):
        p = tmp_path / "search_log.jsonl"
        with p.open("w", encoding="utf-8") as f:
            for q, urls in queries_results.items():
                payload = {
                    "tool_name": "WebSearch",
                    "tool_input": {"query": q},
                    "tool_response": {"query": q, "results": [
                        {"content": [{"title": f"t{i}", "url": u}]}
                        for i, u in enumerate(urls)]},
                }
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return p

    def _journal(self, queries, phase="增量发现", node="服务器CPU", results=10):
        return [{"phase": phase, "node": node, "query": q,
                 "results": results, "extracted": 1} for q in queries]

    def test_lineage_rows_match_collection(self, tmp_path):
        data = base_data()
        # 三个结果里前两个被收录（base_data 的 A/B），第三个未收录
        data["journal"] = self._journal(["q1"])
        raw = write_raw(tmp_path, data)
        ev = self._evidence_results(tmp_path, {"q1": [
            "https://a.com/doc", "https://b.com/data", "https://c.com/other"]})
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["lineage_rows"] == 3
        lineage_csv = next((Path(summary["outdir"]) / "intermediate").glob("*溯源.csv"))
        rows = read_csv_rows(lineage_csv)
        assert rows[0] == ["阶段", "分类节点", "查询词", "返回结果数", "结果URL",
                           "是否收录", "收录条目名称", "收录理由", "备注"]
        by_url = {r[4]: r for r in rows[1:]}
        assert by_url["https://a.com/doc"][5] == "是"
        assert by_url["https://a.com/doc"][6] == "A"
        assert by_url["https://a.com/doc"][0] == "增量发现"
        assert by_url["https://a.com/doc"][3] == "10"
        assert by_url["https://c.com/other"][5] == "否"  # 未收录显式填否

    def test_lineage_fallback_for_summary_text_url(self, tmp_path):
        # 收录 URL 只出现在摘要文本（非结构化结果数组）→ 回退行备注
        data = base_data()
        data["sources"].append({"name": "X", "category_path": "算力服务器-服务器CPU",
                                "source_type": "知识库", "granularity": "合集级",
                                "url": "https://x.com/deep", "description": "d",
                                "reason": "r"})
        data["journal"] = self._journal(["q1"])
        raw = write_raw(tmp_path, data)
        p = tmp_path / "search_log.jsonl"
        with p.open("w", encoding="utf-8") as f:
            # A/B 的 URL 放进非 journal 查询行（保证 grounded 通过，切片时被排除）
            for s in data["sources"][:2]:
                f.write(json.dumps(
                    {"tool_name": "WebSearch", "tool_input": {"query": "test"},
                     "tool_response": {"results": [{"url": s["url"]}]}},
                    ensure_ascii=False) + "\n")
            f.write(json.dumps(
                {"tool_name": "WebSearch", "tool_input": {"query": "q1"},
                 "tool_response": {"query": "q1",
                                   "summary": "see https://x.com/deep for more"}},
                ensure_ascii=False) + "\n")
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(p))

        assert summary["lineage_rows"] == 1
        lineage_csv = next((Path(summary["outdir"]) / "intermediate").glob("*溯源.csv"))
        rows = read_csv_rows(lineage_csv)
        assert rows[1][5] == "是"
        assert rows[1][6] == "X"
        assert rows[1][8] == "摘要文本提取"

    def test_source_csv_first_query_column(self, tmp_path):
        # "来源搜索"列 = 该 URL 在留痕中首次出现的查询词
        data = base_data()
        data["journal"] = self._journal(["q1", "q2"])
        raw = write_raw(tmp_path, data)
        ev = self._evidence_results(tmp_path, {
            "q1": ["https://a.com/doc"], "q2": ["https://a.com/doc", "https://b.com/data"]})
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        by_name = {r[0]: r for r in rows[1:]}
        assert by_name["A"][6] == "q1"  # 首次出现在 q1（虽 q2 也返回了它）
        assert by_name["B"][6] == "q2"

    def test_source_csv_first_query_anchored_url(self, tmp_path):
        # 留痕结构化结果带 #1 引用锚点：剥锚点后的 URL 仍必须归因到首次查询词
        # （实测平板轮 36/84 条来源搜索为空——剥锚点后的子串被边界匹配判为前缀）
        data = base_data()
        data["sources"][0]["url"] = "https://a.com/doc#1"
        data["journal"] = self._journal(["q1"])
        raw = write_raw(tmp_path, data)
        ev = self._evidence_results(tmp_path, {
            "q1": ["https://a.com/doc#1", "https://b.com/data"]})
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        by_name = {r[0]: r for r in rows[1:]}
        assert by_name["A"][6] == "q1"
        assert by_name["A"][4] == "https://a.com/doc"  # 输出仍剥锚点

    def test_first_query_consistent_with_lineage(self, tmp_path):
        # 不变量：清单"来源搜索"= 溯源.csv 中该 URL 第一行的查询词（口径一致）
        data = base_data()
        data["sources"][0]["url"] = "https://a.com/doc#1"
        data["journal"] = self._journal(["q1", "q2"])
        raw = write_raw(tmp_path, data)
        ev = self._evidence_results(tmp_path, {
            "q1": ["https://a.com/doc#1"], "q2": ["https://a.com/doc#1", "https://b.com/data"]})
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        srows = read_csv_rows(source_csv)
        by_name = {r[0]: r for r in srows[1:]}
        lineage_csv = next((Path(summary["outdir"]) / "intermediate").glob("*溯源.csv"))
        lrows = read_csv_rows(lineage_csv)[1:]
        for name, row in by_name.items():
            first = next((l for l in lrows if l[4] == row[4] and l[5] == "是"), None)
            assert first is not None, f"{name} 无血缘行"
            assert first[2] == row[6], f"{name} 来源搜索与溯源首行不一致"

    def test_no_structured_results_no_lineage_file(self, tmp_path):
        data = base_data()
        data["journal"] = self._journal(["q1"])
        raw = write_raw(tmp_path, data)
        p = tmp_path / "search_log.jsonl"
        with p.open("w", encoding="utf-8") as f:
            # A/B 的 URL 放进非 journal 查询行（保证 grounded 通过，切片时被排除）
            for s in data["sources"]:
                f.write(json.dumps(
                    {"tool_name": "WebSearch", "tool_input": {"query": "test"},
                     "tool_response": {"results": [{"url": s["url"]}]}},
                    ensure_ascii=False) + "\n")
            # 结果数组为空：无从构建血缘行
            f.write(json.dumps(
                {"tool_name": "WebSearch", "tool_input": {"query": "q1"},
                 "tool_response": {"query": "q1", "results": []}},
                ensure_ascii=False) + "\n")
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(p))

        assert summary["lineage_rows"] == 0
        assert not list((Path(summary["outdir"]) / "intermediate").glob("*溯源.csv"))


class TestUrlHygiene:
    """不变量：所有交付 CSV 的 URL 列无纯数字引用锚点（#数字 尾巴）。

    这是约定类不变量——任何新输出面若遗漏锚点剥离，此断言即红。
    """

    def test_source_csv_urls_have_no_digit_anchors(self, tmp_path):
        data = base_data()
        data["sources"].append({"name": "C", "category_path": "算力服务器-服务器CPU",
                                "source_type": "知识库", "granularity": "合集级",
                                "url": "https://c.com/list#1", "description": "d",
                                "reason": "r"})
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert all(not re.search(r"#\d+$", r[4]) for r in rows[1:])

    def test_lineage_csv_urls_have_no_digit_anchors(self, tmp_path):
        data = base_data()
        data["journal"] = [{"phase": "增量发现", "node": "服务器CPU", "query": "q1",
                            "results": 10, "extracted": 1}]
        raw = write_raw(tmp_path, data)
        p = tmp_path / "search_log.jsonl"
        with p.open("w", encoding="utf-8") as f:
            # 结构化结果数组里的 URL 带引用锚点（真实留痕形态）
            f.write(json.dumps(
                {"tool_name": "WebSearch", "tool_input": {"query": "q1"},
                 "tool_response": {"query": "q1", "results": [
                     {"content": [{"title": "t", "url": "https://a.com/doc#1"}]},
                     {"content": [{"title": "t2", "url": "https://b.com/other#2"}]}]}},
                ensure_ascii=False) + "\n")
            for s in data["sources"]:
                f.write(json.dumps(
                    {"tool_name": "WebSearch", "tool_input": {"query": "test"},
                     "tool_response": {"results": [{"url": s["url"]}]}},
                    ensure_ascii=False) + "\n")
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(p))

        lineage_csv = next((Path(summary["outdir"]) / "intermediate").glob("*溯源.csv"))
        rows = read_csv_rows(lineage_csv)
        assert len(rows) >= 3  # header + 2 条结果行
        assert all(not re.search(r"#\d+$", r[4]) for r in rows[1:])


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


class TestCliRejectedRun:
    """CLI 端到端：被拒条目不进清单、明细在 stdout，运行正常结束（无修正重跑环节）。"""

    def test_rejected_run_produces_bundle_without_rejected_entries(self, tmp_path):
        data = base_data()
        real_sources = [dict(s) for s in data["sources"]]
        data["sources"].append({"name": "Fake", "category_path": "算力服务器-服务器CPU",
                                "source_type": "官方文档", "url": "https://fabricated.example/x",
                                "description": "编造的 URL"})
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, real_sources)
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "postprocess.py"), str(raw),
             "--evidence-log", str(ev), "--out-dir", str(tmp_path / "out")],
            capture_output=True, encoding="utf-8", timeout=60)

        assert result.returncode == 0
        assert "证据校验移除明细" in result.stdout
        assert "证据校验移除: 1" in result.stdout
        outdir = next((tmp_path / "out").glob("算力服务器_*"))  # CLI 用真实时钟命名
        assert not list(outdir.glob("*被拒记录*"))
        assert not raw.exists()  # 无修正重跑环节，临时文件正常清理


class TestEvidenceEncoding:
    """证据留痕含非法 UTF-8 字节（hook 端编码损坏）时，读取容错不中止运行。

    损坏行 decode 后仍是非 JSON（含替换符或残余片段），由既有 ValueError
    跳过逻辑处理——证据校验语义不变：损坏内容不可能为任何 URL 提供依据。
    """

    def test_invalid_utf8_bytes_in_evidence_tolerated(self, tmp_path):
        data = base_data()
        raw = write_raw(tmp_path, data)
        p = tmp_path / "search_log.jsonl"
        with p.open("wb") as f:
            f.write(b'\x98\xaf\xe4\xb8\x96\xe7\x95\x8c\xe3\x80\x82\n')  # 损坏字节片段
            for s in data["sources"]:
                payload = {"tool_name": "WebSearch",
                           "tool_input": {"query": "test"},
                           "tool_response": {"results": [{"url": s["url"],
                                                           "title": s.get("name", "")}]}}
                f.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(p))

        assert summary["kept"] == 2
        assert summary["ungrounded"] == 0
        assert summary["evidence_slice_skipped"] >= 1


class TestPrepareRunDir:
    """--prepare：流程开始时预留唯一运行目录（run_{时间戳}/，同秒加后缀）。"""

    def test_creates_unique_dir_with_same_second_suffix(self, tmp_path):
        p1 = Path(prepare_run_dir(tmp_path, now=FIXED_NOW))
        p2 = Path(prepare_run_dir(tmp_path, now=FIXED_NOW))
        assert p1.name == "run_2026-08-13-183045"
        assert p2.name == "run_2026-08-13-183045_1"
        assert p1.is_dir() and p2.is_dir()

    def test_prepare_flag_prints_path_and_creates_dir(self, tmp_path):
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "postprocess.py"),
             "--prepare", "--out-dir", str(tmp_path)],
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0
        path = Path(result.stdout.strip())
        assert path.parent == tmp_path
        assert re.match(r"run_\d{4}-\d{2}-\d{2}-\d{6}$", path.name)
        assert path.is_dir()


class TestPreparedRunDirFlow:
    """收尾：预留目录重命名为 {领域词}_{时间戳}，raw.json 归档，交付物在根。"""

    def _prepared_run(self, tmp_path, data, timestamp="2026-08-13-183045"):
        run_dir = tmp_path / f"run_{timestamp}"
        run_dir.mkdir()
        raw = run_dir / "raw.json"
        raw.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        evidence = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path), evidence_log=str(evidence),
                      now=FIXED_NOW)
        return summary, run_dir

    def test_renamed_to_domain_timestamp_and_archived(self, tmp_path):
        data = base_data()
        summary, run_dir = self._prepared_run(tmp_path, data)
        assert summary["outdir"].endswith("算力服务器_2026-08-13-183045")
        assert not run_dir.exists()  # run_ 目录已重命名为最终交付目录
        outdir = Path(summary["outdir"])
        assert outdir.is_dir()
        assert (outdir / "intermediate" / "raw_input.json").is_file()
        assert not (outdir / "raw.json").exists()  # 归档后不再留根目录
        assert len(list(outdir.glob("*.csv"))) == 2  # 脚本只写两个 CSV（分析报告.md 由模型收尾时写入）

    def test_reuses_run_dir_timestamp_not_clock(self, tmp_path):
        # 预留目录时间戳与注入时钟不同：收尾必须复用 run_ 名内的时间戳
        data = base_data()
        summary, _ = self._prepared_run(tmp_path, data, timestamp="2026-08-24-103503")
        assert summary["outdir"].endswith("算力服务器_2026-08-24-103503")

    def test_same_name_clash_gets_suffix(self, tmp_path):
        # 重命名目标已存在（如并发同域运行）：加 _1 后缀，不覆盖
        data = base_data()
        (tmp_path / "算力服务器_2026-08-13-183045").mkdir()
        summary, _ = self._prepared_run(tmp_path, data)
        assert summary["outdir"].endswith("算力服务器_2026-08-13-183045_1")

    def test_legacy_fixed_path_still_supported(self, tmp_path):
        # 旧固定路径 outputs/raw.json（父目录非 run_）：行为不变，收尾删除暂存
        data = base_data()
        raw = write_raw(tmp_path, data)
        evidence = write_evidence(tmp_path, data["sources"])
        summary = run(str(raw), out_dir=str(tmp_path), evidence_log=str(evidence),
                      now=FIXED_NOW)
        assert summary["outdir"].endswith("算力服务器_2026-08-13-183045")
        assert not (tmp_path / "raw.json").exists()


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


class TestFinalizeReport:
    def test_renames_with_dirname_prefix(self, tmp_path):
        d = tmp_path / "电源和能源硬件_2026-08-25-180439"
        d.mkdir()
        (d / "分析报告.md").write_text("报告内容", encoding="utf-8")
        result = finalize_report(str(d))
        assert result == str(d / "电源和能源硬件_2026-08-25-180439_分析报告.md")
        assert (d / "电源和能源硬件_2026-08-25-180439_分析报告.md").read_text(encoding="utf-8") == "报告内容"
        assert not (d / "分析报告.md").exists()

    def test_missing_report_raises(self, tmp_path):
        d = tmp_path / "某领域_2026-08-25-180439"
        d.mkdir()
        with pytest.raises(FileNotFoundError):
            finalize_report(str(d))

    def test_target_exists_raises(self, tmp_path):
        d = tmp_path / "某领域_2026-08-25-180439"
        d.mkdir()
        (d / "分析报告.md").write_text("新", encoding="utf-8")
        (d / "某领域_2026-08-25-180439_分析报告.md").write_text("旧", encoding="utf-8")
        with pytest.raises(FileExistsError):
            finalize_report(str(d))


class TestSessionIsolatedEvidenceLog:
    """证据留痕按会话隔离（2026-08-26 起）：并行运行互不删除对方留痕。"""

    def test_default_evidence_log_uses_session_id(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-abc")
        assert default_evidence_log() == "outputs/search_log_sess-abc.jsonl"
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
        assert default_evidence_log() == "outputs/search_log.jsonl"

    def test_log_tool_without_arg_writes_session_file(self, tmp_path):
        payload = json.dumps(
            {"tool_name": "WebSearch", "tool_input": {"query": "测试"}}, ensure_ascii=False)
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID="sess-1")
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "log_tool.py")],
            input=payload, capture_output=True, encoding="utf-8", timeout=30,
            cwd=str(tmp_path), env=env)
        assert result.returncode == 0
        log = tmp_path / "outputs" / "search_log_sess-1.jsonl"
        assert log.exists()
        assert json.loads(log.read_text(encoding="utf-8").strip())["tool_input"]["query"] == "测试"
