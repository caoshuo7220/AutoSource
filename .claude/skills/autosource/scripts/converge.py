"""AutoSource 2.0 收敛判定：客观覆盖条件 + 连续两批失败率 + 存活熔断。

客观覆盖是 LLM 收敛确认的前置（一票否决，实现规格第四章）：所有节点
dims - angles 为空、缺口清单为空、连续 K 批无新增来源、连续 K 批无新修订
提案、修订池为空（协议时序不变量）；K 从 config.converge.k 读取，不硬编码
（实现规格第一章）。存活熔断与失败率同属脚本确定性判定。
"""
from typing import Optional


def objective_conditions(state: dict, k: int) -> tuple[bool, list[str]]:
    """客观覆盖条件校验。返回 (是否全部满足, 未满足原因列表)。

    条件（实现规格 L2 修订）：所有节点 dims - angles 为空；gaps 为空；
    连续无新增 ≥ K；连续无新修订提案 ≥ K；修订池为空（协议时序不变量）。
    """
    reasons: list[str] = []
    for node in state["structure"]["nodes"]:
        uncovered = [dim for dim in node.get("dims", [])
                     if dim not in node.get("angles", [])]
        if uncovered:
            reasons.append(f"节点「{node['name']}」存在未搜索维度: {uncovered}")
    gaps = state["exploration"]["gaps"]
    if gaps:
        reasons.append(f"缺口清单非空（{len(gaps)} 项）")
    stats = state["exploration"]["loop_stats"]
    consecutive = stats["consecutive_no_new"]
    if consecutive < k:
        reasons.append(f"连续无新增批次 {consecutive} 未达 K({k})")
    no_proposal = stats["consecutive_no_proposal"]
    if no_proposal < k:
        reasons.append(f"连续无新提案批次 {no_proposal} 未达 K({k})")
    if state.get("pending_revisions"):
        reasons.append(f"修订池非空（{len(state['pending_revisions'])} 项待裁决）")
    return (not reasons, reasons)


def last_two_batches_failing(history: list[dict], threshold: float) -> bool:
    """连续 2 批失败率均 ≥ 阈值 → 判定搜索服务异常（实现规格第四章）。

    按 search_history 行的 batch 分组求每批失败率，只看最近 2 批；
    不足 2 批不判定。
    """
    batches: dict[int, list[int]] = {}
    for row in history:
        batch = row.get("batch")
        if not isinstance(batch, int):
            continue
        batches.setdefault(batch, []).append(1 if row.get("failed") else 0)
    recent = [batches[b] for b in sorted(batches)[-2:]]
    if len(recent) < 2:
        return False
    return all(sum(rows) / len(rows) >= threshold for rows in recent)


def fuse_triggered(batch_count: int, limit: int) -> bool:
    """存活熔断：总批次数达到上限判定收敛判据失效，异常中止（实现规格第一章）。"""
    return batch_count >= limit


def fail_reason(history: list[dict]) -> Optional[str]:
    """失败率判定的可读说明（触发时用）。"""
    batches: dict[int, list[int]] = {}
    for row in history:
        batch = row.get("batch")
        if not isinstance(batch, int):
            continue
        batches.setdefault(batch, []).append(1 if row.get("failed") else 0)
    recent = [batches[b] for b in sorted(batches)[-2:]]
    rates = [f"{sum(rows)}/{len(rows)}" for rows in recent]
    return "连续 2 批失败率 " + "、".join(rates)
