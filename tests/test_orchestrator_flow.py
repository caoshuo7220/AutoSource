"""orchestrator.py 的流程测试：五子命令协作 + 实现规格第十章验收清单逐项覆盖。

全部走真实 CLI 入口（进程内调用 main），LLM 由 FakeLLM 替代、搜索由搜索结果文件模拟；
状态落盘、收敛判定、证据校验、去重均为真实代码路径。
"""
import json
import os
from pathlib import Path

import pytest

from llm_client import LLMError

from tests.conftest import done_entry, load_state_dict, write_results

URL_A = "https://a.example/1"
URL_B = "https://b.example/2"


def update_config(cfg: dict, **converge_overrides) -> dict:
    """改收敛参数并重写 config 文件（参数从 config 读取的测试即真实路径）。"""
    cfg["converge"].update(converge_overrides)
    Path(os.environ["AUTOSOURCE_CONFIG"]).write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return cfg


def plan_queries(cli, run_dir: Path) -> list[dict]:
    code, out, err = cli("--plan", run_dir)
    assert code == 0, f"--plan 失败: {out}{err}"
    return json.loads(out)["queries"]


def commit_results(cli, run_dir: Path, queries: list[dict],
                   results_by_id: dict) -> tuple[int, str, str]:
    """按 query_id 写搜索结果文件并 commit；results_by_id: {qid: (results, failed, attempts[, error])}。"""
    entries = []
    for query in queries:
        qid = query["query_id"]
        if qid in results_by_id:
            results, failed, attempts = results_by_id[qid][:3]
            entry = {"query_id": qid, "query": query["query"], "results": results,
                     "failed": failed, "attempts": attempts}
            if len(results_by_id[qid]) > 3:
                entry["error"] = results_by_id[qid][3]
        else:
            entry = done_entry(qid, query["query"], [])
        entries.append(entry)
    write_results(run_dir, entries)
    return cli("--commit", run_dir, run_dir / "search_results.json")


def commit_no_new(cli, run_dir: Path, queries: list[dict]) -> tuple[int, str, str]:
    """全部 query 成功且 0 结果的批次提交。"""
    return commit_results(cli, run_dir, queries,
                          {q["query_id"]: ([], False, 1) for q in queries})


def converge(cli, fake_llm, run_dir: Path) -> str:
    """两批无新增、覆盖全部 dims 后 review 收敛，返回 review stdout。"""
    queries = plan_queries(cli, run_dir)
    code, out, err = commit_no_new(cli, run_dir, queries)
    assert code == 0, f"第一批 commit 失败: {out}{err}"
    fake_llm.responses["plan"] = {"queries": [
        {"query": "数据中心交换机 行业标准", "node": "数据中心交换机",
         "angle": "行业标准", "reason": "补行业标准"},
        {"query": "campus switch open source", "node": "园区交换机",
         "angle": "开源社区", "reason": "补开源社区"},
    ]}
    queries = plan_queries(cli, run_dir)
    code, out, err = commit_no_new(cli, run_dir, queries)
    assert code == 0, f"第二批 commit 失败: {out}{err}"
    code, out, err = cli("--review", run_dir)
    assert code == 0, f"--review 失败: {out}{err}"
    return out


# ---------- 基础流程 ----------

def test_init_creates_state_and_prints_path(config, init_run):
    run_dir = init_run("AI 算力服务器")
    state = load_state_dict(run_dir)
    assert state["domain"] == "交换机"  # domain = 根节点名（init 提炼）
    assert state["phase"] == "running"
    assert state["pending_batch"] == {}
    assert state["exploration"]["loop_stats"]["batch_count"] == 0
    assert run_dir.name.startswith("run_")
    assert run_dir.parent == Path(os.environ["AUTOSOURCE_OUTPUTS"])


def test_init_invalid_tree_fails(config, cli, fake_llm):
    """init 输出双根节点（树校验失败）→ 显式失败 + phase=failed 状态保留。"""
    fake_llm.responses["init"] = {"nodes": [
        {"name": "A", "parent": "", "terms": [], "dims": []},
        {"name": "B", "parent": "", "terms": [], "dims": []},
    ]}
    code, out, err = cli("--init", "交换机")
    assert code == 1
    assert "本次运行失败" in err
    run_dirs = list(Path(os.environ["AUTOSOURCE_OUTPUTS"]).glob("run_*"))
    assert len(run_dirs) == 1
    assert load_state_dict(run_dirs[0])["phase"] == "failed"


def test_plan_persists_batch_and_angle_to_dims(config, init_run, cli, fake_llm):
    run_dir = init_run()
    fake_llm.responses["plan"] = {"queries": [
        {"query": "数据中心交换机 行业标准", "node": "数据中心交换机",
         "angle": "行业标准", "reason": "补行业标准"},
        {"query": "campus switch 新维度", "node": "园区交换机",
         "angle": "新维度", "reason": "探索新维度"},
    ]}
    queries = plan_queries(cli, run_dir)
    assert [q["query_id"] for q in queries] == [1, 2]
    state = load_state_dict(run_dir)
    pending = state["pending_batch"]
    assert pending["batch_id"] == 1
    assert all(q["status"] == "pending" and q["attempts"] == 0
               for q in pending["queries"])
    nodes = {n["name"]: n for n in state["structure"]["nodes"]}
    assert "行业标准" in nodes["数据中心交换机"]["dims"]  # 原 dims 已有，不重复
    assert "新维度" in nodes["园区交换机"]["dims"]  # plan 新 angle 记入 dims
    assert nodes["园区交换机"]["dims"].count("新维度") == 1


def test_plan_rerun_prints_existing_pending_without_llm(config, init_run, cli, fake_llm):
    """中断恢复辅助：pending 非空时 --plan 重印已有批次，不调 LLM、不重新编号。"""
    run_dir = init_run()
    first = plan_queries(cli, run_dir)
    second = plan_queries(cli, run_dir)
    assert first == second
    assert sum(1 for node, _ in fake_llm.calls if node == "plan") == 1


def test_plan_refused_when_phase_failed(config, init_run, cli):
    run_dir = init_run()
    state = load_state_dict(run_dir)
    state["phase"] = "failed"
    (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False),
                                        encoding="utf-8")
    code, out, err = cli("--plan", run_dir)
    assert code == 1
    assert "failed" in err


def test_commit_evidence_rejection(config, init_run, cli, fake_llm):
    """证据链：extract 的 URL 不在搜索结果中 → 拒绝且不入库，批次照常完成。"""
    run_dir = init_run()
    queries = plan_queries(cli, run_dir)
    fake_llm.responses["extract"] = {"sources": [
        {"name": "合法源", "url": URL_A, "source_type": "官方文档",
         "granularity": "合集级", "node": "数据中心交换机", "description": "在结果中"},
        {"name": "编造源", "url": "https://evil.example/fake", "source_type": "官方文档",
         "granularity": "合集级", "node": "数据中心交换机", "description": "凭先验知识补"},
    ], "new_entities": [], "new_terms": [], "new_nodes": []}
    code, out, err = commit_results(cli, run_dir, queries,
                                    {1: ([{"title": "A", "url": URL_A, "snippet": ""}], False, 1)})
    assert code == 0
    assert "证据" in out and "编造源" in out
    sources = load_state_dict(run_dir)["sources"]
    assert [s["name"] for s in sources] == ["合法源"]


def test_commit_missing_entry_marked_failed(config, init_run, cli):
    """搜索结果文件缺失某 query 条目 → 视为 failed（attempts=0），不静默丢弃。"""
    run_dir = init_run()
    queries = plan_queries(cli, run_dir)
    # 文件只含 q1，q2 缺失
    write_results(run_dir, [done_entry(1, queries[0]["query"], [])])
    code, out, err = cli("--commit", run_dir, run_dir / "search_results.json")
    assert code == 0
    state = load_state_dict(run_dir)
    assert state["exploration"]["loop_stats"]["failed_queries"] == 1
    assert [h["failed"] for h in state["exploration"]["search_history"]] == [0, 1]
    assert state["pending_batch"] == {}


def test_commit_extract_failure_sets_failed(config, init_run, cli, fake_llm):
    """LLM 节点失败无法恢复 → phase=failed，保留状态（pending 不清空）。"""
    run_dir = init_run()
    queries = plan_queries(cli, run_dir)
    fake_llm.responses["extract"] = LLMError("网关超时")
    code, out, err = commit_no_new(cli, run_dir, queries)
    assert code == 1
    assert "本次运行失败" in err
    state = load_state_dict(run_dir)
    assert state["phase"] == "failed"
    assert state["pending_batch"]["batch_id"] == 1  # 状态保留供检查


def test_commit_failure_rate_abort(config, init_run, cli, fake_llm):
    """连续 2 批失败率 ≥ 50% → 搜索服务异常中止，保留已收录数据源。"""
    run_dir = init_run()
    queries = plan_queries(cli, run_dir)
    fake_llm.responses["extract"] = {"sources": [
        {"name": "已收录源", "url": URL_A, "source_type": "官方文档",
         "granularity": "合集级", "node": "数据中心交换机", "description": ""}],
        "new_entities": [], "new_terms": [], "new_nodes": []}
    code, out, err = commit_results(cli, run_dir, queries,
                                    {1: ([{"title": "A", "url": URL_A, "snippet": ""}], False, 1)})
    assert code == 0
    # 第 2、3 批：全部 failed
    for _ in range(2):
        queries = plan_queries(cli, run_dir)
        code, out, err = commit_results(
            cli, run_dir, queries,
            {q["query_id"]: ([], True, 3, "timeout") for q in queries})
    assert code == 1
    assert "本次运行失败" in out
    assert "失败率" in out or "搜索服务异常" in out
    state = load_state_dict(run_dir)
    assert state["phase"] == "failed"
    assert [s["name"] for s in state["sources"]] == ["已收录源"]


def test_review_objective_veto(config, init_run, cli, fake_llm):
    """客观覆盖一票否决：LLM 声称 converged 但 dims 未覆盖 → 返回 false。"""
    run_dir = init_run()
    queries = plan_queries(cli, run_dir)
    commit_no_new(cli, run_dir, queries)
    fake_llm.responses["review"] = {"gaps": [], "converged": True,
                                    "reason": "自认为充分"}
    code, out, err = cli("--review", run_dir)
    assert code == 0
    result = json.loads(out)
    assert result["converged"] is False
    assert "客观覆盖" in result["reason"]
    assert load_state_dict(run_dir)["phase"] == "running"


def test_review_llm_failure_sets_failed(config, init_run, cli, fake_llm):
    run_dir = init_run()
    queries = plan_queries(cli, run_dir)
    commit_no_new(cli, run_dir, queries)
    fake_llm.responses["review"] = LLMError("网关超时")
    code, out, err = cli("--review", run_dir)
    assert code == 1
    assert "本次运行失败" in err
    assert load_state_dict(run_dir)["phase"] == "failed"


def test_finalize_requires_converged(config, init_run, cli):
    run_dir = init_run()
    code, out, err = cli("--finalize", run_dir)
    assert code == 1
    assert "converged" in err or "收敛" in err
    assert run_dir.exists()  # 未收敛时目录不被重命名


# ---------- 第十章验收清单 ----------

def test_empty_success_results(config, init_run, cli, fake_llm):
    """验收 1：搜索成功但 0 条结果 → 计入 done + 无新增，不计 failed。"""
    run_dir = init_run()
    queries = plan_queries(cli, run_dir)
    code, out, err = commit_results(cli, run_dir, queries,
                                    {q["query_id"]: ([], False, 1) for q in queries})
    assert code == 0
    state = load_state_dict(run_dir)
    history = state["exploration"]["search_history"]
    assert [h["failed"] for h in history] == [0, 0]
    assert [h["result_count"] for h in history] == [0, 0]
    assert state["exploration"]["loop_stats"]["failed_queries"] == 0
    assert state["exploration"]["loop_stats"]["consecutive_no_new"] == 1
    assert sum(1 for node, _ in fake_llm.calls if node == "extract") == 1  # 空结果也走 extract


def test_partial_failure(config, init_run, cli, fake_llm):
    """验收 2：部分失败 → 失败率统计正确、failed 不影响无新增判断。"""
    run_dir = init_run()
    fake_llm.responses["extract"] = {"sources": [
        {"name": "合法源", "url": URL_A, "source_type": "官方文档",
         "granularity": "合集级", "node": "数据中心交换机", "description": ""}],
        "new_entities": [], "new_terms": [], "new_nodes": []}
    queries = plan_queries(cli, run_dir)
    code, out, err = commit_results(cli, run_dir, queries, {
        1: ([{"title": "A", "url": URL_A, "snippet": ""}], False, 1),
        2: ([], True, 3, "timeout"),
    })
    assert code == 0
    state = load_state_dict(run_dir)
    assert state["exploration"]["loop_stats"]["failed_queries"] == 1
    assert state["exploration"]["loop_stats"]["consecutive_no_new"] == 0  # 有新增即清零
    nodes = {n["name"]: n for n in state["structure"]["nodes"]}
    assert nodes["园区交换机"]["angles"] == []  # failed 的 angle 不记入已搜索

    # 第二批：done 的 query 0 新增 + 1 条 failed → 无新增计数只按 done 算
    fake_llm.responses["plan"] = {"queries": [
        {"query": "q1b", "node": "数据中心交换机", "angle": "行业标准", "reason": ""},
        {"query": "q2b", "node": "园区交换机", "angle": "开源社区", "reason": ""},
        {"query": "q3b", "node": "数据中心交换机", "angle": "官方文档", "reason": ""},
    ]}
    fake_llm.responses["extract"] = {"sources": [], "new_entities": [],
                                     "new_terms": [], "new_nodes": []}
    queries = plan_queries(cli, run_dir)
    code, out, err = commit_results(cli, run_dir, queries, {
        1: ([], False, 1), 2: ([], True, 3, "timeout"), 3: ([], False, 1)})
    assert code == 0
    state = load_state_dict(run_dir)
    assert state["exploration"]["loop_stats"]["failed_queries"] == 2
    assert state["exploration"]["loop_stats"]["consecutive_no_new"] == 1  # failed 不参与
    assert state["phase"] == "running"  # 最近两批失败率 1/2 与 1/3，未触发中止


def test_all_failed_resets_no_new(config, init_run, cli):
    """验收 3：整批全 failed → consecutive_no_new 重置为 0，不当作收敛证据。"""
    run_dir = init_run()
    queries = plan_queries(cli, run_dir)
    commit_no_new(cli, run_dir, queries)
    assert load_state_dict(run_dir)["exploration"]["loop_stats"]["consecutive_no_new"] == 1
    queries = plan_queries(cli, run_dir)
    code, out, err = commit_results(
        cli, run_dir, queries,
        {q["query_id"]: ([], True, 3, "timeout") for q in queries})
    assert code == 0
    state = load_state_dict(run_dir)
    assert state["exploration"]["loop_stats"]["consecutive_no_new"] == 0
    assert state["exploration"]["loop_stats"]["failed_queries"] == 2


def test_review_gaps_feed_plan(config, init_run, cli, fake_llm):
    """验收 4：review 每批更新 gaps，plan 能按新 gaps 补搜（反馈闭环）。"""
    run_dir = init_run()
    queries = plan_queries(cli, run_dir)
    commit_no_new(cli, run_dir, queries)
    fake_llm.responses["review"] = {"gaps": [
        {"description": "缺少国内交换机厂商的配置指南", "node": "数据中心交换机"}],
        "converged": False, "reason": "还有缺口"}
    code, out, err = cli("--review", run_dir)
    assert code == 0
    assert json.loads(out)["converged"] is False
    assert load_state_dict(run_dir)["exploration"]["gaps"][0]["description"] \
        == "缺少国内交换机厂商的配置指南"
    # 下一轮 plan 的 prompt 必须携带新 gaps（整体替换后的最新值）
    code, out, err = cli("--plan", run_dir)
    assert code == 0
    plan_prompt = [p for node, p in fake_llm.calls if node == "plan"][-1]
    assert "缺少国内交换机厂商的配置指南" in plan_prompt


def test_duplicate_query_text(config, init_run, cli, fake_llm):
    """验收 5：两个 query 文本相同 → 靠 query_id 区分，互不混淆。"""
    run_dir = init_run()
    fake_llm.responses["plan"] = {"queries": [
        {"query": "same text", "node": "数据中心交换机", "angle": "官方文档", "reason": "r1"},
        {"query": "same text", "node": "园区交换机", "angle": "厂商文档", "reason": "r2"},
    ]}
    fake_llm.responses["extract"] = {"sources": [
        {"name": "源A", "url": URL_A, "source_type": "官方文档", "granularity": "合集级",
         "node": "数据中心交换机", "description": ""},
        {"name": "源B", "url": URL_B, "source_type": "官方文档", "granularity": "合集级",
         "node": "园区交换机", "description": ""}],
        "new_entities": [], "new_terms": [], "new_nodes": []}
    queries = plan_queries(cli, run_dir)
    code, out, err = commit_results(cli, run_dir, queries, {
        1: ([{"title": "A", "url": URL_A, "snippet": ""}], False, 1),
        2: ([{"title": "B", "url": URL_B, "snippet": ""}], False, 1),
    })
    assert code == 0
    sources = {s["url"]: s for s in load_state_dict(run_dir)["sources"]}
    assert sources[URL_A]["node"] == "数据中心交换机"
    assert sources[URL_B]["node"] == "园区交换机"
    assert sources[URL_A]["first_seen_query"] == "same text"
    assert len(load_state_dict(run_dir)["exploration"]["search_history"]) == 2


def test_commit_rerun_idempotent(config, init_run, cli, fake_llm):
    """验收 6：commit 中断恢复——pending 已清空时重跑无操作、不重复入库。"""
    run_dir = init_run()
    queries = plan_queries(cli, run_dir)
    fake_llm.responses["extract"] = {"sources": [
        {"name": "合法源", "url": URL_A, "source_type": "官方文档",
         "granularity": "合集级", "node": "数据中心交换机", "description": ""}],
        "new_entities": [], "new_terms": [], "new_nodes": []}
    code, out, err = commit_results(cli, run_dir, queries,
                                    {1: ([{"title": "A", "url": URL_A, "snippet": ""}], False, 1)})
    assert code == 0
    before = load_state_dict(run_dir)
    code, out, err = cli("--commit", run_dir, run_dir / "search_results.json")
    assert code == 0
    assert "无待处理批次" in out
    after = load_state_dict(run_dir)
    assert after == before  # 幂等：状态一字不差


def test_finalize_after_converged(config, init_run, cli, fake_llm):
    """验收 7：review 已收敛、finalize 前中断 → 直接重跑 --finalize 成功。"""
    run_dir = init_run()
    review_out = converge(cli, fake_llm, run_dir)
    assert json.loads(review_out)["converged"] is True
    assert load_state_dict(run_dir)["phase"] == "converged"
    # 模拟中断后直接 finalize（无其他步骤）
    code, out, err = cli("--finalize", run_dir)
    assert code == 0, f"--finalize 失败: {out}{err}"
    assert not run_dir.exists()  # 目录已重命名
    out_dirs = list(Path(os.environ["AUTOSOURCE_OUTPUTS"]).glob("交换机_*"))
    assert len(out_dirs) == 1
    outdir = out_dirs[0]
    assert (outdir / f"{outdir.name}_数据源清单.csv").is_file()
    report = (outdir / f"{outdir.name}_分析报告.md").read_text(encoding="utf-8")
    assert "## 数据总览" in report
    assert "报告正文（测试桩）" in report
    intermediate = outdir / "intermediate"
    for name in ("_stats.csv", "_搜索日志.csv", "_溯源.csv", "state.json"):
        assert any(f.name.endswith(name) for f in intermediate.iterdir())
    assert (intermediate / "raw" / "batch_1.json").is_file()
    assert (intermediate / "raw" / "batch_2.json").is_file()


def test_first_seen_earliest(config, init_run, cli, fake_llm):
    """验收 8：同一 URL 多次命中，first_seen 取最早成功查询。"""
    run_dir = init_run()
    fake_llm.responses["extract"] = {"sources": [
        {"name": "SONiC 文档", "url": URL_A, "source_type": "官方文档",
         "granularity": "合集级", "node": "数据中心交换机", "description": ""}],
        "new_entities": [], "new_terms": [], "new_nodes": []}
    queries = plan_queries(cli, run_dir)
    code, out, err = commit_results(cli, run_dir, queries,
                                    {1: ([{"title": "A", "url": URL_A, "snippet": ""}], False, 1)})
    assert code == 0
    first_query = queries[0]["query"]
    # 第二批：同一 URL 再次命中 → 幂等跳过，first_seen 不变
    queries = plan_queries(cli, run_dir)
    code, out, err = commit_results(cli, run_dir, queries,
                                    {1: ([{"title": "A", "url": URL_A, "snippet": ""}], False, 1)})
    assert code == 0
    sources = load_state_dict(run_dir)["sources"]
    assert len(sources) == 1
    assert sources[0]["first_seen_batch"] == 1
    assert sources[0]["first_seen_query"] == first_query


def test_fuse_abort(config, init_run, cli, fake_llm):
    """验收 9：批次数达熔断上限 → 异常中止 + 保留状态 + 声明失败（未收敛）。"""
    update_config(config, fuse_batch_limit=3)
    run_dir = init_run()
    fake_llm.responses["extract"] = {"sources": [
        {"name": "已收录源", "url": URL_A, "source_type": "官方文档",
         "granularity": "合集级", "node": "数据中心交换机", "description": ""}],
        "new_entities": [], "new_terms": [], "new_nodes": []}
    for batch in range(1, 4):
        queries = plan_queries(cli, run_dir)
        code, out, err = commit_results(cli, run_dir, queries,
                                        {1: ([{"title": "A", "url": URL_A, "snippet": ""}], False, 1)})
    assert code == 1
    assert "本次运行失败" in out
    assert "熔断" in out and "未收敛" in out
    state = load_state_dict(run_dir)
    assert state["phase"] == "failed"
    assert state["exploration"]["loop_stats"]["batch_count"] == 3
    assert [s["name"] for s in state["sources"]] == ["已收录源"]  # 已收录数据源保留
    code, out, err = cli("--plan", run_dir)
    assert code == 1  # failed 状态拒绝继续规划


def test_report_llm_failure_degrade(config, init_run, cli, fake_llm):
    """验收 10：报告 LLM 失败 → 重试后仍失败则报告降级，CSV/stats 正常产出。"""
    run_dir = init_run()
    converge(cli, fake_llm, run_dir)
    fake_llm.responses["report"] = LLMError("网关超时")
    code, out, err = cli("--finalize", run_dir)
    assert code == 0
    outdir = next(Path(os.environ["AUTOSOURCE_OUTPUTS"]).glob("交换机_*"))
    report = (outdir / f"{outdir.name}_分析报告.md").read_text(encoding="utf-8")
    assert "生成失败" in report  # 降级正文
    assert "## 数据总览" in report  # 脚本注入的数据总览不受影响
    assert (outdir / f"{outdir.name}_数据源清单.csv").is_file()
    assert any(f.name.endswith("_stats.csv") for f in (outdir / "intermediate").iterdir())
