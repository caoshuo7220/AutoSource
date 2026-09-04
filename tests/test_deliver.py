"""deliver.py 的单元测试：交付去重、CSV 导出、统计、搜索日志、溯源与报告注入。

格式契约以实现规格第七章为准：数据源清单 7 列（UTF-8 BOM）、stats 长表 + 总计、
搜索日志每搜一行、溯源正查/反查、报告"数据总览"由脚本注入。
"""
import csv
import io
import json
from pathlib import Path

from deliver import (DEGRADED_REPORT_BODY, LINEAGE_CSV_HEADER,
                     SEARCH_LOG_CSV_HEADER, SOURCE_CSV_HEADER, STATS_CSV_HEADER,
                     build_lineage, compose_report, compute_stats,
                     deduplicate_delivery, report_stats_block, write_lineage_csv,
                     write_search_log_csv, write_source_csv, write_stats_csv)

TREE = [
    {"name": "交换机", "parent": "", "terms": [], "entities": [],
     "dims": [], "angles": []},
    {"name": "数据中心交换机", "parent": "交换机", "terms": [], "entities": [],
     "dims": [], "angles": []},
    {"name": "园区交换机", "parent": "交换机", "terms": [], "entities": [],
     "dims": [], "angles": []},
]
LEAVES = ["数据中心交换机", "园区交换机"]


def src(name="SONiC 文档", url="https://sonic-net.github.io/SONiC/",
        node="数据中心交换机", source_type="官方文档", granularity="合集级",
        batch=1, query="SONiC documentation"):
    return {"name": name, "url": url, "source_type": source_type,
            "granularity": granularity, "node": node, "description": "说明",
            "first_seen_batch": batch, "first_seen_query": query}


def read_csv_rows(path: Path) -> list[list[str]]:
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))


# ---------- 交付去重（实现规格第四章：域名+名称，合集级优先，最早批次） ----------

def test_deduplicate_delivery_same_domain_name_keeps_one():
    kept = deduplicate_delivery([
        src(), src(name="SONiC 文档", url="https://sonic-net.github.io/SONiC/#3"),
    ])
    assert len(kept) == 1


def test_deduplicate_delivery_prefers_collection_level():
    """同一域名+名称：合集级优先于单篇级（与 first_seen 次序无关）。"""
    kept = deduplicate_delivery([
        src(granularity="单篇级"),
        src(granularity="合集级"),
    ])
    assert len(kept) == 1
    assert kept[0]["granularity"] == "合集级"


def test_deduplicate_delivery_earliest_batch_tiebreak():
    """同粒度并列：取 first_seen_batch 最早者。"""
    kept = deduplicate_delivery([src(batch=5), src(batch=2)])
    assert kept[0]["first_seen_batch"] == 2


def test_deduplicate_delivery_keeps_mirror_domains():
    """镜像站（不同域名）各自保留。"""
    kept = deduplicate_delivery([
        src(), src(url="https://mirror.example/SONiC/"),
    ])
    assert len(kept) == 2


def test_deduplicate_delivery_stable_first_appearance_order():
    kept = deduplicate_delivery([
        src(name="B 源"), src(name="A 源"),
    ])
    assert [s["name"] for s in kept] == ["B 源", "A 源"]


# ---------- 数据源清单 CSV（7 列） ----------

def test_write_source_csv_header_and_bom(tmp_path):
    path = tmp_path / "清单.csv"
    write_source_csv(path, [src()], TREE)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM，Excel 直接打开
    rows = read_csv_rows(path)
    assert rows[0] == ["数据源名称", "分类路径", "数据源类型", "粒度",
                       "访问地址", "简要说明", "来源搜索"]
    assert len(rows) == 2
    assert rows[1] == ["SONiC 文档", "交换机-数据中心交换机", "官方文档", "合集级",
                       "https://sonic-net.github.io/SONiC/", "说明", "SONiC documentation"]


def test_write_source_csv_strips_citation_anchor(tmp_path):
    path = tmp_path / "清单.csv"
    write_source_csv(path, [src(url="https://a.example/x.pdf#3#1")], TREE)
    assert read_csv_rows(path)[1][4] == "https://a.example/x.pdf"


def test_write_source_csv_keeps_word_anchor(tmp_path):
    """单词锚点保留（#content 是页面锚点而非引用记号）。"""
    path = tmp_path / "清单.csv"
    write_source_csv(path, [src(url="https://a.example/x#content")], TREE)
    assert read_csv_rows(path)[1][4] == "https://a.example/x#content"


# ---------- stats.csv ----------

def test_compute_stats_per_leaf_and_total():
    summary = compute_stats([src(), src(name="园区源", node="园区交换机",
                                         source_type="行业标准")], LEAVES)
    assert summary["total"] == 2
    assert summary["per_node"]["数据中心交换机"]["count"] == 1
    assert summary["per_node"]["园区交换机"]["types"] == {"行业标准": 1}
    assert summary["total_types"] == {"官方文档": 1, "行业标准": 1}


def test_write_stats_csv_layout(tmp_path):
    path = tmp_path / "stats.csv"
    summary = compute_stats([src()], LEAVES)
    write_stats_csv(path, summary)
    rows = read_csv_rows(path)
    assert rows[0] == STATS_CSV_HEADER
    assert rows[-1][0] == "总计"
    assert rows[-1][1] == "1"
    assert "官方文档:1" in rows[-1][2]


# ---------- 搜索日志.csv ----------

def test_write_search_log_csv(tmp_path):
    path = tmp_path / "搜索日志.csv"
    write_search_log_csv(path, [
        {"batch": 3, "query": "SONiC documentation", "node": "数据中心交换机",
         "result_count": 10, "extracted": 5, "new_count": 2, "failed": 0},
    ])
    rows = read_csv_rows(path)
    assert rows[0] == SEARCH_LOG_CSV_HEADER
    assert rows[1] == ["3", "数据中心交换机", "SONiC documentation", "10", "5", "2", "0"]


# ---------- 溯源.csv ----------

def _raw_dir(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "batch_1.json").write_text(json.dumps([
        {"query_id": 1, "query": "SONiC documentation", "node": "数据中心交换机",
         "results": [{"title": "SONiC", "url": "https://sonic-net.github.io/SONiC/", "snippet": "..."},
                     {"title": "other", "url": "https://other.example/x", "snippet": ""}],
         "failed": False, "attempts": 1},
    ], ensure_ascii=False), encoding="utf-8")
    (raw / "batch_2.json").write_text(json.dumps([
        {"query_id": 1, "query": "SONiC docs", "node": "数据中心交换机",
         "results": [{"title": "t", "url": "https://a.example/", "snippet": "参见 https://snippet.example/y"}],
         "failed": False, "attempts": 1},
    ], ensure_ascii=False), encoding="utf-8")
    return raw


def test_build_lineage_forward_and_reverse_lookup(tmp_path):
    """正查（结果是否收录）与反查（条目出自哪个查询）同一张表承载。"""
    delivered = [src(), src(name="摘要源", url="https://snippet.example/y")]
    rows = build_lineage({"sources": delivered}, delivered, _raw_dir(tmp_path))
    by_url = {row[3]: row for row in rows}
    assert by_url["https://sonic-net.github.io/SONiC/"][4:6] == ["是", "SONiC 文档"]
    assert by_url["https://other.example/x"][4:6] == ["否", ""]
    assert by_url["https://snippet.example/y"][4:6] == ["是", "摘要源"]
    assert by_url["https://snippet.example/y"][6] == "摘要文本提及"


def test_write_lineage_csv(tmp_path):
    path = tmp_path / "溯源.csv"
    write_lineage_csv(path, [[1, "n", "q", "https://a/x", "是", "条目", ""]])
    rows = read_csv_rows(path)
    assert rows[0] == LINEAGE_CSV_HEADER
    assert rows[1][:6] == ["1", "n", "q", "https://a/x", "是", "条目"]


# ---------- 报告 ----------

def test_report_stats_block_counts(tmp_path):
    block = report_stats_block([src()], LEAVES)
    assert block.startswith("## 数据总览")
    assert "数据源总数：1 条（合集级 1 / 单篇级 0）" in block
    assert "官方文档:1" in block


def test_compose_report_layout():
    text = compose_report("交换机", "## 一、领域概览\n正文", "## 数据总览\n\n- 数据源总数：1 条\n")
    assert text.startswith("# 交换机 领域分析报告")
    assert text.index("## 数据总览") < text.index("## 一、领域概览")
    assert "正文" in text


def test_degraded_report_body_has_six_sections():
    """报告降级正文：六板块标题齐全，每节注明生成失败。"""
    for heading in ("领域概览", "技术格局", "产业生态", "标准", "对比", "趋势"):
        assert heading in DEGRADED_REPORT_BODY
    assert "生成失败" in DEGRADED_REPORT_BODY
