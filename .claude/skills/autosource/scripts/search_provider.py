"""AutoSource 2.0 搜索源接口：将"从哪拿结果"与"怎么处理结果"解耦（实现规格第四章）。

契约：
    SearchProvider.fetch(queries) -> SearchBatch
    SearchBatch = [{query_id, query, results: [{title, url, snippet}], failed, attempts, error?}]

orchestrator 只经 SearchProvider 接口获取结果，不直接读搜索文件；state / converge /
evidence / prompts / deliver 等业务模块不得感知搜索来源。迁移独立程序时仅新增
ApiSearchProvider 并替换编排外壳，业务模块零改动。
"""
import json
from pathlib import Path


class SearchProvider:
    """搜索源抽象接口。"""

    def fetch(self, queries: list[dict]) -> list[dict]:
        raise NotImplementedError


class HostSearchProvider(SearchProvider):
    """Skill 形态（当前）：宿主执行 websearch 后把结果写入 search_results.json。

    构造时接收该文件路径（来自 --commit 参数），按 query_id 与 pending 查询匹配
    并校验完整性（不按 query 文本匹配——重复查询词靠 query_id 区分）；文件中缺失的
    query 视为 failed（attempts=0，error 说明），多余条目忽略，输出按 pending 顺序。
    """

    def __init__(self, results_path: Path):
        self.results_path = Path(results_path)

    def fetch(self, queries: list[dict]) -> list[dict]:
        if not self.results_path.exists():
            raise FileNotFoundError(f"搜索结果文件不存在: {self.results_path}")
        try:
            entries = json.loads(self.results_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"搜索结果文件不是合法 JSON: {exc}") from exc
        if not isinstance(entries, list):
            raise ValueError("搜索结果文件应为数组")

        by_id: dict[int, dict] = {}
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("query_id"), int):
                by_id[entry["query_id"]] = entry

        batch: list[dict] = []
        for query in queries:
            query_id = query["query_id"]
            entry = by_id.get(query_id)
            if entry is None:
                # 文件缺失条目：显式标记 failed，不静默丢弃（批次生命周期完整性）
                batch.append({"query_id": query_id, "query": str(query.get("query") or ""),
                              "results": [], "failed": True, "attempts": 0,
                              "error": "搜索结果文件缺少该 query 的条目"})
                continue
            entry = dict(entry)
            entry.setdefault("query", str(query.get("query") or ""))
            entry.setdefault("results", [])
            entry.setdefault("failed", False)
            entry.setdefault("attempts", 0)
            batch.append(entry)
        return batch
