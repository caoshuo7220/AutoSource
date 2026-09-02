"""AutoSource 分析报告模块：数据总览注入与报告命名（自 postprocess 拆分）。

报告内容由模型生成（语义环节），文件名与统计数字是确定性环节——文件名按
目录名派生（与数据源清单/stats 同前缀，模型没有时钟、禁止模型自行命名）；
统计数字由脚本从交付物 CSV 生成注入（模型不写数字，2026-08-28 起）。
"""
import csv
from pathlib import Path
from typing import Optional


def _report_stats_block(outdir: Path) -> Optional[str]:
    """从交付物 CSV 生成"数据总览"段——报告统计数字由脚本生成、模型不写数字。

    数字口径以 stats.csv（脚本统计）与数据源清单.csv 为准；模型运行记忆中的
    提取数是去重前口径，与最终清单不一致（2026-08-28 实证：报告体裁数与
    stats 对不上）。stats.csv 缺失时返回 None（跳过注入，不阻断重命名）。
    """
    inter = outdir / "intermediate"
    stats_csv = next((f for f in inter.iterdir() if f.name.endswith("_stats.csv")), None) \
        if inter.is_dir() else None
    if stats_csv is None:
        return None
    node_rows = []
    total_row = None
    with open(stats_csv, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))[1:]
    for r in rows:
        if not r or not r[0]:
            continue
        if r[0] == "总计":
            total_row = r
        else:
            node_rows.append(r)
    if total_row is None:
        return None
    total = total_row[1] if len(total_row) > 1 else "0"
    type_dist = total_row[2] if len(total_row) > 2 else ""
    coll = single = 0
    list_csv = next((f for f in outdir.iterdir() if f.name.endswith("数据源清单.csv")), None)
    if list_csv:
        with open(list_csv, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                g = r.get("粒度") or ""
                if g == "单篇级":
                    single += 1
                elif g == "合集级":
                    coll += 1
    return "\n".join([
        "## 数据总览",
        "",
        f"- 数据源总数：{total} 条（合集级 {coll} / 单篇级 {single}）",
        f"- 分类节点：{len(node_rows)} 个",
        "- 节点分布：" + "; ".join(f"{r[0]}: {r[1]}" for r in node_rows),
        "- 体裁分布：" + type_dist,
        "",
    ])


def _insert_stats_section(text: str, block: str) -> str:
    """已存在"## 数据总览"标题则替换其内容（到下一个 ## 标题为止），
    否则在标题行（第一行）之后插入完整段落——两种形态均确定性落地。"""
    heading = "## 数据总览"
    idx = text.find(heading)
    if idx != -1:
        nxt = text.find("\n## ", idx + len(heading))
        if nxt == -1:
            nxt = len(text)
        return text[:idx] + block + text[nxt:]
    first_nl = text.find("\n")
    if first_nl == -1:
        return text + "\n\n" + block
    return text[:first_nl + 1] + "\n" + block + text[first_nl + 1:]


def finalize_report(outdir: str) -> str:
    """把模型写入的 分析报告.md 重命名为 {目录名}_分析报告.md，并注入"数据总览"段。

    报告内容由模型生成（语义环节），文件名与统计数字是确定性环节——文件名按
    目录名派生（与数据源清单/stats 同前缀，模型没有时钟、禁止模型自行命名）；
    统计数字由本函数从交付物 CSV 生成注入（模型不写数字，见 _report_stats_block）。
    报告缺失或目标已存在时报错——错误显式化，不让命名漂移静默发生。
    """
    d = Path(outdir)
    report = d / "分析报告.md"
    if not report.exists():
        raise FileNotFoundError(f"未找到 分析报告.md: {report}（报告需先由模型写入该文件）")
    block = _report_stats_block(d)
    if block:
        text = _insert_stats_section(report.read_text(encoding="utf-8"), block)
        report.write_text(text, encoding="utf-8")
    else:
        text = _insert_stats_section(report.read_text(encoding="utf-8"),
                                     "（本次统计注入失败：未找到 stats.csv，见 intermediate/）")
        report.write_text(text, encoding="utf-8")
        print("注意: 未找到 stats.csv，已往报告数据总览写入占位提示（统计未注入）")
    target = d / f"{d.name}_分析报告.md"
    if target.exists():
        raise FileExistsError(f"目标文件已存在: {target}")
    report.rename(target)
    return str(target)
