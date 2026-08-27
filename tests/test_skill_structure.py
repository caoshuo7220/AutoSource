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
    """增量发现每节点 12 次（2026-08-27 起：固定 4 + 自由 8——数据验证搜索次数是增产主力，
    同时修复论文/专利/标准裸搜命中通用平台首页导致提取 0 的问题）。
    角度池独立成组（六类分行），并新增易失效角度说明。"""
    assert "每节点 **12 次**搜索" in SKILL_MD
    assert "**自由 8 次**" in SKILL_MD
    assert "**角度池**" in SKILL_MD
    assert "**易失效角度（必须带领域词 + 入口词）**" in SKILL_MD
    assert "**英文裸后缀同样易失效**" in SKILL_MD  # 2026-08-27：英文 X list 模式返回垃圾页，扩展易失效规则
    assert "directory / registry / collection 这类泛化目录词" in SKILL_MD  # 2026-08-27：directory 实证命中率低，降级为泛化目录词


def test_prepare_run_dir_contract():
    """运行目录契约：--prepare 预留唯一目录，阶段 5 写其中 raw.json，阶段 6 同一路径收尾。
    预留步骤命名"初始化"（2026-08-25 起）——旧名"阶段 0 前"易误读为阶段序列的一部分。"""
    assert "postprocess.py --prepare" in SKILL_MD
    assert "<运行目录>/raw.json" in SKILL_MD
    assert "outputs/raw.json" not in SKILL_MD  # 固定路径契约已废止
    assert "## 初始化 · 预留运行目录" in SKILL_MD
    assert "阶段 0 前" not in SKILL_MD


def test_termination_condition_aligned_with_finalization():
    """终止条件与定案机制对齐（2026-08-27 修订）：扩量轮从"全局 2 轮"改为"按节点独立判断"，
    仍薄弱节点继续、非薄弱节点停止，连续一轮无新增即收敛。
    总条件为"非薄弱或已收敛"——收敛是正规退出路径，避免体裁单一等客观上仍薄弱的节点
    使"所有节点非薄弱"字面条件永不满足（与 08-25"全部验证通过"同类病）。"""
    assert "清单项全部了结（验证通过或已定案）" in SKILL_MD
    assert "非薄弱或已收敛" in SKILL_MD
    assert "扩量轮按节点独立判断，不再全局共享轮数" in SKILL_MD
    assert "某节点连续一轮无任何新增有效来源时，该节点视为收敛并停止" in SKILL_MD
    assert "连续两轮无进展" not in SKILL_MD


def test_source_role_dimension_and_coverage_check():
    """来源角色维度 + 覆盖评估：角度池含来源角色类，薄弱判定含来源维度单一。"""
    assert "来源角色" in SKILL_MD
    assert "来源维度单一" in SKILL_MD
    assert "**薄弱判定**" in SKILL_MD


def test_verification_query_component_pool():
    """验证搜索组件池：自由组合（每词 2-4 组件），来源识别必带，旧固定模板废止。
    2026-08-25 重组：语言规则独立成条（双语来源可原名+英文名并搜），
    "每项只搜 1 次"全文唯一（原两处重复合并）。"""
    assert "组件池" in SKILL_MD
    assert "来源识别（必带其一）" in SKILL_MD
    assert "{机构/体系名} 官方文档 / 官网" not in SKILL_MD
    assert "按来源语言搜索" in SKILL_MD
    assert "原名+英文名" in SKILL_MD
    assert "**首轮优先官方入口**" in SKILL_MD  # 2026-08-27：验证首轮 42% 落空实证，官网式优先、落空留扩量轮
    assert SKILL_MD.count("只搜 1 次") == 1


def test_extraction_per_item_no_whole_row_rejection():
    """提取端禁止整行拒收（2026-08-27 交换机 Nokia 事件实证：查询返回 10 条官方文档页提取 0）：
    逐条判断、提取为 0 的唯一前提、粒度"优先收合集入口"歧义澄清。"""
    assert "#### 逐条判断（禁止整行拒收）" in SKILL_MD
    assert "提取为 0 的唯一前提" in SKILL_MD
    assert "没看到合集入口就整行放弃" in SKILL_MD
    assert "合集入口与单篇同时出现在结果里" in SKILL_MD


def test_node_search_profile():
    """节点搜索画像（2026-08-26 起）：基础契约钉桩——不改 nodes 契约、禁编造机构、
    核心搜索词含行业术语与细分场景词（2026-08-27 补：薄弱节点维度窄的治理）。"""
    assert "### 节点搜索画像" in SKILL_MD
    assert "不改变 `nodes` 字段契约" in SKILL_MD
    assert "禁止为填画像强行编造机构名称" in SKILL_MD
    assert "行业术语与细分场景词" in SKILL_MD


def test_no_cost_driven_trimming():
    """防模型自砍搜索次数：无"代价"成本措辞，两处决策点写明不以搜索成本缩减/合并。"""
    assert "列多列杂的代价" not in SKILL_MD
    assert "无需以搜索成本为由缩减清单" in SKILL_MD
    assert "不以搜索次数或运行时长为由合并节点" in SKILL_MD


def test_settings_raises_web_search_session_budget():
    """项目级 settings.json 调高每会话 WebSearch 配额——Claude Code 默认 200 次/会话，
    2026-08-25 两轮运行撞线实证（扩量轮被配额砍掉）。项目级分发：所有使用者受益。"""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings.get("env", {}).get("CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION") == "1000"


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
    """收尾最后一步：写领域分析报告（第三交付物），模板在 references/；
    文件名由脚本 --rename-report 命名（模型写内容、不自行命名）。"""
    assert "分析报告.md" in SKILL_MD
    assert "领域分析报告" in SKILL_MD
    assert "--rename-report" in SKILL_MD
    assert "_分析报告.md" in SKILL_MD
    template = (SKILL_DIR / "references" / "分析报告模板.md").read_text(encoding="utf-8")
    assert "严格按以下固定模板" in template
    assert "ALWAYS" not in template  # 2026-08-25 审查：英文全大写命令式改为中文祈使
    assert "不是统计罗列" not in template  # 与写作纪律 3 重复，删
    assert "不要自行命名" in template


def test_contradiction_and_wording_cleanup():
    """2026-08-25 全局审查修订钉桩：矛盾表述对齐（Bash 范围/定案时机/剔除语义），
    非正式措辞移除（乱搜/掺长尾垃圾/不死循环/纪律保留/不花一次搜索），
    重复规则改指针（数据集主导只留总纲、通用平台指向总则），裸禁令补理由（自锚定）。"""
    assert "初始化 `--prepare` 与阶段 6 收尾" in SKILL_MD
    assert "（阶段 6 收尾执行）" not in SKILL_MD
    assert "留待扩量轮换角度重试后按阶段 4 定案" in SKILL_MD
    assert "从有效来源中剔除" in SKILL_MD
    assert "以下约束仍适用" in SKILL_MD
    assert "历史清单是上轮结果的基线，照搬会继承上轮的遗漏与偏差" in SKILL_MD
    assert SKILL_MD.count("数据集只是其中一类，不应占主导") == 1
    assert "见通用纪律“平台准入判据”" in SKILL_MD
    for bad in ["乱搜", "掺长尾垃圾", "不死循环", "纪律保留", "不花一次搜索"]:
        assert bad not in SKILL_MD
    # 厂商/产品名规则单一归属：并入通用纪律词类清单第 4 条，阶段 3 改指针，图标移除
    assert "**厂商 / 产品名**（按意图区分）" in SKILL_MD
    assert "厂商 / 产品名的用法见通用纪律“搜索词构造”" in SKILL_MD
    assert SKILL_MD.count("裸搜产品名") == 1
    assert "禁止裸搜产品名" not in SKILL_MD
    assert "❌" not in SKILL_MD and "✅" not in SKILL_MD
    # 数据源类型标签治理（2026-08-26 起）：从词类词汇中选，不自创同义新词；
    # 组合仅限词类词汇两两拼合、不加领域名（子集分裂治理）
    assert "#### 数据源类型（标签从词类词汇中选）" in SKILL_MD
    assert "不另造同义新词" in SKILL_MD
    assert "不要往标签里加领域名" in SKILL_MD
    assert "市场研究" in SKILL_MD  # 2026-08-26 交换机运行实证（26 条高频、用词稳定）纳入体裁词表


def test_intro_structure_reorganized():
    """2026-08-25 结构重排（skill-creator 规范）：文件头分组为 分工与边界 / 参数与运行约定 / 通用纪律，
    流程图带阶段编号成为全文地图；总则改名通用纪律（原"阶段 2-4 适用"标注与内容矛盾），
    证据链标题注明脚本强制校验（解释为什么硬）。"""
    assert "初始化（预留运行目录） → 0 领域拆解 → 1 知识清单" in SKILL_MD
    assert "## 分工与边界" in SKILL_MD
    assert "## 参数与运行约定" in SKILL_MD
    assert "## 通用纪律" in SKILL_MD
    assert "### 搜索词构造" in SKILL_MD
    assert "（脚本强制校验）" in SKILL_MD
    assert "总则" not in SKILL_MD
    assert "**运行约定**：" not in SKILL_MD


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
                assert "log_tool.py outputs/" not in cmd  # 2026-08-26 起按会话隔离命名，不留固定共享路径
                log_tool = cmd.split("python ", 1)[1].split(" outputs/", 1)[0]
                assert (ROOT / log_tool).is_file()
