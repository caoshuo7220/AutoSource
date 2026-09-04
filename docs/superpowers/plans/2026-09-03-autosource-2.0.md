# AutoSource 2.0 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans 在本会话内按任务顺序执行。
> 提交纪律：本计划不含 git commit 步骤——按用户约定，提交永远等用户发话。

**Goal:** 在 autosource-2.0 分支把 1.0 线性流水线重写为收敛式循环 Skill（SKILL.md 编排 + Python 脚本持有状态与收敛判定）。

**Architecture:** 宿主（TRAE / Claude Code）按固定步骤执行循环：`--init → 循环(--plan → websearch → --commit → --review) → --finalize`。脚本为宿主子进程，持有 state.json、执行证据校验/去重/收敛判定；LLM 经 OpenAI 兼容网关承担 init/plan/extract/review/report 五个语义节点。

**Tech Stack:** Python 3.10+ 标准库（urllib 调网关，零第三方依赖）；pytest。

**Spec:** docs/AutoSource2.0设计.md + docs/AutoSource2.0实现规格.md（实现规格优先）。

## Global Constraints

- 收敛与失败参数集中在 config.json（默认值：k=4、每批 3-5 查询、单次查询重试 2、失败率阈值 0.5、熔断批次上限 100）；脚本读取，不硬编码。
- 状态仅记录事实，不记录判断；converged 布尔与理由不持久化（--review 即时输出）；phase 取值 init/running/converged/failed。
- 证据链：候选 URL 逐字出现在 search_results.json（边界匹配；`#` 后接字符通过、`?` 严格拒绝）；输出前剥离 `#数字` 引用锚点。
- 中文书面表述；文件/注释风格沿用 1.0（模块头 docstring、中文注释、类型标注）。
- 交付物：数据源清单 CSV（UTF-8 BOM，7 列）、分析报告 md（六板块+脚本注入数据总览）、intermediate/{stats,搜索日志,溯源}.csv + state.json + raw/。
- 1.0 文件处置按实现规格第三章；1.0 历史文档已删除、README.md 已重写为 2.0 索引（2026-09-04 收尾，1.0 封盘于 master/tag v1.0）。

## 已确认的规格解释（无用户确认前照此执行）

1. **domain 来源**：init 输出 JSON 无 domain 字段 → domain = 根节点 name（根节点即领域词，由 init 从领域描述提炼）。
2. **outputs 位置**（2026-09-04 用户裁决变更）：2.0 运行产物在仓库根 `outputs/`；1.0 历史产物已由用户改名为 `outputs_1/`；.gitignore 追加 config.json。
3. **搜索结果文件缺失条目**：search_results.json 缺某 query_id → 视为 failed（attempts=0，error 注明缺失）；多余 query_id 忽略。
4. **熔断触发点**：--commit 处理完本批后 batch_count ≥ fuse_batch_limit → phase=failed、声明失败退出（第 100 批处理完即中止）。
5. **搜索历史 extracted 字段**：search_history 行增加 extracted（经证据校验的候选按 query 归因数）——规格第七章"搜索日志.csv"要求"提取数"列，state 表未列该字段，属最小必要扩展。
6. **--plan 幂等**：pending_batch 非空时 --plan 不调 LLM，直接重印 pending_batch 的 queries（中断恢复辅助）。
7. **报告降级形态**：report LLM 重试后仍失败 → 报告文件仍产出（标题+数据总览+六板块标题各注"内容生成失败"）；CSV/stats/日志/溯源正常产出。
8. **init LLM 失败**：run 目录保留，写 phase=failed 的 state.json，显式报错退出。
9. **new_nodes 第二根节点**（parent="" 且已有根）→ 拒绝该节点并计数（保持单根树）。
10. **stats.csv 每节点** → 仅叶子节点行（候选只挂叶子）+ 总计行；体裁分布格式沿用 1.0（`类型:数量; `，按数量降序）。
11. **extract 的 node 校验**：source.node 须为结构内叶子节点，否则拒绝该条并给出原因。
12. **finalize 后 search_results.json 删除**（内容已归档 raw/batch_*.json）。
13. **配置覆盖**：环境变量 AUTOSOURCE_CONFIG（config 路径）、AUTOSOURCE_OUTPUTS（输出根目录）可覆盖默认值（测试隔离用）。
14. **requirements.txt**：零第三方依赖，文件注明 Python 3.10+ 标准库。
15. **query_id 随 --plan stdout 输出**（写入 search_results.json 时原样沿用）。

## File Structure

```
.claude/skills/autosource/
├── SKILL.md                  # 重写：循环编排指令（宿主按固定步骤执行）
├── config.example.json       # 新增：LLM 与收敛参数样例（不含密钥）
├── requirements.txt          # 新增：依赖清单
├── scripts/
│   ├── orchestrator.py       # 新增：--init/--plan/--commit/--review/--finalize 入口
│   ├── state.py              # 新增：state.json 读写/schema 校验/原子写入/树校验/增量写回
│   ├── converge.py           # 新增：客观覆盖三条件 + 失败率 + 熔断
│   ├── search_provider.py    # 新增：SearchProvider 接口 + HostSearchProvider
│   ├── evidence.py           # 复用（保留不改）
│   ├── llm_client.py         # 新增：OpenAI 兼容网关（可注入 transport）
│   ├── prompts.py            # 新增：5 节点 prompt 模板 + 填充/解析
│   └── deliver.py            # 新增：CSV/stats/搜索日志/溯源/报告（1.0 postprocess/lineage/report 有效逻辑迁入）
└── references/分析报告模板.md  # 复用（保留不改）
tests/                        # 重写（conftest + 8 个测试文件）
仓库根 outputs/                # 运行产物（gitignore；2026-09-04 由用户裁决从 skill 目录移出）
.claude/settings.json         # 重写（去 MCP/hook，2.0 权限白名单）
.gitignore                    # 追加 2 行
删除：.mcp.json、.claude/skills/autosource/scripts/{mcp_server,evidence_hook,postprocess,store,lineage,report}.py、tests/{test_mcp_server,test_postprocess,test_store,test_skill_structure}.py
```

## 模块接口（后续任务照此引用）

**state.py**
- `StateError(ValueError)`、`PHASES = ("init", "running", "converged", "failed")`
- `new_state(domain: str, nodes: list[dict]) -> dict`（phase=running、sources=[]、pending_batch={}、exploration 三字段归零）
- `load_state(path: Path) -> dict`（结构校验，坏文件抛出 StateError）
- `save_state(path: Path, state: dict)`（写 .tmp + os.replace 原子替换）
- `validate_tree(nodes) -> None`（单根、名字唯一、parent 可解析、无孤儿）
- `leaf_names(nodes) -> set[str]`、`node_map(nodes) -> dict[str, dict]`、`path_of(node, nodes) -> str`（根到节点以 `-` 连接）
- `apply_writeback(state, new_nodes, new_entities, new_terms) -> dict`（返回 rejected 明细；逐条校验后追加去重）
- `ensure_angle_in_dims(nodes, node, angle) -> bool`（plan 用：angle 不在 dims 则加入）
- `mark_angles(nodes, queries) -> None`（commit 用：status=done 的 query 的 angle 加入对应节点 angles，去重）
- `sanitize_domain(domain) -> str`（1.0 同名函数迁入）

**converge.py**
- `objective_conditions(state: dict, k: int) -> tuple[bool, list[str]]`（三条件，返回未满足原因列表）
- `last_two_batches_failing(history: list[dict], threshold: float) -> bool`（按 batch 分组求失败率）
- `fuse_triggered(batch_count: int, limit: int) -> bool`

**search_provider.py**
- `class SearchProvider`（`fetch(queries: list[dict]) -> list[dict]` 抽象接口）
- `class HostSearchProvider(SearchProvider)`：`__init__(self, results_path: Path)`；按 query_id 匹配；缺失条目 → failed（attempts=0, error 说明）；多余条目忽略；文件非法 JSON → 抛出 ValueError

**llm_client.py**
- `class LLMError(RuntimeError)`
- `class LLMClient`：`__init__(self, config: dict, post=None)`（post 可注入供测试）；`chat_json(self, prompt: str) -> dict`（JSON 解析失败/超时/网关错误重试 llm.retry 次）；`chat_text(self, prompt: str) -> str`（仅网络类重试）；模型/base_url/api_key/timeout 从 config.llm 读

**prompts.py**
- `fill(template, mapping) -> str`（替换 `{{key}}`）
- `parse_json(content: str) -> dict`（剥 ```json 围栏后 json.loads，失败抛出 ValueError）
- `INIT_TMPL / PLAN_TMPL / EXTRACT_TMPL / REVIEW_TMPL / REPORT_TMPL`（规格第六章原文）
- `build_init_prompt(domain_description)`、`build_plan_prompt(structure, gaps, recent_history)`、`build_extract_prompt(done_results)`、`build_review_prompt(structure, source_summary, history)`、`build_report_prompt(structure, sources, history)`

**deliver.py**
- `deduplicate_delivery(sources) -> list[dict]`（域名+名称：合集级优先 → first_seen_batch 最早 → 保持首次出现顺序）
- `write_source_csv(path, sources, structure)`（7 列：数据源名称/分类路径/数据源类型/粒度/访问地址/简要说明/来源搜索；URL 剥锚点）
- `compute_stats(sources, leaves) -> dict`（per_leaf/count/types + total_types）
- `write_stats_csv(path, summary)`、`write_search_log_csv(path, history)`、`write_lineage_csv(path, rows)`
- `build_lineage(state, delivered, raw_dir) -> list[list]`（批次/分类节点/查询词/结果URL/是否收录/收录条目名称/备注）
- `report_stats_block(sources, leaves) -> str`（数据总览 markdown）
- `compose_report(domain, body, stats_block) -> str`
- `DEGRADED_REPORT_BODY`（报告降级正文）

**orchestrator.py**
- `main(argv=None) -> int`；`build_client(config) -> LLMClient`（测试 monkeypatch 点）
- `load_config() -> dict`（AUTOSOURCE_CONFIG 或 skill 根 config.json，缺失报错提示复制 config.example.json）
- `outputs_root() -> Path`（AUTOSOURCE_OUTPUTS 或 skill 根 outputs/）
- 子命令语义严格按规格第四章表格；--commit 内部流程见下方 Task 6。

## 任务分解

### Task 1: 删除 1.0 废弃文件 + .gitignore + config.example.json + requirements.txt
- 删除：.mcp.json、scripts/{mcp_server,evidence_hook}.py、tests/test_mcp_server.py
- 新增：config.example.json（规格第九章原文）、requirements.txt、.gitignore 追加 2 行
- 验证：`git status` 显示删除；文件存在性测试（Task 8）

### Task 2: tests/conftest.py + state.py + converge.py（TDD）
- 先写 tests/test_state.py、tests/test_converge.py，再实现两模块至全绿
- 验证：`python -m pytest tests/test_state.py tests/test_converge.py -q`

### Task 3: evidence.py 测试 + search_provider.py + llm_client.py + prompts.py
- tests/test_evidence.py（边界匹配：# 通过、? 严格、锚点剥离、前缀拒绝——校验复用文件不改）
- tests/test_search_provider.py、tests/test_llm_client.py
- 验证：四个测试文件全绿

### Task 4: deliver.py
- 迁移 1.0：deduplicate→交付去重口径改造、write_source_csv/write_stats_csv/write_journal_csv、build_lineage（1.0 血缘 join 逻辑改造）、report 数据总览注入（_report_stats_block 改造）、sanitize_domain
- tests/test_deliver.py（CSV 7 列/BOM/锚点、交付去重三规则、分类路径、stats、搜索日志、溯源、数据总览）
- 验证：test_deliver.py 全绿

### Task 5: SKILL.md 重写（skill-creator 技能前置调用）+ .claude/settings.json 重写（update-config 技能前置调用）
- SKILL.md：frontmatter（name/description/allowed-tools: WebSearch, Read, Write, Edit, Bash）+ 六步循环说明 + 宿主按固定步骤执行的循环（规格第四章原文步骤）+ 子命令表 + search_results.json 格式 + 重试/失败处理 + 中断恢复表 + 配置说明
- settings.json：allow = WebSearch/Read(**/Read(outputs/**)/Write(outputs/**)/Edit(outputs/**)/Bash(python .claude/skills/autosource/scripts/orchestrator.py *)/Skill(autosource)/Skill(autosource:*)；hooks 整体删除；保留 env 与 additionalDirectories（2026-09-04 用户裁决：运行产物移到仓库根 outputs/，1.0 的 deny 防自锚定规则随之移除）

### Task 6: orchestrator.py + 验收流程测试（第十章 10 项全覆盖）
- 实现 --init/--plan/--commit/--review/--finalize（流程细节见接口块与规格第四/八章）
- --commit 内部顺序：读 state（phase 校验、pending 空则无操作）→ 归档 raw/batch_N.json → HostSearchProvider 取批 → 逐 query 定 status → extract（仅 done 结果）→ 写回 new_nodes/entities/terms → 候选逐条：字段校验→证据校验（批文件全文 haystack）→node 叶子校验→按 query 归因→剥锚点去重→入库 → angles 记入 → search_history（含 extracted）→ loop_stats 更新 → 失败率/熔断判定 → 清 pending、原子保存 → 打印统计
- tests/test_orchestrator_flow.py 按第十章 10 项逐条落测试 + 全流程收敛 happy path
- 验证：`python -m pytest tests/test_orchestrator_flow.py -q`

### Task 7: tests/test_skill_structure.py 重写 + 全量验收
- 契约纳入测试：五子命令与循环步骤在 SKILL.md；无 MCP/hook 残留（SKILL.md/settings.json/.mcp.json）；废弃文件不存在；evidence.py 与报告模板原样保留；config.example.json 合法且键齐全；requirements.txt 存在
- 验证：`python -m pytest tests -q` 全绿；对照第十章清单逐项核对

## 验收映射（规格第十章 → 测试）

| 验收项 | 测试 |
|---|---|
| 空成功结果 | test_orchestrator_flow::test_empty_success_results |
| 部分失败 | test_orchestrator_flow::test_partial_failure |
| 整批失败 | test_orchestrator_flow::test_all_failed_resets_no_new |
| review 新 gaps 反馈闭环 | test_orchestrator_flow::test_review_gaps_feed_plan |
| 重复 query 靠 query_id | test_orchestrator_flow::test_duplicate_query_text |
| commit 中断恢复幂等 | test_orchestrator_flow::test_commit_rerun_idempotent |
| review 后 finalize 前恢复 | test_orchestrator_flow::test_finalize_after_converged |
| 溯源首见归因 | test_orchestrator_flow::test_first_seen_earliest |
| 熔断 | test_orchestrator_flow::test_fuse_abort |
| 报告 LLM 失败降级 | test_orchestrator_flow::test_report_llm_failure_degrade |

## Self-Review 结果

- 规格覆盖：设计文档 6 项机制（领域理解/反馈闭环/收敛判据/存活熔断/失败处理/职责分工）与实现规格 10 章逐节落到任务 1-7；第十章 10 项验收全部映射。
- 占位符扫描：无 TBD/TODO；接口块给出全部函数签名。
- 类型一致性：state/converge/search_provider/llm_client/prompts/deliver/orchestrator 的接口块为单一事实源，后续任务照此引用。
