# AutoSource 2.0 实现规格

> 状态：已定稿。
> 定稿日期：2026-09-03
> 配套文档：本文档是《AutoSource2.0设计.md》的"怎么做"层。设计决策（为什么这么做）见设计文档，本文档只给出可执行的实现细节。开发者须两份文档一起阅读，本文档不重复设计文档已写的决策理由。

## 一、收敛与失败参数

以下参数集中在配置文件 `config.json` 中，脚本读取，不硬编码。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| K（连续无新增批次阈值） | 4 | 连续 K 批无任何新增来源时触发收敛判断（沿用 1.0"连续 4 次无新增饱和"的实测经验） |
| 批次上限 | 30 | 总批次数达到上限强制终止（成本兜底，非目标值） |
| 每批查询词数 | 3-5 | plan 节点每次生成的查询词数量 |
| 单次查询重试次数 | 2 | 仍失败则跳过该查询并在探索记录标记失败 |
| 失败率阈值 | 50% | 连续 2 批失败率 ≥ 50% 判定搜索服务异常，中止循环 |

收敛判据（引用设计文档）：`dims - angles` 为空 且 缺口清单为空 且 连续 K 批无新增，再由 LLM 确认覆盖充分，或达到批次上限强制终止。

## 二、State 完整 JSON schema

状态以单个 `state.json` 文件保存，结构如下：

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
      "description": "SONiC 开源网络操作系统官方文档站"
    }
  ],
  "exploration": {
    "search_history": [
      {"batch": 3, "query": "SONiC documentation", "node": "数据中心交换机", "result_count": 10, "new_count": 2}
    ],
    "gaps": [
      {"description": "缺少国内交换机厂商的配置指南", "node": "数据中心交换机", "status": "未补"}
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

| 字段 | 类型 | 维护方 | 说明 |
|------|------|--------|------|
| domain | string | 脚本 | 领域词（由 init 从领域描述提炼） |
| phase | string | 脚本 | 运行状态：`init` / `running` / `converged` / `failed` |
| structure.nodes[].name | string | LLM | 节点名（唯一） |
| structure.nodes[].parent | string | LLM | 父节点名（根节点为空字符串） |
| structure.nodes[].terms | string[] | LLM | 核心搜索词：中英文名、缩写、行业术语、细分场景词 |
| structure.nodes[].entities | object[] | LLM | 实体：`{name, kind}`，kind ∈ {机构, 厂商, 产品, 项目, 规范} |
| structure.nodes[].dims | string[] | LLM | 适用探索维度（来源类型/来源角色） |
| structure.nodes[].angles | string[] | 脚本 | 已搜索角度（每次搜索后把 query 的 angle 并入） |
| sources[].name / url / source_type / granularity / node / description | — | 脚本 | 源集合条目（见设计文档状态字段） |
| exploration.search_history | object[] | 脚本 | 搜索历史（query / node / 结果数 / 去重后新增数） |
| exploration.gaps | object[] | 脚本 | 缺口清单（description / node / status ∈ {未补, 已补}） |
| exploration.loop_stats | object | 脚本 | 循环统计（批次计数 / 连续无新增计数 / 失败查询数） |

设计原则（引用设计文档）：状态仅记录事实，不记录判断。`converged` 与理由不在 state 中持久化，只作为 `--review` 子命令的即时输出。

## 三、项目目录结构

2.0 代码位于现有仓库的 `autosource-2.0` 分支，目录如下：

```
.claude/skills/autosource/
├── SKILL.md                  # 循环编排指令（宿主机械执行循环，见"交接接口"）
├── config.json               # LLM 与收敛参数配置（gitignore，含密钥）
├── scripts/
│   ├── orchestrator.py       # 命令入口：--init / --plan / --commit / --review / --finalize
│   ├── state.py              # state.json 读写与 schema 校验
│   ├── converge.py           # 收敛判据（客观覆盖校验 + 批次上限）
│   ├── evidence.py           # 证据链校验（边界匹配算法）
│   ├── llm_client.py         # 公司 OpenAI 兼容网关调用
│   ├── prompts.py            # 4 个 LLM 节点的 prompt 模板
│   └── deliver.py            # 交付物生成（CSV / stats / 报告）
├── references/
│   └── 分析报告模板.md        # 分析报告六板块模板
└── outputs/                  # 运行产物（gitignore）
    └── {领域词}_{时间戳}/
```

`outputs/` 结构见"交付物格式"。

## 四、宿主↔脚本交接接口（循环协议）

Skill 形式下，宿主（TRAE / Claude Code）是唯一能调用 websearch 的主体，脚本是宿主的子进程。因此循环由"宿主机械执行、脚本做确定性判定"协作完成。循环控制权在脚本（脚本判断收敛、决定继续或停），宿主不承担判断职责，只机械执行"反复循环直到脚本返回收敛"。

### 脚本子命令

| 子命令 | 输入 | 输出 | 职责 |
|--------|------|------|------|
| `--init "<领域描述>"` | 领域描述 | 打印 state 摘要 | 调 LLM init 生成领域结构，初始化 state.json |
| `--plan` | 读 state.json | stdout 输出 JSON `{"queries":[{query,node,angle,reason}]}` | 调 LLM plan 生成下一批查询词 |
| `--commit <结果文件>` | 搜索结果文件路径 | 打印入库统计 | 读搜索结果，调 LLM extract，证据校验+去重+更新 state.json |
| `--review` | 读 state.json | stdout 输出 JSON `{"converged":bool,"reason":str}` | 调 LLM review + 脚本客观覆盖校验，判定收敛 |
| `--finalize` | 读 state.json | 生成交付物并打印输出目录 | 生成 CSV/stats/报告，state.phase 置 converged |

### 搜索结果文件格式（宿主写、脚本读）

宿主对 `--plan` 输出的每个 query 调 websearch，把原始结果（title/url/snippet）写入文件，格式：

```json
[
  {
    "query": "SONiC documentation",
    "results": [
      {"title": "SONiC - Software for Open Networking", "url": "https://sonic-net.github.io/SONiC/", "snippet": "..."}
    ]
  }
]
```

该文件即"证据留痕"——脚本证据校验以此文件为唯一依据。

### 宿主循环（写入 SKILL.md 的机械步骤）

```
1. 运行 --init "<领域描述>"
2. 循环：
   a. 运行 --plan，解析 stdout 的 queries
   b. 对每个 query 调 websearch，把全部结果按上述格式写入 search_results.json
   c. 运行 --commit search_results.json
   d. 运行 --review，解析 stdout 的 converged
   e. 若 converged 为 true，退出循环
3. 运行 --finalize
```

宿主在循环中不自行判断是否继续——一切以 `--review` 返回的 converged 为准。

## 五、证据链校验算法

校验目标：候选 URL 必须逐字出现在搜索结果的原始记录（search_results.json）中，杜绝凭先验知识补 URL、URL 转写错误、URL 截短。

算法（边界匹配，逐条对候选 source 执行）：

1. 将搜索结果文件的全部文本拼成单一字符串 `haystack`（包含 title/url/snippet 等全部字段值）；
2. 对候选 URL `needle`，在 `haystack` 中查找其全部出现位置；
3. 对每个出现位置，检查其前一个字符 `before` 与后一个字符 `after`：
   - URL 字符集 `URL_CHARS` = 字母数字 + `-._~:/?#[]@!$&'()*+,;=%`（RFC 3986）；
   - 若 `before` 属于 URL_CHARS，或 `after` 属于 URL_CHARS，说明该匹配只是更长 URL 的前缀/片段（截短），此位置不通过；
   - 例外一：`after` 为 `#` 时放行（fragment 不改变资源主体）；
   - 例外二：`?` 不豁免（query 改变内容，仍严格拒绝）；
4. 存在任意一个位置通过，则该 URL 校验通过；否则拒绝并返回原因"URL 不在证据留痕中"。

输出前处理：剥离 URL 尾部的纯数字引用锚点（`#数字`，如 `#3`、`#3#1`），单词锚点（`#content`）保留。

该算法防的是意外编造（转写错误、凭记忆补 URL），不防对抗性篡改（留痕文件无写保护）。

## 六、LLM 节点 prompt 模板

4 个 LLM 节点（init / plan / extract / review）通过公司 OpenAI 兼容网关调用（见"LLM 参数注入"）。每个 prompt 要求输出合法 JSON，脚本解析。以下为各节点 prompt 模板，`{{...}}` 为注入的运行时数据。

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
- 优先针对缺口清单中"未补"的缺口定向搜索；
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

逐条判断（禁止整行拒收）：
- 每条结果独立判断收或不收；
- 标注 granularity：合集级 / 单篇级；同一来源同时有合集入口与单篇时，优先收合集入口；
- 语言版本偏好：同一内容多语言版本只收一个，优先级 中文 > 英文 > 其他；
- 平台准入：知网、专利库、百科、标准平台首页等跨领域通用平台不收录；平台的领域专属入口可收；
- source_type 从词类词汇（组织形式词/体裁词/来源角色词）中选取，不另造同义新词；
- URL 必须逐字照抄搜索结果，禁止规范化、截短、凭先验知识补 URL。

只输出 JSON，不要其他文字：
{"sources":[{"name":"...","url":"...","source_type":"...","granularity":"...","node":"...","description":"..."}],
 "new_entities":[{"name":"...","kind":"机构|厂商|产品|项目|规范"}],
 "new_terms":["..."],
 "new_nodes":[{"name":"...","parent":"..."}]}
```

### review：评审

```
你是数据源发现系统的评审器。基于当前状态，生成缺口清单与覆盖充分性判断。

领域结构：{{structure}}
源集合统计：{{source_summary}}
搜索历史：{{search_history}}

评审规则：
- 缺口清单：指出该领域尚未覆盖的方向/维度/实体，每条标注归属节点，状态填"未补"；
- 覆盖充分性判断：领域的主要子方向是否都已有数据源覆盖、是否还有明显未探索的维度；
- 客观覆盖条件（脚本另行校验，此处只做语义判断）：各节点适用维度是否都已搜索、是否有明显遗漏。

只输出 JSON，不要其他文字：
{"gaps":[{"description":"...","node":"...","status":"未补"}],
 "converged":true或false,
 "reason":"..."}
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
    └── {领域词}_{时间戳}_溯源.csv      # 数据血缘（结果 URL ↔ 收录条目 ↔ 查询词）
```

### 数据源清单 CSV（7 列）

| 列 | 说明 |
|----|------|
| 数据源名称 | 条目名称 |
| 分类路径 | 完整层级路径，用 `-` 连接，末段与 node 一致 |
| 数据源类型 | source_type（内容体裁） |
| 粒度 | 合集级 / 单篇级 |
| 访问地址 | URL（已剥离引用序号锚点） |
| 简要说明 | description |
| 来源搜索 | 该 URL 首次出现的查询词 |

### 分析报告（六板块）

概览 / 技术格局 / 产业生态 / 标准体系 / 中外对比 / 趋势观察。模板见 `references/分析报告模板.md`。报告由脚本从 state 生成"数据总览"节（统计数字由脚本注入，LLM 不写统计数字）。

### stats.csv

长表，每节点一行：节点名、候选数、体裁分布；末尾一行"总计"。

### 搜索日志.csv

每次搜索一行：phase（规划搜索）、node、query、结果数、提取数、去重后新增数。

### 溯源.csv

数据血缘：结果 URL ↔ 收录条目 ↔ 查询词的正查/反查对应关系。

## 八、断点续跑流程

状态序列化文件 `state.json` 是断点续跑的唯一依据。

- 运行开始：`--init` 若发现 state.json 存在且 `phase` 为 `running`，则跳过初始化、从当前状态续跑（不重新拆解领域）；
- 续跑起点：直接进入 `--plan`，基于已有 state 继续规划下一批查询词；
- 运行结束：`--finalize` 将 `phase` 置为 `converged`；失败中止时置为 `failed`（保留已收录数据源）；
- 重新运行：`phase` 为 `converged` 或 `failed` 时，`--init` 提示用户是否重置（避免覆盖历史运行产物）。

## 九、LLM 参数注入

LLM 通过公司 OpenAI 兼容网关调用（OpenAI Chat Completions 协议，支持 function calling，无内置搜索服务）。

`config.json`（gitignore，不提交密钥）字段：

```json
{
  "llm": {
    "base_url": "公司网关地址",
    "api_key": "由部署环境提供",
    "model": "deepseek-v4-flash"
  },
  "converge": {
    "k": 4,
    "batch_limit": 30,
    "queries_per_batch_min": 3,
    "queries_per_batch_max": 5,
    "retry": 2,
    "fail_rate_threshold": 0.5
  }
}
```

脚本通过环境变量或 config.json 读取，密钥不写入代码、不写入文档。搜索不接入外部搜索 API（宿主内置 websearch，免费且国内可用）。
