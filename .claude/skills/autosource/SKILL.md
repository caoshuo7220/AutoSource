---
name: autosource
description: 系统性发现技术、行业或研究领域的多个公开数据源并导出结构化清单。仅在用户需要多来源收集、分类整理或 CSV 输出时使用，不适用于单个网页、文档或数据集查询。
allowed-tools: WebSearch, Read, Write, Bash, mcp__autosource-store__record_sources, mcp__autosource-store__record_search, mcp__autosource-store__record_knowledge, mcp__autosource-store__coverage, mcp__autosource-store__finalize
---

# AutoSource — 数据源自动发现

**目标是发现该领域机构发布的成体系信息载体与非新闻单篇技术内容**——既包括"整理好的资源集合"（列表 / 数据库 / 合集 / 排名 / 标准文件），也包括"权威的内容入口"（官方文档站、技术手册、知识库、API 文档、帮助中心、论文库等），还包括非新闻的领域单篇技术内容（论文、专利、白皮书、标准文件、厂商技术文档、市场研究报告、测试评测报告的单篇等）。新闻、资讯与媒体文章不属于收录对象。

输入 `/autosource <领域描述>`，执行一条线性流程：

```
初始化（预留运行目录） → 0 领域拆解 → 1 厂商清单与官网搜索 → 2 知识清单 → 3 验证搜索 → 4 增量发现 → 5 覆盖评估与扩量 → 6 清单了结自检 → 7 收尾
```

流程中间没有交接停靠点——运行到最后一步（阶段 7 收尾）才算结束，任何一个中间环节都不是终点。**上下文增长是常态**：数据已分批落库，发生会话压缩时可凭 store 与 `coverage()` 恢复进度；不得以上下文长为由停靠、缩减配额或谎报进度。

## 分工与边界

**分工原则**：LLM 只负责语义环节（领域拆解、知识清单、搜索、验证与提取判断），一切确定性环节（时间戳、目录命名、入库校验、证据链、对账、去重、CSV 编码、stats 统计、搜索日志、折叠收尾、文件清理）由脚本保证。**数据落盘不靠写大文件**：新增条目、搜索日志与清单核对结果通过 record_sources / record_search / record_knowledge 工具入库（store.jsonl 由脚本持有、模型不可见），收尾由 finalize 工具一次性折叠；元数据（领域/节点/模型/厂商清单/知识清单声明）在 manifest.json，由模型 Write 一次（阶段 0-2）——清单核对结果**不进 manifest 转写**，随验证过程分批落库。

**运行期边界**：本流程不写代码、不跑测试——运行中禁止调用开发类技能；唯一合法的 Bash 是 postprocess 命令（初始化 `--prepare` 与阶段 7 报告命名 `--rename-report`）；数据写入只走五个 MCP 工具（record_sources / record_search / record_knowledge / coverage / finalize），禁止自创脚本或 Write 数据文件组装。

**存储机制自检**：五个 MCP 工具每次调用前由服务自检装配层——装配层故障时工具报「存储机制自检失败」并按失败路径中止，按报错修装配层后重跑即可。

## 参数与运行约定

| 参数 | 必填 | 说明 |
|------|------|------|
| `<领域描述>` | 是 | 粗粒度文本，如 `算力服务器` |

- **领域词**：从领域描述中提炼核心关键词，空格替换为 `_`，作为 manifest.json 的 domain 字段。
- **输出目录**：流程开始时脚本预留 `outputs/run_{时间戳}/`（每次运行唯一），finalize 时重命名为 `outputs/{领域词}_{时间戳}/` 并生成交付物——最终产物位置以 finalize 返回的"输出目录"为准。

示例：输入 `帮我收集用于AI训练的GPU算力服务器相关数据源` → 目录 `outputs/算力服务器_2026-08-12-143000/`。

## 阶段文件地图

本 skill 的细则按阶段拆分在 `references/` 下。**进入对应阶段前，先 Read 对应文件，按文件内细则执行；未读不得开始该阶段。按需逐阶段读——不要提前读后续阶段文件。**

| 时机 | 先读 |
|------|------|
| 开跑前（初始化之前） | `.claude/skills/autosource/references/通用纪律.md` + `.claude/skills/autosource/references/阶段0-2-初始化与清单.md` |
| 进入阶段 3 前 | `.claude/skills/autosource/references/阶段3-验证搜索.md` |
| 进入阶段 4 前 | `.claude/skills/autosource/references/阶段4-增量发现.md` |
| 进入阶段 5 前 | `.claude/skills/autosource/references/阶段5-覆盖评估与扩量.md` |
| 进入阶段 6 前 | `.claude/skills/autosource/references/阶段6-7-自检与收尾.md`（含阶段 7 收尾） |

阶段文件末尾写有下一阶段的出口指针（与上表重复指向同一文件）。

**会话压缩或恢复后**：先重读通用纪律与当前阶段文件，再凭 store 与 `coverage()` 恢复进度——不要凭记忆继续。

**指针文件读不到时**：不得跳过该阶段——如实中止并报告。
