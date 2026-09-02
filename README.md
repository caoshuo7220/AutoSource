# AutoSource

数据源自动发现 Skill（Claude Code）——输入 `/autosource <领域描述>`，自动完成**领域拆解 → 数据源搜索 → 去重整理 → 导出 CSV**全流程，产出该领域的公开数据源清单。

## 快速开始

```
/autosource 交换机

输出: outputs/{领域词}_{时间戳}/
      ├── {领域词}_{时间戳}_数据源清单.csv   ← 交付物（UTF-8 BOM，Excel 直接打开）
      ├── {领域词}_{时间戳}_分析报告.md      ← 交付物（领域分析：概览/技术格局/产业生态/标准体系/中外对比/趋势观察）
      └── intermediate/                      ← 排障与对比材料（发现问题时才查）
          ├── {领域词}_{时间戳}_stats.csv    ← 清单统计（每节点条数/体裁分布 + 总计行；跨轮对比）
          ├── {领域词}_{时间戳}_搜索日志.csv  ← 搜索复盘（每次搜索的查询词/提取数）
          ├── {领域词}_{时间戳}_溯源.csv      ← 数据血缘（正查/反查：结果与收录对应）
          ├── store_input.jsonl             ← 存储快照（record 工具落库的原始 store）
          ├── manifest_input.json           ← 清单快照（声明版/最终核对态归档）
          └── evidence_log.jsonl            ← 证据留痕（本运行切片，调试/复盘）
```

首次使用需信任项目（确认一次 settings.json 的权限与 hook 分发）。

## 架构

```
Skill     .claude/skills/autosource/SKILL.md               ← 单条线性流程：拆解 → 知识清单 → 验证搜索 → 增量发现 → 扩量 → 收尾
后处理    .claude/skills/autosource/scripts/postprocess.py ← 流水线编排（证据校验/去重/CSV/stats/清理 + finalize 折叠）
          ├── evidence.py ← 证据链（留痕定位/边界匹配/切片/锚点清理）
          ├── lineage.py  ← 数据血缘（溯源.csv 与"来源搜索"归因）
          └── report.py   ← 分析报告（数据总览注入 + 命名）
存储层    .claude/skills/autosource/scripts/store.py       ← store.jsonl 追加日志 + 入库即验 + coverage 对账（模型不碰数据文件）
MCP 服务  .claude/skills/autosource/scripts/mcp_server.py  ← 四工具：record_sources / record_search / coverage / finalize（.mcp.json 注册，会话自动拉起）
证据链    .claude/skills/autosource/scripts/evidence_hook.py ← PostToolUse hook：系统记录搜索留痕，防 URL 编造（校验逻辑在 evidence.py）
```

设计原则：**LLM 只负责语义（拆解、判断），确定性环节全部脚本化**——数据落盘走 MCP 工具入库（单次输出 ≤ 一个节点批次，写入截断在机制上不可能发生），元数据走 manifest.json。

## 测试

```bash
python -m pytest tests/ -q   # 184 个测试，预期全过
```

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/01-交付手册.md](docs/01-交付手册.md) | 交付接手者必读：项目全景、关键决策、验收标准、使用说明 |
| [docs/02-日志手册.md](docs/02-日志手册.md) | 每日工作日志（干了什么、每个决策的来龙去脉）、两路方案决策集附录（D1-D11） |
| [docs/03-待办账本.md](docs/03-待办账本.md) | 挂账/待实施/验收未决项（单一事实源，带触发条件与出处） |
| [docs/04-存储架构改造方案.md](docs/04-存储架构改造方案.md) | 写入截断根治方案（已实施，2026-08-31 验收通过）：MCP 四工具 + manifest 瘦身 + store JSONL |
| [docs/05-参考方案手册.md](docs/05-参考方案手册.md) | 外部调研：同类方案盘点、可复用组件、社区共识模式对照 |
