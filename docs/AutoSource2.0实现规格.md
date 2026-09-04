# AutoSource 2.0 实现规格

> 状态：已定稿。
> 定稿日期：2026-09-03
> 配套文档：本文档是《AutoSource2.0设计.md》的"怎么做"层。设计决策（为什么这么做）见设计文档，本文档只给出可执行的实现细节。开发者须两份文档一起阅读，本文档不重复设计文档已写的决策理由。

## 一、收敛与失败参数

以下参数集中在配置文件 `config.json` 中，脚本读取，不硬编码。

| 参数           | 默认值 | 说明                                             |
| ------------ | --- | ---------------------------------------------- |
| K（连续无新增批次阈值） | 4   | 连续 K 批无任何新增来源时触发收敛判断（沿用 1.0"连续 4 次无新增饱和"的实测经验） |
| 每批查询词数       | 3-5 | plan 节点每次生成的查询词数量                              |
| 单次查询重试次数     | 2   | 首次 + 2 次重试（最多 3 次尝试）；仍异常则标记该查询 failed          |
| 失败率阈值        | 50% | 连续 2 批失败率 ≥ 50% 判定搜索服务异常，中止循环                  |
| 熔断批次上限       | 100 | 存活熔断：总批次数达到上限判定收敛判据失效，异常中止（保留状态、声明失败），非成功终止    |

收敛判据（引用设计文档）：`dims - angles` 为空 且 缺口清单为空 且 连续 K 批无新增，再由 LLM 确认覆盖充分，即正常终止。

存活熔断：搜索量不设成本上限；总批次数达到 100 时异常中止（保留状态、声明失败、报告"未收敛"），防止收敛判据失效导致无限循环。

## 二、State 完整 JSON schema

状态以单个 `state.json` 文件保存于运行目录，结构如下：

```json
{
  "domain": "交换机",
  "phase": "running",
  "structure": {
    "nodes": [
      {
        "name": "数据中心交换机",
        "parent": "交换机",
        "terms": ["数据中心交换机", "data center switch", "DC switch", "ToR"],
        "entities": [
          {"name": "SONiC", "kind": "项目"},
          {"name": "Arista", "kind": "厂商"}
        ],
        "dims": ["官方文档", "行业标准", "开源社区"],
        "angles": ["官方文档", "开源社区"]
      }
    ]
  },
  "sources": [
    {
      "name": "SONiC 官方文档",
      "url": "https://sonic-net.github.io/SONiC/",
      "source_type": "官方文档",
      "granularity": "合集级",
      "node": "数据中心交换机",
      "description": "SONiC 开源网络操作系统官方文档站",
      "first_seen_batch": 3,
      "first_seen_query": "SONiC documentation"
    }
  ],
  "pending_batch": {
    "batch_id": 6,
    "queries": [
      {
        "query_id": 1,
        "query": "SONiC documentation",
        "node": "数据中心交换机",
        "angle": "开源社区",
        "reason": "补 SONiC 官方文档入口",
        "status": "pending",
        "attempts": 0
      }
    ]
  },
  "exploration": {
    "search_history": [
      {"batch": 3, "query": "SONiC documentation", "node": "数据中心交换机", "result_count": 10, "new_count": 2, "failed": 0}
    ],
    "gaps": [
      {"description": "缺少国内交换机厂商的配置指南", "node": "数据中心交换机"}
    ],
    "loop_stats": {
      "batch_count": 5,
      "consecutive_no_new": 1,
      "failed_queries": 0
    }
  }
}
```

字段类型与维护方：

| 字段                          | 类型        | 维护方    | 说明                                                                                              |
| --------------------------- | --------- | ------ | ----------------------------------------------------------------------------------------------- |
| domain                      | string    | 脚本     | 领域词（由 init 从领域描述提炼）                                                                             |
| phase                       | string    | 脚本     | 运行状态：init / running / converged / failed；中间态由 pending\_batch 承载                                 |
| structure.nodes\[].name     | string    | LLM    | 节点名（唯一）                                                                                         |
| structure.nodes\[].parent   | string    | LLM    | 父节点名（根节点为空字符串）                                                                                  |
| structure.nodes\[].terms    | string\[] | LLM+脚本 | 核心搜索词；extract 的 new\_terms 由脚本写回对应节点                                                            |
| structure.nodes\[].entities | object\[] | LLM+脚本 | 实体 {name, kind}，kind ∈ {机构, 厂商, 产品, 项目, 规范}；extract 的 new\_entities 由脚本写回对应节点                   |
| structure.nodes\[].dims     | string\[] | LLM+脚本 | 适用探索维度；plan 使用新 angle 时脚本加入 dims                                                                |
| structure.nodes\[].angles   | string\[] | 脚本     | 已搜索角度；--commit 时 status=done 的 query 其 angle 才加入                                                |
| sources\[]                  | object\[] | 脚本     | 源集合条目（name/url/source\_type/granularity/node/description/first\_seen\_batch/first\_seen\_query） |
| pending\_batch              | object    | 脚本     | 当前已规划待搜索的批次（--plan 写入，--commit 处理完清空）                                                           |
| pending\_batch.queries\[]   | object\[] | 脚本     | {query\_id, query, node, angle, reason, status, attempts}，status ∈ {pending, done, failed}      |
| exploration.search\_history | object\[] | 脚本     | 搜索历史（batch/query/node/结果数/去重后新增数/失败数）                                                           |
| exploration.gaps            | object\[] | 脚本     | 开放缺口清单（description/node）；每轮 review 后整体替换，无 status                                               |
| exploration.loop\_stats     | object    | 脚本     | 循环统计（批次计数/连续无新增计数/失败查询数）                                                                        |

设计原则（引用设计文档）：状态仅记录事实，不记录判断。`converged` 布尔与理由不持久化，只作为 `--review` 即时输出；但 phase 在 --review 判定收敛时置为 converged（作为运行状态持久化，供 --finalize 与断点续跑识别）。

### 领域结构增量写回

extract 输出的 `new_entities` / `new_terms` / `new_nodes` 由脚本写回 structure，规则：

- new\_entities 的每个 `{name, kind, node}` → 追加到 name 匹配节点的 entities（去重）；

- new\_terms 的每个 `{term, node}` → 追加到 name 匹配节点的 terms（去重）；

- new\_nodes 的每个 `{name, parent, terms, dims}` → 作为完整节点加入 structure.nodes（含可搜索字段，保证新节点可被后续 plan 搜索）；

- plan 输出中某 query 的 angle 不在其节点 dims 中时，脚本将该 angle 加入 dims（声明该维度需要搜索）；

- \--commit 时，仅当 query status=done 才把其 angle 加入该节点的 angles（表示已搜索）；failed 时 angle 保持在 dims - angles，后续 plan 可继续补搜，不视为已覆盖。

### 树校验规则

- 根节点 parent 为空字符串；其余节点 parent 必须指向已存在的节点名；

- source 的 node 必须是叶子节点（无子节点的节点）；

- 完整分类路径 = 从根到该节点的路径，各节点名用 `-` 连接，末段与 node 一致（供 CSV"分类路径"列）；

- 脚本在 --init 与每次增量写回后校验：无孤儿节点、无重复节点名、parent 可解析、source 的 node 均在叶子节点集合内。

## 三、项目目录结构

2.0 代码位于现有仓库的 `autosource-2.0` 分支，目录如下：

```
.claude/skills/autosource/
├── SKILL.md                  # 循环编排指令（宿主按固定步骤执行循环，见"交接接口"）
├── config.json               # LLM 与收敛参数配置（gitignore，含密钥）
├── config.example.json       # 配置样例（不含密钥，git 跟踪）
├── requirements.txt          # 依赖清单（Python 依赖）
├── scripts/
│   ├── orchestrator.py       # 命令入口：--init / --plan / --commit / --review / --finalize
│   ├── state.py              # state.json 读写、schema 校验、原子写入
│   ├── converge.py           # 收敛判据（客观覆盖校验 + 存活熔断）
│   ├── search_provider.py    # 搜索源接口（SearchProvider.fetch）+ HostSearchProvider 实现
│   ├── evidence.py           # 证据链校验（边界匹配算法）
│   ├── llm_client.py         # OpenAI 兼容网关调用
│   ├── prompts.py            # 5 个 LLM 节点的 prompt 模板
│   └── deliver.py            # 交付物生成（CSV / stats / 报告）
├── references/
│   └── 分析报告模板.md        # 分析报告六板块模板
```

运行产物目录位于仓库根 `outputs/`（gitignore；1.0 历史产物已改名为 `outputs_1/`），skill 目录内不放置运行产物：

运行目录 `outputs/run_{时间戳}/` 内容：

```
run_{时间戳}/
├── state.json              # 状态（含 pending_batch）
├── search_results.json     # 当前批次宿主搜索结果（宿主写）
└── raw/                    # 每批原始结果归档（溯源用）
    └── batch_{batch_id}.json
```

### 1.0 文件处置清单

本分支从 master 拉出，仍包含 1.0 的全部文件；1.0 已封盘于 master（tag v1.0），本分支可自由处置：

- 删除：`.mcp.json`、`scripts/mcp_server.py`、`scripts/evidence_hook.py`、`tests/test_mcp_server.py`（MCP 架构整体废弃：2.0 不用 MCP 工具入库，也不用 hook 留痕）；

- 复用（保留不改）：`scripts/evidence.py`（证据链边界匹配算法）、`references/分析报告模板.md`（报告六板块模板）；

- 重写：`SKILL.md`（线性流程 → 循环编排指令）、`.claude/settings.json`（去掉 MCP 工具与 hook，改 2.0 权限白名单）、`tests/`（按第十章验收清单重写）；

- 拆分迁移：`postprocess.py` → `orchestrator.py` + `deliver.py`（编排与交付分离，CSV/去重/stats/溯源逻辑迁入 deliver.py）；`store.py` → `state.py`（状态读写与 schema 校验）；`lineage.py`、`report.py` → 溯源与报告注入逻辑迁入 deliver.py；

- 删除：`docs/01-05`（1.0 历史文档，已封盘于 master/tag v1.0）；`README.md` 重写为 2.0 索引；`outputs/` 保留（gitignore 运行产物）。

处理原则：旧模块名（postprocess / store / lineage / report / mcp\_server / evidence\_hook）不再作为独立文件存在；仅 evidence.py 与报告模板跨版本复用。

## 四、宿主↔脚本交接接口（循环协议）

Skill 形式下，宿主（TRAE / Claude Code）是唯一能调用 websearch 的主体，脚本是宿主的子进程。因此循环由"宿主按固定步骤执行、脚本做确定性判定"协作完成。循环控制权在脚本（脚本判断收敛、决定继续或停），宿主不承担判断职责，只按固定步骤执行"反复循环直到脚本返回收敛"。

### 脚本子命令

所有子命令均携带运行目录参数 `<run_dir>`（`--init` 创建并打印，后续命令原样沿用）。

| 子命令                                        | 输入           | 输出                                          | 职责                                                                                                            |
| ------------------------------------------ | ------------ | ------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `--init "<领域描述>"`                          | 领域描述         | 创建运行目录并打印路径                                 | 调 LLM init 生成领域结构，初始化 state.json（phase=running）                                                               |
| `--plan <run_dir>`                         | 读 state.json | stdout 输出 `{"queries":[...]}`               | 调 LLM plan 生成查询词，写入 state.pending\_batch（status=pending），再输出                                                  |
| `--commit <run_dir> <search_results.json>` | 结果文件路径       | 打印入库统计                                      | 读 pending\_batch，经 SearchProvider 取 SearchBatch，校验各 query 状态，extract + 证据校验 + 去重 + 更新 state，清空 pending\_batch |
| `--review <run_dir>`                       | 读 state.json | stdout 输出 `{"converged":bool,"reason":str}` | 调 LLM review、整体替换 state.gaps、脚本客观覆盖校验，判定收敛（收敛时置 phase=converged）                                              |
| `--finalize <run_dir>`                     | 读 state.json | 生成交付物，重命名运行目录                               | 前置条件 phase=converged；生成 CSV/stats/报告并重命名目录                                                                    |

### 批次生命周期与失败处理

- `--plan` 把查询词持久化到 `state.pending_batch`（status=pending，attempts=0）后才输出——即使宿主后续中断，state 仍记录"本批计划了什么"；

- 宿主对每个 query 调 websearch（首次 + 最多 2 次重试，即总共最多 3 次尝试），把每个 query 的结果或失败标记写入 search\_results.json；

- `--commit` 读取 pending\_batch 与 search\_results.json，逐 query 判定：搜索执行成功 → status=done（results 允许为空数组，表示成功但 0 条结果）；执行异常（超时/网关错误）且重试耗尽 → status=failed（attempts 记录实际尝试次数）；

- `--commit` 只处理 status=done 的 query 结果；failed 的 query 计入 exploration.loop\_stats.failed\_queries 与 search\_history.failed；

- 批次处理完成（含 failed 判定）后清空 pending\_batch；连续 2 批失败率 ≥ 50% 触发失败中止；

- "连续 K 批无新增"只基于 status=done 的 query 统计——failed 的 query 不参与无新增判断（它未搜成功，不能作为"领域已挖完"的证据），只累计进失败率。

### 搜索结果文件格式（宿主写、脚本读）

宿主对 `--plan` 输出的每个 query 调 websearch（首次 + 最多 2 次重试），把结果或失败标记写入文件。每个结果项必填 query\_id / query / results / failed / attempts；`failed=false` 表示执行成功（results 可为空数组，即 0 条结果），`failed=true` 表示执行异常（可附加 error 说明原因）：

```json
[
  {
    "query_id": 1,
    "query": "SONiC documentation",
    "results": [
      {"title": "SONiC - Software for Open Networking", "url": "https://sonic-net.github.io/SONiC/", "snippet": "..."}
    ],
    "failed": false,
    "attempts": 1
  },
  {
    "query_id": 2,
    "query": "某个成功但无结果的查询",
    "results": [],
    "failed": false,
    "attempts": 1
  },
  {
    "query_id": 3,
    "query": "某个失败的查询",
    "results": [],
    "failed": true,
    "attempts": 3,
    "error": "timeout"
  }
]
```

该文件即"证据留痕"——脚本证据校验以此文件为唯一依据。

### SearchProvider 接口（搜索源抽象）

脚本通过 `SearchProvider` 接口获取搜索结果，将"从哪拿结果"与"怎么处理结果"解耦。接口契约：

```text
SearchProvider.fetch(queries) -> SearchBatch
SearchBatch = [{query_id, query, results: [{title, url, snippet}], failed, attempts, error?}]
```

两个实现：

| Provider           | 形态        | 实现                                                                                          |
| ------------------ | --------- | ------------------------------------------------------------------------------------------- |
| HostSearchProvider | Skill（当前） | 构造时接收 search\_results.json 路径（来自 --commit 参数），按 query\_id 匹配并校验完整性（不按 query 文本匹配，避免重复查询词歧义） |
| ApiSearchProvider  | 独立程序（将来）  | 逐个 query 调搜索 API，产出同样的 SearchBatch                                                          |

约束：

- orchestrator 只经 SearchProvider 接口获取结果，不直接读取搜索文件；

- state / converge / evidence / prompts / deliver 等业务模块不得感知搜索来源；

- 迁移独立程序时，仅新增 ApiSearchProvider 并替换编排外壳，业务模块零改动。

### 去重规则

两级去重，口径不同：

- 入库幂等（--commit 内）：URL 精确相等（剥离引用序号锚点后）即跳过，不重复入库；

- 交付去重（--finalize 内）：同一"域名 + 名称"只保留一条，优先合集级，其次取 first\_seen\_batch 最早者。

### 收敛判定结合逻辑（--review 内部）

`--review` 内部按以下顺序执行：

1. 调 LLM review，输出 gaps + converged + reason；
2. 脚本用 review 的 gaps 整体替换 state.exploration.gaps（开放缺口每批更新，反馈闭环生效）；
3. 脚本校验客观覆盖三条件（用更新后的 gaps）：所有节点 `dims - angles` 为空、gaps 为空、连续 K 批无新增；
4. 客观覆盖未达成 → 返回 converged=false（客观覆盖一票否决，LLM 的 converged 无效）；
5. 客观覆盖达成 → 返回 converged = LLM 的 converged（LLM 确认是最后一关），并原子写入 phase=converged。

### 循环统计计数规则

- batch\_count：每批 --commit 处理完成后 +1（存活熔断以此计数）；

- consecutive\_no\_new：每批 --commit 后，若本批 status=done 的 query 去重后新增 0 条来源，则 +1；否则清零；整批 query 全部 failed（无任何 done）时重置为 0（无搜索证据的批次不作收敛证据）；

- failed\_queries：累计所有 status=failed 的 query 数。

### 宿主循环（写入 SKILL.md 的固定步骤）

```
1. run_dir = 运行 --init "<领域描述>"，解析打印的路径
2. 循环：
   a. 运行 --plan <run_dir>，解析 stdout 的 queries
   b. 对每个 query 调 websearch（失败重试 ≤2 次），把结果或失败标记写入 <run_dir>/search_results.json
   c. 运行 --commit <run_dir> <run_dir>/search_results.json
   d. 运行 --review <run_dir>，解析 stdout 的 converged
   e. 若 converged 为 true，退出循环
3. 运行 --finalize <run_dir>
```

宿主在循环中不自行判断是否继续——一切以 `--review` 返回的 converged 为准。搜索重试由宿主执行（脚本不主动调搜索）。

## 五、证据链校验算法

校验目标：候选 URL 必须逐字出现在搜索结果的原始记录（search\_results.json，归档于 raw/batch\_\*.json）中，杜绝凭先验知识补 URL、URL 转写错误、URL 截短。

算法（边界匹配，逐条对候选 source 执行）：

1. 将搜索结果文件的全部文本拼成单一字符串 `haystack`（包含 title/url/snippet 等全部字段值）；
2. 对候选 URL `needle`，在 `haystack` 中查找其全部出现位置；
3. 对每个出现位置，检查其前一个字符 `before` 与后一个字符 `after`：

   - URL 字符集 `URL_CHARS` = 字母数字 + `-._~:/?#[]@!$&'()*+,;=%`（RFC 3986）；

   - 若 `before` 属于 URL\_CHARS，或 `after` 属于 URL\_CHARS，说明该匹配只是更长 URL 的前缀/片段（截短），此位置不通过；

   - 例外一：`after` 为 `#` 时通过（fragment 不改变资源主体）；

   - 例外二：`?` 不豁免（query 改变内容，仍严格拒绝）；
4. 存在任意一个位置通过，则该 URL 校验通过；否则拒绝并返回原因"URL 不在证据留痕中"。

输出前处理：剥离 URL 尾部的纯数字引用锚点（`#数字`，如 `#3`、`#3#1`），单词锚点（`#content`）保留。

该算法防的是意外编造（转写错误、凭记忆补 URL），不防对抗性篡改（留痕文件无写保护）。

## 六、LLM 节点 prompt 模板

5 个 LLM 节点（init / plan / extract / review / report）通过 OpenAI 兼容网关调用（见"LLM 参数注入"）。其中 init / plan / extract / review 输出合法 JSON（脚本解析），report 输出 Markdown 正文。以下为各节点 prompt 模板，`{{...}}` 为注入的运行时数据。

### init：领域拆解

```
你是数据源发现系统的领域拆解器。给定领域描述，把领域拆解为树状分类结构。

领域描述：{{domain_description}}

拆解原则：
- 层级清晰：上下级严格包含，同级互斥不重叠；
- 符合领域常识：分类体系与该领域公认知识体系一致；
- 深度自适应：不同领域层级数可不同；
- 覆盖完整：覆盖该领域主要方向，无明显遗漏。

对每个叶子节点，补充：
- terms：核心搜索词（中文名、英文名、常见缩写、行业术语、细分场景词）；
- dims：该节点适合搜索的探索维度（从来源类型/来源角色中选，如"官方文档""行业标准""开源社区""论文""数据集"）。

只输出 JSON，不要其他文字：
{"nodes":[{"name":"...","parent":"...","terms":["..."],"dims":["..."]}]}
根节点 parent 为空字符串。
```

### plan：规划下一批查询词

```
你是数据源发现系统的搜索规划器。基于当前领域结构、缺口清单与搜索历史，生成下一批 3-5 个查询词。

领域结构：{{structure}}
缺口清单：{{gaps}}
搜索历史（最近几批）：{{recent_search_history}}

规划规则：
- 优先针对缺口清单中的开放缺口定向搜索；
- 查询词角度取自对应节点的 dims（优先搜索尚未覆盖的角度），允许使用搜索中新发现的维度；
- 查询词构造词类：
  1. 组织形式词：数据库、排名、列表、标准、仓库、数据集、文献库、合集等；
  2. 体裁/入口词：官方文档、开发者文档、technical documentation、手册、知识库、API 参考、帮助中心、白皮书、论文文献、专利、市场研究等；
  3. 来源角色词：标准组织、监管机构、政府部门、行业协会、厂商、研究机构、大学、评测机构、基金会、公共数据平台等；
  4. 厂商/产品名规则：不单独搜产品名、不搜规格参数页；搜文档站、知识库、API、帮助中心入口；
- 实体选题：对新发现的实体（机构/厂商/产品/规范），用"实体名 + 入口词"构造查询，可不含领域词；
- 易失效角度：论文/专利/标准必须组合"领域词 + 入口词"；英文查询避免未组合泛化后缀（list/directory/registry/collection）。

只输出 JSON，不要其他文字：
{"queries":[{"query":"...","node":"...","angle":"...","reason":"..."}]}
```

### extract：提取

```
你是数据源发现系统的提取器。对搜索结果逐条判断是否收录，并提取新实体、术语与子方向。

搜索结果：{{search_results}}

逐条判断（禁止整行拒绝）：
- 每条结果独立判断收或不收；
- 标注 granularity：合集级 / 单篇级；同一来源同时有合集入口与单篇时，优先收合集入口；
- 语言版本偏好：同一内容多语言版本只收一个，优先级 中文 > 英文 > 其他；
- 平台准入：知网、专利库、百科、标准平台首页等跨领域通用平台不收录；平台的领域专属入口可收；
- source_type 从词类词汇（组织形式词/体裁词/来源角色词）中选取，不另造同义新词；
- URL 必须从搜索结果中逐字复制，禁止规范化、截短、凭先验知识补 URL。

只输出 JSON，不要其他文字：
{"sources":[{"name":"...","url":"...","source_type":"...","granularity":"...","node":"...","description":"..."}],
 "new_entities":[{"name":"...","kind":"机构|厂商|产品|项目|规范","node":"..."}],
 "new_terms":[{"term":"...","node":"..."}],
 "new_nodes":[{"name":"...","parent":"...","terms":["..."],"dims":["..."]}]}
new_entities 与 new_terms 必须带 node（归属节点）；new_nodes 必须给完整可搜索字段（terms/dims）。
```

### review：评审

```
你是数据源发现系统的评审器。基于当前状态，生成开放缺口清单与覆盖充分性判断。

领域结构：{{structure}}
源集合统计：{{source_summary}}
搜索历史：{{search_history}}

评审规则：
- 开放缺口清单：只列出当前仍未覆盖的方向/维度/实体，每条标注归属节点（已覆盖的不再列出，缺口被补齐后自然消失）；
- 覆盖充分性判断：领域的主要子方向是否都已有数据源覆盖、是否还有明显未探索的维度；
- 客观覆盖条件（脚本另行校验，此处只做语义判断）：各节点适用维度是否都已搜索、是否有明显遗漏。

只输出 JSON，不要其他文字：
{"gaps":[{"description":"...","node":"..."}],
 "converged":true或false,
 "reason":"..."}
gaps 是"当前全部开放缺口"——脚本用它整体替换 state.exploration.gaps。
```

### report：分析报告生成（--finalize 内调用）

```
你是数据源发现系统的分析报告撰写器。基于领域结构、已收录数据源清单与搜索历史，按六板块撰写分析报告正文。

领域结构：{{structure}}
已收录数据源清单：{{sources}}
搜索历史：{{search_history}}

六板块：概览 / 技术格局 / 产业生态 / 标准体系 / 中外对比 / 趋势观察。

写作要求：
- 依据锚定已收录的数据源，不空谈、不编造；
- 统计数字一律不写（"数据总览"节由脚本注入）。

输出 Markdown 正文（六板块，不含"数据总览"节）。
```

## 七、交付物格式

`--finalize` 生成的交付物沿用 1.0 格式，目录结构：

```
outputs/{领域词}_{时间戳}/
├── {领域词}_{时间戳}_数据源清单.csv    # 交付物（UTF-8 BOM，Excel 直接打开）
├── {领域词}_{时间戳}_分析报告.md       # 交付物（六板块）
└── intermediate/
    ├── {领域词}_{时间戳}_stats.csv     # 清单统计（每节点条数/体裁分布 + 总计行）
    ├── {领域词}_{时间戳}_搜索日志.csv  # 搜索复盘（每次搜索的查询词/提取数）
    ├── {领域词}_{时间戳}_溯源.csv      # 数据血缘（结果 URL ↔ 收录条目 ↔ 查询词）
    ├── state.json                     # 状态快照归档
    └── raw/                           # 每批原始搜索结果归档（溯源依据）
```

### 数据源清单 CSV（7 列）

| 列     | 说明                                           |
| ----- | -------------------------------------------- |
| 数据源名称 | 条目名称                                         |
| 分类路径  | 完整层级路径，用 `-` 连接，末段与 node 一致                  |
| 数据源类型 | source\_type（内容体裁）                           |
| 粒度    | 合集级 / 单篇级                                    |
| 访问地址  | URL（已剥离引用序号锚点）                               |
| 简要说明  | description                                  |
| 来源搜索  | 该 URL 首次出现的查询词（取自 source.first\_seen\_query） |

### 分析报告（六板块）

概览 / 技术格局 / 产业生态 / 标准体系 / 中外对比 / 趋势观察。模板见 `references/分析报告模板.md`。"数据总览"节由脚本从 state 生成（统计数字由脚本注入，LLM 不写统计数字）；报告六板块正文由 `--finalize` 内的一次额外 LLM 调用（report 节点）生成。

### stats.csv

长表，每节点一行：节点名、候选数、体裁分布；末尾一行"总计"。

### 搜索日志.csv

每次搜索一行：batch、node、query、结果数、提取数、去重后新增数、失败数。

### 溯源.csv

数据血缘：结果 URL ↔ 收录条目 ↔ 查询词的正查/反查对应关系。依据为 state 中每条 source 的 first\_seen\_batch / first\_seen\_query，结合 intermediate/raw/ 归档的原始结果反查。同一 URL 多次命中时取最早成功查询（first\_seen 语义）。

## 八、状态机、断点续跑与隔离

### 状态机

phase 取值：`init` / `running` / `converged` / `failed`。循环中间态（已规划待搜索 / 部分搜索完成）由 `pending_batch` 承载——pending\_batch 非空表示存在待处理批次。

| 迁移                        | 触发                        |
| ------------------------- | ------------------------- |
| init → running            | --init 完成领域拆解后            |
| running → converged       | --review 判定收敛（原子写入 phase） |
| running → failed          | 失败率超阈值、熔断触发、或 LLM 异常无法恢复  |
| converged / failed → 重新运行 | --init 需用户确认重置（避免覆盖历史产物）  |

\--finalize 前置条件为 phase=converged，负责生成交付物与目录重命名。

### 运行目录与隔离

每次运行一个独立运行目录 `outputs/run_{时间戳}/`（--init 创建并打印路径，后续命令均携带该路径）。state.json、search\_results.json、raw/ 归档均在运行目录内，多次运行、并发运行互不干扰。finalize 时重命名为 `outputs/{领域词}_{时间戳}/`。

### 原子写入与幂等

- state.json 写入采用"写临时文件 + 原子替换"（崩溃不损坏状态）；

- `--commit` 幂等：处理完 pending\_batch 即清空；重跑时 pending\_batch 为空则无操作；URL 幂等去重保证不重复入库。

### 中断恢复

| 中断位置                            | 恢复方式                                                |
| ------------------------------- | --------------------------------------------------- |
| --plan 后（pending\_batch 已写、未搜索） | 续跑时宿主先搜索 pending\_batch 的 queries，再 --commit        |
| 搜索中（部分 query 已搜）                | 宿主按 search\_results.json 已有的 query 补齐缺失项，再 --commit |
| --commit 中                      | 重跑 --commit 安全（幂等）                                  |
| --review 已收敛、--finalize 前中断     | 直接重跑 --finalize                                     |
| --review 后（未收敛）                 | state 已更新，续跑时 --plan 继续下一批                          |

恢复入口：宿主保存上次运行的 run\_dir，中断后显式用该 run\_dir 调 `--plan`（继续循环）或 `--finalize`（phase 已 converged 时）。脚本不扫描 outputs/、不猜测恢复哪个目录。

## 九、LLM 参数注入

LLM 通过 OpenAI 兼容网关调用（OpenAI Chat Completions 协议，支持 function calling，无内置搜索服务）。

`config.json`（gitignore，不提交密钥）字段，`config.example.json` 提供不含密钥的样例：

```json
{
  "llm": {
    "base_url": "网关地址",
    "api_key": "由部署环境提供",
    "model": "<模型名，由部署环境提供>",
    "timeout": 60,
    "retry": 2
  },
  "converge": {
    "k": 4,
    "queries_per_batch_min": 3,
    "queries_per_batch_max": 5,
    "fuse_batch_limit": 100,
    "retry": 2,
    "fail_rate_threshold": 0.5
  }
}
```

脚本通过环境变量或 config.json 读取，密钥不写入代码、不写入文档。搜索不接入外部搜索 API（宿主内置 websearch，免费且国内可用）。

### LLM 异常策略

- 返回 JSON 非法或字段缺失（仅适用于 init / plan / extract / review 四个节点，report 输出 Markdown 不适用）：重试（最多 `llm.retry` 次），仍失败则该节点调用失败；

- 超时 / 网关错误：重试（最多 `llm.retry` 次）；

- 连续 LLM 调用失败无法恢复：state.phase 置 failed，保留已收录数据源，显式声明失败。

## 十、验收测试清单

开发完成后的自动化测试至少覆盖：

- 空成功结果：搜索成功但 0 条结果 → 计入 done + 无新增，不计 failed；

- 部分失败：一批中部分 query failed → 失败率统计正确、failed 不影响无新增判断；

- 整批失败：整批 query 全 failed → consecutive\_no\_new 重置为 0，不当作收敛证据；

- review 产生新 gaps：review 每批更新 gaps，plan 能按新 gaps 补搜（反馈闭环）；

- 重复 query：两个 query 文本相同 → 靠 query\_id 区分，互不混淆；

- commit 中断恢复：pending\_batch 未清空时重跑 --commit 幂等、不重复入库；

- review 后 finalize 前中断恢复：phase 已 converged → 直接重跑 --finalize；

- 溯源首见归因：同一 URL 多次命中，first\_seen 取最早成功查询；

- 熔断：批次数达 100 → 异常中止 + 保留状态 + 声明失败；

- 报告 LLM 失败：--finalize 内报告生成 LLM 失败 → 重试后仍失败则报告降级（正文缺失但 CSV/stats 正常产出）。

测试命令：`python -m pytest tests/ -q`。
