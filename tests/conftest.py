"""AutoSource 2.0 测试公共设施：脚本路径注入、模拟 LLM、配置与 CLI 调用 fixture。"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SKILL_DIR = ROOT / ".claude" / "skills" / "autosource"
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# 标准测试领域树：根节点 dims 为空（客观覆盖恒满足），两个叶子各带 dims
SAMPLE_NODES = [
    {"name": "交换机", "parent": "", "terms": ["交换机", "switch"], "dims": []},
    {"name": "数据中心交换机", "parent": "交换机",
     "terms": ["数据中心交换机", "data center switch", "ToR"],
     "dims": ["官方文档", "行业标准"]},
    {"name": "园区交换机", "parent": "交换机",
     "terms": ["园区交换机", "campus switch"],
     "dims": ["厂商文档", "开源社区"]},
]

DEFAULT_RESPONSES = {
    "init": {"nodes": SAMPLE_NODES},
    "plan": {"queries": [
        {"query": "数据中心交换机 官方文档", "node": "数据中心交换机",
         "angle": "官方文档", "reason": "补官方文档入口"},
        {"query": "campus switch documentation", "node": "园区交换机",
         "angle": "厂商文档", "reason": "补厂商文档入口"},
    ]},
    "extract": {"sources": [], "new_entities": [], "new_terms": [], "new_nodes": []},
    "review": {"gaps": [], "converged": True, "reason": "领域覆盖充分"},
    "report": "## 一、领域概览\n报告正文（测试桩）",
}


class FakeLLM:
    """模拟 LLM 客户端：按节点名脚本化响应并记录每次调用（替代 orchestrator.build_client）。

    响应可为 dict / str / Exception 实例 / 可调用对象（可调用时以 calls 列表为参，
    供按调用次序返回不同结果的场景）。与 llm_client.LLMClient 的签名一致：
    chat_json(prompt, node="") / chat_text(prompt, node="")。
    """

    def __init__(self, responses=None):
        self.responses = dict(DEFAULT_RESPONSES)
        if responses:
            self.responses.update(responses)
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, prompt: str, node: str = "") -> dict:
        self.calls.append((node, prompt))
        response = self.responses.get(node)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(self.calls)
        return response

    def chat_text(self, prompt: str, node: str = "") -> str:
        self.calls.append((node, prompt))
        response = self.responses.get(node)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(self.calls)
        return response


@pytest.fixture
def config(tmp_path, monkeypatch):
    """写一份收敛参数调小的 config（便于测试）并注入环境变量。

    参数从 config 读取（不硬编码）即测试目标本身，故 fixture 走真实 config 加载路径。
    """
    cfg = {
        "llm": {"base_url": "http://example.test/v1", "api_key": "test-key",
                "model": "test-model", "timeout": 5, "retry": 2},
        "converge": {"k": 2, "queries_per_batch_min": 1, "queries_per_batch_max": 3,
                     "fuse_batch_limit": 100, "retry": 2, "fail_rate_threshold": 0.5},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("AUTOSOURCE_CONFIG", str(path))
    monkeypatch.setenv("AUTOSOURCE_OUTPUTS", str(tmp_path / "outputs"))
    return cfg


@pytest.fixture
def fake_llm(monkeypatch):
    """注入模拟 LLM：orchestrator.build_client 直接返回本实例，不发起网络请求。"""
    fake = FakeLLM()
    import orchestrator
    monkeypatch.setattr(orchestrator, "build_client", lambda cfg: fake)
    return fake


@pytest.fixture
def cli(capsys):
    """以进程内方式调用 orchestrator 子命令，返回 (退出码, stdout, stderr)。"""
    def _run(*args):
        from orchestrator import main
        code = main([str(a) for a in args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err
    return _run


@pytest.fixture
def init_run(cli, fake_llm):
    """以模拟 LLM 的 init 响应建一个 running 状态的运行目录，返回 run_dir 路径。"""
    def _init(domain="交换机", init_response=None):
        if init_response is not None:
            fake_llm.responses["init"] = init_response
        code, out, err = cli("--init", domain)
        assert code == 0, f"--init 失败: {out}{err}"
        return Path(out.strip().splitlines()[-1])
    return _init


def write_results(run_dir: Path, entries: list[dict]) -> Path:
    """按第四章格式写 search_results.json（搜索结果文件的模拟），返回文件路径。"""
    path = Path(run_dir) / "search_results.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return path


def done_entry(query_id: int, query: str, results: list[dict],
               attempts: int = 1) -> dict:
    """构造一条成功（failed=false）的搜索结果条目。"""
    return {"query_id": query_id, "query": query, "results": results,
            "failed": False, "attempts": attempts}


def failed_entry(query_id: int, query: str, attempts: int = 3,
                 error: str = "timeout") -> dict:
    """构造一条失败（failed=true）的搜索结果条目。"""
    return {"query_id": query_id, "query": query, "results": [],
            "failed": True, "attempts": attempts, "error": error}


def load_state_dict(run_dir: Path) -> dict:
    """读运行目录的 state.json（测试断言用）。"""
    return json.loads((Path(run_dir) / "state.json").read_text(encoding="utf-8"))
