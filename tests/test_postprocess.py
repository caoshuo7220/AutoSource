"""postprocess.py 的单元测试。"""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
import sys

# 将 .claude/skills/autosource 加入 path 以便导入
SKILL_DIR = Path(__file__).parent.parent / ".claude" / "skills" / "autosource"
sys.path.insert(0, str(SKILL_DIR))

from postprocess import deduplicate, check_url, process


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


class TestCheckUrl:
    def test_valid_url(self, mocker):
        mock_head = mocker.patch("requests.head")
        mock_head.return_value.status_code = 200
        assert check_url("https://example.com") is True

    def test_head_fails_get_succeeds(self, mocker):
        import requests as req_mod
        mock_head = mocker.patch("requests.head")
        mock_head.side_effect = req_mod.ConnectionError()
        mock_get = mocker.patch("requests.get")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        mock_get.return_value.raise_for_status = lambda: None

        assert check_url("https://example.com") is True

    def test_both_fail(self, mocker):
        import requests as req_mod
        mock_head = mocker.patch("requests.head")
        mock_head.side_effect = req_mod.ConnectionError()
        mock_get = mocker.patch("requests.get")
        mock_get.side_effect = req_mod.ConnectionError()

        assert check_url("https://dead.com") is False

    def test_first_attempt_fails_retry_succeeds(self, mocker):
        """HEAD+GET both fail on first iteration, HEAD succeeds on retry."""
        import requests as req_mod
        mock_head = mocker.patch("requests.head")
        # First call: HEAD fails; second call (retry): HEAD succeeds
        mock_head.side_effect = [req_mod.ConnectionError(), MagicMock(status_code=200)]
        mock_get = mocker.patch("requests.get")
        # GET is only called when HEAD fails (first iteration)
        mock_get.side_effect = req_mod.ConnectionError()

        assert check_url("https://flaky.com") is True
        # HEAD called twice (fail + retry), GET called once (only on first fail)
        assert mock_head.call_count == 2
        assert mock_get.call_count == 1


class TestProcess:
    def test_full_pipeline(self, mocker):
        mocker.patch("postprocess.check_url", return_value=True)
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
            assert stats["valid"] == 2
            assert stats["invalid"] == 0
            assert stats["valid_ratio"] == 1.0
        finally:
            in_path.unlink(missing_ok=True)
            out_path.unlink(missing_ok=True)
