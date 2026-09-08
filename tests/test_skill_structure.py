"""skill 结构约定的契约钉进测试：单 skill 合并、脚本归位 scripts/、路径引用一致。"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
SKILL_DIR = ROOT / ".claude" / "skills" / "autosource"
SCRIPTS_DIR = SKILL_DIR / "scripts"
SKILL_MD = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_skill_anatomy_single_skill_with_scripts():
    """标准 skill 目录：SKILL.md 在根、脚本在 scripts/（不散在根目录）。"""
    assert (SKILL_DIR / "SKILL.md").is_file()
    assert (SCRIPTS_DIR / "postprocess.py").is_file()
    assert (SCRIPTS_DIR / "evidence_hook.py").is_file()
    assert not (SCRIPTS_DIR / "log_tool.py").exists()  # 2026-09-02 改名 evidence_hook（与 evidence.py 读写分工一眼可见）


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


def test_skill_has_no_phantom_c_parameter():
    """-c 预设分类路径参数已删除（a3cecd9 结构重构丢掉了"用户未指定 -c 时执行"的流程接线，
    参数表行成悬空契约，README/docs 仍宣传）——用户决策：删参数。
    与 -t 删除同款做法：按参数形态断言（-c "…"），只匹配参数表形态。"""
    assert "-c \"" not in SKILL_MD
    assert "预设分类" not in SKILL_MD


def test_skill_frontmatter_tools_no_agent():
    """frontmatter allowed-tools 为运行所需最小集，不含未被流程使用的 Agent。
    四个 MCP 工具（.mcp.json 注册的 autosource-store）与正文强依赖对齐，一并声明。"""
    frontmatter = SKILL_MD.split("---")[1]
    assert "allowed-tools" in frontmatter
    assert "Agent" not in frontmatter
    for tool in ["mcp__autosource-store__record_sources",
                 "mcp__autosource-store__record_search",
                 "mcp__autosource-store__coverage",
                 "mcp__autosource-store__finalize"]:
        assert tool in frontmatter


def test_failure_path_output_contract():
    """失败路径输出契约：failed: true 是两层架构时代的层间状态 token，单 skill
    直接对用户说话后无载体定义——改为用户可见的失败声明措辞。"""
    assert "failed: true" not in SKILL_MD
    assert "本次运行失败" in SKILL_MD


def test_model_field_optional_contract():
    """2026-09-02 审查修复：model 字段改可选——脚本侧早已按可选处理（postprocess 注释：
    "模型名（可选，fold 路径随 manifest_input.json 归档留存）"），SKILL 却当必填，且阶段 2
    输入块混入"模型名"行易被误当搜索输入；模型自报名字不可靠（代理环境下系统提示的
    模型标识可为假名），填错比留空更糟（虚假溯源）。修法：阶段 2 输入块删模型名行，
    阶段 5 约束与示例标注可选、会话未明确提供时留空。"""
    assert "模型名: 当前实际使用的模型名" not in SKILL_MD
    assert "会话未明确提供可靠模型名时留空" in SKILL_MD
    assert '"model": "会话提供的模型名（未提供时留空）"' in SKILL_MD


def test_incremental_search_count_per_node():
    """增量发现每节点基底 16 次（固定 4 + 自由 8 + 实体 4），扩充有界化（09-08 修订：
    09-07 SOM 轮无限制扩充 489 次撞 1M 上下文墙——扩充改为至多 2 批、批级有效新增 ≥2 才继续、
    每节点上限 24 次；实体配额 4 为 09-08 用户决策恢复（09-02 曾 4→2、空出并入自由 8→10，
    现自由回 8、基底 16 不变）。角度池独立成组（六类分行），并新增易失效角度说明。"""
    assert "每节点**至少 16 次**搜索" in SKILL_MD
    assert "**固定 4 次实体选题**" in SKILL_MD
    assert "**扩充（第 1 批必跑，至多 2 批，每节点上限 24 次）**" in SKILL_MD
    assert "必须先执行第 1 批扩充" in SKILL_MD  # 2026-09-08：首轮实测模型 0 批扩充（把增产并入阶段 4 扩量轮）——第 1 批改必跑，消灭"要不要扩"的决策空间
    assert "有效新增 0-3 条" in SKILL_MD
    assert "扩充不得并入扩量轮执行" in SKILL_MD  # 阶段 3 扩充与阶段 4 扩量轮语义区分（模型曾混淆跳过扩充）
    assert "**自由 8 次**（**仅中文搜索词**）" in SKILL_MD  # 2026-09-01 决策：自由保持仅中文，只有扩充放开中英文
    assert "**自由 8 次**" in SKILL_MD
    assert "**角度池**" in SKILL_MD
    assert "**易失效角度（必须带领域词 + 入口词）**" in SKILL_MD
    assert "**英文未组合后缀同样易失效**" in SKILL_MD  # 2026-08-27：英文 X list 模式返回垃圾页，扩展易失效规则；09-02 "裸后缀"俚语改为"未组合后缀"
    assert "directory / registry / collection 这类泛化目录词" in SKILL_MD  # 2026-08-27：directory 实证命中率低，降级为泛化目录词


def test_prepare_run_dir_contract():
    """运行目录契约：--prepare 预留唯一目录，阶段 1/5 写其中 manifest.json，阶段 6 同一路径收尾。
    预留步骤命名"初始化"（2026-08-25 起）——旧名"阶段 0 前"易误读为阶段序列的一部分。
    raw.json 契约已取消（docs/04 存储改造：sources/journal 走 store，元数据走 manifest）。"""
    assert "postprocess.py --prepare" in SKILL_MD
    assert "运行目录 + `/manifest.json`" in SKILL_MD
    assert "outputs/raw.json" not in SKILL_MD  # 固定路径契约已废止
    assert "raw.json" not in SKILL_MD  # raw.json 契约整体取消（docs/04）
    assert "## 初始化 · 预留运行目录" in SKILL_MD
    assert "阶段 0 前" not in SKILL_MD


def test_storage_contract_mcp_tools():
    """存储架构契约（docs/04 定案）：四工具 + manifest 两段式 Write + 禁 Edit + 记录时机 + 哨兵。
    数据落盘只走 MCP 工具——模型不 Write 数据文件、不自创脚本组装。"""
    for tool in ["record_sources", "record_search", "coverage", "finalize"]:
        assert tool in SKILL_MD
    assert "manifest.json" in SKILL_MD
    assert "**必须整体 Write，禁止 Edit**" in SKILL_MD
    assert "基底 16 次搜索完成后" in SKILL_MD
    assert "**防截断哨兵**" in SKILL_MD
    assert "数据落盘只走 MCP 工具" in SKILL_MD
    assert "存储机制自检" in SKILL_MD  # 装配层故障时四工具硬失败的行为说明（mcp_server.self_check）
    assert "model = " not in SKILL_MD  # 防误用：禁止模型用 Write 写数据文件组装


def test_scripts_include_store_and_mcp_server():
    """docs/04 实现落地：store.py（存储层）与 mcp_server.py（四工具服务）归位 scripts/。"""
    assert (SCRIPTS_DIR / "store.py").is_file()
    assert (SCRIPTS_DIR / "mcp_server.py").is_file()
    assert (ROOT / ".mcp.json").is_file()


def test_skill_genre_free_label_no_taxonomy_leak():
    """source_type 字段语义与机制隔离（2026-09-08 终版）：SKILL 只给模型字段语义
    （内容形态标签，不填机构名/领域名/网址）；词表 12 类、归一、表外词审计等脚本
    机制不出现在 SKILL——词表作为提取筛子的两版尝试均致产量暴跌（每搜提取
    0.85→0.20、store 类型 22→5 类），机制细节告知模型只会诱发保守提取。"""
    assert "#### 数据源类型" in SKILL_MD
    assert "内容形态标签" in SKILL_MD
    assert "不填机构名、领域名或网址" in SKILL_MD
    assert "SOURCE_TYPES" not in SKILL_MD  # 脚本机制词不泄漏进 SKILL（分类归 store.py）
    assert "归一" not in SKILL_MD
    assert "词表" not in SKILL_MD


def test_termination_condition_aligned_with_finalization():
    """终止条件与定案机制对齐（2026-08-27 修订）：扩量轮从"全局 2 轮"改为"按节点独立判断"，
    仍薄弱节点继续、非薄弱节点停止，连续一轮无新增即收敛。
    总条件为"非薄弱或已收敛"——收敛是正规退出路径，避免体裁单一等客观上仍薄弱的节点
    使"所有节点非薄弱"字面条件永不满足（与 08-25"全部验证通过"同类病）。"""
    assert "清单项全部了结（验证通过或已定案）" in SKILL_MD
    assert "非薄弱或已收敛" in SKILL_MD
    assert "扩量轮按节点独立判断，不再全局共享轮数" in SKILL_MD
    assert "某节点在一轮扩量中无任何新增有效来源时，该节点视为收敛并停止" in SKILL_MD
    assert "连续两轮无进展" not in SKILL_MD
    assert "仍薄弱且未收敛的节点继续扩量" in SKILL_MD  # 2026-09-02 消歧：收敛优先于薄弱
    assert "仍薄弱的节点继续扩量" not in SKILL_MD


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


def test_extraction_yield_benchmark_and_self_check():
    """提取量自查标尺（2026-09-08 决策）：提取环节是全流水线唯一无机制锚的量——
    URL 有证据链、查询词有留痕比对、搜索数有配额，唯独"收几条"纯靠模型自觉：
    同版同模型三轮 172/274/813（08-27 SKILL）、pro 基底 ~1.1/搜 vs 08-28 全收
    6.39/搜 实证提取默认值漂移是产量主变量。"全量提取"义务型文本约束不住执行，
    改补量化常态预期（10 条通常拒 1-2 条）作自查标尺 + group commit 前提取率
    自查；同时写死两条防线：词题例外（整批书商/元器件站/SEO 页如实 0，不为凑数
    收录垃圾）、重审不重新搜索（防复核变成加搜拖长运行）。"""
    assert "**常态预期（自查标尺）**" in SKILL_MD
    assert "通常只有 1-2 条需要拒收" in SKILL_MD
    assert "不为凑数收录垃圾" in SKILL_MD
    assert "**入库前自查**" in SKILL_MD
    assert "明显低于 1 条/搜" in SKILL_MD
    assert "重审不重新搜索" in SKILL_MD


def test_node_search_profile():
    """节点搜索画像（2026-08-26 起）：基础契约钉进测试——不改 nodes 契约、禁编造机构、
    核心搜索词含行业术语与细分场景词（2026-08-27 补：薄弱节点维度窄的治理）。"""
    assert "### 节点搜索画像" in SKILL_MD
    assert "不改变 `nodes` 字段契约" in SKILL_MD
    assert "禁止为填画像强行编造机构名称" in SKILL_MD
    assert "行业术语与细分场景词" in SKILL_MD


def test_journal_verified_field_contract():
    """2026-09-01：验证搜索日志拆分 verified/extracted 两字段（一个字段装一个事实，
    治理两轮记账口径不一致——交换机轮曾把验证通过计入 extracted）。"""
    assert "verified" in SKILL_MD
    assert "extracted 只记顺路新源数" in SKILL_MD


def test_entity_query_second_type_contract():
    """2026-09-01：实体选题放开——阶段 3 查询词构造改两类选题，
    旧"每个查询词必须包含领域词"一刀切句移除（与通用纪律 3/4 类词的矛盾根）。
    实体选题固定配额（新实体不足退回角度池）：09-02 实测 82 样本每搜 0.87 后曾 4→2，
    09-08 用户决策恢复 4（配合扩充有界化，实体覆盖由固定配额保障）。"""
    assert "两类选题" in SKILL_MD
    assert "实体选题" in SKILL_MD
    assert "可不含领域词" in SKILL_MD
    assert "每个查询词必须包含领域词" not in SKILL_MD
    assert "新实体不足 4 个时，空缺次数退回角度池" in SKILL_MD


def test_coverage_missing_rough_signal_contract():
    """2026-09-01 评审修复钉桩：coverage 的 missing 是粗略缺口信号（提取含去重前/
    跨节点顺路发现，与幂等后已收数口径不同）——小额不触发补搜，只有接近一整批
    提取量才怀疑漏调 record_sources。"""
    assert "missing 是粗略缺口信号" in SKILL_MD
    assert "missing > 0（提取过但落库不足）时只补该节点" not in SKILL_MD


def test_verified_field_incremental_wording():
    """2026-09-01：增量发现项的 verified 表述与实际契约一致——store 对所有 search
    行都写 verified（缺省空），脚本按 phase 过滤，填了也不计入验证账。"""
    assert "增量发现项的 verified 留空即可" in SKILL_MD
    assert "增量发现项无 verified 字段" not in SKILL_MD


def test_journal_phase_vocabulary_pinned():
    """2026-09-01 实测 bug 钉桩：phase 是模型自由文本，曾被缩写为"增量/验证"
    导致选题分布 0/0 与 verified 警告误报——契约钉死三个取值。"""
    assert "phase 只允许三个取值" in SKILL_MD


def test_manifest_example_is_valid_json():
    """2026-09-01 钉进测试：manifest 示例必须可解析——曾用全角引号（非法 JSON），
    模型照抄会写出不可解析的 manifest，finalize 报"manifest.json 损坏"。"""
    import json as jsonlib
    import re as re_mod
    blocks = re_mod.findall(r"```json\n(.*?)```", SKILL_MD, re_mod.S)
    assert blocks, "SKILL.md 应有 json 代码块"
    for b in blocks:
        if '"domain"' in b or '"domain"' in b.replace("“", '"').replace("”", '"'):
            jsonlib.loads(b)
            return
    raise AssertionError("未找到 manifest 示例块")


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
    """防历史自锚定：deny Read(outputs/**) + Glob(outputs*)（转录实证的两条通道）。
    2026-09-08 用户决策移除：开发/复盘会话需读 outputs（运行防自锚定暂由 SKILL
    提示词承担；动态 hook 拦截方案挂账，见 docs/02 09-08 日志）。钉桩断言现状并留痕。"""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    deny = settings["permissions"].get("deny", [])
    assert "Read(outputs/**)" not in deny
    assert "Glob(outputs*)" not in deny


def test_settings_outputs_write_only_no_edit():
    """docs/04 §3.3/§10 契约：outputs 目录只放行 Write——Edit 不入 allow。
    2026-09-07：allow 曾显式放行 Edit(outputs/**)，与契约文本矛盾（Edit 墙实际由
    deny Read(outputs/**) 兜底），删除并对齐。"""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    allow = settings["permissions"]["allow"]
    assert "Write(outputs/**)" in allow
    assert "Edit(outputs/**)" not in allow


def test_settings_allow_entries_well_formed():
    """2026-09-07：allow 曾含两个缺右括号的死条目（WebSearch(*、Read(**）——匹配不到
    任何调用。每个条目必须是裸工具名或 Tool(说明符) 完整形式。"""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    for entry in settings["permissions"]["allow"]:
        assert re.match(r"^[A-Za-z_][\w-]*(\(.*\))?$", entry), entry


def test_settings_allow_mcp_store_tools():
    """MCP 四工具进 settings.json 会话级白名单：frontmatter 放行是回合级
    （用户中途插话即失效），运行期零弹窗需会话级放行兜底。
    服务名从 .mcp.json 读取，settings 条目与注册保持一致。"""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    mcp_config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    (server,) = mcp_config["mcpServers"]
    allow = settings["permissions"]["allow"]
    for tool in ["record_sources", "record_search", "coverage", "finalize"]:
        assert f"mcp__{server}__{tool}" in allow


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
    # 2026-08-28：统计数字由脚本注入（报告体裁数与 stats 对不上的修复）——模型不写任何统计数字
    assert "## 数据总览" in template
    assert "不写任何统计数字" in template
    assert "由收尾脚本自动生成" in template
    assert "统计数字一律不写" in SKILL_MD
    # 2026-09-01：模板不承载一次性实证记录（skill 只放可复用指令，日期化记录进 02 日志）
    assert "2026-08-28" not in template


def test_contradiction_and_wording_cleanup():
    """2026-08-25 全局审查修订钉进测试：矛盾表述对齐（Bash 范围/定案时机/剔除语义），
    非正式措辞移除（乱搜/掺长尾垃圾/不死循环/纪律保留/不花一次搜索），
    重复规则改指针（数据集主导只留总纲、通用平台指向总则），裸禁令补理由（自锚定）。"""
    assert "初始化 `--prepare` 与阶段 6 报告命名 `--rename-report`" in SKILL_MD
    assert "（阶段 6 收尾执行）" not in SKILL_MD
    assert "留待扩量轮换角度重试后按阶段 4 定案" in SKILL_MD
    assert "从有效来源中剔除" in SKILL_MD
    assert "以上约束只管本类" in SKILL_MD
    assert "历史清单是上轮结果的基线，照搬会继承上轮的遗漏与偏差" in SKILL_MD
    assert SKILL_MD.count("数据集只是其中一类，不应占主导") == 1
    assert '见通用纪律"平台准入判据"' in SKILL_MD
    for bad in ["乱搜", "掺长尾垃圾", "不死循环", "纪律保留", "不花一次搜索"]:
        assert bad not in SKILL_MD
    # 厂商/产品名规则单一归属：并入通用纪律词类清单第 4 条，阶段 3 改指针，图标移除
    assert "**厂商 / 产品名**（按意图区分）" in SKILL_MD
    assert '厂商 / 产品名的用法见通用纪律"搜索词构造"' in SKILL_MD
    assert SKILL_MD.count("单独搜产品名") == 1
    assert "裸搜" not in SKILL_MD  # 2026-09-02："裸"俚语前缀清除（裸搜/裸后缀→单独搜/未组合）
    assert "禁止裸搜产品名" not in SKILL_MD
    assert "❌" not in SKILL_MD and "✅" not in SKILL_MD
    # 数据源类型（2026-09-08 终版）：SKILL 只留字段语义，脚本机制（词表 12 类/归一/
    # 表外词审计）全部在 store.py——词表当提取筛子的两版尝试均致产量暴跌，机制不进 SKILL
    assert "#### 数据源类型" in SKILL_MD
    assert "内容形态标签" in SKILL_MD
    assert "SOURCE_TYPES" not in SKILL_MD and "归一" not in SKILL_MD


def test_conciseness_review_cleanup():
    """2026-09-01 规范符合性审查钉桩：① 分工原则截断句砍论证尾——"离截断边界约 10 倍"
    是设计算术、非执行指令，机制结论（写入截断在机制上不可能发生）原保留（防回到写大文件
    老路，与阶段 3 可执行批量规则分工）——2026-09-08 用户删除该结论句（判断冗余：
    数据落盘只走 MCP 工具等强约束已足够），钉桩断言同步改为不在；② 弹窗机制解释单一归属——
    55 行落盘规则保留细节版（含 build_xxx.py 实例），57 行证据核对只留"这是脚本的职责"。"""
    assert "离截断边界约 10 倍" not in SKILL_MD
    assert "写入截断在机制上不可能发生" not in SKILL_MD
    assert "越界命令会被权限白名单拦截弹窗" not in SKILL_MD
    assert "执行任何 Bash 如 `python build_xxx.py` 都会越界被权限白名单拦截弹窗" in SKILL_MD


def test_skill_no_multilang_audit_promise():
    """2026-09-01 多语言审计下线（两轮实测全假阳性、修不如删）后，SKILL 语言版本偏好节
    仍残留"脚本会对最终清单做确定性审计（同域名剥语言码路径段…）"的悬空承诺——脚本已无
    此实现，模型会期待一个永不出现的 stdout 提示（2026-09-02 审查发现）。钉死承诺句不再出现。"""
    assert "同域名剥语言码路径段后相同的组计数" not in SKILL_MD


def test_docs_01_skill_layer_quota_sync():
    """docs/01 分层表的增量发现组合与 SKILL 当前配额一致——2026-09-02 实体 4→2、
    自由 8→10 后该行未同步（文档人工同步反复漂移的又一例），钉进测试强制下次配额调整一并改。"""
    docs01 = (ROOT / "docs" / "01-交付手册.md").read_text(encoding="utf-8")
    assert "固定 4 + 自由 8 + 实体 4 = 基底 16 次/节点，扩充至多 2 批" in docs01


def test_quote_style_unified_straight():
    """2026-09-02 排版统一：SKILL.md 引号全部统一为直引号——此前 91/92/148/193 等十余行
    混用弯引号（“”），与正文主体直引号风格不一致（曾有两处用右引号当开引号的排版 bug，
    统一为直引号后该类 bug 结构性消失）。阶段 6 标题括弧与交接强调次数同前。"""
    assert "“" not in SKILL_MD
    assert "”" not in SKILL_MD
    assert "（写完 manifest 后立即执行，不要结束回合）" not in SKILL_MD
    assert SKILL_MD.count("不要结束回合") == 1
    assert SKILL_MD.count("没有交接") == 2


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
            if "evidence_hook.py" in cmd:
                assert "scripts/evidence_hook.py" in cmd
                assert "evidence_hook.py outputs/" not in cmd  # 2026-08-26 起按会话隔离命名，不留固定共享路径
                hook_script = cmd.split("python ", 1)[1].split(" outputs/", 1)[0]
                assert (ROOT / hook_script).is_file()
            assert "log_tool.py" not in cmd  # 2026-09-02 改名 evidence_hook，旧名不残留


def test_script_dependency_direction():
    """2026-09-02 依赖方向修正钉桩：存储层不反向依赖编排层（store 曾 from postprocess
    import GRANULARITY_LEVELS/check_grounded/leaf_node——import store 会把整个流水线拖进来，
    且 postprocess 内曾以延迟导入 store 绕循环）。契约归属：GRANULARITY_LEVELS（条目粒度）
    与 leaf_node（路径→节点推导）是存储层契约；依赖单向向下：mcp_server→store/postprocess
    →evidence/lineage/report。"""
    store_src = (SCRIPTS_DIR / "store.py").read_text(encoding="utf-8")
    assert "from postprocess" not in store_src
    assert "from evidence import" in store_src
    assert "GRANULARITY_LEVELS =" in store_src
    assert "def leaf_node" in store_src
    pp_src = (SCRIPTS_DIR / "postprocess.py").read_text(encoding="utf-8")
    assert "from store import" in pp_src
    assert "延迟导入避免循环依赖" not in pp_src
