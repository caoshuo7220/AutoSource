"""2.0 结构契约纳入测试：SKILL.md 编排契约、settings.json 权限白名单、
1.0 文件处置清单、配置样例与依赖清单。

实现规格第三章处置清单与第四章交接接口的自动化核对——文件删除/复用/重写
一旦漂移即失败。
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
SKILL_DIR = ROOT / ".claude" / "skills" / "autosource"
SCRIPTS_DIR = SKILL_DIR / "scripts"
SKILL_MD = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


# ---------- 1.0 文件处置清单（实现规格第三章） ----------

def test_deleted_mcp_and_hook_files_absent():
    """MCP 架构与 hook 留痕整体废弃：相关文件不得残留。"""
    assert not (ROOT / ".mcp.json").exists()
    for name in ("mcp_server.py", "evidence_hook.py"):
        assert not (SCRIPTS_DIR / name).exists()
    assert not (ROOT / "tests" / "test_mcp_server.py").exists()


def test_deleted_old_module_names_absent():
    """旧模块名（postprocess/store/lineage/report）不再作为独立文件存在。"""
    for name in ("postprocess.py", "store.py", "lineage.py", "report.py"):
        assert not (SCRIPTS_DIR / name).exists()


def test_reused_files_preserved():
    """复用（保留不改）：evidence.py 与报告模板原位存在，且含核心算法标记。"""
    evidence = (SCRIPTS_DIR / "evidence.py").read_text(encoding="utf-8")
    assert "URL_CHARS" in evidence
    assert "check_grounded" in evidence
    assert "strip_citation_anchors" in evidence
    template = (SKILL_DIR / "references" / "分析报告模板.md").read_text(encoding="utf-8")
    assert "数据总览" in template
    assert "六" in template or "趋势" in template


def test_2_0_modules_present():
    """实现规格第三章目录树：六个新模块 + 配置样例 + 依赖清单齐全。"""
    for name in ("orchestrator.py", "state.py", "converge.py", "search_provider.py",
                 "llm_client.py", "prompts.py", "deliver.py"):
        assert (SCRIPTS_DIR / name).is_file(), f"缺 {name}"
    assert (SKILL_DIR / "config.example.json").is_file()
    assert (SKILL_DIR / "requirements.txt").is_file()


def test_deleted_1_0_docs_absent():
    """1.0 历史文档已删除（封盘于 master/tag v1.0）：docs/ 下不得残留 0X- 编号文档；
    README 只是重写，仍须存在。"""
    for index in range(1, 6):
        assert not list((ROOT / "docs").glob(f"0{index}-*.md")), \
            f"1.0 历史文档残留: 0{index}-*.md"
    assert (ROOT / "README.md").is_file()


# ---------- SKILL.md 编排契约（实现规格第四章循环协议） ----------

def test_skill_frontmatter_2_0_tools():
    """allowed-tools 为 2.0 所需最小集：无 MCP 工具、无 Agent。"""
    frontmatter = SKILL_MD.split("---")[1]
    assert "allowed-tools" in frontmatter
    assert "mcp__" not in frontmatter
    assert "Agent" not in frontmatter
    for tool in ("WebSearch", "Read", "Write", "Edit", "Bash"):
        assert tool in frontmatter


def test_skill_contains_five_subcommands():
    """循环协议的全部子命令与循环步骤在 SKILL.md 中。"""
    for cmd in ("--init", "--plan", "--commit", "--review", "--finalize"):
        assert cmd in SKILL_MD
    assert "orchestrator.py" in SKILL_MD
    assert "converged" in SKILL_MD


def test_skill_documents_search_results_format():
    """search_results.json 契约（由流程写入、脚本读取）逐字段在案。"""
    for field in ("query_id", "results", "failed", "attempts"):
        assert field in SKILL_MD
    assert "search_results.json" in SKILL_MD


def test_skill_documents_recovery_table():
    """中断恢复表（实现规格第八章）与恢复入口（显式 run_dir，不扫描 outputs/）。"""
    assert "中断恢复" in SKILL_MD
    assert "pending_batch" in SKILL_MD
    assert "不扫描 outputs/" in SKILL_MD


def test_skill_documents_failure_declaration():
    """失败声明契约：如实报告"本次运行失败"，不调用 --finalize。"""
    assert "本次运行失败" in SKILL_MD
    assert "不调用 --finalize" in SKILL_MD
    assert "熔断" in SKILL_MD and "未收敛" in SKILL_MD


def test_skill_documents_evidence_rules():
    """证据链纪律：逐字一致、边界匹配、锚点剥离。"""
    assert "逐字" in SKILL_MD
    assert "边界匹配" in SKILL_MD
    assert "#数字" in SKILL_MD


def test_skill_no_1_0_linear_flow():
    """1.0 线性流程与 MCP/hook 架构术语不得残留。"""
    for stale in ("阶段 0", "阶段 6", "record_sources", "record_search",
                  "store.jsonl", "manifest.json", "finalize 工具", "hook",
                  "--prepare", "--rename-report", "PostToolUse"):
        assert stale not in SKILL_MD


def test_skill_config_setup_documented():
    """运行前提：config.example.json → config.json 的复制与参数说明在案。"""
    assert "config.example.json" in SKILL_MD
    assert "config.json" in SKILL_MD


# ---------- settings.json 权限白名单（实现规格第三章重写） ----------

def test_settings_no_mcp_no_hooks():
    """2.0 白名单：无 MCP 工具条目、无 hooks 配置。"""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "hooks" not in settings
    assert not any("mcp__" in entry for entry in settings["permissions"]["allow"])


def test_settings_2_0_allowlist():
    """2.0 权限白名单：websearch、orchestrator Bash、运行目录读写、skill 调用。"""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    allow = settings["permissions"]["allow"]
    assert "WebSearch" in allow
    assert "Bash(python .claude/skills/autosource/scripts/orchestrator.py *)" in allow
    assert "Read(outputs/**)" in allow
    assert "Write(outputs/**)" in allow
    assert "Edit(outputs/**)" in allow
    assert "Skill(autosource)" in allow
    # 1.0 的 postprocess Bash 规则与 skill 内 outputs 路径不得残留
    assert not any("postprocess.py" in entry for entry in allow)
    assert not any("skills/autosource/outputs" in entry for entry in allow)


def test_settings_keeps_budget_no_outputs_deny():
    """保留项：WebSearch 会话预算；运行产物移到仓库根 outputs/ 后，
    1.0 的防自锚定 deny 规则（Read(outputs/**)/Glob(outputs*)）已移除——
    否则与中断恢复所需的读权限冲突。"""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"]["CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION"] == "1000"
    assert "deny" not in settings["permissions"]
    assert settings["permissions"]["additionalDirectories"] == [".claude/skills"]


# ---------- 配置样例与依赖清单（实现规格第一/九章） ----------

def test_config_example_schema_and_defaults():
    """config.example.json：字段齐全、默认值即实现规格第一章参数表。"""
    config = json.loads((SKILL_DIR / "config.example.json").read_text(encoding="utf-8"))
    llm = config["llm"]
    for key in ("base_url", "api_key", "model", "timeout", "retry"):
        assert key in llm
    conv = config["converge"]
    assert conv["k"] == 4
    assert conv["queries_per_batch_min"] == 3
    assert conv["queries_per_batch_max"] == 5
    assert conv["fuse_batch_limit"] == 100
    assert conv["retry"] == 2
    assert conv["fail_rate_threshold"] == 0.5
    # 样例不含真实密钥（只有占位说明）
    assert "sk-" not in json.dumps(config)


def test_gitignore_covers_2_0_artifacts():
    """config.json（含密钥）与运行产物入 .gitignore；1.0 历史产物在 outputs_1/。"""
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/skills/autosource/config.json" in gitignore
    assert "outputs/" in gitignore
    assert "outputs_1/" in gitignore
    assert ".claude/skills/autosource/outputs/" not in gitignore


def test_outputs_root_defaults_to_repo_root(monkeypatch):
    """运行产物根目录 = 仓库根 outputs/（与 skill 目录分离，2026-09-04 用户裁决）。"""
    import orchestrator
    monkeypatch.delenv("AUTOSOURCE_OUTPUTS", raising=False)
    assert orchestrator.outputs_root() == ROOT / "outputs"


def test_requirements_txt_stdlib_only():
    """依赖清单：Python 3.10+ 标准库，无第三方依赖。"""
    requirements = (SKILL_DIR / "requirements.txt").read_text(encoding="utf-8")
    assert "3.10" in requirements
    assert "标准库" in requirements
