"""llm_client.py 的单元测试：OpenAI 兼容网关调用、重试策略（实现规格第九章）。

transport 注入（post 参数）：不发起网络请求、可脚本化 HTTP 行为。
重试契约：JSON 非法 / 超时 / 网关错误重试（最多 llm.retry 次）；重试耗尽抛出 LLMError。
"""
import json
from urllib.error import HTTPError, URLError

import pytest

from llm_client import LLMClient, LLMError


def make_config(**overrides):
    cfg = {"llm": {"base_url": "http://gw.example/v1", "api_key": "secret",
                   "model": "test-model", "timeout": 5, "retry": 2}}
    cfg["llm"].update(overrides)
    return cfg


def openai_response(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


class FakeTransport:
    """可脚本化响应序列的模拟 HTTP 传输；记录每次请求。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, data, headers, timeout):
        self.requests.append({"url": url, "data": data, "headers": headers,
                              "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return openai_response(response)


def test_chat_json_parses_json():
    transport = FakeTransport(['{"nodes": [{"name": "交换机"}]}'])
    client = LLMClient(make_config(), post=transport)
    assert client.chat_json("拆解领域", node="init") == {"nodes": [{"name": "交换机"}]}


def test_chat_json_strips_fences():
    """LLM 常带 ```json 围栏输出——解析前剥除（实现规格第六章"只输出 JSON"的容错）。"""
    transport = FakeTransport(['```json\n{"a": 1}\n```'])
    client = LLMClient(make_config(), post=transport)
    assert client.chat_json("p", node="init") == {"a": 1}


def test_chat_json_invalid_json_retries():
    """JSON 非法 → 重试；retry=2 时最多 3 次尝试，第 3 次成功即返回。"""
    transport = FakeTransport(["不是JSON", "也不是", '{"ok": true}'])
    client = LLMClient(make_config(retry=2), post=transport)
    assert client.chat_json("p", node="plan") == {"ok": True}
    assert len(transport.requests) == 3


def test_chat_json_invalid_json_exhausted():
    """重试耗尽仍非法 → LLMError（调用方据此置 phase=failed）。"""
    transport = FakeTransport(["不是JSON"] * 3)
    client = LLMClient(make_config(retry=2), post=transport)
    with pytest.raises(LLMError, match="init"):
        client.chat_json("p", node="init")
    assert len(transport.requests) == 3


def test_chat_json_http_error_retries():
    """超时 / 网关错误 → 重试；随后成功即返回。"""
    transport = FakeTransport([URLError("timeout"), '{"a": 1}'])
    client = LLMClient(make_config(retry=2), post=transport)
    assert client.chat_json("p", node="extract") == {"a": 1}
    assert len(transport.requests) == 2


def test_chat_json_http_error_exhausted():
    transport = FakeTransport([HTTPError("http://x", 500, "err", None, None)] * 3)
    client = LLMClient(make_config(retry=2), post=transport)
    with pytest.raises(LLMError):
        client.chat_json("p", node="review")


def test_chat_text_no_json_validation():
    """report 节点输出 Markdown：只重试网络类错误，不校验 JSON。"""
    transport = FakeTransport(["# 标题\n正文（不是 JSON）"])
    client = LLMClient(make_config(), post=transport)
    assert client.chat_text("写报告", node="report") == "# 标题\n正文（不是 JSON）"


def test_chat_text_http_error_retries():
    transport = FakeTransport([URLError("boom"), "正文"])
    client = LLMClient(make_config(retry=1), post=transport)
    assert client.chat_text("p", node="report") == "正文"
    assert len(transport.requests) == 2


def test_request_payload_matches_openai_protocol():
    """请求按 OpenAI Chat Completions 协议：URL 拼接 / Authorization 头 / model+user 消息。"""
    transport = FakeTransport(["{}"])
    client = LLMClient(make_config(), post=transport)
    client.chat_json("拆解：交换机", node="init")
    req = transport.requests[0]
    assert req["url"] == "http://gw.example/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer secret"
    body = json.loads(req["data"])
    assert body["model"] == "test-model"
    assert body["messages"] == [{"role": "user", "content": "拆解：交换机"}]
    assert req["timeout"] == 5
