"""postprocess.py 与 evidence_hook.py 的单元测试。"""
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

from postprocess import (check_grounded, check_granularity,
                         deduplicate, default_evidence_log, finalize_report, fold,
                         leaf_node, prepare_run_dir, query_in_evidence, run_pipeline,
                         run_evidence_log, sanitize_domain, slice_evidence,
                         strip_citation_anchors)

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

    def test_same_url_different_name_deduped(self):
        """URL 相同即同一资源，名称措辞不同不构成两个条目（2026-09-11 实证：
        200054 轮同一入口被 record_sources 与 record_knowledge 各记一次、两次
        措辞不同，(域名,名称) 键不命中——交付清单虚高 116 条/16%）。"""
        sources = [
            {"name": "Arista 产品文档中心", "url": "https://a.com/docs"},
            {"name": "Arista Networks 官方文档", "url": "https://a.com/docs"},
        ]
        result = deduplicate(sources)
        assert len(result) == 1
        assert result[0]["name"] == "Arista 产品文档中心"

    def test_same_url_differing_only_by_citation_anchor_deduped(self):
        """引用序号锚点不改变资源指向：剥离后同 URL 同样判重——输出 CSV 会剥离
        纯数字锚点，只在原始串上判重会在交付清单里留下两行完全相同的 URL。"""
        sources = [
            {"name": "A", "url": "https://a.com/doc.pdf#2#1"},
            {"name": "B", "url": "https://a.com/doc.pdf#3"},
        ]
        assert len(deduplicate(sources)) == 1


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

    def test_markdown_bold_around_url_kept(self):
        """摘要把官网地址写成 `**www.example.com**`——加粗记号不是 URL 的一部分。

        2026-09-14（171459 轮 IDC 实证）：`*` 属 RFC 3986 sub-delims、在 URL_CHARS
        内，加粗记号让边界匹配失败；该轮摘要只写了 `**www.idc.com**`，链接块里
        只有带跟踪参数的子页——两条路都堵死，官网入口收不进来。
        """
        evidence = "根据搜索结果，IDC 的官方网站是 **www.idc.com**。"
        kept, rejected = check_grounded([{"url": "www.idc.com"}], evidence)
        assert len(kept) == 1
        assert rejected == []

    def test_markdown_bold_does_not_weaken_truncation_guard(self):
        """剥加粗记号不放松截短保护——父路径仍拒。"""
        evidence = "入口 **https://a.com/doc/123** 见上"
        kept, rejected = check_grounded([{"url": "https://a.com/doc"}], evidence)
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

    def test_url_form_variant_kept(self):
        """去 scheme / 去尾斜杠的等价写法放行（2026-09-15 实测）。

        scheme 与尾斜杠不属于资源身份——摘要是生成式散文，同一 URL 在不同句子
        里写法不同（留痕写 `www.example.com`，模型抄成 `https://www.example.com/`）。
        该轮 861 条提交里 50 条被拒，其中 10 条属这一类。

        方向是单向的：只归一**候选自己带的**渲染装饰（去 scheme、去尾斜杠）。
        反向（候选裸域名、留痕带 scheme）实测仅 1 条收益，且那等于让更短的串
        通过，故不做——候选写短了本就该拒。
        """
        kept, _ = check_grounded([{"url": "https://www.example.com/"}],
                                 "官网是 www.example.com。")
        assert len(kept) == 1
        kept, _ = check_grounded([{"url": "https://www.example.com/"}],
                                 "见面页：www.example.com")
        assert len(kept) == 1

    def test_form_variants_do_not_weaken_truncation_guard(self):
        """变体归一不放松截短保护——取父路径 / 去参数仍然拒。

        归一化只动 scheme 与尾斜杠，路径、主机、query 一个字符不动。
        """
        kept, rejected = check_grounded([{"url": "https://a.com/doc"}],
                                        '{"url":"https://a.com/doc/1"}')
        assert kept == [] and len(rejected) == 1
        kept, rejected = check_grounded([{"url": "https://a.com/doc"}],
                                        '{"url":"https://a.com/doc?dgcid=1"}')
        assert kept == [] and len(rejected) == 1

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
    def test_multilang_audit_removed(self, tmp_path):
        """2026-09-01 钉进测试：多语言审计已下线——两轮实测全为假阳性
        （分组键忽略 query 参数，同端点不同资源被误判为多语言重复），
        真多语言重复零发生；无消费者 + 假信号会误导触发，修不如删。
        stdout 不再报告该指标，summary 不再携带相关键。"""
        data = base_data()
        data["sources"] = [
            {"name": "规格页中文", "category_path": "算力服务器-服务器CPU",
             "source_type": "官方文档", "granularity": "单篇级",
             "url": "https://support.apple.com/zh-cn/122240", "description": "d", "reason": "r"},
            {"name": "规格页英文", "category_path": "算力服务器-服务器CPU",
             "source_type": "官方文档", "granularity": "单篇级",
             "url": "https://support.apple.com/en-nz/122240", "description": "d", "reason": "r"},
        ]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        import io
        from contextlib import redirect_stdout
        from postprocess import _print_summary
        buf = io.StringIO()
        with redirect_stdout(buf):
            summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                          evidence_log=str(ev))
            _print_summary(summary)
        assert not any(k.startswith("multilang") for k in summary)
        assert "多语言版本并存" not in buf.getvalue()


class TestSourceTypeNormalization:
    """体裁封闭词表归一（12 类 + 其他）：三来源（CLI sources / knowledge manifest /
    store）在 pipeline 汇合处统一归一——幂等，双入口（CLI/MCP）口径一致；
    表外词落「其他」并按原始词计数进审计行（与来源无关）。"""

    def test_alias_normalized_in_csv_and_stats(self, tmp_path):
        data = base_data()
        data["sources"][0]["source_type"] = "厂商文档"  # 别名 → 文档
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))
        assert summary["unmapped_types"] == {}
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert all(r[2] in ("文档", "数据集") for r in rows[1:])
        stats_csv = next((Path(summary["outdir"]) / "intermediate").glob("*stats.csv"))
        srows = read_csv_rows(stats_csv)
        assert srows[1][2] == "文档:1"

    def test_unmapped_counted_across_all_three_sources(self, tmp_path):
        """审计覆盖三来源：CLI sources 一条 + knowledge manifest 一条（store 行
        入库时已归一，pipeline 再归一为幂等——表外词只计一次）。"""
        data = base_data()
        data["sources"][0]["source_type"] = "某新词"
        kn = {"name": "K", "node": "服务器CPU", "verified": True,
              "category_path": "算力服务器-服务器CPU", "source_type": "另一新词",
              "url": "https://k.com/doc", "description": "d", "reason": "r"}
        data["knowledge"] = [kn]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"] + [kn])
        import io
        from contextlib import redirect_stdout
        from postprocess import _print_summary
        buf = io.StringIO()
        with redirect_stdout(buf):
            summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                          evidence_log=str(ev))
            _print_summary(summary)
        assert summary["unmapped_types"] == {"某新词": 1, "另一新词": 1}
        out = buf.getvalue()
        assert "表外词兜底: 2 条" in out
        assert "某新词×1" in out and "另一新词×1" in out
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert sum(1 for r in rows[1:] if r[2] == "其他") == 2


class TestRun:
    def test_full_pipeline(self, tmp_path):
        data = base_data()
        # 加一条重复 + 一条缺 url 的坏记录 + 一条缺 name 的坏记录
        data["sources"].append(dict(data["sources"][0]))
        data["sources"].append({"name": "X", "category_path": "算力服务器-服务器CPU"})
        data["sources"].append({"url": "https://y.com", "category_path": "算力服务器-服务器CPU"})
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])

        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        stats_csv = outdir / "intermediate" / "算力服务器_2026-08-13-183045_stats.csv"
        assert stats_csv.exists()
        assert stats_csv.read_bytes()[:3] == BOM
        rows = read_csv_rows(stats_csv)
        assert len(rows) == 5  # header + 3 nodes + 总计
        by_node = {r[0]: r for r in rows[1:] if r[0] != "总计"}
        assert by_node["AI训练GPU"][1] == "1"
        assert by_node["AI训练GPU"][2] == "文档:1"
        assert by_node["图形渲染GPU"][1] == "1"
        assert by_node["服务器CPU"][1] == "0"
        assert by_node["服务器CPU"][2] == ""
        assert rows[-1][0] == "总计"
        assert rows[-1][1] == "2"  # 最终收录 = 候选数求和
        assert rows[-1][2] == "数据集:1; 文档:1"  # 同数按体裁名升序：数(U+6570) < 文(U+6587)
        # stats 列集合钉死：纯清单统计表（领域/时间戳在文件名、模型在 manifest、健康指标在 stdout）
        assert rows[0] == ["分类节点", "候选数", "体裁分布"]

    def test_keep_raw(self, tmp_path):
        raw = write_raw(tmp_path, base_data())
        ev = write_evidence(tmp_path, base_data()["sources"])
        run_pipeline(str(raw), out_dir=str(tmp_path / "out"), keep_raw=True, now=FIXED_NOW,
            evidence_log=str(ev))
        assert raw.exists()
        assert ev.exists()

    def test_outdir_collision_gets_suffix(self, tmp_path):
        raw1 = write_raw(tmp_path, base_data())
        ev1 = write_evidence(tmp_path, base_data()["sources"])
        run_pipeline(str(raw1), out_dir=str(tmp_path / "out"), now=FIXED_NOW, evidence_log=str(ev1))
        raw2 = write_raw(tmp_path, base_data())
        ev2 = write_evidence(tmp_path, base_data()["sources"])
        summary = run_pipeline(str(raw2), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev2))
        assert summary["outdir"].endswith("算力服务器_2026-08-13-183045_1")

    def test_csv_escaping(self, tmp_path):
        data = base_data()
        data["sources"][0]["name"] = '名称,含"逗号"和引号'
        data["sources"][0]["description"] = "多行\n描述"
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
            run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW)
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "nodes" in str(e)

    def test_unmatched_source_counted_but_kept_in_csv(self, tmp_path):
        data = base_data()
        data["sources"].append({"name": "U", "category_path": "算力服务器-不存在的节点",
                                "source_type": "媒体", "url": "https://u.com",
                                "description": "d"})
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(tmp_path / "不存在.jsonl"))

        assert summary["kept"] == 0
        assert set(summary["empty_nodes"]) == set(data["nodes"])
        stats_csv = next((Path(summary["outdir"]) / "intermediate").glob("*stats.csv"))
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["kept"] == 3

    def test_knowledge_item_explicit_single_level_kept(self, tmp_path):
        data = base_data()
        kn = {"name": "国标文件", "node": "服务器CPU", "verified": True,
              "granularity": "单篇级",
              "category_path": "算力服务器-服务器CPU",
              "source_type": "标准", "url": "https://m.com/gb.pdf",
              "description": "标准全文", "reason": "标准文件，无合集可替代"}
        data["knowledge"] = [kn]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"] + [kn])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["ungrounded"] == 0
        assert summary["kept"] == 3
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert any(r[0] == "C" and r[4] == "https://c.com/list" for r in rows)

    def test_missing_evidence_log_raises(self, tmp_path):
        raw = write_raw(tmp_path, base_data())
        try:
            run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["list_verified"] == "0/1"
        assert summary["incomplete"] == 1
        assert summary["kept"] == 2

    def test_verified_missing_name_counts_incomplete_only(self, tmp_path):
        data = base_data()
        data["knowledge"] = [self._knowledge_item(name="")]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["list_verified"] == "1/1"
        assert summary["kept"] == 3
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        merged_row = next(r for r in rows[1:] if r[0] == "IEEE 802.3 工作组")
        assert merged_row[2] == "标准"

    def test_knowledge_missing_tolerated(self, tmp_path):
        data = base_data()
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["knowledge_missing"] is True
        assert summary["list_verified"] == "0/0"
        assert summary["kept"] == 2

    def test_knowledge_wrong_type_tolerated(self, tmp_path):
        data = base_data()
        data["knowledge"] = {"name": "不是列表"}
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["list_verified"] == "0/0"
        assert summary["kept"] == 2

    def test_knowledge_empty_list_tolerated(self, tmp_path):
        data = base_data()
        data["knowledge"] = []
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["knowledge_missing"] is True
        assert summary["list_verified"] == "0/0"

    def test_knowledge_non_dict_entries_not_in_denominator(self, tmp_path):
        data = base_data()
        data["knowledge"] = [self._knowledge_item(), "垃圾条目"]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"] + [data["knowledge"][0]])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["list_verified"] == "1/1"  # 非 dict 不计入分母

    def test_merged_url_must_be_grounded(self, tmp_path):
        data = base_data()
        kn = self._knowledge_item(url="https://fabricated.example/portal")
        data["knowledge"] = [kn]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])  # 证据里没有清单项 URL
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["journal_count"] == 2
        journal_csv = next((Path(summary["outdir"]) / "intermediate").glob("*搜索日志.csv"))
        assert journal_csv.read_bytes()[:3] == BOM
        rows = read_csv_rows(journal_csv)
        assert rows[0] == ["阶段", "节点", "查询词", "返回链接数", "提取候选数", "验证通过", "证据缺失", "零提取理由"]
        assert len(rows) == 3
        assert rows[1][2] == "IEEE 802.3 official"
        assert rows[1][5] == ""  # 验证通过列：非验证行留空
        assert rows[1][6] == "否"  # 证据缺失列：未缺失显式填否
        assert rows[1][7] == ""  # 零提取理由列：非零提取行留空

    def test_verification_count_mismatch_flagged(self, tmp_path):
        """2026-09-01 钉进测试：journal verified 计数与清单验证通过数的一致性校验
        ——方向性判定：仅当声称数 < 实际并入数（漏填）时警告；≥ 视为正常
        （一次搜索验证多入口 / 复验会多记，不误报）。"""
        data = base_data()
        kn1 = {"name": "机构A", "node": "AI训练GPU", "verified": True,
               "category_path": "算力服务器-GPU服务器-AI训练GPU",
               "source_type": "官方文档", "granularity": "合集级",
               "url": "https://a.com/doc", "description": "d", "reason": "r"}
        kn2 = {"name": "机构B", "node": "AI训练GPU", "verified": True,
               "category_path": "算力服务器-GPU服务器-AI训练GPU",
               "source_type": "官方文档", "granularity": "合集级",
               "url": "https://k.com/doc", "description": "d", "reason": "r"}
        data["knowledge"] = [kn1, kn2]  # merged=2
        data["journal"] = [{"phase": "验证搜索", "node": "AI训练GPU",
                            "query": "机构A 官网", "results": 10,
                            "extracted": 0, "verified": True}]  # claims=1 < 2
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, ["https://a.com/doc", "https://k.com/doc"])
        import io
        from contextlib import redirect_stdout
        from postprocess import _print_summary
        buf = io.StringIO()
        with redirect_stdout(buf):
            summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                          evidence_log=str(ev))
            _print_summary(summary)
        assert summary["verification_mismatch"] == (1, 2)
        out = buf.getvalue()
        assert "验证通过标记数与清单验证通过数不一致" in out
        assert "声称 1 次 < 清单实际并入 2 项（差 1）" in out  # 2026-09-02：警告带数字，复盘一眼看到差距

    def test_verification_overclaim_not_flagged(self, tmp_path):
        """声称数 ≥ 实际并入数（一次搜索验证多个入口/复验多记）不误报。"""
        data = base_data()
        kn = {"name": "机构A", "node": "AI训练GPU", "verified": True,
              "category_path": "算力服务器-GPU服务器-AI训练GPU",
              "source_type": "官方文档", "granularity": "合集级",
              "url": "https://a.com/doc", "description": "d", "reason": "r"}
        data["knowledge"] = [kn]  # merged=1
        data["journal"] = [
            {"phase": "验证搜索", "node": "AI训练GPU", "query": "机构A 官网",
             "results": 10, "extracted": 0, "verified": True},
            {"phase": "扩量轮", "node": "AI训练GPU", "query": "机构A 官方文档",
             "results": 10, "extracted": 0, "verified": True},
        ]  # claims=2 >= 1
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, ["https://a.com/doc"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))
        assert summary["verification_mismatch"] is None

    def test_verification_count_consistent_no_warning(self, tmp_path):
        data = base_data()
        raw = write_raw(tmp_path, data)
        ev = write_evidence_queries(tmp_path, [], data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))
        assert summary["verification_mismatch"] is None


    def test_journal_query_missing_flagged(self, tmp_path):
        data = base_data()
        data["journal"] = self._journal(["不存在的查询词"])
        raw = write_raw(tmp_path, data)
        ev = write_evidence_queries(tmp_path, ["另一个查询"], data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        journal_csv = next((Path(summary["outdir"]) / "intermediate").glob("*搜索日志.csv"))
        rows = read_csv_rows(journal_csv)
        assert rows[1][6] == "是"

    def test_no_journal_no_csv(self, tmp_path):
        data = base_data()
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        assert summary["journal_count"] == 0
        assert not list((Path(summary["outdir"]) / "intermediate").glob("*搜索日志.csv"))

    def test_journal_non_dict_entries_skipped_and_counted(self, tmp_path):
        data = base_data()
        data["journal"] = self._journal(["IEEE 802.3 official"]) + ["垃圾条目"]
        raw = write_raw(tmp_path, data)
        ev = write_evidence_queries(tmp_path, ["IEEE 802.3 official", "test"],
                                    data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
            run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
            run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        journal_csv = next((Path(summary["outdir"]) / "intermediate").glob("*搜索日志.csv"))
        rows = read_csv_rows(journal_csv)
        assert rows[1][6] == "是"


class TestQueryScope:
    """2026-09-01：选题分类（后验统计）——实体选题放开后的验证度量。
    判定键只有一个：查询词是否含领域词/节点名，完全确定性；
    英文角度词/抽象词归非框架内是已知近似，数字按趋势读不按绝对值。"""

    def test_phase_group_prefix_tolerant(self):
        """2026-09-01 实测 bug 钉进测试：phase 是模型自由文本（"验证搜索"曾被缩写
        为"验证"、"增量发现"为"增量"），字面全等匹配导致选题分布 0/0 与
        verified 警告误报——按前缀归组。"""
        from postprocess import _phase_group
        assert _phase_group("验证搜索") == "验证"
        assert _phase_group("验证") == "验证"
        assert _phase_group("增量发现") == "增量"
        assert _phase_group("增量") == "增量"
        assert _phase_group("扩量轮") == "扩量"
        assert _phase_group("扩量") == "扩量"
        assert _phase_group("") == ""
        assert _phase_group("乱写") == ""

    def test_short_phase_names_still_counted(self, tmp_path):
        """缩写 phase 下选题分布与 verified 校验照常工作（不 0/0、不误报）。"""
        data = base_data()
        kn = {"name": "机构A", "node": "AI训练GPU", "verified": True,
              "category_path": "算力服务器-GPU服务器-AI训练GPU",
              "source_type": "官方文档", "granularity": "合集级",
              "url": "https://a.com/doc", "description": "d", "reason": "r"}
        data["knowledge"] = [kn]
        data["journal"] = [
            {"phase": "验证", "node": "AI训练GPU", "query": "机构A 官网",
             "results": 10, "extracted": 0, "verified": True},
            {"phase": "增量", "node": "AI训练GPU", "query": "算力服务器 厂商 文档",
             "results": 10, "extracted": 2},
            {"phase": "增量", "node": "AI训练GPU", "query": "超以太网联盟 UEC",
             "results": 10, "extracted": 8},
        ]
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, ["https://a.com/doc"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))
        assert summary["verification_mismatch"] is None  # claims=1 == merged=1，缩写不误报
        assert summary["in_framework"] == {"count": 1, "extracted": 2}
        assert summary["out_framework"]["count"] == 1
        assert summary["out_framework"]["extracted"] == 8

    def test_domain_or_node_word_is_framework(self):
        from postprocess import classify_query_scope
        assert classify_query_scope("算力服务器 厂商 文档", "算力服务器",
                                    ["AI训练GPU"]) == "框架内"
        assert classify_query_scope("AI训练GPU 厂商 文档", "算力服务器",
                                    ["AI训练GPU"]) == "框架内"
        assert classify_query_scope("", "算力服务器", ["AI训练GPU"]) == "框架内"

    def test_no_domain_no_node_word_is_non_framework(self):
        from postprocess import classify_query_scope
        assert classify_query_scope("超以太网联盟 UEC", "算力服务器",
                                    ["AI训练GPU"]) == "非框架内"
        assert classify_query_scope("TOP500 supercomputer list", "算力服务器",
                                    ["AI训练GPU"]) == "非框架内"

    def test_scope_summary_and_stdout(self, tmp_path):
        data = base_data()
        data["journal"] = [
            {"phase": "增量发现", "node": "AI训练GPU", "query": "算力服务器 厂商 文档",
             "results": 10, "extracted": 2},
            {"phase": "增量发现", "node": "AI训练GPU", "query": "超以太网联盟 UEC",
             "results": 10, "extracted": 8},
            {"phase": "验证搜索", "node": "AI训练GPU", "query": "寒武纪 官网",
             "results": 10, "extracted": 0, "verified": True},
        ]
        raw = write_raw(tmp_path, data)
        ev = write_evidence_queries(tmp_path, ["算力服务器 厂商 文档", "超以太网联盟 UEC",
                                               "寒武纪 官网"], data["sources"])
        import io
        from contextlib import redirect_stdout
        from postprocess import _print_summary
        buf = io.StringIO()
        with redirect_stdout(buf):
            summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                          evidence_log=str(ev))
            _print_summary(summary)
        # 验证搜索行不参与分类
        assert summary["in_framework"] == {"count": 1, "extracted": 2}
        assert summary["out_framework"]["count"] == 1
        assert summary["out_framework"]["extracted"] == 8
        assert summary["out_framework"]["by_node"] == {"AI训练GPU": (1, 8)}
        out = buf.getvalue()
        assert "选题分布" in out
        assert "不含领域词 1 次" in out
        assert "含领域词 1 次" in out


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
        ev = self._evidence_mixed(tmp_path, queries, data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        outdir = Path(summary["outdir"])
        intermediate = outdir / "intermediate"
        # raw_input.json 已取消（2026-08-31：与 store_input.jsonl 重复的推导件）
        assert not (intermediate / "raw_input.json").exists()
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
        # 脚本在根目录只写数据源清单（分析报告.md 由模型收尾时写入）；
        # stats.csv 随排障材料入 intermediate/（2026-08-31：根目录只留两个交付物——清单+报告）
        root_files = {p.name for p in outdir.iterdir() if p.is_file()}
        # 运行核对.json 由 fold 路径写出（docs/06 第 2 期）；本用例走 CLI 兼容路径
        # run_pipeline，不产出该文件
        assert root_files == {"算力服务器_2026-08-13-183045_数据源清单.csv"}
        assert (intermediate / "算力服务器_2026-08-13-183045_stats.csv").exists()
        # 会话临时文件仍按现状删除
        assert not raw.exists()
        assert not ev.exists()

    def test_no_journal_no_evidence_archive(self, tmp_path):
        data = base_data()
        raw = write_raw(tmp_path, data)
        ev = write_evidence(tmp_path, data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        # 被拒仍产出完整 bundle（不落被拒文件）；raw/留痕正常清理
        assert summary["ungrounded"] == 1
        outdir = Path(summary["outdir"])
        assert not list(outdir.glob("*被拒记录*"))
        assert not (outdir / "intermediate" / "raw_input.json").exists()
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))

        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        by_name = {r[0]: r for r in rows[1:]}
        assert by_name["A"][6] == "q1"  # 首次出现在 q1（虽 q2 也返回了它）
        assert by_name["B"][6] == "q2"

    def test_source_csv_first_query_anchored_url(self, tmp_path):
        # 留痕结构化结果带 #1 引用锚点：剥锚点后的 URL 仍必须归因到首次查询词
        # （实测平板电脑轮 36/84 条来源搜索为空——剥锚点后的子串被边界匹配判为前缀）
        data = base_data()
        data["sources"][0]["url"] = "https://a.com/doc#1"
        data["journal"] = self._journal(["q1"])
        raw = write_raw(tmp_path, data)
        ev = self._evidence_results(tmp_path, {
            "q1": ["https://a.com/doc#1", "https://b.com/data"]})
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path), evidence_log=str(evidence),
                      now=FIXED_NOW)
        return summary, run_dir

    def test_renamed_to_domain_timestamp_and_archived(self, tmp_path):
        data = base_data()
        summary, run_dir = self._prepared_run(tmp_path, data)
        assert summary["outdir"].endswith("算力服务器_2026-08-13-183045")
        assert not run_dir.exists()  # run_ 目录已重命名为最终交付目录
        outdir = Path(summary["outdir"])
        assert outdir.is_dir()
        assert not (outdir / "intermediate" / "raw_input.json").exists()
        assert not (outdir / "raw.json").exists()  # 归档后不再留根目录
        assert len(list(outdir.glob("*.csv"))) == 1  # 脚本只写清单 CSV（stats 入 intermediate/，报告由模型收尾时写入）

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
        summary = run_pipeline(str(raw), out_dir=str(tmp_path), evidence_log=str(evidence),
                      now=FIXED_NOW)
        assert summary["outdir"].endswith("算力服务器_2026-08-13-183045")
        assert not (tmp_path / "raw.json").exists()


class TestEvidenceHook:
    def test_appends_hook_payload(self, tmp_path):
        log = tmp_path / "log.jsonl"
        payload = json.dumps(
            {"tool_name": "WebSearch", "tool_input": {"query": "测试"},
             "tool_response": {"results": [{"url": "https://a.com", "title": "结果"}]}},
            ensure_ascii=False)
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "evidence_hook.py"), str(log)],
            input=payload, capture_output=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["tool_name"] == "WebSearch"
        assert json.loads(lines[0])["tool_response"]["results"][0]["url"] == "https://a.com"

    def test_malformed_input_silently_passes(self, tmp_path):
        log = tmp_path / "log.jsonl"
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "evidence_hook.py"), str(log)],
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
        content = (d / "电源和能源硬件_2026-08-25-180439_分析报告.md").read_text(encoding="utf-8")
        assert "报告内容" in content
        assert "统计注入失败" in content  # 无 stats.csv 时注入占位提示（2026-09-02）
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


class TestReportStatsInjection:
    """数据总览注入（2026-08-28 起）：报告统计数字由脚本生成、模型不写数字——
    修"报告体裁数与 stats 对不上"（模型凭去重前记忆自算）的确定性下沉缺口。"""

    def _setup(self, tmp_path, report_text, with_stats=True):
        d = tmp_path / "交换机_2026-08-28-114546"
        d.mkdir()
        (d / "分析报告.md").write_text(report_text, encoding="utf-8")
        if with_stats:
            (d / "intermediate").mkdir()
            (d / "intermediate" / "交换机_2026-08-28-114546_stats.csv").write_text(
                "分类节点,候选数,体裁分布\n"
                "工业交换机,98,\"厂商文档: 30; 行业标准: 10\"\n"
                "数据中心交换机,84,\"厂商文档: 40; 资讯平台: 20\"\n"
                "总计,182,\"厂商文档: 70; 行业标准: 10; 资讯平台: 20\"\n",
                encoding="utf-8-sig")
            (d / "交换机_2026-08-28-114546_数据源清单.csv").write_text(
                "数据源名称,分类路径,数据源类型,粒度,访问地址,简要说明,来源搜索\n"
                "a,交换机-工业交换机,厂商文档,合集级,https://a.com,desc,src\n"
                "b,交换机-工业交换机,厂商文档,单篇级,https://b.com,desc,src\n"
                "c,交换机-数据中心交换机,行业标准,单篇级,https://c.com,desc,src\n",
                encoding="utf-8-sig")
        return d

    def test_inject_into_existing_section(self, tmp_path):
        d = self._setup(tmp_path,
            "# 交换机 领域分析报告\n"
            "> 本报告基于本次自动发现的数据源生成。\n\n"
            "## 数据总览\n\n（本段由收尾脚本自动生成）\n\n"
            "## 一、领域概览\n（定性内容）\n")
        result = finalize_report(str(d))
        text = Path(result).read_text(encoding="utf-8")
        assert "数据源总数：182 条（合集级 1 / 单篇级 2）" in text
        assert "体裁分布：厂商文档: 70; 行业标准: 10; 资讯平台: 20" in text
        assert "工业交换机: 98" in text
        assert "（本段由收尾脚本自动生成）" not in text
        assert "## 一、领域概览" in text

    def test_insert_after_title_when_section_missing(self, tmp_path):
        d = self._setup(tmp_path,
            "# 交换机 领域分析报告\n\n## 一、领域概览\n（定性内容）\n")
        result = finalize_report(str(d))
        text = Path(result).read_text(encoding="utf-8")
        assert text.index("## 数据总览") < text.index("## 一、领域概览")
        assert "数据源总数：182 条" in text

    def test_missing_stats_writes_placeholder(self, tmp_path):
        """2026-09-02：stats.csv 缺失时注入不静默跳过——往"数据总览"节写占位提示，
        避免交付空白标题（模型被禁止写数字，占位由脚本负责）。"""
        d = self._setup(tmp_path,
            "# 交换机 领域分析报告\n\n## 数据总览\n\n（本段由收尾脚本自动生成）\n\n"
            "## 一、领域概览\n（定性内容）\n", with_stats=False)
        result = finalize_report(str(d))
        text = Path(result).read_text(encoding="utf-8")
        assert "统计注入失败" in text
        assert "（本段由收尾脚本自动生成）" not in text

    def test_missing_stats_skips_injection(self, tmp_path):
        d = self._setup(tmp_path, "# 标题\n\n## 一、领域概览\n内容\n", with_stats=False)
        result = finalize_report(str(d))
        assert "数据总览" not in Path(result).read_text(encoding="utf-8")


class TestSessionIsolatedEvidenceLog:
    """证据留痕按会话隔离（2026-08-26 起）：并行运行互不删除对方留痕。"""

    def test_default_evidence_log_uses_session_id(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-abc")
        assert default_evidence_log() == "outputs/search_log_sess-abc.jsonl"
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
        assert default_evidence_log() == "outputs/search_log.jsonl"

    def test_evidence_hook_without_arg_writes_session_file(self, tmp_path):
        payload = json.dumps(
            {"tool_name": "WebSearch", "tool_input": {"query": "测试"}}, ensure_ascii=False)
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID="sess-1")
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "evidence_hook.py")],
            input=payload, capture_output=True, encoding="utf-8", timeout=30,
            cwd=str(tmp_path), env=env)
        assert result.returncode == 0
        log = tmp_path / "outputs" / "search_log_sess-1.jsonl"
        assert log.exists()
        assert json.loads(log.read_text(encoding="utf-8").strip())["tool_input"]["query"] == "测试"


class TestRunScopedEvidence:
    """证据留痕运行级归属（2026-08-31 分层原则修订）：--prepare 写会话标记，
    hook 按标记把留痕写进 run_*/evidence.jsonl，收尾按运行目录读/切片/删除——
    outputs/ 顶层不再有平铺的会话级留痕文件。"""

    def test_prepare_writes_session_marker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-run-ev")
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        assert (run_dir / ".session_id").read_text(encoding="utf-8") == "sess-run-ev"

    def test_run_uses_run_dir_evidence_and_cleans_up(self, tmp_path):
        """不传 --evidence-log 时优先用运行目录内的 evidence.jsonl；收尾后根目录
        不留证据与标记（切片已进 intermediate/）。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        data = base_data()
        data["journal"] = [{"phase": "增量发现", "node": "AI训练GPU",
                            "query": "GPU 排名 数据库", "results": 10, "extracted": 2}]
        write_raw(run_dir, data)
        (run_dir / "evidence.jsonl").write_text(
            json.dumps({"tool_name": "WebSearch", "tool_input": {"query": "test"},
                        "tool_response": {"results": [{"url": "https://a.com/doc"}]}},
                       ensure_ascii=False) + "\n" +
            json.dumps({"tool_name": "WebSearch", "tool_input": {"query": "test"},
                        "tool_response": {"results": [{"url": "https://b.com/data"}]}},
                       ensure_ascii=False) + "\n" +
            json.dumps({"tool_name": "WebSearch", "tool_input": {"query": "GPU 排名 数据库"},
                        "tool_response": {"results": [{"url": "https://x.com/list"}]}},
                       ensure_ascii=False) + "\n",
            encoding="utf-8")
        summary = run_pipeline(str(run_dir / "raw.json"), out_dir=str(tmp_path / "outputs"),
                      now=FIXED_NOW)
        assert summary["kept"] == 2
        outdir = Path(summary["outdir"])
        assert not (outdir / "evidence.jsonl").exists()
        assert not (outdir / ".session_id").exists()
        assert (outdir / "intermediate" / "evidence_log.jsonl").exists()

    def test_run_scoped_evidence_archived_whole(self, tmp_path):
        """运行级留痕即本运行专属：全量归档，不按记账查询词切片。

        2026-09-11 实证（200054 轮）：模型实做 345 次搜索只记账 217 条，
        切片把未记账搜索的留痕一并删掉，交付清单 29% 的 URL 归档后无法溯源。
        运行级留痕不混入其他运行的搜索，无需切片。
        """
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        data = base_data()
        data["journal"] = [{"phase": "增量发现", "node": "AI训练GPU",
                            "query": "GPU 排名 数据库", "results": 10, "extracted": 2}]
        write_raw(run_dir, data)
        (run_dir / "evidence.jsonl").write_text(
            json.dumps({"tool_name": "WebSearch", "tool_input": {"query": "GPU 排名 数据库"},
                        "tool_response": {"results": [{"url": "https://a.com/doc"}]}},
                       ensure_ascii=False) + "\n" +
            json.dumps({"tool_name": "WebSearch", "tool_input": {"query": "漏记的验证搜索"},
                        "tool_response": {"results": [{"url": "https://b.com/data"}]}},
                       ensure_ascii=False) + "\n",
            encoding="utf-8")
        summary = run_pipeline(str(run_dir / "raw.json"), out_dir=str(tmp_path / "outputs"),
                               now=FIXED_NOW)
        archived = (Path(summary["outdir"]) / "intermediate" / "evidence_log.jsonl").read_text(
            encoding="utf-8")
        assert "GPU 排名 数据库" in archived
        assert "漏记的验证搜索" in archived

    def test_run_evidence_log_prefers_run_dir(self, tmp_path):
        run_dir = tmp_path / "run_2026-01-01-000000"
        run_dir.mkdir()
        ev = run_dir / "evidence.jsonl"
        ev.write_text("x", encoding="utf-8")
        assert run_evidence_log(run_dir) == ev
        assert run_evidence_log(tmp_path) is None  # 非 run_ 目录不适用
        assert run_evidence_log(run_dir.parent / "run_empty") is None

    def test_evidence_hook_writes_run_scoped_evidence(self, tmp_path):
        run_dir = tmp_path / "outputs" / "run_2026-01-01-000000"
        run_dir.mkdir(parents=True)
        (run_dir / ".session_id").write_text("sess-run-ev", encoding="utf-8")
        payload = json.dumps(
            {"tool_name": "WebSearch", "tool_input": {"query": "测试"}}, ensure_ascii=False)
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID="sess-run-ev")
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "evidence_hook.py")],
            input=payload, capture_output=True, encoding="utf-8", timeout=30,
            cwd=str(tmp_path), env=env)
        assert result.returncode == 0
        assert (run_dir / "evidence.jsonl").exists()
        assert not (tmp_path / "outputs" / "search_log_sess-run-ev.jsonl").exists()


class TestFinalizeFold:
    """docs/04 存储改造：finalize 折叠（store+manifest → 等价 raw → 全链路）。"""

    FOLD_NODES = ["AI训练GPU", "图形渲染GPU", "服务器CPU"]

    def _manifest(self, run_dir: Path, knowledge=None, vendors=None):
        data = {"domain": "算力服务器", "nodes": self.FOLD_NODES, "model": "test-model",
                "knowledge": knowledge if knowledge is not None else [
                    {"name": "K1", "node": "AI训练GPU", "verified": True,
                     "category_path": "算力服务器-GPU服务器-AI训练GPU",
                     "source_type": "官方文档", "granularity": "合集级",
                     "url": "https://k.com/doc", "description": "kd", "reason": "kr"}]}
        if vendors is not None:
            data["vendors"] = vendors
        p = run_dir / "manifest.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return p

    def _source_row(self, node="AI训练GPU", name="A", url="https://a.com/doc",
                    category_path="算力服务器-GPU服务器-AI训练GPU", **kw):
        row = {"type": "source", "node": node, "name": name, "category_path": category_path,
               "source_type": "官方文档", "granularity": "合集级", "url": url,
               "description": "d", "reason": "r"}
        row.update(kw)
        return row

    def _search_row(self, node="AI训练GPU", query="GPU 排名 数据库", extracted=3, **kw):
        row = {"type": "search", "phase": "增量发现", "node": node, "query": query,
               "results": 10, "extracted": extracted}
        row.update(kw)
        return row

    def _knowledge_row(self, name, url, source_type="文档"):
        return {"type": "knowledge", "ts": "t", "node": "AI训练GPU", "name": name,
                "verified": True, "category_path": "算力服务器-GPU服务器-AI训练GPU",
                "source_type": source_type, "source_type_raw": source_type,
                "granularity": "合集级", "url": url, "description": "kd", "reason": "kr"}

    def _write_store(self, run_dir: Path, rows):
        p = run_dir / "store.jsonl"
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                     encoding="utf-8")
        return p

    def test_fold_equivalent_to_run(self, tmp_path):
        """相同输入：fold(store+manifest) 与 run_pipeline(raw.json) 的交付物逐文件一致。"""
        queries = ["GPU 排名 数据库"]
        sources = [{"name": "A", "category_path": "算力服务器-GPU服务器-AI训练GPU",
                    "source_type": "官方文档", "url": "https://a.com/doc",
                    "description": "d", "reason": "r"}]
        knowledge = [{"name": "K1", "node": "AI训练GPU", "verified": True,
                      "category_path": "算力服务器-GPU服务器-AI训练GPU",
                      "source_type": "官方文档", "url": "https://k.com/doc",
                      "description": "kd", "reason": "kr"}]
        evidence1 = write_evidence_queries(tmp_path, queries, sources + knowledge)
        evidence2_dir = tmp_path / "b"
        evidence2_dir.mkdir()
        evidence2 = write_evidence_queries(evidence2_dir, queries, sources + knowledge)

        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        self._manifest(run_dir)
        self._write_store(run_dir, [self._source_row(), self._search_row()])
        fold_summary = fold(str(run_dir), evidence_log=str(evidence1),
                            out_dir=str(tmp_path / "outputs"), now=FIXED_NOW,
                            enforce_quotas=False)  # 单条搜索：本测试钉等价性，护栏由专门测试覆盖

        raw = write_raw(tmp_path, {
            "domain": "算力服务器", "nodes": self.FOLD_NODES, "model": "test-model",
            "knowledge": knowledge,
            "journal": [{"phase": "增量发现", "node": "AI训练GPU",
                         "query": "GPU 排名 数据库", "results": 10, "extracted": 3}],
            "sources": [{"name": "A", "category_path": "算力服务器-GPU服务器-AI训练GPU",
                         "source_type": "官方文档", "url": "https://a.com/doc",
                         "description": "d", "reason": "r"}],
        })
        run_summary = run_pipeline(str(raw), out_dir=str(tmp_path / "outputs"),
                          evidence_log=str(evidence2), now=FIXED_NOW)

        assert fold_summary["kept"] == run_summary["kept"]
        # raw_input.json 已取消（2026-08-31）：fold 与 CLI 路径交付物逐字节等价即可
        for name in ["算力服务器_2026-08-13-183045_数据源清单.csv",
                     "intermediate/算力服务器_2026-08-13-183045_stats.csv",
                     "intermediate/算力服务器_2026-08-13-183045_搜索日志.csv",
                     "intermediate/算力服务器_2026-08-13-183045_溯源.csv"]:
            fold_file = Path(fold_summary["outdir"]) / name
            run_file = Path(run_summary["outdir"]) / name
            assert fold_file.read_text(encoding="utf-8-sig" if name.endswith(".csv") else "utf-8") \
                == run_file.read_text(encoding="utf-8-sig" if name.endswith(".csv") else "utf-8"), name

    def test_fold_merges_verified_knowledge_records_from_store(self, tmp_path):
        """2026-09-08 架构修订：清单核对结果存 store（type=knowledge），fold 从
        store 并入 verified 项——废除阶段 6 一次性转写 manifest（214051 实证漏写
        60 个 verified 字段的事故类别）。manifest 只承载阶段 0-2 声明态清单
        （name/node/预期体裁，无 verified 字段）。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        self._manifest(run_dir, knowledge=[
            {"name": "K1", "node": "AI训练GPU", "预期体裁": "官方文档"}])
        self._write_store(run_dir, [
            {"type": "knowledge", "ts": "2026-09-08 21:30:00", "node": "AI训练GPU",
             "name": "K1", "verified": True,
             "category_path": "算力服务器-GPU服务器-AI训练GPU",
             "source_type": "文档", "source_type_raw": "官方文档",
             "granularity": "合集级", "url": "https://k.com/doc",
             "description": "kd", "reason": "kr"},
            self._search_row(phase="验证搜索", query="K1 官网")])
        evidence = write_evidence(tmp_path, [{"url": "https://k.com/doc", "name": "K1"}])
        summary = fold(str(run_dir), evidence_log=str(evidence),
                       out_dir=str(tmp_path / "outputs"), now=FIXED_NOW)
        assert summary["kept"] == 1
        assert summary["list_verified"] == "1/1"
        assert not run_dir.exists()  # 正常重命名收尾

    def test_fold_unrecorded_declared_items_raise_with_names(self, tmp_path):
        """清单了结哨兵（2026-09-08 架构修订配套）：声明清单项在 store 中无核对
        记录 → 拒绝收尾并点名（防漏调 record_knowledge）。失败发生在重命名前，
        补录后可安全重跑——与防截断哨兵同构。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        self._manifest(run_dir, knowledge=[
            {"name": "K1", "node": "AI训练GPU", "预期体裁": "官方文档"},
            {"name": "K2", "node": "服务器CPU", "预期体裁": "行业标准"}])
        self._write_store(run_dir, [
            {"type": "knowledge", "ts": "t", "node": "AI训练GPU", "name": "K1",
             "verified": True,
             "category_path": "算力服务器-GPU服务器-AI训练GPU",
             "source_type": "文档", "source_type_raw": "官方文档",
             "granularity": "合集级", "url": "https://k.com/doc",
             "description": "kd", "reason": "kr"}])
        evidence = write_evidence(tmp_path, [{"url": "https://k.com/doc", "name": "K1"}])
        with pytest.raises(ValueError, match="清单项未了结"):
            fold(str(run_dir), evidence_log=str(evidence),
                 out_dir=str(tmp_path / "outputs"), now=FIXED_NOW)
        assert run_dir.exists()
        assert run_dir.name.startswith("run_")
        assert not (run_dir / "raw.json").exists()

    def test_fold_vendors_field_declared_and_checked(self, tmp_path):
        """2026-09-11 阶段 1（厂商官网）独立成段：manifest 两栏分装——knowledge 是
        阶段 3 待验证的清单，vendors 是阶段 1 的厂商清单。收尾对账合并检查两栏。
        防回潮：厂商项再混进 knowledge，会重新逼出"阶段 3 跳过厂商项"的补丁规则。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        self._manifest(run_dir,
                       knowledge=[{"name": "K1", "node": "AI训练GPU", "预期体裁": "官方文档"}],
                       vendors=[{"name": "Cisco 官网", "node": "AI训练GPU"},
                                {"name": "华为 官网", "node": "AI训练GPU"}])
        self._write_store(run_dir, [
            self._search_row(phase="验证搜索", query="K1 官网", extracted=0),
            self._knowledge_row("K1", "https://k.com/doc"),
            self._knowledge_row("Cisco 官网", "https://cisco.com/", source_type="官网")])
        evidence = write_evidence(tmp_path, [{"url": "https://k.com/doc", "name": "K1"},
                                             {"url": "https://cisco.com/", "name": "Cisco 官网"}])
        # 华为 官网 声明了但没录 → 拒绝收尾并点名
        with pytest.raises(ValueError, match="华为 官网"):
            fold(str(run_dir), evidence_log=str(evidence),
                 out_dir=str(tmp_path / "outputs"), now=FIXED_NOW)

    def test_fold_vendors_recorded_pass_and_merged(self, tmp_path):
        """vendors 栏全部录毕 → 正常收尾，厂商官网页并入最终清单（收尾时清单由
        store 核对记录重建，不依赖 manifest）。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        self._manifest(run_dir,
                       knowledge=[{"name": "K1", "node": "AI训练GPU", "预期体裁": "官方文档"}],
                       vendors=[{"name": "Cisco 官网", "node": "AI训练GPU"}])
        self._write_store(run_dir, [
            self._search_row(phase="验证搜索", query="K1 官网", extracted=0),
            self._knowledge_row("K1", "https://k.com/doc"),
            self._knowledge_row("Cisco 官网", "https://cisco.com/", source_type="官网")])
        evidence = write_evidence(tmp_path, [{"url": "https://k.com/doc", "name": "K1"},
                                             {"url": "https://cisco.com/", "name": "Cisco 官网"}])
        summary = fold(str(run_dir), evidence_log=str(evidence),
                       out_dir=str(tmp_path / "outputs"), now=FIXED_NOW)
        assert summary["list_verified"] == "2/2"
        assert summary["kept"] == 2

    def test_fold_manifest_carried_knowledge_still_works(self, tmp_path):
        """旧路径兜底：store 无 knowledge 记录、manifest 清单带核对字段
        （verified/url——2026-09-08 前的运行归档）→ 照旧按最终核对态并入，
        历史归档可复盘、迁移期新旧流程并存。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        self._manifest(run_dir)  # 默认清单：verified=true 带 url（最终核对态）
        self._write_store(run_dir, [self._search_row(phase="验证搜索", query="K1 官网")])
        evidence = write_evidence(tmp_path, [{"url": "https://k.com/doc", "name": "K1"}])
        summary = fold(str(run_dir), evidence_log=str(evidence),
                       out_dir=str(tmp_path / "outputs"), now=FIXED_NOW)
        assert summary["kept"] == 1
        assert summary["list_verified"] == "1/1"

    def test_fold_archives_store_manifest_and_cleans_root(self, tmp_path):
        """2026-09-01 实测 bug 钉进测试：fold 只归档并删除了 manifest，store.jsonl
        既未归档也未删除——交付目录根残留 store.jsonl（用户实测发现）。
        修复口径：store 归档进 intermediate/store_input.jsonl 且原件删除，
        交付目录根只留数据源清单（分析报告.md 由模型收尾时写入）。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        self._manifest(run_dir, knowledge=[])  # 空清单空 store：无来源无搜索（本测试只钉归档与清理行为）
        store_text = ""
        (run_dir / "store.jsonl").write_text(store_text, encoding="utf-8")
        summary = fold(str(run_dir), out_dir=str(tmp_path / "outputs"),
                       evidence_log=str(tmp_path / "不存在.jsonl"), now=FIXED_NOW)
        outdir = Path(summary["outdir"])
        root_files = {p.name for p in outdir.iterdir() if p.is_file()}
        # 2026-09-15（docs/06 第 2 期）：fold 路径在根写出「运行核对.json」
        assert root_files == {"算力服务器_2026-08-13-183045_数据源清单.csv", "运行核对.json"}
        assert (outdir / "intermediate" / "store_input.jsonl").read_text(encoding="utf-8") \
            == store_text
        assert (outdir / "intermediate" / "manifest_input.json").exists()
        assert not (outdir / "store.jsonl").exists()
        assert not (outdir / "manifest.json").exists()

    def test_fold_strips_source_type_raw_but_archives_it(self, tmp_path):
        """store 内部字段 source_type_raw 不漏进组装的 raw（交付物干净），
        但归档的 store_input.jsonl 保留原始词（审计零损失）。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        self._manifest(run_dir)
        row = self._source_row()
        row["source_type_raw"] = "厂商文档"
        self._write_store(run_dir, [row, self._search_row()])
        evidence = write_evidence_queries(tmp_path, ["GPU 排名 数据库"],
                                          [{"url": "https://a.com/doc", "name": "A"},
                                           {"url": "https://k.com/doc", "name": "K1"}])
        summary = fold(str(run_dir), evidence_log=str(evidence),
                       out_dir=str(tmp_path / "outputs"), now=FIXED_NOW,
                       enforce_quotas=False)  # 单条搜索：本测试钉归档行为，护栏由专门测试覆盖
        outdir = Path(summary["outdir"])
        archived = (outdir / "intermediate" / "store_input.jsonl").read_text(encoding="utf-8")
        assert "source_type_raw" in archived
        assert "厂商文档" in archived
        source_csv = next(outdir.glob("*数据源清单.csv"))
        assert "source_type_raw" not in source_csv.read_text(encoding="utf-8-sig")

    def test_sentinel_not_triggered_by_verification_only_run(self, tmp_path):
        """2026-09-01 钉进测试：哨兵只对增量/扩量轮的提取计数——
        纯清单验证运行（store 零来源、验证搜索 extracted>0 且清单有验证通过项）
        是合法结果，不得中止。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        self._manifest(run_dir)  # 默认含 verified 清单项 K1（url https://k.com/doc）
        self._write_store(run_dir, [self._search_row(phase="验证搜索", query="K1 官网")])
        evidence = write_evidence(tmp_path, [{"url": "https://k.com/doc", "name": "K1"}])
        summary = fold(str(run_dir), evidence_log=str(evidence),
                       out_dir=str(tmp_path / "outputs"), now=FIXED_NOW)
        assert summary["kept"] == 1  # 清单项并入
        assert not run_dir.exists()  # 正常重命名收尾

    def test_sentinel_empty_sources_with_extractions_raises(self, tmp_path):
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        self._manifest(run_dir, knowledge=[])
        self._write_store(run_dir, [self._search_row(extracted=3)])
        with pytest.raises(ValueError, match="来源未入库"):
            fold(str(run_dir), out_dir=str(tmp_path / "outputs"), now=FIXED_NOW)
        # 失败发生在重命名前：运行目录原样保留，可修正后重跑
        assert run_dir.exists()
        assert run_dir.name.startswith("run_")
        assert not (run_dir / "raw.json").exists()

    def test_manifest_missing_raises(self, tmp_path):
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        with pytest.raises(FileNotFoundError, match="manifest.json"):
            fold(str(run_dir), out_dir=str(tmp_path / "outputs"), now=FIXED_NOW)
        assert run_dir.exists() and run_dir.name.startswith("run_")

    def test_failure_before_rename_leaves_run_dir(self, tmp_path):
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        self._manifest(run_dir)
        self._write_store(run_dir, [self._source_row(), self._search_row(extracted=1)])
        with pytest.raises(FileNotFoundError, match="证据留痕"):
            fold(str(run_dir), evidence_log=str(tmp_path / "absent.jsonl"),
                 out_dir=str(tmp_path / "outputs"), now=FIXED_NOW)
        assert run_dir.exists() and run_dir.name.startswith("run_")


def write_evidence_map(tmp_path: Path, query_urls: dict[str, list[str]],
                       sources: list[dict] | None = None) -> Path:
    """构造证据留痕：按查询词各配模拟结果 URL（供零提取审计/护栏测试用）。"""
    p = tmp_path / "search_log.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for q, urls in query_urls.items():
            payload = {
                "tool_name": "WebSearch",
                "tool_input": {"query": q},
                "tool_response": {"results": [{"url": u} for u in urls]},
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


# 每节点增量搜索配额（钉桩：与 postprocess.MIN_INCREMENTAL_SEARCHES 对齐，改常量须同步）
# 2026-09-10：16 → 20（基底 16 + 扩充固定 4）
QUOTA_N = 20


class _SentinelFoldBase:
    """三个收尾护栏（2026-09-09）共用夹具：fold 路径（enforce_quotas 缺省开启）。"""

    NODES = ["AI训练GPU", "图形渲染GPU"]

    def _manifest(self, run_dir, knowledge=None):
        data = {"domain": "算力服务器", "nodes": self.NODES, "model": "test-model",
                "knowledge": knowledge if knowledge is not None else []}
        (run_dir / "manifest.json").write_text(json.dumps(data, ensure_ascii=False),
                                               encoding="utf-8")
        return run_dir / "manifest.json"

    def _source_row(self, node="AI训练GPU", name="A", url="https://a.com/doc"):
        return {"type": "source", "node": node, "name": name,
                "category_path": f"算力服务器-GPU服务器-{node}",
                "source_type": "官方文档", "granularity": "合集级",
                "url": url, "description": "d", "reason": "r"}

    def _search_row(self, node="AI训练GPU", query="q", extracted=0,
                    phase="增量发现", **kw):
        row = {"type": "search", "phase": phase, "node": node, "query": query,
               "results": 10, "extracted": extracted}
        row.update(kw)
        return row

    def _write_store(self, run_dir, rows):
        (run_dir / "store.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8")

    def _searches(self, node, n, base="q", **kw):
        return [self._search_row(node=node, query=f"{base}{i}", **kw)
                for i in range(n)]

    def _fold(self, tmp_path, run_dir, store_rows, query_urls, evidence_sources=()):
        """组装一次 fold 调用：manifest + store + 证据（查询词各配结果 URL）。"""
        self._manifest(run_dir)
        self._write_store(run_dir, store_rows)
        evidence = write_evidence_map(tmp_path, query_urls, evidence_sources)
        return fold(str(run_dir), evidence_log=str(evidence),
                    out_dir=str(tmp_path / "outputs"), now=FIXED_NOW)


class TestQuotaSentry(_SentinelFoldBase):
    """哨兵 1：每节点增量搜索 ≥QUOTA_N（20 = 基底 16 + 扩充固定 4）——配额缩水/谎报过不了收尾。"""

    def test_node_shortfall_raises_with_names(self, tmp_path):
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N, zero_reason="垃圾域") + \
            self._searches("图形渲染GPU", 8, base="g", zero_reason="垃圾域")
        with pytest.raises(ValueError, match="增量搜索未达标") as ei:
            self._fold(tmp_path, run_dir, rows, {}, evidence_sources=rows[:1])
        assert "图形渲染GPU 8 次（缺 12）" in str(ei.value)
        assert run_dir.exists() and run_dir.name.startswith("run_")

    def test_exact_quota_per_node_passes(self, tmp_path):
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N, zero_reason="垃圾域") + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", zero_reason="垃圾域")
        summary = self._fold(tmp_path, run_dir, rows, {}, evidence_sources=rows[:1])
        assert not run_dir.exists()  # 正常重命名收尾
        assert summary["zero_audit"]["total"] == QUOTA_N * 2

    def test_expansion_rows_do_not_rescue_shortfall(self, tmp_path):
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N - 2, zero_reason="垃圾域") + \
            self._searches("AI训练GPU", 4, base="x", phase="扩量轮", zero_reason="垃圾域")
        with pytest.raises(ValueError, match="AI训练GPU 18 次（缺 2）"):
            self._fold(tmp_path, run_dir, rows, {}, evidence_sources=rows[:1])
        assert run_dir.exists()

    def test_verification_only_run_not_quota_checked(self, tmp_path):
        """纯清单验证运行（零增量行）：与防截断哨兵豁免口径一致，不拦。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        knowledge = [{"name": "K1", "node": "AI训练GPU", "verified": True,
                      "category_path": "算力服务器-GPU服务器-AI训练GPU",
                      "source_type": "官方文档", "granularity": "合集级",
                      "url": "https://k.com/doc", "description": "kd", "reason": "kr"}]
        self._manifest(run_dir, knowledge=knowledge)
        self._write_store(run_dir, [self._search_row(phase="验证搜索", query="K1 官网")])
        evidence = write_evidence(tmp_path, [{"url": "https://k.com/doc", "name": "K1"}])
        fold(str(run_dir), evidence_log=str(evidence),
             out_dir=str(tmp_path / "outputs"), now=FIXED_NOW)
        assert not run_dir.exists()

    def test_cli_path_not_enforced(self, tmp_path):
        """CLI 兼容路径（enforce_quotas 缺省关闭）：历史轮次复盘重跑不受影响。"""
        data = base_data()
        data["journal"] = [{"phase": "增量发现", "node": "AI训练GPU",
                            "query": "GPU 排名", "results": 10, "extracted": 0}]
        raw = write_raw(tmp_path, data)
        ev = write_evidence_queries(tmp_path, ["GPU 排名"], data["sources"])
        summary = run_pipeline(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW,
                      evidence_log=str(ev))
        assert summary["kept"] == 2


class TestSingleDocZeroReport(_SentinelFoldBase):
    """待复核提示：零提取但结果含单篇详情页——**只进收尾输出、不拦截**（2026-09-14）。

    171459 轮实证：9 次专利搜索零提取，结果里 59 条是单篇专利页（Google Patents /
    国家知识产权局全文 / 万方专利页…），摘要里还列了 85 个专利公开号，模型写的理由
    是"结果均为专利检索聚合平台页…无机构发布的成体系载体"——与两者都矛盾。判据落在
    URL 形态这个客观事实上，但"已收/重复"的核实按域名粗查、跨平台重复会误判，误报
    代价是整轮交不出交付物，故先只报不拦。
    """

    def test_single_doc_zero_row_reported_not_blocked(self, tmp_path):
        from postprocess import summary_text
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N - 1, zero_reason="垃圾域") + \
            [self._search_row(query="q19", extracted=0,
                              zero_reason="结果均为专利检索聚合平台页，无成体系载体")] + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", zero_reason="垃圾域")
        query_urls = {f"q{i}": ["https://books.google.com/x"] for i in range(QUOTA_N)}
        query_urls.update({f"g{i}": ["https://books.google.com/x"] for i in range(QUOTA_N)})
        query_urls["q19"] = ["https://patents.google.com/patent/CN115412475A/en",
                             "https://www.patentguru.com/cn/inventor/x"]
        summary = self._fold(tmp_path, run_dir, rows, query_urls,
                             evidence_sources=rows[:1])
        assert not run_dir.exists()  # 不拦截：正常收尾
        assert summary["zero_audit"]["single_doc_zero"] == [("AI训练GPU", "q19")]
        txt = summary_text(summary)
        assert "待复核" in txt and "含单篇详情页 1 行" in txt

    def test_aggregator_only_zero_row_not_reported(self, tmp_path):
        """结果是纯聚合/检索页时不报——这条只针对单篇详情页形态。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N - 1, zero_reason="垃圾域") + \
            [self._search_row(query="q19", extracted=0, zero_reason="结果均为检索聚合页")] + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", zero_reason="垃圾域")
        query_urls = {f"q{i}": ["https://books.google.com/x"] for i in range(QUOTA_N)}
        query_urls.update({f"g{i}": ["https://books.google.com/x"] for i in range(QUOTA_N)})
        query_urls["q19"] = ["https://www.patentguru.com/cn/inventor/x",
                             "https://s.wanfangdata.com.cn/patent?q=x"]
        summary = self._fold(tmp_path, run_dir, rows, query_urls,
                             evidence_sources=rows[:1])
        assert summary["zero_audit"]["single_doc_zero"] == []


class TestZeroReasonSentry(_SentinelFoldBase):
    """哨兵 2：增量/扩量零提取必须带 zero_reason（拒收留痕可审计）。"""

    def test_zero_extraction_without_reason_raises(self, tmp_path):
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N - 1, zero_reason="垃圾域") + \
            [self._search_row(query="q19", extracted=0)] + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", zero_reason="垃圾域")
        with pytest.raises(ValueError, match="零提取留痕缺失或与证据矛盾") as ei:
            self._fold(tmp_path, run_dir, rows, {}, evidence_sources=rows[:1])
        assert "q19" in str(ei.value)
        assert run_dir.exists()

    def test_verification_search_zero_extraction_exempt(self, tmp_path):
        """验证搜索的 extracted 只记顺路新源——零提取是合法结果，不需要理由。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N, zero_reason="垃圾域") + \
            [self._search_row(phase="验证搜索", query="K1 官网", extracted=0)] + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", zero_reason="垃圾域")
        self._fold(tmp_path, run_dir, rows, {}, evidence_sources=rows[:1])
        assert not run_dir.exists()

    def test_positive_extraction_needs_no_reason(self, tmp_path):
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N, extracted=1) + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", extracted=1)
        self._fold(tmp_path, run_dir, rows, {}, evidence_sources=rows[:1])
        assert not run_dir.exists()

    def test_duplicate_row_latest_wins(self, tmp_path):
        """补录机制：同 phase+node+query 重传 = 末次覆盖（append-only 语义）。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N - 1, zero_reason="垃圾域") + \
            [self._search_row(query="q19", extracted=0),          # 缺理由
             self._search_row(query="q19", extracted=0, zero_reason="无主题边界")] + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", zero_reason="垃圾域")
        summary = self._fold(tmp_path, run_dir, rows, {}, evidence_sources=rows[:1])
        assert not run_dir.exists()
        assert summary["journal_count"] == QUOTA_N * 2  # 20+20，重复行合并为一条
        journal_csv = next((Path(summary["outdir"]) / "intermediate").glob("*搜索日志.csv"))
        csv_rows = read_csv_rows(journal_csv)
        assert len(csv_rows) == QUOTA_N * 2 + 1  # header + 40
        q19_rows = [r for r in csv_rows if r[2] == "q19"]
        assert len(q19_rows) == 1  # 重复行合并
        assert q19_rows[0][7] == "无主题边界"  # 末次覆盖后的理由进了 CSV


class TestZeroReasonContradiction(_SentinelFoldBase):
    """哨兵 3：zero_reason 与结果域名证据矛盾拦截（2026-09-11：分类计数与疑似漏收
    审计清单随之删除——清单是"人工复核"输出，已下线）。"""

    def test_reason_claims_collected_but_domains_not_in_list_raises(self, tmp_path):
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N, zero_reason="已收") + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", zero_reason="垃圾域")
        query_urls = {f"q{i}": [f"https://example.org/r{i}"] for i in range(QUOTA_N)}
        with pytest.raises(ValueError, match="理由声称已收") as ei:
            self._fold(tmp_path, run_dir, rows, query_urls, evidence_sources=rows[:1])
        assert "q0" in str(ei.value)
        assert run_dir.exists()

    def test_reason_claims_garbage_but_domains_clean_raises(self, tmp_path):
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N, zero_reason="垃圾域") + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", zero_reason="垃圾域")
        query_urls = {f"q{i}": [f"https://www.cisco.com/doc{i}"] for i in range(QUOTA_N)}
        with pytest.raises(ValueError, match="理由声称垃圾域"):
            self._fold(tmp_path, run_dir, rows, query_urls, evidence_sources=rows[:1])
        assert run_dir.exists()

    def test_consistent_reasons_pass_and_audit_reported(self, tmp_path):
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N, zero_reason="已收") + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", zero_reason="垃圾域")
        query_urls = {f"q{i}": ["https://a.com/doc"] for i in range(QUOTA_N)}
        query_urls.update({f"g{i}": ["https://books.google.com/x"] for i in range(QUOTA_N)})
        summary = self._fold(tmp_path, run_dir, rows, query_urls, evidence_sources=rows[:1])
        assert not run_dir.exists()
        from postprocess import summary_text
        assert "零提取审计" in summary_text(summary)

    def test_zero_reason_contradiction_not_flagged_when_clean(self, tmp_path):
        """理由与域名证据一致（无矛盾）时 violations 为空——哨兵 2/3 不误报。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N, zero_reason="已收") + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", zero_reason="垃圾域")
        query_urls = {f"q{i}": ["https://a.com/doc"] for i in range(QUOTA_N)}
        query_urls.update({f"g{i}": ["https://books.google.com/x"] for i in range(QUOTA_N)})
        summary = self._fold(tmp_path, run_dir, rows, query_urls, evidence_sources=rows[:1])
        assert summary["zero_audit"]["violations"] == []


class TestGarbageFilter(_SentinelFoldBase):
    """2026-09-10 收录政策收紧：收尾过滤垃圾域/低价值聚合平台（与入库即拒双层——
    历史数据与 CLI 路径不经入库闸门，收尾兜底）。"""

    def test_fold_filters_garbage_sources_and_reports(self, tmp_path):
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row(),
                self._source_row(name="B", url="https://shuma.taobao.com/item/1")] + \
            self._searches("AI训练GPU", QUOTA_N, extracted=1) + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", extracted=1)
        self._manifest(run_dir)
        self._write_store(run_dir, rows)
        evidence = write_evidence_map(tmp_path, {},
                                      [{"url": "https://a.com/doc", "name": "A"}])
        summary = fold(str(run_dir), evidence_log=str(evidence),
                       out_dir=str(tmp_path / "outputs"), now=FIXED_NOW)
        assert summary["kept"] == 1
        assert summary["garbage_filtered"] == 1
        from postprocess import summary_text
        assert "垃圾域过滤移除: 1 条" in summary_text(summary)

    def test_all_garbage_candidates_do_not_trigger_search_failure(self, tmp_path):
        """过滤后零候选 ≠ 搜索工具异常——失败路径按过滤前计数判断（政策过滤是合法
        结果，不得误报"疑似搜索工具异常"）。"""
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row(url="https://shuma.taobao.com/item/1")] + \
            self._searches("AI训练GPU", QUOTA_N, extracted=1) + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", extracted=1)
        self._manifest(run_dir)
        self._write_store(run_dir, rows)
        evidence = write_evidence_map(tmp_path, {})
        summary = fold(str(run_dir), evidence_log=str(evidence),
                       out_dir=str(tmp_path / "outputs"), now=FIXED_NOW)
        assert summary["kept"] == 0
        assert summary["garbage_filtered"] == 1

class TestRunAttestation(_SentinelFoldBase):
    """2026-09-15（docs/06 第 2 期）：收尾在运行目录根写出「运行核对.json」——
    由脚本从 store 与流水线 summary 直接算出，不经过模型；只记录事实、不做拦截。"""

    def _fold_clean(self, tmp_path):
        run_dir = Path(prepare_run_dir(tmp_path / "outputs", now=FIXED_NOW))
        rows = [self._source_row()] + \
            self._searches("AI训练GPU", QUOTA_N, zero_reason="已收") + \
            self._searches("图形渲染GPU", QUOTA_N, base="g", zero_reason="垃圾域")
        query_urls = {f"q{i}": ["https://a.com/doc"] for i in range(QUOTA_N)}
        query_urls.update({f"g{i}": ["https://books.google.com/x"] for i in range(QUOTA_N)})
        return self._fold(tmp_path, run_dir, rows, query_urls, evidence_sources=rows[:1])

    def test_attestation_written_with_facts(self, tmp_path):
        summary = self._fold_clean(tmp_path)
        outdir = Path(summary["outdir"])
        path = outdir / "运行核对.json"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["domain"] == "算力服务器"
        assert data["sources"]["kept"] == summary["kept"]
        assert data["quota"]["required_per_node"] == QUOTA_N
        assert data["quota"]["per_node"] == {"AI训练GPU": QUOTA_N, "图形渲染GPU": QUOTA_N}
        assert data["zero_extraction"]["count"] == 2 * QUOTA_N
        assert {r["zero_reason"] for r in data["zero_extraction"]["rows"]} == {"已收", "垃圾域"}
        assert data["knowledge"]["declared"] == 0
        assert data["knowledge"]["missing"] == []
        assert len(data["script_fingerprint"]) == 12
        assert data["generated_at"]

    def test_kept_matches_deliverable_rows(self, tmp_path):
        """核对文件上的数字必须与交付物对得上——这是"不用读报告就能判断"的前提。"""
        summary = self._fold_clean(tmp_path)
        outdir = Path(summary["outdir"])
        data = json.loads((outdir / "运行核对.json").read_text(encoding="utf-8"))
        csv_path = next(outdir.glob("*_数据源清单.csv"))
        assert data["sources"]["kept"] == len(read_csv_rows(csv_path)) - 1

    def test_attestation_failure_does_not_block_delivery(self, tmp_path, monkeypatch):
        """自证工具不得成为新的报废来源：写不出来也要照常出交付物。"""
        import postprocess as pp

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(pp, "_write_run_attestation", _boom)
        summary = self._fold_clean(tmp_path)
        outdir = Path(summary["outdir"])
        assert not (outdir / "运行核对.json").exists()
        assert list(outdir.glob("*_数据源清单.csv"))
