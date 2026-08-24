"""skill 结构约定的钉桩测试：单 skill 合并、脚本归位 scripts/、路径引用一致。"""
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
SKILL_DIR = ROOT / ".claude" / "skills" / "autosource"
SCRIPTS_DIR = SKILL_DIR / "scripts"
SKILL_MD = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_skill_anatomy_single_skill_with_scripts():
    """标准 skill 目录：SKILL.md 在根、脚本在 scripts/（不散在根目录）。"""
    assert (SKILL_DIR / "SKILL.md").is_file()
    assert (SCRIPTS_DIR / "postprocess.py").is_file()
    assert (SCRIPTS_DIR / "log_tool.py").is_file()


def test_no_stale_autosource_search_skill():
    """合并后 autosource-search 目录与权限项均不残留。"""
    assert not (ROOT / ".claude" / "skills" / "autosource-search").exists()
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert not any("autosource-search" in entry for entry in settings["permissions"]["allow"])


def test_merged_skill_has_linear_phases():
    """合并后的线性流程：阶段 0-6 齐全，收尾为最后阶段（无中间停靠点）。"""
    for i in range(7):
        assert f"## 阶段 {i} ·" in SKILL_MD
    assert SKILL_MD.rindex("## 阶段 6 · 收尾") > SKILL_MD.rindex("## 阶段 5 ·")


def test_skill_references_only_scripts_paths():
    """SKILL.md 只引用 scripts/ 下的脚本路径，不残留旧路径、不引用已删除的搜索层。"""
    assert ".claude/skills/autosource/scripts/postprocess.py" in SKILL_MD
    assert ".claude/skills/autosource/postprocess.py" not in SKILL_MD
    assert "/autosource-search" not in SKILL_MD


def test_settings_reference_existing_scripts():
    """settings.json 的 hook 命令与 postprocess 权限规则指向真实存在的脚本文件。"""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    for entry in settings["permissions"]["allow"]:
        if "postprocess.py" in entry:
            assert "scripts/postprocess.py" in entry
    for rule in settings["hooks"]["PostToolUse"]:
        for hook in rule["hooks"]:
            cmd = hook["command"]
            if "log_tool.py" in cmd:
                assert "scripts/log_tool.py" in cmd
                log_tool = cmd.split("python ", 1)[1].split(" outputs/", 1)[0]
                assert (ROOT / log_tool).is_file()
