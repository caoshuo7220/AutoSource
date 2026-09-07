# AutoSource

数据源自动发现 Skill（收敛式循环）——输入 `/autosource <领域描述>`，系统性地发现该领域的公开数据源（官方文档、知识库、数据集、标准、社区等成体系载体），导出结构化清单。

## 快速开始

```
/autosource 交换机

输出: outputs/{领域词}_{时间戳}/
      ├── {领域词}_{时间戳}_数据源清单.csv   ← 交付物（UTF-8 BOM，Excel 直接打开）
      ├── {领域词}_{时间戳}_分析报告.md      ← 交付物（六板块正文 + 脚本注入数据总览）
      └── intermediate/                      ← 排障与溯源材料
          ├── {领域词}_{时间戳}_stats.csv    ← 清单统计（每节点条数/体裁分布 + 总计行）
          ├── {领域词}_{时间戳}_搜索日志.csv  ← 搜索复盘（每次搜索的查询词/提取数）
          ├── {领域词}_{时间戳}_溯源.csv      ← 数据血缘（结果 URL ↔ 收录条目 ↔ 查询词）
          ├── state.json                     ← 状态快照归档
          └── raw/                           ← 每批原始搜索结果归档
```

## 运行前提

复制 `.claude/skills/autosource/config.example.json` 为 `.claude/skills/autosource/config.json`，填入 LLM 网关的 `base_url` / `api_key` / `model`（含密钥，不入库）。全部参数集中在此文件，脚本读取、不硬编码：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| llm.base_url | 无（必填） | LLM 网关地址（OpenAI Chat Completions 兼容；结尾不带 `/chat/completions`，脚本自动拼接） |
| llm.api_key | 无（必填） | 网关密钥（不入库） |
| llm.model | 无（必填） | 网关模型名 |
| llm.timeout | 60 | 单次网关调用的网络超时（秒）；网关对长生成偏慢时可调大（如 300） |
| llm.retry | 2 | LLM 调用重试次数（JSON 非法 / 超时 / 网关错误） |
| converge.k | 4 | 连续无新增批次阈值（同时作为修订证据窗口宽度与"连续无新提案"阈值） |
| converge.queries_per_batch_min / max | 3 / 5 | plan 每批生成的查询词数量区间（脚本校验，超界视为契约失败） |
| converge.retry | 2 | 单次查询重试次数（首次 + 2 次重试） |
| converge.fail_rate_threshold | 0.5 | 连续 2 批失败率 ≥ 此值判定搜索服务异常，中止循环 |
| converge.fuse_batch_limit | 100 | 存活熔断：总批次数达上限判定收敛判据失效，异常中止（保留状态） |
| converge.revision_evidence_min | 2 | 新节点提案须携带的有效证据来源数下限（须为近 K 批新增，历史来源不作证据） |
| converge.revision_accept_max | 2 | review 每轮最多采纳的修订数（超出按拒绝处理） |
| converge.gaps_max | 10 | 缺口清单条目上限（超限视为契约违反，重试） |

## 架构

```
Skill     .claude/skills/autosource/SKILL.md          ← 循环编排指令（固定步骤循环，脚本判定收敛）
脚本      .claude/skills/autosource/scripts/
          ├── orchestrator.py   命令入口：--init / --plan / --commit / --review / --finalize
          ├── state.py          状态读写 / schema 校验 / 原子写入 / 树校验 / 增量写回
          ├── converge.py       收敛判据（客观覆盖三条件 + 存活熔断 + 失败率）
          ├── search_provider.py 搜索源接口（SearchProvider.fetch）+ HostSearchProvider
          ├── evidence.py       证据链校验（边界匹配算法）
          ├── llm_client.py     OpenAI 兼容网关调用
          ├── prompts.py        5 个 LLM 节点 prompt 模板（init / plan / extract / review / report）
          └── deliver.py        交付物生成（CSV / stats / 搜索日志 / 溯源 / 报告）
```

循环由脚本驱动、按固定步骤执行：「--init → 循环（--plan → websearch → --commit → --review）→ --finalize」；收敛判定、证据校验、去重、状态管理全部由脚本保证，LLM 只承担语义环节（拆解 / 规划 / 提取 / 评审 / 报告）。

## 测试

```bash
python -m pytest tests/ -q
```

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/AutoSource2.0设计.md](docs/AutoSource2.0设计.md) | 设计决策：收敛式循环（领域理解 / 反馈闭环 / 收敛判据）、职责分工、复用边界与领域知识沉淀 |
| [docs/AutoSource2.0实现规格.md](docs/AutoSource2.0实现规格.md) | 实现规格：收敛与失败参数、状态 schema、宿主↔脚本交接接口、证据链算法、prompt 模板、交付物格式、验收测试清单 |
