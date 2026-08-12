"""AutoSource 后处理脚本：去重 + URL 可达性校验。

用法:
    python postprocess.py input.json output.json

从 input.json 读取数据源列表，去重后校验 URL，将有效条目写入 output.json，
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


def check_url(url: str, timeout: int = 5, max_retries: int = 1) -> bool:
    """校验单个 URL 的可达性（HEAD 优先，失败回退 GET）。"""
    import requests

    for _ in range(max_retries + 1):
        try:
            resp = requests.head(url, timeout=timeout, allow_redirects=True)
            resp.raise_for_status()
            return True
        except requests.RequestException:
            try:
                resp = requests.get(url, timeout=timeout, stream=True)
                resp.raise_for_status()
                resp.close()
                return True
            except requests.RequestException:
                continue
    return False


def process(input_path: str, output_path: str) -> dict:
    """执行完整后处理流程，返回统计信息。"""
    data = json.loads(Path(input_path).read_text(encoding="utf-8"))

    total = len(data)
    unique = deduplicate(data)
    removed = total - len(unique)

    valid: list[dict] = []
    invalid_urls: list[str] = []
    for s in unique:
        if check_url(s["url"]):
            valid.append(s)
        else:
            invalid_urls.append(s["url"])

    Path(output_path).write_text(
        json.dumps(valid, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    stats = {
        "total": total,
        "removed_duplicates": removed,
        "checked": len(unique),
        "valid": len(valid),
        "invalid": len(invalid_urls),
        "valid_ratio": round(len(valid) / max(len(unique), 1), 3),
        "invalid_urls": invalid_urls,
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

    print(f"去重: {stats['total']} → {stats['checked']} (移除 {stats['removed_duplicates']} 条重复)")
    print(f"URL 校验: {stats['valid']}/{stats['checked']} 有效 ({stats['valid_ratio']:.0%})")
    if stats["invalid_urls"]:
        print(f"无效 URL:")
        for u in stats["invalid_urls"]:
            print(f"  - {u}")
    print(f"输出: {out_path}")


if __name__ == "__main__":
    main()
