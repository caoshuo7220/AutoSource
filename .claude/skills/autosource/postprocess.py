"""AutoSource 后处理脚本：去重。

用法:
    python postprocess.py input.json output.json

从 input.json 读取数据源列表，去重后将结果写入 output.json，
统计信息打印到 stdout。
"""

import json
import sys
from urllib.parse import urlparse
from pathlib import Path


def _domain(url: str) -> str:
    return urlparse(url).netloc


def deduplicate(sources: list[dict]) -> list[dict]:
    """按域名 + 名称去重，保留首次出现，镜像站（不同域名）保留。"""
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for s in sources:
        key = (_domain(s["url"]), s["name"])
        if key not in seen:
            seen.add(key)
            result.append(s)
    return result


def process(input_path: str, output_path: str) -> dict:
    """执行去重流程，返回统计信息。"""
    data = json.loads(Path(input_path).read_text(encoding="utf-8"))

    total = len(data)
    unique = deduplicate(data)
    removed = total - len(unique)

    Path(output_path).write_text(
        json.dumps(unique, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    stats = {
        "total": total,
        "removed_duplicates": removed,
        "kept": len(unique),
    }
    return stats


def main() -> None:
    if len(sys.argv) != 3:
        print("用法: python postprocess.py <input.json> <output.json>", file=sys.stderr)
        sys.exit(2)

    in_path, out_path = sys.argv[1], sys.argv[2]

    if not Path(in_path).exists():
        print(f"错误: 输入文件不存在: {in_path}", file=sys.stderr)
        sys.exit(1)

    stats = process(in_path, out_path)

    print(f"去重: {stats['total']} → {stats['kept']} (移除 {stats['removed_duplicates']} 条重复)")
    print(f"输出: {out_path}")


if __name__ == "__main__":
    main()
