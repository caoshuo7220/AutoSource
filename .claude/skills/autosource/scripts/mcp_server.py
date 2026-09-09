"""AutoSource MCP stdio 服务：五工具（docs/04 存储架构改造）。

record_sources(run_dir, entries)   — 批次入库即验（store.py.record_sources）
record_search(run_dir, entries)    — 搜索日志批量追加（store.py.record_search）
record_knowledge(run_dir, entries) — 清单核对结果批量入库（store.py.record_knowledge）
coverage(run_dir)                  — 每节点"已收 vs 提取"只读计数（只测缺失）
finalize(run_dir)                  — 收尾折叠（postprocess.fold），返回汇总文本

run_dir 为初始化 --prepare 打印的运行目录（outputs/run_{时间戳}/）；
manifest.json（阶段 0-1 由模型 Write）与 store.jsonl（本服务持有、模型不可见）
都在 run_dir 内。分工：模型只传语义判断结果，持久化/校验/对账/折叠全部在本服务。

模型最大单次输出 = record 批次（一个节点条目量 15-25KB）——写入截断在机制上
不可能发生（docs/04 §2）。
"""
import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import postprocess
import store

mcp = FastMCP("autosource-store")

# scripts 目录在 .claude/skills/autosource/scripts 下，项目根 = 上溯 4 级
# （parents[3] 是 .claude——2026-08-31 首轮实测 off-by-one：相对路径解析到
# .claude/outputs/ 下导致 store 与证据留痕全找不到，钉进测试）
PROJECT_ROOT = Path(__file__).resolve().parents[4]

_self_check_problems: list | None = None


def _resolve(run_dir: str) -> Path:
    p = Path(run_dir)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _evidence_path(run_dir: str | Path) -> Path:
    """本运行目录内的证据留痕（运行级归属，2026-08-31 分层原则修订）：hook 按
    .session_id 标记写入 run_*/evidence.jsonl——服务从 run_dir 直接定位，
    不再依赖会话 ID 环境变量。"""
    d = _resolve(str(run_dir))
    return d / "evidence.jsonl"


def self_check(run_dir: str | Path | None = None) -> list[dict]:
    """存储机制装配层自检：返回结构化问题列表 [{"code", "message"}]（空 = 通过）。

    code 是契约（调用方按 code 过滤，不按文案）：project_root / session_id /
    evidence_missing。2026-08-31 首轮实测教训：PROJECT_ROOT off-by-one 使 store 与
    证据留痕全部错位，18 条落库被拒才暴露，且运行模型被 deny 挡住无法看盘、只能
    推测"hook 未生效"。自检把装配层故障提前到首次工具调用，点名报错：
    ① 项目根锚定错误；② hook 进程依赖的会话 ID 环境变量缺失（hook 靠它按
    .session_id 标记定位运行目录）；③（传入 run_dir 时）证据文件缺失——
    hook 未生效，或未先执行 --prepare。
    """
    problems: list[dict] = []
    anchor = PROJECT_ROOT / ".claude" / "skills" / "autosource" / "scripts" / "mcp_server.py"
    if not anchor.is_file():
        problems.append({
            "code": "project_root",
            "message": (f"项目根解析错误：{PROJECT_ROOT}（应解析到仓库根、含 "
                        ".claude/skills/autosource/scripts/mcp_server.py；请检查 PROJECT_ROOT 推导）"),
        })
    if not os.environ.get("CLAUDE_CODE_SESSION_ID"):
        problems.append({
            "code": "session_id",
            "message": ("hook 进程依赖的 CLAUDE_CODE_SESSION_ID 环境变量缺失——hook 按会话"
                        "标记定位运行目录写入证据，缺失会回退旧共享路径、证据进不了运行目录"),
        })
    if run_dir is not None:
        evidence = _evidence_path(run_dir)
        if not evidence.exists():
            problems.append({
                "code": "evidence_missing",
                "message": (f"证据留痕不存在：{evidence}（PostToolUse hook 未生效，或未先执行"
                            " --prepare——hook 按 .session_id 标记写入运行目录）"),
            })
    return problems


def _ensure_healthy(run_dir: str | Path) -> None:
    """每个工具入口调用：静态自检一次（结果缓存），证据存在性每次随 run_dir
    检查——装配层故障点名报错、按失败路径中止。"""
    global _self_check_problems
    if _self_check_problems is None:
        _self_check_problems = self_check()
    problems = [p["message"] for p in _self_check_problems]
    problems += [p["message"] for p in self_check(run_dir) if p["code"] == "evidence_missing"]
    if problems:
        raise RuntimeError(
            "存储机制自检失败，按失败路径中止本次运行（先修装配层再重跑）：\n"
            + "\n".join(f"- {p}" for p in problems))


def _manifest_nodes(run_dir: Path) -> list[str]:
    manifest = run_dir / "manifest.json"
    if not manifest.exists():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    nodes = data.get("nodes") if isinstance(data, dict) else None
    return [str(n) for n in nodes] if isinstance(nodes, list) else []


@mcp.tool()
def record_sources(run_dir: str, entries: list) -> str:
    """把一批新增数据源条目落库（入库即验）。

    何时调用：每完成一批搜索并提取新条目后调用一次——阶段 3 每节点基底 16 次
    搜索完成后一批、扩充每批 4 次再落一次；阶段 4 每节点收敛后一批；阶段 2 不需要
    （清单核对结果走 record_knowledge，见 SKILL 阶段 2/4）。条目字段：name/category_path/source_type/
    granularity/url/description/reason，URL 必须逐字照抄搜索结果（脚本逐条
    比对证据留痕，不在则当场拒绝并返回原因，可立即修正重传）。

    返回 JSON：{"accepted": 入库数, "skipped": 裸 URL 精确重复跳过数,
    "rejected": [{index, name, url, reason}], "unmapped": [表外原始体裁词]}——
    单条被拒不阻断批次；unmapped 列出 source_type 落「其他」的原始词（词表外，
    应改从 SKILL 词表内选择；高频新词可补别名进 store.SOURCE_TYPE_ALIASES）。
    """
    _ensure_healthy(run_dir)
    d = _resolve(run_dir)
    result = store.record_sources(d / "store.jsonl", entries, _evidence_path(d),
                                  _manifest_nodes(d))
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def record_search(run_dir: str, entries: list) -> str:
    """把一批搜索日志落库（替代 raw.json 的 journal 字段）。

    何时调用：阶段 2 每 ~10-15 次验证搜索一批；阶段 3/4 每节点完成时一批。
    每项 {phase, node, query, results, extracted}——query 必须照抄实际发出的
    查询词（脚本在收尾时逐字比对证据留痕，不在则标注"证据缺失"）。

    返回追加条数。
    """
    _ensure_healthy(run_dir)
    d = _resolve(run_dir)
    n = store.record_search(d / "store.jsonl", entries)
    return json.dumps({"appended": n}, ensure_ascii=False)


@mcp.tool()
def record_knowledge(run_dir: str, entries: list) -> str:
    """把一批知识清单核对结果落库（2026-09-08 架构修订：核对结果随验证过程落库，
    废除"会话暂存 + 阶段 5 一次性转写 manifest"——手工转写 65 条 JSON 曾漏写 60
    个 verified 字段）。

    何时调用：阶段 2 每验证 ~10-15 项后一批（与 record_search 同节奏）；阶段 4
    扩量轮定案项一批。每项 {name, node, verified, ...}：
    - verified=true：带 category_path/source_type/granularity/url/description/
      reason，URL 逐字照抄搜索结果（入库即验证据链，不在则当场拒绝）
    - verified=false：带 note（"疑似无效机构"/"未找到官方入口"），不带 URL
    同名重录 = 状态更新（收尾折叠取末次）。清单项全部了结是阶段 4 的终止条件——
    收尾时声明清单中无核对记录的项会拒绝折叠并点名。

    返回 JSON：{"accepted": 入库数, "rejected": [{index, name, reason}],
    "unmapped": [表外原始体裁词]}。
    """
    _ensure_healthy(run_dir)
    d = _resolve(run_dir)
    result = store.record_knowledge(d / "store.jsonl", entries, _evidence_path(d))
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def coverage(run_dir: str) -> str:
    """查每节点"已收 vs 提取"的缺口（只读，不改数据）。

    何时调用：阶段 4 扩量判断与收尾前自查。missing 是粗略缺口信号（提取数含
    去重前与跨节点顺路发现、已收数是幂等去重后的入库数，两口径天然有差），
    小额 missing 不触发补搜；接近该节点一整批提取量才怀疑漏调 record_sources。
    只测条数缺失；体裁/来源维度是否单一由模型在会话内判断（依据是它刚提取的内容）。

    返回 [{node, recorded, extracted, missing}]（missing = max(0, 提取-已收)）。
    """
    _ensure_healthy(run_dir)
    d = _resolve(run_dir)
    result = store.coverage(d / "store.jsonl", _manifest_nodes(d))
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def finalize(run_dir: str) -> str:
    """收尾折叠：读 store + manifest → 复用 postprocess 全链路（证据终检/清单
    并入/去重/CSV/stats/溯源/搜索日志/目录重命名/归档）→ 返回汇总文本。

    何时调用：阶段 6 收尾、全部搜索与记录完成后调用一次。之后写分析报告并
    运行 --rename-report（报告流程不变）。

    防截断哨兵：store 来源为 0 且搜索提取合计 > 0 时拒绝折叠并报错（模型漏调
    record_sources 时失败响亮，不会静默产出空清单）；清单了结哨兵：声明清单中
    无核对记录的项拒绝折叠并点名（漏调 record_knowledge 时同样响亮）。
    失败发生在目录重命名前，修正后可安全重跑。成功时 store/manifest 归档进 intermediate/。
    """
    _ensure_healthy(run_dir)
    d = _resolve(run_dir)
    summary = postprocess.fold(str(d), out_dir=str(PROJECT_ROOT / "outputs"))
    return postprocess.summary_text(summary)


if __name__ == "__main__":
    mcp.run()
