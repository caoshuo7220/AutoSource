"""converge.py 的单元测试：客观覆盖三条件、连续两批失败率、存活熔断。"""
from converge import fuse_triggered, last_two_batches_failing, objective_conditions


def _state(gaps=(), angles=None, dims=None, consecutive_no_new=4, k=4):
    """构造客观覆盖判定所需的最小状态。"""
    nodes = [
        {"name": "交换机", "parent": "", "terms": [], "entities": [],
         "dims": [], "angles": []},
        {"name": "数据中心交换机", "parent": "交换机", "terms": [], "entities": [],
         "dims": dims if dims is not None else ["官方文档"],
         "angles": angles if angles is not None else ["官方文档"]},
    ]
    return {
        "exploration": {
            "gaps": [{"description": d, "node": "数据中心交换机"} for d in gaps],
            "loop_stats": {"consecutive_no_new": consecutive_no_new},
        },
        "structure": {"nodes": nodes},
    }


def test_objective_met():
    met, reasons = objective_conditions(_state(), k=4)
    assert met is True
    assert reasons == []


def test_objective_unmet_dims():
    """客观覆盖：dims - angles 非空即不通过，一票否决。"""
    met, reasons = objective_conditions(_state(angles=[]), k=4)
    assert met is False
    assert any("维度" in r for r in reasons)


def test_objective_unmet_gaps():
    met, reasons = objective_conditions(_state(gaps=["缺国内厂商指南"]), k=4)
    assert met is False
    assert any("缺口" in r for r in reasons)


def test_objective_unmet_no_new():
    """连续无新增未达 K：不通过（K 从参数来，不硬编码）。"""
    met, reasons = objective_conditions(_state(consecutive_no_new=2), k=4)
    assert met is False
    assert any("无新增" in r for r in reasons)


def test_objective_no_new_reaches_k():
    """连续无新增恰达 K：该条件满足。"""
    met, _ = objective_conditions(_state(consecutive_no_new=4), k=4)
    assert met is True


def test_last_two_batches_failing_trigger():
    """连续 2 批失败率均 ≥ 阈值 → 判定搜索服务异常。"""
    history = [
        {"batch": 1, "query": "a", "node": "n", "failed": 1},
        {"batch": 1, "query": "b", "node": "n", "failed": 0},
        {"batch": 2, "query": "c", "node": "n", "failed": 1},
        {"batch": 2, "query": "d", "node": "n", "failed": 1},
    ]
    assert last_two_batches_failing(history, 0.5) is True


def test_last_two_batches_failing_not_trigger_mixed():
    """最近一批失败率不足（1/3 < 50%）→ 不触发。"""
    history = [
        {"batch": 1, "query": "a", "node": "n", "failed": 1},
        {"batch": 1, "query": "b", "node": "n", "failed": 1},
        {"batch": 2, "query": "c", "node": "n", "failed": 1},
        {"batch": 2, "query": "d", "node": "n", "failed": 0},
        {"batch": 2, "query": "e", "node": "n", "failed": 0},
    ]
    assert last_two_batches_failing(history, 0.5) is False


def test_last_two_batches_failing_insufficient_batches():
    """不足 2 批不判定（连续 2 批的条件不成立）。"""
    history = [{"batch": 1, "query": "a", "node": "n", "failed": 1},
               {"batch": 1, "query": "b", "node": "n", "failed": 1}]
    assert last_two_batches_failing(history, 0.5) is False


def test_last_two_batches_failing_ignores_older_batches():
    """只看最近 2 批：更早的失败批次不影响判定。"""
    history = [
        {"batch": 1, "query": "a", "node": "n", "failed": 1},
        {"batch": 1, "query": "b", "node": "n", "failed": 1},  # 批 1 全失败
        {"batch": 2, "query": "c", "node": "n", "failed": 0},
        {"batch": 3, "query": "d", "node": "n", "failed": 0},
    ]
    assert last_two_batches_failing(history, 0.5) is False


def test_fuse_triggered():
    assert fuse_triggered(100, 100) is True
    assert fuse_triggered(101, 100) is True
    assert fuse_triggered(99, 100) is False
