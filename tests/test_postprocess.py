"""postprocess.py 的单元测试。"""
import json
import tempfile
from pathlib import Path
import sys

# 将 .claude/skills/autosource 加入 path 以便导入
SKILL_DIR = Path(__file__).parent.parent / ".claude" / "skills" / "autosource"
sys.path.insert(0, str(SKILL_DIR))

from postprocess import deduplicate, process


class TestDeduplicate:
    def test_no_duplicates(self):
        sources = [
            {"name": "A", "url": "https://a.com/page1"},
            {"name": "B", "url": "https://b.com/page1"},
        ]
        result = deduplicate(sources)
        assert len(result) == 2

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
        result = deduplicate(sources)
        assert len(result) == 2

    def test_different_name_same_domain_kept(self):
        sources = [
            {"name": "A", "url": "https://a.com/1"},
            {"name": "B", "url": "https://a.com/2"},
        ]
        result = deduplicate(sources)
        assert len(result) == 2


class TestProcess:
    def test_full_pipeline(self):
        input_data = [
            {"name": "A", "category_path": "a-b", "source_type": "数据集",
             "url": "https://a.com", "description": "d"},
            {"name": "A", "category_path": "a-b", "source_type": "数据集",
             "url": "https://a.com/page2", "description": "duplicate"},
            {"name": "B", "category_path": "c-d", "source_type": "产品文档",
             "url": "https://b.com", "description": "d2"},
        ]
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump(input_data, f)
            in_path = Path(f.name)

        out_path = in_path.parent / "output.json"
        try:
            stats = process(str(in_path), str(out_path))

            result = json.loads(out_path.read_text(encoding="utf-8"))
            assert len(result) == 2  # dedup removed 1
            assert stats["total"] == 3
            assert stats["removed_duplicates"] == 1
            assert stats["kept"] == 2
        finally:
            in_path.unlink(missing_ok=True)
            out_path.unlink(missing_ok=True)
