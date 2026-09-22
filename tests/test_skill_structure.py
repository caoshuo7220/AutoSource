"""skill 结构约定的契约钉进测试：单 skill 合并、脚本归位 scripts/、路径引用一致。"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
SKILL_DIR = ROOT / ".claude" / "skills" / "autosource"
SCRIPTS_DIR = SKILL_DIR / "scripts"
REF_DIR = SKILL_DIR / "references"

# 2026-09-17 拆分：SKILL.md 退化为路由（总则 + 分工边界 + 参数 + 阶段文件地图），
# 阶段细则搬到 references/ 下、进入该阶段前才 Read。断言分两类：
#   - 行为类：指向规则真正落地的那个文件（下文的 REF_* 常量），搬错文件就红；
#   - 文风/全局类：在 DOC（运行时实际会读到的全套文本）上查。
# DOC 顺序固定 = 路由 → 通用纪律 → 0-2 → 3 → 4 → 5 → 6-7，与运行时读取顺序一致。
SKILL_ROUTER = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
REF_DISCIPLINE = (REF_DIR / "通用纪律.md").read_text(encoding="utf-8")
REF_0_2 = (REF_DIR / "阶段0-2-初始化与清单.md").read_text(encoding="utf-8")
REF_3 = (REF_DIR / "阶段3-验证搜索.md").read_text(encoding="utf-8")
REF_4 = (REF_DIR / "阶段4-增量发现.md").read_text(encoding="utf-8")
REF_5 = (REF_DIR / "阶段5-覆盖评估与扩量.md").read_text(encoding="utf-8")
REF_6_7 = (REF_DIR / "阶段6-7-自检与收尾.md").read_text(encoding="utf-8")

DOC = "\n".join([
    SKILL_ROUTER, REF_DISCIPLINE, REF_0_2, REF_3, REF_4, REF_5, REF_6_7,
])


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
    """合并后的线性流程：阶段 0-7 齐全，收尾为最后阶段（无中间停靠点）。"""
    for i in range(8):
        assert f"## 阶段 {i} ·" in DOC
    assert DOC.rindex("## 阶段 7 · 收尾") > DOC.rindex("## 阶段 6 ·")


def test_skill_references_only_scripts_paths():
    """SKILL.md 只引用 scripts/ 下的脚本路径，不残留旧路径、不引用已删除的搜索层。"""
    assert ".claude/skills/autosource/scripts/postprocess.py" in DOC
    assert ".claude/skills/autosource/postprocess.py" not in DOC
    assert "/autosource-search" not in DOC


def test_skill_uses_unified_granularity_terms():
    """清单条目粒度术语统一为 合集级/单篇级（2026-08-21 决策 + 2026-08-24 统一），
    不再使用"体系级"这一遗留叫法。"""
    assert "体系级" not in DOC
    assert "合集级" in DOC


def test_skill_has_no_phantom_t_filter():
    """-t 类型过滤参数已删除（2026-08-24 审查）：description 与参数表不得再宣传。
    按参数形态断言（-t "…"），避免误伤 allowed-tools 等含 "-t" 子串的正常词。"""
    assert "-t \"" not in DOC
    assert "类型过滤" not in DOC


def test_skill_has_no_phantom_c_parameter():
    """-c 预设分类路径参数已删除（a3cecd9 结构重构丢掉了"用户未指定 -c 时执行"的流程接线，
    参数表行成悬空契约，README/docs 仍宣传）——用户决策：删参数。
    与 -t 删除同款做法：按参数形态断言（-c "…"），只匹配参数表形态。"""
    assert "-c \"" not in DOC
    assert "预设分类" not in DOC


def test_skill_frontmatter_tools_declare_agent():
    """frontmatter allowed-tools 声明流程实际使用的工具（docs/07 §七）。
    阶段 1/3/4/5 的搜索、提取、落库下放子代理后 Agent 成为强依赖，一并声明
    （2026-09-18；此前"不含未被流程使用的 Agent"的钉桩随下放改造作废）。
    五个 MCP 工具（.mcp.json 注册的 autosource-store）与正文强依赖对齐。"""
    frontmatter = DOC.split("---")[1]
    assert "allowed-tools" in frontmatter
    assert "Agent" in frontmatter
    for tool in ["mcp__autosource-store__record_sources",
                 "mcp__autosource-store__record_search",
                 "mcp__autosource-store__record_knowledge",
                 "mcp__autosource-store__coverage",
                 "mcp__autosource-store__finalize"]:
        assert tool in frontmatter


def test_failure_path_output_contract():
    """失败路径输出契约：failed: true 是两层架构时代的层间状态 token，单 skill
    直接对用户说话后无载体定义——改为用户可见的失败声明措辞。"""
    assert "failed: true" not in DOC
    assert "本次运行失败" in REF_DISCIPLINE


def test_model_field_optional_contract():
    """2026-09-02 审查修复：model 字段改可选——脚本侧早已按可选处理（postprocess 注释：
    "模型名（可选，fold 路径随 manifest_input.json 归档留存）"），SKILL 却当必填，且阶段 3
    输入块混入"模型名"行易被误当搜索输入；模型自报名字不可靠（代理环境下系统提示的
    模型标识可为假名），填错比留空更糟（虚假溯源）。修法：阶段 3 输入块删模型名行，
    阶段 6 约束与示例标注可选、会话未明确提供时留空。"""
    assert "模型名: 当前实际使用的模型名" not in DOC
    assert "会话未明确提供可靠模型名时留空" in REF_0_2
    assert '"model": "会话提供的模型名（未提供时留空）"' in REF_0_2


def test_incremental_search_count_per_node():
    """增量发现每节点固定 20 次（基底 16 = 固定 4 + 自由 8 + 实体 4，扩充固定 4）。
    扩充演进：09-01 起按产出不限次数 → 09-07 SOM 轮 489 次触顶后 09-08 有界化（至多
    2 批 + 续批判定 + 上限 24）→ 09-10 改固定 4 次（实证 24 次轮的扩充批被整体跳过：
    模型读着 finalize 的 ≥16 校验线把预算定到 16，续批判定给了"不扩"的决策空间）；
    实体配额 4 为 09-08 用户决策恢复（09-02 曾 4→2、空出并入自由 8→10，现自由回 8、
    基底 16 不变）。角度池独立成组（六类分行），并新增易失效角度说明。"""
    assert "每节点**固定 20 次**搜索（基底 16 次 + 扩充 4 次）" in REF_4
    assert "**实体 4 次**（本节点搜索过程中新发现的实体）" in REF_4
    assert "**扩充 4 次**（角度池或实体选题均可）" in REF_4
    assert "基底 16 次完成后固定执行" in REF_4  # 2026-09-10：固定 4 次——消灭"要不要扩"的决策空间（09-08 必跑第 1 批仍被整体跳过）
    assert "续批条件" not in DOC  # 防回潮：批级续批判定已废（模型曾据此跳过扩充）
    assert "扩充不得并入扩量轮执行" in REF_4  # 阶段 4 扩充与阶段 5 扩量轮语义区分（模型曾混淆跳过扩充）
    assert "**自由 8 次**（仅中文搜索词）" in REF_4  # 2026-09-01 决策：自由保持仅中文，只有扩充放开中英文
    assert "**自由 8 次**" in REF_4
    assert "**角度池**" in REF_4
    assert "**易失效词形（两类已知易命中低相关结果集的模式）**" in REF_4  # 2026-09-09 重构：易失效角度与英文后缀两条事故规则合并上移通用纪律（模式化收敛，阶段 4 留指针）
    assert "directory / registry / collection 这类泛化目录词" in REF_4  # 2026-08-27：directory 实证命中率低，降级为泛化目录词


def test_prepare_run_dir_contract():
    """运行目录契约：--prepare 预留唯一目录，阶段 2 写其中 manifest.json，阶段 7 同一路径收尾。
    预留步骤命名"初始化"（2026-08-25 起）——旧名"阶段 0 前"易误读为阶段序列的一部分。
    raw.json 契约已取消（docs/04 存储改造：sources/journal 走 store，元数据走 manifest）。
    2026-09-08 架构修订：manifest 只写一次（阶段 0-2 声明版），阶段 6 不再重写——
    清单核对结果随验证过程走 record_knowledge（钉桩断言语义同步，防阶段 6 重写回潮）。"""
    assert "postprocess.py --prepare" in REF_0_2
    assert "Write **manifest.json**（阶段 0-2 声明版）到初始化" in REF_0_2
    assert "整轮只写这一次" in REF_0_2
    assert "outputs/raw.json" not in DOC  # 固定路径契约已废止
    assert "raw.json" not in DOC  # raw.json 契约整体取消（docs/04）
    assert "## 初始化 · 预留运行目录" in REF_0_2
    assert "阶段 0 前" not in DOC


def test_storage_contract_mcp_tools():
    """存储架构契约（docs/04 定案）：五工具 + manifest 单次 Write + 禁 Edit + 记录时机 + 哨兵。
    数据落盘只走 MCP 工具——模型不 Write 数据文件、不自创脚本组装。
    2026-09-08 架构修订：清单核对结果走 record_knowledge 分批落库（废除阶段 6
    一次性转写 manifest），清单了结哨兵与防截断哨兵并列。"""
    for tool in ["record_sources", "record_search", "record_knowledge",
                 "coverage", "finalize"]:
        assert tool in DOC
    assert "manifest.json" in DOC
    assert "**必须整体 Write，禁止 Edit**" in REF_0_2
    assert "基底 16 次搜索完成后" in REF_4
    assert "**防截断哨兵**" in REF_6_7
    assert "数据落盘只走 MCP 工具" in REF_DISCIPLINE
    assert "存储机制自检" in SKILL_ROUTER  # 装配层故障时五工具显式报错的行为说明（mcp_server.self_check）
    assert "model = " not in DOC  # 防误用：禁止模型用 Write 写数据文件组装


def test_finalize_guards_quota_zero_reason_and_evidence():
    """2026-09-09 收尾护栏三哨兵钉桩（155658 轮复盘落地）：① 每节点增量搜索
    ≥16（配额谎报过不了收尾）；② 增量/扩量零提取必填 zero_reason（拒收留痕）；
    ③ 理由与结果域名证据一致。以及配套的两处纪律：符合判据一律收录
    （打掉"怕清单变杂"的自创拒收借口）、上下文不停靠（打掉停靠与缩水借口）。
    防回潮：这些文本是脚本哨兵的 SKILL 侧契约（postprocess.run_pipeline
    enforce_quotas），删文案会重新打开模型谎报/静默拒收的缺口。

    2026-09-16（docs/06 第 5 期文本收口）：两条断言随文案改写——
    ① "增量搜索不足 20 次拒绝并点名"：哨兵的触发条件细节与恢复指令已由
       postprocess 报错文案承担（逐条核过五条哨兵的报错，均带节点点名与恢复
       步骤），SKILL 侧改钉"不合规的收尾会被拒绝并点名"这一后果声明；
    ② "声称已收但域名不在清单"：哨兵 3 的具体判据同理由报错承担，SKILL 侧
       改钉哨兵名"理由与结果域名证据"。
    两处的义务本句未删：配额在"固定 20 次"（rules.json 锚点 node-search-quota），
    zero_reason 在阶段 4 记录时机与本节。"""
    assert "**收尾护栏**" in REF_6_7
    assert "不合规的收尾会被拒绝并点名" in REF_6_7
    assert "`zero_reason`" in DOC
    assert "理由与结果域名证据" in REF_6_7
    assert "**符合判据的一律收录**" in REF_DISCIPLINE
    assert '不得以"清单会变杂""数量太多""同类已有"为由拒收' in REF_DISCIPLINE
    assert "不得以上下文长为由停靠" in SKILL_ROUTER
    assert "不得谎报次数" in REF_4
    assert "末次覆盖" in REF_DISCIPLINE  # 补录机制：重传同 phase+node+query 行


def test_manifest_knowledge_no_granularity_field():
    """2026-09-10 文本矛盾修复①：knowledge 字段契约与粒度条冲突——字段条写
    "只含 name/node/预期体裁"，粒度条又要求"声明 granularity"，模型 thinking
    原话「Hmm, conflict.」后放弃声明、各行其是。定案：粒度与 source_type 同
    规矩（验证时按实际形态确定），manifest 不声明 granularity。"""
    assert "manifest 不声明 `granularity`" in REF_0_2
    assert "声明 `granularity`（默认合集级）" not in DOC


def test_single_carrier_not_auto_passed():
    """2026-09-10 文本矛盾修复②③：合集级项只搜到单篇载体时——旧文"以该单篇
    验证通过"使其不再进扩量轮（扩量轮范围只含未验证项），却又要求"扩量轮换
    角度专找入口"（范围条款自禁）；且激励倒挂（搜到单篇的不再找入口、什么都
    没搜到的反被追着换角度找两次）。定案：不算通过 → 按未通过落库 → 扩量轮
    找入口 → 定案时才以单篇通过。"""
    assert "**不算通过**" in REF_3
    assert 'note="仅找到单篇载体"' in DOC
    assert "扩量轮换角度专找入口" in REF_3
    assert '含 note="仅找到单篇载体"的合集级项' in DOC  # 扩量轮范围①显式纳入
    assert "**以该单篇通过**（granularity 单篇级）" in REF_5  # 定案条款（阶段 5）
    assert "**以该单篇验证通过**" not in DOC  # 防回潮：阶段 3 不得直接判通过


def test_unverified_note_values_on_the_spot():
    """2026-09-10 文本矛盾修复④：阶段 3 未通过项的 note 没有合法取值（两个定案
    值在阶段 5 才出现）——模型据此决定"不落库、攒到定案再记"，触顶后 16 项全丢
    （收尾被清单了结哨兵拦下，靠人工补录救回）。定案：三个取值当场落库，禁止攒。"""
    assert "note 按当次搜索结果如实取一个" in REF_3
    assert "不得攒到定案时再记" in REF_3
    for value in ["未找到官方入口", "仅找到单篇载体", "疑似无效机构"]:
        assert value in DOC


def test_single_paper_patent_collected_not_rejected():
    """2026-09-10 判据-执行矛盾修复（用户决策：单篇论文专利肯定要收）——180530 轮
    实证：49% 零提取里论文/专利类 20 条，理由均为"非机构成体系载体"整批拒收，
    而判据①明写"论文、专利……单篇同样收录"（模型按"平台准入"标题句的"机构发布的
    信息载体"盖过了它）。修法：标题句补"单篇技术文件同等收录"、易失效词形把
    论文/专利移出（其目标就是单篇内容页）、零提取理由禁止"非成体系载体"。"""
    assert "**成体系载体与单篇技术文件同等收录**" in REF_DISCIPLINE
    assert "**论文、专利角度不受此限**" in REF_4
    assert '"非成体系载体""只有单篇没有入口"不构成拒收理由' in REF_DISCIPLINE
    assert "论文、专利、标准角度词单独使用" not in DOC  # 防回潮：论文/专利已移出易失效词形


def test_category_path_and_vendor_site_query_contract():
    """2026-09-10 两条契约（172015 轮实证）：
    ① `category_path` 写法——模型填成"节点名/条目名"致 108 条全部归不到节点、
       stats 每节点 0（交付物节点维度报废）→ 契约写死"填节点名本身"，入库即验拒绝；
    ② 厂商官网搜索独立成阶段 1（2026-09-11）——实证（180530 与 120118 两轮对比）：
       干净的 `{厂商名} 官网` 拿到 31 条厂商根域名；掺入节点词/体裁词后只拿到 2-3 条，
       结果被推向子页面。查询词因此收窄为唯一形态。"""
    assert "**`category_path` 写法**" in REF_DISCIPLINE
    assert "**不带条目名、不用 `/` 分隔**" in REF_DISCIPLINE
    assert "固定 `{厂商名} 官网`，不加其他词、不做英文版" in REF_0_2
    assert "{vendor} official website" not in DOC


def test_vendor_official_site_mandatory_and_entry_form():
    """2026-09-10 厂商官网必收 + 入口形态否定清单（用户决策）——112053 轮实测：
    606 条里指向根域名的只有 5 条、33 家厂商 0 家有官网条目（判据把首页当"纯导流页"
    排除，而同一判据却对平台放宽为"领域专属入口可收"）；且 79 条"官网"里 23 条
    形态不合格（新闻页 15 / 跟踪参数 4 / 机构介绍页 4 / 打印版 1）。
    防回潮：删这组文案会重新打开"官网一条没有 + 新闻页冒充入口"。"""
    assert "**厂商官网首页 / 根域名（含语言首页）可收，且每个厂商必收一条**" in REF_DISCIPLINE
    assert "主题边界看机构、不看页面" in REF_DISCIPLINE
    assert "## 阶段 1 · 厂商清单与官网搜索" in REF_0_2
    assert "命中官网首页 / 根域名（含语言首页）即通过" in REF_0_2
    assert "**以下不算入口**" in REF_3
    for bad in ["帮助页", "FAQ", "政策条款页", "单篇 PDF", "新闻公告页", "课程页", "机构介绍页"]:
        assert bad in DOC
    assert "`?utm_`" in REF_3
    assert "官网首页与其文档门户是两个独立条目" in REF_DISCIPLINE


def test_cross_reference_names_unified():
    """2026-09-10 引用漂移治理：同一段落三种引法（"平台准入" / 通用纪律"提取规则"的
    平台准入段），与文件既有惯例（直接引加粗段落名）不一致；改标题时无人会同步这些
    散文引用。统一为 通用纪律"<段落名>"，钉桩防再漂移。
    （2026-09-11 搜索词构造下沉后，"搜索词构造"/"易失效词形"两节已不在通用纪律下，
    交叉引用只剩"平台准入"与"收录判据"两处。）"""
    assert DOC.count('通用纪律"平台准入"') >= 3
    assert DOC.count('通用纪律"收录判据"') >= 2
    assert '"提取规则"的' not in DOC  # 不再用节名前缀作引用（漂移源）


def test_criterion_institution_carrier_and_exclusions():
    """2026-09-10 收录政策收紧钉桩（2110 条清单复盘落地）：判据① = 只收"机构发布
    的信息载体"两条规则——新闻/资讯/媒体文章/个人内容/导购/百科词条一律不收，
    垃圾域与低价值聚合平台由脚本名单入库即拒；提取率标尺按实况重校准（2-4 条/10）。
    防回潮：2110 条里 794 条媒体+530 条噪声的成因就是旧判据（"有主题边界就收"），
    删这些文案会重新打开新闻洪水与垃圾域收录的缺口。"""
    assert "机构发布的信息载体" in REF_DISCIPLINE
    assert "新闻、资讯与媒体文章不属于收录对象" in SKILL_ROUTER
    assert "新闻、资讯、媒体文章、个人内容一律不收录" in REF_DISCIPLINE
    assert "百科词条、问答、个人博客与论坛帖不收" in REF_DISCIPLINE
    assert "GARBAGE_DOMAINS" in REF_DISCIPLINE
    assert "机构信息载体通常占 2-4 条" in REF_DISCIPLINE
    assert "行业媒体文章、技术新闻" not in DOC  # 旧目标句的新闻类收录对象，不得回潮
    assert "收录数量不是质量指标" not in DOC  # 旧"宁多勿缺"口径（无限全收），已按机构载体限定
    assert "博客 / 资讯平台" not in DOC  # 角度池的新闻/博客角度已移除（其产出全不收录）


def test_scripts_include_store_and_mcp_server():
    """docs/04 实现落地：store.py（存储层）与 mcp_server.py（四工具服务）归位 scripts/。"""
    assert (SCRIPTS_DIR / "store.py").is_file()
    assert (SCRIPTS_DIR / "mcp_server.py").is_file()
    assert (ROOT / ".mcp.json").is_file()


def test_knowledge_recorded_via_record_knowledge_not_manifest():
    """2026-09-08 架构修订钉桩（214051 事故根治）：清单核对结果随验证过程经
    record_knowledge 分批落库，废除"会话暂存 + 阶段 6 一次性转写 manifest"——
    手工转写 65 条 JSON 曾漏写 60 个 verified 字段（finalize 如实报 0/65 后
    模型违规手动收尾）。断言：manifest 只写一次、阶段 6 为了结自检、
    暂存会话表述不残留、清单了结哨兵在场（防回潮到转写方案）。"""
    assert "## 阶段 6 · 清单了结自检" in REF_6_7
    assert "record_knowledge" in DOC
    assert "清单核对结果**不进 manifest 转写**" in SKILL_ROUTER
    assert "整轮只写这一次" in REF_0_2
    assert "**清单了结哨兵**" in REF_6_7
    assert "暂存会话" not in DOC  # 转写方案的标志词，不得回潮
    assert "阶段 6 随 manifest 重写落盘" not in DOC
    assert "**阶段 6 · 重写 manifest" not in DOC


def test_knowledge_entry_name_copied_verbatim_from_manifest():
    """清单核对落库的 name 必须逐字来自 manifest（181607 轮实测）。

    该轮 35 项按自拟措辞落库（manifest「Arista EOS 文档中心」→ 落库「Arista EOS
    产品文档门户」），了结哨兵按名精确对账判为漏录、首次 finalize 被拒；按原名
    补录后重跑通过。文档里只有"同名重录 = 状态更新"，从未说明 name 的来源。
    """
    assert "`name` 逐字照抄 manifest 里的名称" in REF_DISCIPLINE


def test_skill_genre_free_label_no_taxonomy_leak():
    """source_type 字段语义与机制隔离（2026-09-08 终版）：SKILL 只给模型字段语义
    （内容形态标签，不填机构名/领域名/网址）；词表 12 类、归一、表外词审计等脚本
    机制不出现在 SKILL——词表作为提取筛子的两版尝试均致产量暴跌（每搜提取
    0.85→0.20、store 类型 22→5 类），机制细节告知模型只会诱发保守提取。
    2026-09-09 重构：数据源类型小节并入标注段，断言同步。"""
    assert "**标注**" in REF_DISCIPLINE
    assert "内容形态标签" in REF_DISCIPLINE
    assert "不填机构名、领域名或网址" in REF_DISCIPLINE
    assert "SOURCE_TYPES" not in DOC  # 脚本机制词不泄漏进 SKILL（分类归 store.py）
    assert "归一" not in DOC
    assert "词表" not in DOC


def test_termination_condition_aligned_with_finalization():
    """终止条件与定案机制对齐（2026-08-27 修订）：扩量轮从"全局 2 轮"改为"按节点独立判断"，
    仍薄弱节点继续、非薄弱节点停止，连续一轮无新增即收敛。
    总条件为"非薄弱或已收敛"——收敛是正规退出路径，避免体裁单一等客观上仍薄弱的节点
    使"所有节点非薄弱"字面条件永不满足（与 08-25"全部验证通过"同类病）。"""
    assert "两栏清单项（vendors + knowledge）全部了结（验证通过或已定案）" in REF_5
    assert "非薄弱或已收敛" in REF_5
    assert "扩量轮按节点独立判断，不再全局共享轮数" in REF_5
    assert "某节点在一轮扩量中无任何新增有效来源时，该节点视为收敛并停止" in REF_5
    assert "连续两轮无进展" not in DOC
    assert "仍薄弱且未收敛的节点继续扩量" in REF_5  # 2026-09-02 消歧：收敛优先于薄弱
    assert "仍薄弱的节点继续扩量" not in DOC


def test_source_role_dimension_and_coverage_check():
    """来源角色维度 + 覆盖评估：角度池含来源角色类，薄弱判定含来源维度单一。"""
    assert "来源角色" in DOC
    assert "来源维度单一" in REF_5
    assert "**薄弱判定**" in REF_5


def test_vendor_phase_own_query_form_no_side_extraction():
    """2026-09-11 新增阶段 1（用户决策）：厂商官网单独成阶段，先于知识清单。

    120118 轮实测：180 条"官网"条目里只有 2-3 条指向厂商根域名，173 条是子页面——
    知识清单把厂商列成"某某文档体系"，模型据此搜出文档深链、再标成"官网"；
    「厂商官网项固定搜 {厂商名} 官网」那条规则因清单里本就没有官网项而从未触发。
    定案：厂商单独成阶段、查询词收窄为唯一形态、只服务官网一个目的（不做顺路提取）。
    防回潮：删这组文案会退回"官网标签被文档深链占据"。"""
    assert "## 阶段 1 · 厂商清单与官网搜索" in REF_0_2
    assert DOC.rindex("## 阶段 1 ·") < DOC.rindex("## 阶段 2 · 知识清单")
    assert "固定 `{厂商名} 官网`，不加其他词、不做英文版" in REF_0_2
    assert 'note="未找到官网"' in DOC
    assert "厂商清单写进 manifest 的 `vendors` 数组" in REF_0_2
    # manifest 两栏分装：knowledge = 阶段 3 待验证清单；vendors = 阶段 1 厂商清单
    # （2026-09-11 B 方案）——厂商项混进 knowledge 会重新逼出"阶段 3 跳过厂商项"的补丁
    # 2026-09-18（docs/07 §五.1）：node_profiles 并入 manifest 顶层——画像必须落盘，
    # 否则下放到子代理的搜索拿不到节点搜索依据（子代理读不到主会话上下文）
    assert '`{domain, nodes, model, node_profiles, vendors, knowledge}`' in DOC
    assert "预期体裁为\"官网\"的厂商项由阶段 1 处理" not in DOC


def test_query_word_lists_relocated_no_duplication():
    """2026-09-11 搜索词构造下沉（用户决策）：通用纪律词类表与阶段 4 角度池是同一份东西——
    词类表 28 词里 23 个已在角度池（来源角色两表逐字相同），独有 5 词并进角度池后整节删除；
    易失效词形从通用纪律搬到角度池之后（"角度"概念在上一行定义，不再跨 167 行引用）。
    收尾后词表只剩两处：阶段 3 组件池（验证搜索造"机构名 + 入口意图"）、
    阶段 4 角度池（增量发现造"领域词 + 角度词"）。
    防回潮：通用纪律再长出"搜索词构造"节、或易失效词形回到全局，都会重新打开多份词表互相冲突。"""
    assert "### 搜索词构造" not in DOC
    assert "**角度池**" in REF_4
    assert "**易失效词形（两类已知易命中低相关结果集的模式）**" in REF_4
    # 易失效词形紧跟在角度池之后（同一节内，角度概念就近）
    assert (DOC.rindex("**角度池**")
            < DOC.rindex("**易失效词形（两类已知易命中低相关结果集的模式）**"))
    # 并入角度池的 5 个独有词
    for w in ["文献库", "开发者文档", "API 参考", "帮助中心", "市场研究"]:
        assert w in DOC
    # 逐字重复清单已清：入口词列举只留一处（易失效词形里那次）
    assert DOC.count("检索 / 列表 / 分类 / 目录 / 合集") == 1
    assert "通用纪律\"易失效词形\"" not in DOC


def test_official_site_label_only_for_site_entry():
    """"官网"这一 source_type 的判据（2026-09-11 用户决策）：只标站点入口
    （根域名 / 语言首页）——厂商站内的产品页、文档页、介绍页按实际形态标注。
    120118 轮实证：180 条标"官网"的条目里只有 7 条是站点入口，173 条是子页面
    （模型把"来自厂商官网站的页面"全标成了官网）。防回潮：判据删掉即退回错标。"""
    assert '**标"官网"的只限站点入口**（根域名 / 语言首页）' in DOC
    assert '按实际形态标注（文档 / 报告 / 数据库…），不标"官网"' in DOC


def test_search_response_two_parts_both_valid_evidence():
    """WebSearch 返回两部分——结构化结果链接与摘要正文（2026-09-11）。

    代码侧早已按两来源设计：check_grounded 比对整份留痕、lineage 有"摘要文本提取"
    回退路径（162501 轮溯源.csv 里 113 行标注）。SKILL 侧原先只字未提，模型只能
    自己摸索：120118 轮只从结构化结果取 → 官网入口占比 4%；162501 轮摸到摘要 →
    81%（官网类 45% 的入口只存在于摘要中）。2026-09-14 补"两部分都要过目 /
    摘要里的 URL 一样要提取"——101223 轮厂商阶段 43 家里 37 家的记录 URL 只出现在
    链接列表、0 家只出现在摘要，摘要里写明的根域名一条未被取用。钉桩防回退。"""
    assert "每次搜索返回**两部分**" in REF_DISCIPLINE
    assert "**两部分都是搜索返回的结果，同样重要，都要过目**" in REF_DISCIPLINE
    assert "摘要正文里的 URL 与链接列表里的一样要提取" in REF_DISCIPLINE


def test_verification_query_component_pool():
    """验证搜索组件池：自由组合（每词 2-4 组件），来源识别必带，旧固定模板废止。
    2026-08-25 重组：语言规则独立成条。2026-09-11：语言规则并入"每项固定搜 2 次
    不同角度"（两次查询词不得重复），"只搜 1 次"整条废止。"""
    assert "组件池" in REF_3
    assert "来源识别（必带其一）" in REF_3
    assert "{机构/体系名} 官方文档 / 官网" not in DOC
    assert "**首轮优先官方入口**" in REF_3  # 2026-08-27：验证首轮 42% 落空实证，官网式优先、落空留扩量轮
    # 2026-09-11：每项从"只搜 1 次"改为固定 2 次不同角度；厂商官网另行由阶段 1 负责
    assert "**每项固定搜 2 次，两次取不同角度**" in REF_3
    assert "两次查询词不得重复" in REF_3
    assert "只搜 1 次" not in DOC


def test_extraction_per_item_no_whole_row_rejection():
    """提取端禁止整批拒收（2026-08-27 交换机 Nokia 事件实证：查询返回 10 条官方文档页提取 0）：
    逐条判断、提取为 0 的唯一前提、粒度"优先收合集入口"歧义澄清。
    2026-09-09 重构：小节标题扁平化为六段结构，断言同步新形态（判断方式段）。"""
    assert "**判断方式**" in REF_DISCIPLINE
    assert "提取为 0 的唯一前提" in REF_DISCIPLINE
    assert "没看到合集入口就放弃该批结果" in REF_DISCIPLINE
    assert "合集入口与其包含的同一具体内容页同时出现时" in REF_DISCIPLINE


def test_extraction_yield_benchmark_and_self_check():
    """提取量自查标尺（2026-09-08 决策）：提取环节是全流水线唯一无机制锚的量——
    URL 有证据链、查询词有留痕比对、搜索数有配额，唯独"收几条"纯靠模型自觉：
    同版同模型三轮 172/274/813（08-27 SKILL）、pro 基底 ~1.1/搜 vs 08-28 全收
    6.39/搜 实证提取默认值漂移是产量主变量。"全量提取"义务型文本约束不住执行，
    改补量化标尺 + group commit 前提取率自查；同时写死两条防线：词题例外（整批
    书商/元器件站/SEO 页如实 0，不得为提高数量收录）、重审不重新搜索（防复核
    变成加搜拖长运行）。2026-09-09 重构：改名"提取率自查（非准入条件）"并降格
    ——只触发复核、不覆盖收录判据、低相关结果集不适用。"""
    assert "**提取率自查（非准入条件）**" in REF_DISCIPLINE
    assert "机构信息载体通常占 2-4 条" in REF_DISCIPLINE  # 2026-09-10 按 2110 条实况重校准（旧标尺 1-2 条不符与搜索生态脱节，激励全收）
    assert "不得为提高数量收录" in REF_DISCIPLINE
    assert "**入库前自查**" in REF_4
    assert "明显低于 1 条/搜" in REF_4
    assert "重审不重新搜索" in REF_4


def test_node_search_profile():
    """节点搜索画像（2026-08-26 起）：基础契约钉进测试——不改 nodes 契约、禁编造机构、
    核心搜索词含行业术语与细分场景词（2026-08-27 补：薄弱节点维度窄的治理）。"""
    assert "### 节点搜索画像" in REF_0_2
    assert "不改变 `nodes` 字段契约" in REF_0_2
    assert "禁止为填画像强行编造机构名称" in REF_0_2
    assert "行业术语与细分场景词" in REF_0_2


def test_journal_verified_field_contract():
    """2026-09-01：验证搜索日志拆分 verified/extracted 两字段（一个字段装一个事实，
    治理两轮记账口径不一致——交换机轮曾把验证通过计入 extracted）。"""
    assert "verified" in DOC
    assert "extracted 只记顺路新源数" in REF_3


def test_entity_query_second_type_contract():
    """2026-09-01：实体选题放开——阶段 4 查询词构造改两类选题，
    旧"每个查询词必须包含领域词"的无差别约束句移除（与通用纪律 3/4 类词的矛盾根）。
    实体选题固定配额（新实体不足退回角度池）：09-02 实测 82 样本每搜 0.87 后曾 4→2，
    09-08 用户决策恢复 4（配合扩充有界化，实体覆盖由固定配额保障）。"""
    assert "两类选题" in REF_4
    assert "实体选题" in REF_4
    assert "可不含领域词" in REF_4
    assert "每个查询词必须包含领域词" not in DOC
    assert "新实体不足 4 个时，空缺次数退回角度池" in REF_4


def test_coverage_missing_rough_signal_contract():
    """2026-09-01 评审修复钉桩：coverage 的 missing 是粗略缺口信号（提取含去重前/
    跨节点顺路发现，与幂等后已收数口径不同）——小额不触发补搜，只有接近一整批
    提取量才怀疑漏调 record_sources。"""
    assert "missing 是粗略缺口信号" in REF_5
    assert "missing > 0（提取过但落库不足）时只补该节点" not in DOC


def test_self_check_searches_before_calling_coverage():
    """2026-09-18（122246 轮首跑实证）：开工自检的第 ③ 项 coverage 在首次搜索前
    **必然失败**——证据留痕文件由 WebSearch 的 PostToolUse hook 首次触发时才创建，
    五个 MCP 工具在留痕不存在时一律报「证据留痕不存在：…」（mcp_server.self_check）。
    旧文案「① 工具加载 / ② coverage / ③ manifest，三项全过才开搜」照字面执行，
    会让每一次运行都在开搜前中止。修法：② 先发一次真实的 WebSearch 把留痕造出来。"""
    assert "② **发一次 WebSearch**" in REF_DISCIPLINE
    assert "留痕文件由 hook 首次触发时才创建" in REF_DISCIPLINE


def test_journal_phase_vocabulary_pinned():
    """2026-09-01 实测 bug 钉桩：phase 是模型自由文本，曾被缩写为"增量/验证"
    导致选题分布 0/0 与 verified 警告误报。2026-09-18 起字面量单一来源收敛到
    通用纪律「子代理派工」的分组表（阶段 1 搜索入账，取值增至四个）。"""
    assert "按上表写死" in REF_DISCIPLINE
    assert "按通用纪律「子代理派工」的字面量填" in REF_3


def test_manifest_example_is_valid_json():
    """2026-09-01 钉进测试：manifest 示例必须可解析——曾用全角引号（非法 JSON），
    模型照抄会写出不可解析的 manifest，finalize 报"manifest.json 损坏"。"""
    import json as jsonlib
    import re as re_mod
    blocks = re_mod.findall(r"```json\n(.*?)```", REF_0_2, re_mod.S)
    assert blocks, "阶段 2 文件应有 json 代码块"
    for b in blocks:
        if '"domain"' in b or '"domain"' in b.replace("“", '"').replace("”", '"'):
            jsonlib.loads(b)
            return
    raise AssertionError("未找到 manifest 示例块")


def test_no_cost_driven_trimming():
    """防模型自砍搜索次数：无"代价"成本措辞，两处决策点写明不以搜索成本缩减/合并。"""
    assert "列多列杂的代价" not in DOC
    assert "无需以搜索成本为由缩减清单" in REF_0_2
    assert "不以搜索次数或运行时长为由合并节点" in REF_0_2


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
    deny Read(outputs/**) 兜底），删除并对齐。

    2026-09-22：postprocess 那条收窄到 SKILL 明列的两个子命令（`--prepare` /
    `--rename-report`）——原先的 `postprocess.py *` 宽于契约面。"""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    allow = settings["permissions"]["allow"]
    assert "Write(outputs/**)" in allow
    assert "Edit(outputs/**)" not in allow
    # postprocess 逐子命令收窄：raw.json 形态（`postprocess.py <路径>`）不落白名单
    assert ("Bash(python .claude/skills/autosource/scripts/postprocess.py --prepare)"
            in allow)
    assert ("Bash(python .claude/skills/autosource/scripts/postprocess.py --rename-report *)"
            in allow)
    assert ("Bash(python .claude/skills/autosource/scripts/postprocess.py *)"
            not in allow)


def test_settings_allow_entries_well_formed():
    """2026-09-07：allow 曾含两个缺右括号的死条目（WebSearch(*、Read(**）——匹配不到
    任何调用。每个条目必须是裸工具名或 Tool(说明符) 完整形式。"""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    for entry in settings["permissions"]["allow"]:
        assert re.match(r"^[A-Za-z_][\w-]*(\(.*\))?$", entry), entry


def test_settings_allow_mcp_store_tools():
    """MCP 五工具进 settings.json 会话级白名单：frontmatter 放行是回合级
    （用户中途插话即失效），运行期零弹窗需会话级放行兜底。
    服务名从 .mcp.json 读取，settings 条目与注册保持一致
    （2026-09-08 架构修订起含 record_knowledge）。"""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    mcp_config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    (server,) = mcp_config["mcpServers"]
    allow = settings["permissions"]["allow"]
    for tool in ["record_sources", "record_search", "record_knowledge",
                 "coverage", "finalize"]:
        assert f"mcp__{server}__{tool}" in allow


def test_mcp_args_not_anchored_on_claude_project_dir():
    """2026-09-22 实测：`.mcp.json` **支持** `${VAR}` 展开（CLI 自带缺失变量诊断），
    但 CLI 自身的环境里没有 CLAUDE_PROJECT_DIR——写成 `${CLAUDE_PROJECT_DIR}/…` 时
    服务连不上：`claude mcp list` 报 ✘ CONNECTION_CLOSED，诊断原文
    「Missing environment variables: CLAUDE_PROJECT_DIR」；换回裸相对路径立即
    ✔ Connected。

    与 hook 那条（必须锚定 `${CLAUDE_PROJECT_DIR}`）方向相反，原因不同：hook 由 CLI
    逐回合派发，会话 cwd 一旦漂移（模型 `cd` 进运行目录）就解析不到脚本；MCP 进程在
    会话启动时一次性拉起、cwd 即项目根，且从子目录启动时服务本就因「未批准」不可用
    （2026-09-22 实测 ⏸ Pending approval），与路径写法无关。"""
    mcp_config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    for name, server in mcp_config["mcpServers"].items():
        for arg in server.get("args") or []:
            assert "CLAUDE_PROJECT_DIR" not in arg, (
                f"{name}: MCP args 不得用 ${{CLAUDE_PROJECT_DIR}} 锚定"
                "——该变量不在 CLI 环境里，服务会连不上")
            if arg.endswith(".py"):
                assert (ROOT / arg).is_file(), f"{name}: {arg}"


def test_settings_allow_delegation_tools():
    """下放改造引入的工具进 settings.json 会话级白名单（2026-09-20 实证）。

    default 模式下 13 分钟内弹窗 60 次：主会话 11 次派工、子代理 22 次工具加载。
    `Agent` 此前只在 SKILL frontmatter 声明里，而那是回合级预放行——docs/02
    09-01 记着「回合级放行在用户中途插话即失效」，而确认弹窗本身就是插话：
    弹窗把自己锁死。同 test_settings_allow_mcp_store_tools 的理由，运行期零弹窗
    需会话级兜底。

    Grep / Glob / WebFetch 刻意不入白名单：它们不是流程所需，留着弹窗即越界信号
    （09-18 与 09-20 两轮都有子代理用它们去读源码与本轮数据文件）。
    """
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    allow = settings["permissions"]["allow"]
    for tool in ["Agent", "ToolSearch"]:
        assert tool in allow, tool
    for tool in ["Grep", "Glob", "WebFetch"]:
        assert tool not in allow, f"{tool} 不是流程所需，不应进白名单"


def test_skill_no_history_output_reading():
    """阶段 0 禁止读取历史运行产物（解释式提示词，与 deny 规则互补）。"""
    assert "禁止读取 outputs/ 下历史运行的产物" in REF_0_2


def test_finish_writes_domain_analysis_report():
    """收尾最后一步：写领域分析报告（第三交付物），模板在 references/；
    文件名由脚本 --rename-report 命名（模型写内容、不自行命名）。"""
    assert "分析报告.md" in REF_6_7
    assert "领域分析报告" in REF_6_7
    assert "--rename-report" in DOC
    assert "_分析报告.md" in REF_6_7
    template = (SKILL_DIR / "references" / "分析报告模板.md").read_text(encoding="utf-8")
    assert "严格按以下固定模板" in template
    assert "ALWAYS" not in template  # 2026-08-25 审查：英文全大写命令式改为中文祈使
    assert "不是统计罗列" not in template  # 与写作纪律 3 重复，删
    assert "不要自行命名" in template
    # 2026-08-28：统计数字由脚本注入（报告体裁数与 stats 对不上的修复）——模型不写任何统计数字
    assert "## 数据总览" in template
    assert "不写任何统计数字" in template
    assert "由收尾脚本自动生成" in template
    assert "统计数字一律不写" in REF_6_7
    # 2026-09-01：模板不承载一次性实证记录（skill 只放可复用指令，日期化记录进 02 日志）
    assert "2026-08-28" not in template


def test_contradiction_and_wording_cleanup():
    """2026-08-25 全局审查修订钉进测试：矛盾表述对齐（Bash 范围/定案时机/剔除语义），
    非正式措辞移除（乱搜/掺长尾垃圾/不死循环/纪律保留/不花一次搜索），
    重复规则改指针（数据集主导只留总纲、通用平台指向总则），裸禁令补理由（自锚定）。"""
    assert "初始化 `--prepare` 与阶段 7 报告命名 `--rename-report`" in SKILL_ROUTER
    assert "（阶段 7 收尾执行）" not in DOC
    assert "留待扩量轮换角度重试后按阶段 5 定案" in REF_3
    assert "从有效来源中剔除" in REF_5
    assert "以上约束只管本类" in REF_4
    assert "历史清单是上轮结果的基线，照搬会继承上轮的遗漏与偏差" in REF_0_2
    assert "数据集只是其中一类，不应占主导" not in DOC
    assert '见通用纪律"平台准入"' in DOC  # 2026-09-10 引用名统一（原"提取规则"的平台准入段）
    for bad in ["乱搜", "掺长尾垃圾", "不死循环", "纪律保留", "不花一次搜索"]:
        assert bad not in DOC
    # 厂商/产品名规则归属（2026-09-11 搜索词构造下沉后）：官网入口 → 阶段 1；
    # 不搜规格页 → 阶段 4 实体选题。通用纪律词类表删除，并入阶段 4 角度池
    assert "不单独搜规格参数页" in REF_4
    assert '通用纪律"搜索词构造"' not in DOC
    assert DOC.count("单独搜产品名") == 0
    assert "裸搜" not in DOC  # 2026-09-02："裸"俚语前缀清除（裸搜/裸后缀→单独搜/未组合）
    assert "禁止裸搜产品名" not in DOC
    assert "❌" not in DOC and "✅" not in DOC
    # 数据源类型（2026-09-08 终版）：SKILL 只留字段语义，脚本机制（词表 12 类/归一/
    # 表外词审计）全部在 store.py——词表当提取筛子的两版尝试均致产量暴跌，机制不进 SKILL
    # 2026-09-09 重构：小节并入标注段
    assert "**标注**" in REF_DISCIPLINE
    assert "内容形态标签" in REF_DISCIPLINE
    assert "SOURCE_TYPES" not in DOC and "归一" not in DOC


def test_conciseness_review_cleanup():
    """2026-09-01 规范符合性审查钉桩：① 分工原则截断句砍论证尾——"离截断边界约 10 倍"
    是设计算术、非执行指令，机制结论（写入截断在机制上不可能发生）原保留（防回到写大文件
    老路，与阶段 4 可执行批量规则分工）——2026-09-08 用户删除该结论句（判断冗余：
    数据落盘只走 MCP 工具等强约束已足够），钉桩断言同步改为不在；② 弹窗机制解释单一归属——
    55 行落盘规则保留细节版（含 build_xxx.py 实例），57 行证据核对只留"这是脚本的职责"；
    ③ 2026-09-16 用户删除该细节版括注（现 49 行），与 09-16 删除的 98 行"读取被权限拦截"
    从句同一口径——见 docs/06 第 1 期"靠询问的拦截不算机制"：白名单对越界命令的效果是
    弹窗询问，而弹窗意味着运行停下来等人，与「流程中间没有交接停靠点」冲突；禁读规则
    2026-09-08 移除后 98 行的说法更与配置事实相反。义务本句未删——落盘规则本身（禁止
    自创脚本 / Write 数据文件组装）原样保留，删的只是机制叙述。"""
    assert "离截断边界约 10 倍" not in DOC
    assert "写入截断在机制上不可能发生" not in DOC
    assert "越界命令会被权限白名单拦截弹窗" not in DOC
    assert "执行任何 Bash 如 `python build_xxx.py` 都会越界被权限白名单拦截弹窗" not in DOC
    assert "禁止自创脚本 / Write 数据文件组装" in REF_DISCIPLINE
    assert "读取被权限拦截属护栏按设计工作，不是缺陷" not in DOC


def test_skill_no_multilang_audit_promise():
    """2026-09-01 多语言审计下线（两轮实测全假阳性、修不如删）后，SKILL 语言版本偏好节
    仍残留"脚本会对最终清单做确定性审计（同域名剥语言码路径段…）"的悬空承诺——脚本已无
    此实现，模型会期待一个永不出现的 stdout 提示（2026-09-02 审查发现）。钉死承诺句不再出现。"""
    assert "同域名剥语言码路径段后相同的组计数" not in DOC


def test_quote_style_unified_straight():
    """2026-09-02 排版统一：SKILL.md 引号全部统一为直引号——此前 91/92/148/193 等十余行
    混用弯引号（“”），与正文主体直引号风格不一致（曾有两处用右引号当开引号的排版 bug，
    统一为直引号后该类 bug 结构性消失）。阶段 7 标题括弧与交接强调次数同前。"""
    assert "“" not in DOC
    assert "”" not in DOC
    assert "（写完 manifest 后立即执行，不要结束回合）" not in DOC
    assert DOC.count("不要结束回合") == 1
    assert DOC.count("没有交接") == 2


def test_intro_structure_reorganized():
    """2026-08-25 结构重排（skill-creator 规范）：文件头分组为 分工与边界 / 参数与运行约定 / 通用纪律，
    流程图带阶段编号成为全文地图；总则改名通用纪律（原"阶段 3-5 适用"标注与内容矛盾），
    证据链标题注明脚本强制校验（解释为什么硬）。"""
    assert "初始化（预留运行目录） → 0 领域拆解 → 1 厂商清单与官网搜索 → 2 知识清单" in SKILL_ROUTER
    assert "## 分工与边界" in SKILL_ROUTER
    assert "## 参数与运行约定" in SKILL_ROUTER
    assert "## 通用纪律" in REF_DISCIPLINE
    assert "### 证据链与写入纪律（脚本强制校验）" in REF_DISCIPLINE
    assert "### 提取规则" in REF_DISCIPLINE
    assert "### 失败路径（搜索工具异常）" in REF_DISCIPLINE
    assert "（脚本强制校验）" in REF_DISCIPLINE
    assert "总则" not in DOC
    assert "**运行约定**：" not in DOC


def test_settings_reference_existing_scripts():
    """settings.json 的 hook 命令与 postprocess 权限规则指向真实存在的脚本文件。

    2026-09-18（122246 轮实证）：hook 命令曾为裸相对路径 `python .claude/skills/...`，
    会话 cwd 一旦漂移（模型排查时 `cd` 进运行目录）就解析不到脚本，证据留痕自 12:47
    起整段停写——17 条搜索无留痕、5 个扩量组整组重做。改为 `${CLAUDE_PROJECT_DIR}`
    锚定（CLI 自身 lint 推荐的形式，见 claude.exe 内的 PowerShell 告警文案）；
    不硬编码绝对路径——settings.json 随 git 分发，须对所有使用者有效（docs/02 08-26）。"""
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
                # cwd 漂移免疫：必须锚定 ${CLAUDE_PROJECT_DIR}，裸相对路径会静默停写
                assert "${CLAUDE_PROJECT_DIR}" in cmd, \
                    "hook 命令必须锚定 ${CLAUDE_PROJECT_DIR}（裸相对路径遇 cwd 漂移会静默停写证据留痕）"
                hook_script = cmd.split("${CLAUDE_PROJECT_DIR}/", 1)[1]
                hook_script = hook_script.strip('"').split(" outputs/", 1)[0]
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


def test_phases_3_to_5_forbid_mid_phase_user_interaction():
    """阶段 3/4/5 不得中途与用户交互（2026-09-16 运行 142548 实证）。

    该轮在阶段 4 跑到 41/220 次搜索时停下，产出一份"运行状态报告"并列出三个
    选项请用户选（含"收窄领域"），倾向缩减范围。原文未使用失败路径的任何措辞
    （无"本次运行失败"、无"运行目录保持为空残留"），属主动上报+征询决策；
    被追问后当场承认"the stop wasn't justified"并继续跑完，交付 473 条。

    通用纪律已有"流程中间没有交接停靠点……不得停靠、缩减配额"，但三个阶段
    均无落点，模型在该处读到的是"阶段 0/2 有『无需等待用户确认』、3/4/5 沉默"。
    """
    for ref, phase, nxt in ((REF_3, "阶段 3", "4"), (REF_4, "阶段 4", "5"), (REF_5, "阶段 5", "6")):
        assert (f"**阶段中途不与用户交互**：不产出状态汇报、不请用户选择方向"
                f"（含\"是否继续 / 是否缩小领域\"）——本阶段做完即进入阶段 {nxt}"
                in ref), phase


def test_stage_reference_files_exist_and_linked():
    """2026-09-17 拆分：SKILL.md 退化为路由，阶段细则搬进 references/、进入该阶段前才读。

    守住拆分的结构不变量：① 6 个文件都在；② 路由地图列全 6 个路径（漏一个就有
    一整个阶段的细则读不到）；③ 每个阶段文件带自己的阶段标题（搬错文件即红）；
    ④ 出口指针链完整 0-2 → 3 → 4 → 5 → 6-7（模型靠它逐阶段前进，断链即卡死）；
    ⑤ 路由里不留任何阶段标题（留了就是两处维护，且与"按需读"的设计矛盾）。
    """
    refs = [
        ("通用纪律.md", None),
        ("阶段0-2-初始化与清单.md", "## 初始化 · 预留运行目录"),
        ("阶段3-验证搜索.md", "## 阶段 3 · 验证搜索"),
        ("阶段4-增量发现.md", "## 阶段 4 · 增量发现"),
        ("阶段5-覆盖评估与扩量.md", "## 阶段 5 · 覆盖评估与扩量"),
        ("阶段6-7-自检与收尾.md", "## 阶段 6 · 清单了结自检"),
    ]
    for fname, heading in refs:
        assert (REF_DIR / fname).is_file(), f"缺阶段文件 {fname}"
        assert fname in SKILL_ROUTER, f"路由地图没列出 {fname}"
        if heading:
            assert heading in (REF_DIR / fname).read_text(encoding="utf-8"), \
                f"{fname} 里没有标题「{heading}」"

    chain = [
        ("阶段0-2-初始化与清单.md", "阶段3-验证搜索.md"),
        ("阶段3-验证搜索.md", "阶段4-增量发现.md"),
        ("阶段4-增量发现.md", "阶段5-覆盖评估与扩量.md"),
        ("阶段5-覆盖评估与扩量.md", "阶段6-7-自检与收尾.md"),
    ]
    for this_f, next_f in chain:
        text = (REF_DIR / this_f).read_text(encoding="utf-8")
        assert "**本阶段出口**" in text, f"{this_f} 缺出口指针"
        assert next_f in text, f"{this_f} 的出口没指向 {next_f}"
    assert "运行到此结束" in (REF_DIR / "阶段6-7-自检与收尾.md").read_text(encoding="utf-8")

    for i in range(8):
        assert f"## 阶段 {i} ·" not in SKILL_ROUTER, "路由里残留阶段标题，细则应只在 references/"
