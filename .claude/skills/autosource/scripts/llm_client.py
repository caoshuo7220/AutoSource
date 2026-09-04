"""AutoSource 2.0 LLM 客户端：OpenAI 兼容网关（Chat Completions 协议）。

异常策略（实现规格第九章）：
- JSON 非法（init/plan/extract/review 四节点）→ 重试（最多 llm.retry 次），
  仍失败则节点调用失败（抛出 LLMError，调用方置 phase=failed）；
- 超时 / 网关错误 → 重试（最多 llm.retry 次）；
- report 节点输出 Markdown，不校验 JSON，只重试网络类错误。

密钥经 config.json / 环境变量注入，不写入代码与文档。
"""
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from prompts import parse_json


class LLMError(RuntimeError):
    """LLM 调用失败（重试耗尽）：JSON 非法、超时或网关错误。"""


def _default_post(url: str, data: str, headers: dict, timeout: int) -> bytes:
    request = Request(url, data=data.encode("utf-8"), headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return response.read()


class LLMClient:
    """OpenAI 兼容网关客户端；transport 可注入（post 参数）供测试不发起网络请求。"""

    def __init__(self, config: dict, post=None):
        llm = config["llm"]
        self.base_url = str(llm["base_url"]).rstrip("/")
        self.api_key = str(llm.get("api_key") or "")
        self.model = str(llm["model"])
        self.timeout = int(llm.get("timeout") or 60)
        self.retry = int(llm.get("retry") or 2)
        self._post = post or _default_post

    def _request(self, prompt: str) -> str:
        """单次网关调用，返回 content 文本；网络类错误抛出 LLMError。"""
        body = json.dumps({"model": self.model,
                           "messages": [{"role": "user", "content": prompt}]},
                          ensure_ascii=False)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            payload = self._post(f"{self.base_url}/chat/completions", body,
                                 headers, self.timeout)
            data = json.loads(payload.decode("utf-8"))
        except (URLError, OSError, HTTPError, json.JSONDecodeError,
                UnicodeDecodeError) as exc:
            raise LLMError(f"网关调用失败: {exc}") from exc
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"网关响应缺 choices[0].message.content: {data}") from exc

    def chat_json(self, prompt: str, node: str = "") -> dict:
        """调用并解析 JSON（init/plan/extract/review）；重试耗尽抛出 LLMError。"""
        last_error: Exception | None = None
        for _ in range(self.retry + 1):
            try:
                return parse_json(self._request(prompt))
            except (LLMError, ValueError) as exc:
                last_error = exc
        raise LLMError(f"节点「{node}」LLM 调用失败（重试 {self.retry} 次后仍失败）: {last_error}")

    def chat_text(self, prompt: str, node: str = "") -> str:
        """调用并原样返回 Markdown 正文（report）；只重试网络类错误。"""
        last_error: Exception | None = None
        for _ in range(self.retry + 1):
            try:
                return self._request(prompt)
            except LLMError as exc:
                last_error = exc
        raise LLMError(f"节点「{node}」LLM 调用失败（重试 {self.retry} 次后仍失败）: {last_error}")
