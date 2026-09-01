"""mcp_server.py 协议层测试（docs/05 §7.8：stdio 握手、工具路由、哨兵错误路径）。"""
import json
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent / ".claude" / "skills" / "autosource" / "scripts"
sys.path.insert(0, str(SKILL_DIR))

import mcp_server as ms


def _start():
    return subprocess.Popen([sys.executable, str(SKILL_DIR / "mcp_server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8")


def _rpc(proc, method, params=None, req_id=1):
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout")
        resp = json.loads(line)
        if resp.get("id") == req_id:
            return resp


def _init(proc):
    _rpc(proc, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "t", "version": "0"}})
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()


def _tool_text(resp) -> str:
    return resp["result"]["content"][0]["text"]


def test_project_root_and_relative_resolution():
    """2026-08-31 首轮实测 bug 钉进测试：PROJECT_ROOT 曾上溯到 .claude/（parents[3]
    off-by-one），相对 run_dir 解析到 .claude/outputs/ 下，store 与证据留痕全
    找不到、record_sources 全被拒。项目根必须锚定仓库根，相对路径锚定项目根。"""
    assert ms.PROJECT_ROOT.name == "AutoSource"
    assert (ms.PROJECT_ROOT / ".claude" / "skills" / "autosource" / "scripts"
            / "mcp_server.py").is_file()
    assert ms._resolve("outputs/run_x") == ms.PROJECT_ROOT / "outputs" / "run_x"
    assert ms._resolve(str(ms.PROJECT_ROOT / "outputs" / "run_abs")) \
        == ms.PROJECT_ROOT / "outputs" / "run_abs"


class TestSelfCheck:
    """启动自检：装配层故障提前到首次工具调用（2026-08-31 权限设计讨论结论）。
    证据按运行级归属（run_*/evidence.jsonl）随 run_dir 检查。"""

    def test_healthy_assembly_passes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-selftest-healthy")
        run_dir = tmp_path / "run_x"
        run_dir.mkdir()
        (run_dir / "evidence.jsonl").write_text("{}\n", encoding="utf-8")
        assert ms.self_check(run_dir) == []

    def test_missing_session_env_flagged(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        problems = ms.self_check()
        assert any("CLAUDE_CODE_SESSION_ID" in p for p in problems)

    def test_missing_evidence_flagged_with_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-selftest-no-evidence")
        run_dir = tmp_path / "run_x"
        run_dir.mkdir()
        problems = ms.self_check(run_dir)
        assert any("证据留痕不存在" in p and "evidence.jsonl" in p for p in problems)

    def test_bad_project_root_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ms, "PROJECT_ROOT", tmp_path)
        problems = ms.self_check()
        assert any("项目根解析错误" in p for p in problems)

    def test_unhealthy_assembly_fails_first_tool_call(self, tmp_path, monkeypatch):
        """装配层故障时首次工具调用即报错（而不是烧掉几十次搜索后在落库时爆）。"""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-selftest-unhealthy")
        run_dir = tmp_path / "run_x"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(json.dumps(
            {"domain": "x", "nodes": ["n"], "knowledge": []}, ensure_ascii=False),
            encoding="utf-8")
        proc = _start()
        try:
            _init(proc)
            r = _rpc(proc, "tools/call", {"name": "record_search", "arguments": {
                "run_dir": str(run_dir), "entries": [{"query": "q"}]}}, req_id=3)
            assert r["result"].get("isError") is True
            assert "自检失败" in _tool_text(r)
        finally:
            proc.terminate()


class TestMcpProtocol:
    def test_tool_routing_records_and_sentinel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-mcp-test")

        # run_dir 1：正常 record + coverage（证据按运行级归属写在 run_dir 内）
        run_dir = tmp_path / "run_ok"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(json.dumps(
            {"domain": "交换机", "nodes": ["数据中心交换机"], "model": "t",
             "knowledge": []}, ensure_ascii=False), encoding="utf-8")
        (run_dir / "evidence.jsonl").write_text(json.dumps(
            {"tool_name": "WebSearch", "tool_input": {"query": "q1"},
             "tool_response": {"results": [{"url": "https://a.com/doc"}]}},
            ensure_ascii=False) + "\n", encoding="utf-8")

        # run_dir 2：只有搜索记录没有来源——finalize 应走哨兵错误路径
        run_dir2 = tmp_path / "run_sentinel"
        run_dir2.mkdir()
        (run_dir2 / "manifest.json").write_text(json.dumps(
            {"domain": "交换机", "nodes": ["数据中心交换机"], "model": "t",
             "knowledge": []}, ensure_ascii=False), encoding="utf-8")
        (run_dir2 / "evidence.jsonl").write_text(json.dumps(
            {"tool_name": "WebSearch", "tool_input": {"query": "q1"},
             "tool_response": {"results": [{"url": "https://a.com/doc"}]}},
            ensure_ascii=False) + "\n", encoding="utf-8")

        proc = _start()
        try:
            _init(proc)
            tools = {t["name"] for t in _rpc(proc, "tools/list", req_id=2)["result"]["tools"]}
            assert tools == {"record_sources", "record_search", "coverage", "finalize"}

            r = _rpc(proc, "tools/call", {"name": "record_sources", "arguments": {
                "run_dir": str(run_dir), "entries": [
                    {"name": "A", "category_path": "交换机-数据中心交换机",
                     "source_type": "官方文档", "granularity": "合集级",
                     "url": "https://a.com/doc", "description": "d", "reason": "r"}]}},
                req_id=3)
            assert '"accepted": 1' in _tool_text(r)

            _rpc(proc, "tools/call", {"name": "record_search", "arguments": {
                "run_dir": str(run_dir), "entries": [
                    {"phase": "增量发现", "node": "数据中心交换机", "query": "q1",
                     "results": 10, "extracted": 5}]}}, req_id=4)

            r = _rpc(proc, "tools/call", {"name": "coverage", "arguments": {
                "run_dir": str(run_dir)}}, req_id=5)
            cov = json.loads(_tool_text(r))
            assert cov[0] == {"node": "数据中心交换机", "recorded": 1,
                              "extracted": 5, "missing": 4}

            # 哨兵路径：record_search 有提取、record_sources 未调用 → finalize 报错
            _rpc(proc, "tools/call", {"name": "record_search", "arguments": {
                "run_dir": str(run_dir2), "entries": [
                    {"phase": "增量发现", "node": "数据中心交换机", "query": "q1",
                     "results": 10, "extracted": 5}]}}, req_id=6)
            r = _rpc(proc, "tools/call", {"name": "finalize", "arguments": {
                "run_dir": str(run_dir2)}}, req_id=7)
            assert r["result"].get("isError") is True
            assert "来源未入库" in _tool_text(r)
        finally:
            proc.terminate()
