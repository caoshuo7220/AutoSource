# AutoSource

数据源自动发现 Skill（Claude Code）——输入 `/autosource <领域描述>`，自动完成**领域拆解 → 数据源搜索 → 去重整理 → 导出 CSV**全流程，产出该领域的公开数据源清单。

## 快速开始

```
/autosource 交换机
/autosource 算力服务器 -c "GPU服务器" "CPU服务器"

输出: outputs/{领域词}_{时间戳}/
      ├── {领域词}_{时间戳}_数据源清单.csv   ← 交付物（UTF-8 BOM，Excel 直接打开）
      ├── {领域词}_{时间戳}_stats.csv        ← 交付物（清单统计：每节点条数/体裁分布 + 总计行）
      ├── 分析报告.md                        ← 交付物（领域分析：概览/技术格局/产业生态/标准体系/中外对比/趋势观察）
      └── intermediate/                      ← 排障材料（发现问题时才查）
          ├── {领域词}_{时间戳}_搜索日志.csv  ← 搜索复盘（每次搜索的查询词/提取数）
          ├── {领域词}_{时间戳}_溯源.csv      ← 数据血缘（正查/反查：结果与收录对应）
          ├── raw_input.json                ← 输入快照（过滤前全量，调试/复盘）
          └── evidence_log.jsonl            ← 证据留痕（本运行切片，调试/复盘）
```

首次使用需信任项目（确认一次 settings.json 的权限与 hook 分发）。

## 架构

```
Skill     .claude/skills/autosource/SKILL.md               ← 单条线性流程：拆解 → 知识清单 → 验证搜索 → 增量发现 → 扩量 → 收尾
后处理    .claude/skills/autosource/scripts/postprocess.py ← 证据校验/去重/CSV/stats/清理（确定性环节全部代码化）
证据链    .claude/skills/autosource/scripts/log_tool.py    ← PostToolUse hook：系统记录搜索留痕，防 URL 编造
```

设计原则：**LLM 只负责语义（拆解、判断），确定性环节全部脚本化**。

## 测试

```bash
python -m pytest tests/ -q   # 110 个测试，预期全过
```

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/01-需求文档.md](docs/01-需求文档.md) | 需求规格与验收标准（做什么） |
| [docs/02-搜索方案.md](docs/02-搜索方案.md) | 两路搜索方案设计（知识清单 + 验证搜索 + 增量发现），已定稿并已实施 |
| [docs/03-实施计划.md](docs/03-实施计划.md) | 已建成的实现与当前状态 |
| [docs/04-问题记录手册.md](docs/04-问题记录手册.md) | 问题清单（P-001~P-008）、决策记录与讨论日志 |
| [docs/05-交付手册.md](docs/05-交付手册.md) | 交付接手者必读：关键决策、已知问题、下一步 |
| [docs/06-参考方案手册.md](docs/06-参考方案手册.md) | 外部调研：同类方案盘点、可复用组件、社区共识模式对照 |
