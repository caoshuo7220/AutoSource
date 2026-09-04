---
name: autosource
description: 系统性发现技术、行业或研究领域的多个公开数据源并导出结构化清单。仅在用户需要多来源收集、分类整理或 CSV 输出时使用，不适用于单个网页、文档或数据集查询。
allowed-tools: WebSearch, Read, Write, Edit, Bash
---

# AutoSource — 数据源自动发现（收敛式循环）

**目标是发现该领域下各种成体系的公开信息载体**——既包括"整理好的资源集合"（列表 / 数据库 / 合集 / 排名 / 标准文件），也包括"权威的内容入口"（官方文档站、技术手册、知识库、API 文档、帮助中心、论文库）。数据集只是其中一类，不应占主导。

输入 `/autosource <领域描述>`，执行一条**收敛式循环**：循环由脚本驱动（脚本持有状态、执行收敛判定、决定继续或终止），按下方固定步骤反复执行——**循环中不自行判断是否继续**，一切以 `--review` 返回的 converged 为准。

**分工原则**：LLM 只负责语义环节（领域拆解、查询规划、提取、评审、报告），一切确定性环节（状态管理、证据校验、去重、记录、收敛判定、交付物生成）由脚本保证。搜索由 WebSearch 工具执行（脚本是子进程，不主动调搜索）。

## 运行前提（一次性准备）

1. 复制 `config.example.json` 为 `config.json`，填入 LLM 网关的 `base_url` 与 `api_key`（config.json 含密钥，不入库）。
2. 参数集中在此文件（脚本读取，不硬编码）：K（连续无新增批次阈值，默认 4）、每批查询词数（3-5）、单次查询重试（2）、失败率阈值（50%）、熔断批次上限（100）。

## 固定执行流程

```text
1. run_dir = 运行 --init "<领域描述>"，stdout 输出运行目录路径（后续命令原样沿用）
2. 循环：
   a. 运行 --plan <run_dir>，解析 stdout 的 queries（含 query_id）
   b. 对每个 query 调 websearch（失败重试 ≤2 次），把每个 query 的结果或失败标记写入 <run_dir>/search_results.json
   c. 运行 --commit <run_dir> <run_dir>/search_results.json
   d. 运行 --review <run_dir>，解析 stdout 的 converged
   e. 若 converged 为 true，退出循环
3. 运行 --finalize <run_dir>
```

搜索重试在本流程内完成（脚本不主动调搜索）。循环中间态（已规划待搜索 / 部分搜索完成）由运行目录内的 `state.json` 承载——脚本在 `--plan` 时先把查询词持久化（status=pending）再输出，即使中途中断，状态仍记录"本批计划了什么"。

## 搜索结果文件格式（由本流程写入、脚本读取）

`--commit` 时脚本以此文件为**证据留痕的唯一依据**：候选 URL 必须逐字出现在本文件中，否则校验不通过（详见"证据链纪律"）。每个结果项必填 `query_id / query / results / failed / attempts`，`query_id` 原样沿用 `--plan` 输出：

```json
[
  {"query_id": 1, "query": "SONiC documentation",
   "results": [{"title": "SONiC - Software for Open Networking", "url": "https://sonic-net.github.io/SONiC/", "snippet": "..."}],
   "failed": false, "attempts": 1},
  {"query_id": 2, "query": "某个成功但无结果的查询",
   "results": [], "failed": false, "attempts": 1},
  {"query_id": 3, "query": "某个失败的查询",
   "results": [], "failed": true, "attempts": 3, "error": "timeout"}
]
```

- `failed=false` 表示执行成功（`results` 可为空数组，即 0 条结果，不算失败）；
- `failed=true` 表示执行异常（重试耗尽：首次 + 2 次重试，最多 3 次尝试），`attempts` 记录实际尝试次数，可附加 `error` 说明原因。

## 子命令职责

| 子命令 | 输入 | 输出 | 职责 |
| ------ | ---- | ---- | ---- |
| `python .claude/skills/autosource/scripts/orchestrator.py --init "<领域描述>"` | 领域描述 | 创建运行目录并打印路径 | 调 LLM init 生成领域结构，初始化 state.json |
| `python .claude/skills/autosource/scripts/orchestrator.py --plan <run_dir>` | 读 state.json | stdout 输出 `{"queries":[...]}` | 调 LLM plan 生成查询词，写入 state.pending_batch 后输出 |
| `python .claude/skills/autosource/scripts/orchestrator.py --commit <run_dir> <search_results.json>` | 结果文件路径 | 打印入库统计 | extract + 证据校验 + 去重 + 更新 state，清空 pending_batch |
| `python .claude/skills/autosource/scripts/orchestrator.py --review <run_dir>` | 读 state.json | stdout 输出 `{"converged":bool,"reason":str}` | 调 LLM review、整体替换 gaps、客观覆盖校验、判定收敛 |
| `python .claude/skills/autosource/scripts/orchestrator.py --finalize <run_dir>` | 读 state.json | 生成交付物，重命名运行目录 | 前置条件 phase=converged；生成 CSV/stats/报告并重命名目录 |

## 证据链纪律（脚本强制校验）

- 候选 URL 必须与搜索结果**逐字一致**——`--commit` 用边界匹配逐条校验每个 URL 必须作为完整 URL 出现在 search_results.json 中，找不到的立即拒绝（截短为父路径 / 仅域名本身、改写、规范化、凭先验知识补 URL 均不予通过）。
- 模型"记得"某个权威源时，必须先专门搜一次、让它的 URL 出现在结果里，才能收录。
- URL 尾部的 `#数字` 引用锚点原样保留即可——脚本在输出 CSV 前确定性剥离；单词锚点（如 `#content`）保留。

## 中断恢复

保存上次运行的 `run_dir`，中断后**显式用该 run_dir 调 `--plan`（继续循环）或 `--finalize`（phase 已 converged 时）**；脚本不扫描 outputs/、不猜测恢复哪个目录。

| 中断位置 | 恢复方式 |
| -------- | -------- |
| --plan 后（pending_batch 已写、未搜索） | 续跑时先搜索 pending_batch 的 queries，再 --commit |
| 搜索中（部分 query 已搜） | 按 search_results.json 已有的 query 补齐缺失项，再 --commit |
| --commit 中 | 重跑 --commit 安全（幂等） |
| --review 已收敛、--finalize 前中断 | 直接重跑 --finalize |
| --review 后（未收敛） | state 已更新，续跑时 --plan 继续下一批 |

## 失败处理（脚本判定，如实报告）

- 单次查询失败：重试 ≤2 次，仍失败则写入 failed=true 条目；`--commit` 把该 query 标记为 failed 并计入失败统计。
- 连续 2 批失败率 ≥ 50%：`--commit` 判定搜索服务异常，中止循环、保留状态并声明失败。
- 总批次数达熔断上限（默认 100）：脚本判定收敛判据失效，异常中止、保留已收录数据源与状态、报告"未收敛"。
- LLM 节点调用重试后仍失败：state.phase 置 failed、保留已收录数据源、显式声明失败。
- 出现上述失败时：**停止循环、不调用 --finalize、不产出交付物**，以一句"本次运行失败"开头向用户声明失败状态与原因（脚本 stdout 已含失败声明，如实转述）。
- `--finalize` 前置条件为 phase=converged，未收敛时调用会被拒绝。

## 交付物

`--finalize` 把运行目录重命名为 `outputs/{领域词}_{时间戳}/`（在仓库根 `outputs/` 下）：

```text
{领域词}_{时间戳}/
├── {领域词}_{时间戳}_数据源清单.csv    # 交付物（UTF-8 BOM，Excel 直接打开）
├── {领域词}_{时间戳}_分析报告.md       # 交付物（六板块正文由 report 节点生成，数据总览由脚本注入）
└── intermediate/
    ├── {领域词}_{时间戳}_stats.csv     # 清单统计（每节点条数/体裁分布 + 总计行）
    ├── {领域词}_{时间戳}_搜索日志.csv  # 搜索复盘（每次搜索的查询词/提取数）
    ├── {领域词}_{时间戳}_溯源.csv      # 数据血缘（结果 URL ↔ 收录条目 ↔ 查询词）
    ├── state.json                     # 状态快照归档
    └── raw/                           # 每批原始搜索结果归档（溯源依据）
```

把 `--finalize` 的 stdout 摘要（输出目录、收录统计、报告状态）**原样展示给用户作为最终汇总**。
