"""postprocess.py 的单元测试。"""
import csv
import io
import json
import sys
from datetime import datetime
from pathlib import Path

# 将 .claude/skills/autosource 加入 path 以便导入
SKILL_DIR = Path(__file__).parent.parent / ".claude" / "skills" / "autosource"
sys.path.insert(0, str(SKILL_DIR))

from postprocess import deduplicate, leaf_node, run, sanitize_domain

FIXED_NOW = datetime(2026, 8, 13, 18, 30, 45)
BOM = b"\xef\xbb\xbf"


def read_csv_rows(path: Path) -> list[list[str]]:
    """用 csv.reader 读回 CSV（StringIO 保证引号内换行被正确解析为同一字段）。"""
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))


def write_raw(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "raw.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
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


class TestRun:
    def test_full_pipeline(self, tmp_path):
        data = base_data()
        # 加一条重复 + 一条缺 url 的坏记录 + 一条缺 name 的坏记录
        data["sources"].append(dict(data["sources"][0]))
        data["sources"].append({"name": "X", "category_path": "算力服务器-服务器CPU"})
        data["sources"].append({"url": "https://y.com", "category_path": "算力服务器-服务器CPU"})
        raw = write_raw(tmp_path, data)

        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW)

        outdir = tmp_path / "out" / "算力服务器_2026-08-13-183045"
        assert summary["outdir"] == str(outdir)
        assert summary["total_found"] == 3
        assert summary["removed_duplicates"] == 1
        assert summary["kept"] == 2
        assert summary["invalid"] == 2
        assert summary["empty_nodes"] == ["服务器CPU"]

        # raw.json 默认删除
        assert not raw.exists()

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
        assert by_node["AI训练GPU"][9] == "test-model"
        assert float(by_node["AI训练GPU"][10]) >= 0  # 脚本处理耗时

    def test_keep_raw(self, tmp_path):
        raw = write_raw(tmp_path, base_data())
        run(str(raw), out_dir=str(tmp_path / "out"), keep_raw=True, now=FIXED_NOW)
        assert raw.exists()

    def test_outdir_collision_gets_suffix(self, tmp_path):
        raw1 = write_raw(tmp_path, base_data())
        run(str(raw1), out_dir=str(tmp_path / "out"), now=FIXED_NOW)
        raw2 = write_raw(tmp_path, base_data())
        summary = run(str(raw2), out_dir=str(tmp_path / "out"), now=FIXED_NOW)
        assert summary["outdir"].endswith("算力服务器_2026-08-13-183045_1")

    def test_csv_escaping(self, tmp_path):
        data = base_data()
        data["sources"][0]["name"] = '名称,含"逗号"和引号'
        data["sources"][0]["description"] = "多行\n描述"
        raw = write_raw(tmp_path, data)
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW)

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
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW)

        assert summary["unmatched"] == 1
        source_csv = next((Path(summary["outdir"])).glob("*数据源清单.csv"))
        rows = read_csv_rows(source_csv)
        assert len(rows) == 4  # header + 3 条（未匹配的仍收录）

    def test_all_nodes_empty(self, tmp_path):
        data = base_data()
        data["sources"] = []
        raw = write_raw(tmp_path, data)
        summary = run(str(raw), out_dir=str(tmp_path / "out"), now=FIXED_NOW)

        assert summary["kept"] == 0
        assert set(summary["empty_nodes"]) == set(data["nodes"])
        stats_csv = next((Path(summary["outdir"])).glob("*stats.csv"))
        rows = read_csv_rows(stats_csv)
        assert len(rows) == 4  # header + 3 个空节点行
