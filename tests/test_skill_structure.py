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


def test_skill_uses_unified_granularity_terms():
    """清单条目粒度术语统一为 合集级/单篇级（2026-08-21 决策 + 2026-08-24 统一），
    不再使用"体系级"这一遗留叫法。"""
    assert "体系级" not in SKILL_MD
    assert "合集级" in SKILL_MD


def test_skill_has_no_phantom_t_filter():
    """-t 类型过滤参数已删除（2026-08-24 审查）：description 与参数表不得再宣传。
    按参数形态断言（-t "…"），避免误伤 allowed-tools 等含 "-t" 子串的正常词。"""
    assert "-t \"" not in SKILL_MD
    assert "类型过滤" not in SKILL_MD


def test_skill_frontmatter_tools_no_agent():
    """frontmatter allowed-tools 为运行所需最小集，不含未被流程使用的 Agent。"""
    frontmatter = SKILL_MD.split("---")[1]
    assert "allowed-tools" in frontmatter
    assert "Agent" not in frontmatter


def test_incremental_search_count_per_node():
    """增量发现每节点 8 次（2026-08-24 起：固定 4 + 自由 4——专家反馈题材覆盖偏窄，以加量换广度）。"""
    assert "每节点 **8 次**搜索" in SKILL_MD
    assert "**自由 4 次**" in SKILL_MD


def test_prepare_run_dir_contract():
    """运行目录契约：--prepare 预留唯一目录，阶段 5 写其中 raw.json，阶段 6 同一路径收尾。"""
    assert "postprocess.py --prepare" in SKILL_MD
    assert "<运行目录>/raw.json" in SKILL_MD
    assert "outputs/raw.json" not in SKILL_MD  # 固定路径契约已废止


def test_source_role_dimension_and_coverage_check():
    """来源角色维度 + 覆盖评估：角度池含来源角色类，薄弱判定含来源维度单一。"""
    assert "来源角色" in SKILL_MD
    assert "来源维度单一" in SKILL_MD


def test_verification_query_component_pool():
    """验证搜索组件池：自由组合（每词 2-4 组件），来源识别必带，旧固定模板废止。"""
    assert "组件池" in SKILL_MD
    assert "来源识别（必带其一）" in SKILL_MD
    assert "{机构/体系名} 官方文档 / 官网" not in SKILL_MD


def test_no_cost_driven_trimming():
    """防模型自砍搜索次数：无"代价"成本措辞，两处决策点写明不以搜索成本缩减/合并。"""
    assert "列多列杂的代价" not in SKILL_MD
    assert "无需以搜索成本为由缩减清单" in SKILL_MD
    assert "不以搜索次数或运行时长为由合并节点" in SKILL_MD


def test_settings_deny_outputs_read():
    """防历史自锚定：deny Read(outputs/**) + Glob(outputs*)（转录实证的两条通道）。"""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    deny = settings["permissions"].get("deny", [])
    assert "Read(outputs/**)" in deny
    assert "Glob(outputs*)" in deny


def test_skill_no_history_output_reading():
    """阶段 0 禁止读取历史运行产物（解释式提示词，与 deny 规则互补）。"""
    assert "禁止读取 outputs/ 下历史运行的产物" in SKILL_MD


def test_finish_writes_domain_analysis_report():
    """收尾最后一步：写领域分析报告（第三交付物），模板在 references/。"""
    assert "分析报告.md" in SKILL_MD
    assert "领域分析报告" in SKILL_MD
    assert (SKILL_DIR / "references" / "分析报告模板.md").is_file()


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
