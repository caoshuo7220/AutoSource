# -*- coding: utf-8 -*-
"""运行复盘分析工具（开发侧手段，不进 skill 包）。

用法：
    python tools/analyze_run.py <run_dir> [--transcript 会话.jsonl] [--out 文件]
                                   [--thinking] [--thinking-max N]

产出六张表（有 transcript 才有后三张）：
  1. 漏斗指标——各阶段搜索/提取/零提取，每节点分布，manifest 声明与体裁
  2. 零提取构成——词题(结果垃圾)/疑似漏收/已收重复/混合 四分类 + 样本 URL
  3. 交付物成色——验收单摘要、体裁/粒度/域名/URL 形态/官网/归因缺口/配额/同领域跨轮
  4. 模型身份——manifest 与 transcript 的 model 字段实录
  5. 上下文消耗——thinking/工具返回/工具参数占比、每搜成本、异常工具序列
  6. thinking 决策（--thinking）——LLM 思考中"收/不收"的决策句子（按时间戳）

分析口径与 2026-09-08 复盘一致：零提取构成按结果 URL 域名与最终清单交叉判定
（垃圾域启发式，必要非充分——"疑似漏收"需人工抽查定案）；思考过程是证据不是
真相，须与产出数据三角验证。

2026-09-21 修复与扩展：`验证通过` 一列在 09-16 前后从搜索日志移除（验证结果改
走 store 的 knowledge 记录），原漏斗表据此崩溃、此后每轮跑不起来；manifest 也
只承载声明态、不再带 verified。核对结果改从运行验收单读，并新增「交付物成色」
一节——近几轮实际复盘的关切（体裁映射、粒度、域名集中度、官网有效性、归因
完整度、配额）此前只散落在一次性脚本里。
"""
import argparse
import csv
import glob
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 配额下限与 skill 脚本同源（避免两处各写一个 20）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / ".claude" / "skills" / "autosource" / "scripts"))
from postprocess import MIN_INCREMENTAL_SEARCHES   # noqa: E402

# 垃圾域启发式（书商/电商/SEO/无关行业站/聚合器）——零提取构成分类用
GARBAGE_DOMAINS = [
    "books.google", "worldofbooks", "manualslib", "directindustry", "elcodis",
    "iczoom", "alibris", "abebooks", "springer", "ebay", "amazon", "tradeindia",
    "alibaba", "made-in-china", "globalsources", "b2b", "job.", "zhaopin",
    "liepin", "51job", "seek", "indeed", "docin", "doc88", "wenku", "zhidao",
    "qianzhan", "chinairn", "smartchina", "toutiao", "dxpress", "gelonghui",
    "finance.eastmoney", "api3.cls", "baike.baidu",
]
# 思考决策关键词（thinking 抽取用）
DECISION_PATTERN = re.compile(
    r"不收|拒收|提取|收录|放弃|跳过|skip|单篇|规格页|平台准入|已在库|重复|无符合"
    r"|没有符合|零提取|0 条|same-source|same source|collect|reject|镜像|mirror")
# 机构名特征——官网条目里区分"厂商"与"协会/大学/实验室/标准组织"
ORG_PATTERN = re.compile(
    r"协会|学会|联盟|组织|委员会|工作组|研究院|研究所|科学院|大学|学院|学校|实验室|"
    r"中心|政府|基金会|商会|论坛|峰会|大会|媒体|咨询|研究|标准|认证|检测|检验|计量|"
    r"情报|图书馆|博物馆|园区|孵化|MSA|University|Institute|Alliance|Association|"
    r"Consortium|Foundation|Forum|Society|Committee|Standards|Lab|Centre|Center|"
    r"Initiative|Council|Federation|Union|Fraunhofer", re.I)
# 机构名特征词覆盖不到的（无中文词形、无 Institute/MSA 类后缀）——名单性质同
# GARBAGE_DOMAINS：一条一行、渐进收敛，新出现的补进来即可
ORG_NAMES = {"3GPP", "IEEE", "JEDEC", "GSMA", "ETSI", "Omdia", "LightCounting",
             "OCP开放计算项目", "Matter 中文官方网站", "IEEE Sensors 会议论文库",
             "中国卫星导航系统管理办公室"}
# 站点入口形态：根路径 / index.* / 语言首页
ENTRY_PATTERN = re.compile(r"^(|index\.(html?|php|aspx?)|[a-z]{2}(-[a-z]{2})?|default\.html?)$", re.I)


def _csv_rows(path: Path):
    return [r for r in csv.reader(open(path, encoding="utf-8-sig")) if r]


def _idx(head, *names):
    for n in names:
        if n in head:
            return head.index(n)
    return None


def section_metrics(run_dir: Path, w):
    """1. 漏斗指标。"""
    w("=" * 24, "漏斗指标")
    slogs = sorted(glob.glob(str(run_dir / "intermediate" / "*搜索日志.csv")))
    if not slogs:
        w("  无搜索日志")
        return
    rows = _csv_rows(Path(slogs[0]))
    i = {n: k for k, n in enumerate(rows[0])}
    body = rows[1:]
    by_phase = defaultdict(lambda: [0, 0, 0])  # n/ext/zero
    for r in body:
        a = by_phase[r[i["阶段"]]]
        a[0] += 1
        a[1] += int(r[i["提取候选数"]] or 0)
        a[2] += int(r[i["提取候选数"]] or 0) == 0
    w(f"  总搜索 {len(body)} 次")
    for p, (n, e, z) in sorted(by_phase.items()):
        w(f"  {p}: {n} 搜 提取 {e} ({e / n:.2f}/搜) 零提取 {z} ({z * 100 // n}%)")
    nodes = Counter(r[i["节点"]] for r in body if r[i["阶段"]] == "增量发现")
    w(f"  增量节点 {len(nodes)} 个: "
      + " ".join(f"{k}:{c}" for k, c in sorted(nodes.items(), key=lambda x: -x[1])))
    # 清单
    fins = sorted(glob.glob(str(run_dir / "*数据源清单.csv")))
    if fins:
        frows = _csv_rows(Path(fins[0]))
        fi = {n: k for k, n in enumerate(frows[0])}
        items = frows[1:]
        gran = Counter(r[fi["粒度"]] for r in items)
        types = Counter(r[fi["数据源类型"]] for r in items)
        w(f"  清单 {len(items)} 条 粒度 {dict(gran)} 体裁 "
          + " ".join(f"{k}:{c}" for k, c in types.most_common(10)))
    mf = glob.glob(str(run_dir / "intermediate" / "manifest_input.json"))
    if mf:
        m = json.load(open(mf[0], encoding="utf-8"))
        w(f"  manifest 声明 {len(m.get('vendors') or [])} 厂商 + "
          f"{len(m.get('knowledge') or [])} 清单项 model={m.get('model')!r}"
          "（核对结果见「交付物成色」的运行验收单）")


def section_zero_split(run_dir: Path, w):
    """2. 零提取构成：词题/疑似漏收/已收/混合。"""
    w("=" * 24, "零提取构成")
    base = run_dir / "intermediate"
    traces = sorted(glob.glob(str(base / "*溯源.csv")))
    slogs = sorted(glob.glob(str(base / "*搜索日志.csv")))
    fins = sorted(glob.glob(str(run_dir / "*数据源清单.csv")))
    if not (traces and slogs and fins):
        w("  缺溯源/搜索日志/清单文件")
        return
    trace = _csv_rows(Path(traces[0])); ti = {n: k for k, n in enumerate(trace[0])}
    slog = _csv_rows(Path(slogs[0])); si = {n: k for k, n in enumerate(slog[0])}
    fin = _csv_rows(Path(fins[0])); fi = {n: k for k, n in enumerate(fin[0])}
    listed_domains = {urlparse(r[fi["访问地址"]]).netloc for r in fin[1:]}
    zero = {(r[si["阶段"]], r[si["节点"]], r[si["查询词"]])
            for r in slog[1:] if r[si["阶段"]] == "增量发现"
            and int(r[si["提取候选数"]] or 0) == 0}
    by_query = defaultdict(list)
    for r in trace[1:]:
        by_query[(r[ti["阶段"]], r[ti["查询词"]])].append(r[ti["结果URL"]])
    cats = Counter()
    samples = defaultdict(list)
    for (ph, node, q) in sorted(zero):
        urls = by_query.get((ph, q))
        if not urls:
            continue
        doms = {urlparse(u).netloc for u in urls}
        g = sum(any(gw in d for gw in GARBAGE_DOMAINS) for d in doms)
        dup = sum(d in listed_domains for d in doms)
        if dup == len(doms):
            cat = "已收(拒收合理)"
        elif g == len(doms):
            cat = "词题(结果垃圾)"
        elif g > 0:
            cat = "混合"
        else:
            cat = "疑似漏收"
        cats[cat] += 1
        if len(samples[cat]) < 4:
            samples[cat].append((q, urls[:3]))
    n = sum(cats.values())
    if not n:
        w("  无零提取增量搜索")
        return
    for cat in ["疑似漏收", "词题(结果垃圾)", "已收(拒收合理)", "混合"]:
        if cats[cat]:
            w(f"  {cat}: {cats[cat]} ({cats[cat] * 100 // n}%)")
    w("  注：'疑似漏收'为启发式（域名不在清单且非垃圾域），需人工抽查定案")
    for cat in ["疑似漏收", "混合"]:
        for q, urls in samples[cat]:
            w(f"  [{cat}] {q[:70]}")
            for u in urls:
                w(f"      {urlparse(u).netloc} | {u[:80]}")


def _parts(url: str) -> tuple[str, str, str]:
    """(host, path, query)——无协议写法照认（结果的结构化链接带协议、摘要散文常不带）。"""
    p = urlparse(url if "://" in url else "http://" + url)
    return p.netloc.lower(), p.path, p.query


def _unmapped_words(run_dir: Path) -> Counter:
    """落「其他」的原始体裁词（从 store 的 source_type_raw 读，收尾产物里没有）。"""
    f = run_dir / "intermediate" / "store_input.jsonl"
    if not f.exists():
        return Counter()
    words = Counter()
    for line in open(f, encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") == "source" and r.get("source_type") == "其他":
            words[str(r.get("source_type_raw") or "")] += 1
    return words


def _history(run_dir: Path, w, limit: int = 8):
    """同领域近几轮对比（按目录名前缀取同目录下的兄弟运行）。"""
    prefix = run_dir.name.split("_")[0]
    rows = []
    for d in sorted(run_dir.parent.glob(prefix + "_*")):
        fins = sorted(glob.glob(str(d / "*数据源清单.csv")))
        if not fins:
            continue
        rr = _csv_rows(Path(fins[0]))
        fi = {n: k for k, n in enumerate(rr[0])}
        types = Counter(x[fi["数据源类型"]] for x in rr[1:])
        slogs = sorted(glob.glob(str(d / "intermediate" / "*搜索日志.csv")))
        rows.append((d.name, len(rr) - 1, types.get("其他", 0),
                     len(_csv_rows(Path(slogs[0]))) - 1 if slogs else 0))
    if len(rows) < 2:
        return
    rows = rows[-limit:]
    w(f"  同领域近 {len(rows)} 轮（交付 / 其他 / 其他率 / 搜索 / 每条搜索）：")
    for name, n, o, ns in rows:
        w(f"    {name[-15:]}  {n:>5}  {o:>4}  {o * 100 / max(1, n):>5.1f}%  {ns:>4}"
          f"  {n / max(1, ns):>5.2f}")


def section_delivery(run_dir: Path, w):
    """3. 交付物成色：验收单摘要 + 体裁/粒度/域名/URL 形态/官网/归因/配额/跨轮。"""
    w("=" * 24, "交付物成色")
    att = sorted(glob.glob(str(run_dir / "*_运行验收单.json")))
    if att:
        a = json.load(open(att[0], encoding="utf-8"))
        s, k = a.get("sources") or {}, a.get("knowledge") or {}
        # 验收单字段随版本增补（reverse_gap 2026-09-18 起）——老轮次缺键按 "-" 显示
        w(f"  验收单：收录 {s.get('kept', '-')} / 候选 {s.get('candidates_before_filter', '-')}"
          f" / 去重移除 {s.get('removed_duplicates', '-')}"
          f" / 证据移除 {s.get('ungrounded_urls', '-')} / 垃圾域 {s.get('garbage_filtered', '-')}"
          f" / 被拒 {len(s.get('rejected') or [])}")
        w(f"  清单 {k.get('declared', '-')} 声明 / {k.get('recorded', '-')} 已录"
          f" / 验证 {k.get('verified', '-')} / 未验证 {k.get('unverified', '-')}"
          f" | 零提取 {(a.get('zero_extraction') or {}).get('count', '-')}"
          f" | 反向差额（漏记）{(a.get('reverse_gap') or {}).get('unlogged', '-')}")
    fins = sorted(glob.glob(str(run_dir / "*数据源清单.csv")))
    if not fins:
        w("  无数据源清单")
        return
    rows = _csv_rows(Path(fins[0]))
    fi = {n: k for k, n in enumerate(rows[0])}
    body = rows[1:]
    n = len(body)
    gi, qi = _idx(rows[0], "粒度"), _idx(rows[0], "来源搜索")   # 早期产物没有这两列
    types = Counter(r[fi["数据源类型"]] for r in body)
    other = types.get("其他", 0)
    if gi is None:
        w(f"  交付 {n} 条（早期产物无粒度列）")
    else:
        gran = Counter(r[gi] for r in body)
        w(f"  交付 {n} 条：合集 {gran.get('合集级', 0)} / 单篇 {gran.get('单篇级', 0)}"
          f"（单篇 {gran.get('单篇级', 0) * 100 // max(1, n)}%）")
    w("  体裁 " + " ".join(f"{k}:{v}" for k, v in types.most_common()))
    if other:
        words = _unmapped_words(run_dir)
        w(f"  落「其他」{other} 条（{other * 100 / n:.1f}%）"
          "——表外原词 top10: " + " ".join(f"{k}:{v}" for k, v in words.most_common(10)))
    doms = Counter(_parts(r[fi["访问地址"]])[0] for r in body)
    big = sum(c for h, c in doms.items() if c >= 5)
    w(f"  唯一域名 {len(doms)}（只出现 1 次 {sum(1 for c in doms.values() if c == 1)}）"
      f" | ≥5 条站点 {big} 条（{big * 100 // max(1, n)}%）")
    w("  top 域名 " + " ".join(f"{h}:{c}" for h, c in doms.most_common(8)))
    pdf = sum(1 for r in body if _parts(r[fi["访问地址"]])[1].lower().endswith(".pdf"))
    deep = sum(1 for r in body if _parts(r[fi["访问地址"]])[1].strip("/").count("/") >= 3)
    param = sum(1 for r in body if _parts(r[fi["访问地址"]])[2])
    guan = [r for r in body if r[fi["数据源类型"]] == "官网"]
    if guan:
        entry = sum(1 for r in guan
                    if ENTRY_PATTERN.match(_parts(r[fi["访问地址"]])[1].strip("/")))
        org = sum(1 for r in guan
                  if ORG_PATTERN.search(r[fi["数据源名称"]])
                  or r[fi["数据源名称"]] in ORG_NAMES)
        w(f"  官网 {len(guan)} 条：站点入口 {entry} / 站内页 {len(guan) - entry}"
          f" | 非厂商机构 {org} / 厂商 {len(guan) - org}")
    w(f"  PDF 直链 {pdf}（{pdf * 100 // max(1, n)}%）| 深链≥4 段 {deep}"
      f"（{deep * 100 // max(1, n)}%）| 带 query {param}")
    if qi is not None:
        noq = sum(1 for r in body if not r[qi].strip())
        w(f"  来源搜索列为空 {noq} 条（{noq * 100 / n:.1f}%）"
          + ("  ← 归因缺口" if noq else ""))
    slogs = sorted(glob.glob(str(run_dir / "intermediate" / "*搜索日志.csv")))
    if slogs:
        srows = _csv_rows(Path(slogs[0]))
        si = {nn: kk for kk, nn in enumerate(srows[0])}
        inc = Counter(r[si["节点"]] for r in srows[1:] if r[si["阶段"]] == "增量发现")
        short = {k2: v for k2, v in inc.items() if v < MIN_INCREMENTAL_SEARCHES}
        w(f"  配额：{len(inc)} 节点，增量搜索最少 {min(inc.values(), default=0)} 次"
          f"（下限 {MIN_INCREMENTAL_SEARCHES}）" + (f"  未达标 {short}" if short else ""))
    _history(run_dir, w)


def section_model(transcript: Path | None, run_dir: Path, w):
    """3. 模型身份（transcript 的 model 字段实录 + manifest）。"""
    w("=" * 24, "模型身份")
    if transcript and transcript.exists():
        models = Counter()
        for line in open(transcript, encoding="utf-8"):
            if '"model"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            m = o.get("message", {}).get("model")
            if m:
                models[m] += 1
        w("  transcript model 字段:", dict(models) or "无")
    mf = glob.glob(str(run_dir / "intermediate" / "manifest_input.json"))
    if mf:
        w("  manifest model:",
          repr(json.load(open(mf[0], encoding="utf-8")).get("model")))


def section_context(transcript: Path | None, w):
    """4. 上下文消耗结构（transcript 字符占比 + 每搜成本 + 异常工具序列）。"""
    w("=" * 24, "上下文消耗")
    if not (transcript and transcript.exists()):
        w("  未提供 --transcript")
        return
    chars = Counter()
    tool_calls = Counter()
    ws_result_chars = []
    ws_thinking = []
    cur_thinking = 0
    anomaly = []
    pending = {}
    for line in open(transcript, encoding="utf-8"):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        ts = (o.get("timestamp") or "")[11:19]
        if o.get("type") == "assistant":
            c = o.get("message", {}).get("content", [])
            if isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "thinking":
                        cur_thinking += len(b.get("thinking", ""))
                    elif bt == "tool_use":
                        nm = b.get("name", "")
                        tool_calls[nm] += 1
                        pending[b.get("id", "")] = nm
                        chars[f"参数:{nm}"] += len(
                            json.dumps(b.get("input", {}), ensure_ascii=False))
                        if nm == "WebSearch":
                            ws_thinking.append(cur_thinking)
                            cur_thinking = 0
                        if nm in ("Bash", "Edit", "Grep", "Glob"):
                            anomaly.append((ts, nm, len(json.dumps(
                                b.get("input", {}), ensure_ascii=False))))
        elif o.get("type") == "user":
            c = o.get("message", {}).get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        ct = b.get("content")
                        s = len(ct) if isinstance(ct, str) else sum(
                            len(x.get("text", "")) for x in ct
                            if isinstance(x, dict))
                        chars["工具返回"] += s
                        nm = pending.pop(b.get("tool_use_id", ""), "")
                        if nm == "WebSearch":
                            ws_result_chars.append(s)
    chars["思考"] = sum(ws_thinking) + cur_thinking
    tot = sum(chars.values())
    w(f"  内容总字符 ≈ {tot:,}")
    for k, v in chars.most_common():
        w(f"    {k:12s} {v:>11,} ({v * 100 // max(1, tot)}%)")
    ws_n = tool_calls.get("WebSearch", 0)
    if ws_n:
        avg_ret = sum(ws_result_chars) / len(ws_result_chars) if ws_result_chars else 0
        w(f"  WebSearch {ws_n} 次: 返回均值 {avg_ret:,.0f} 字符")
        w(f"  每次搜索前思考均值 {sum(ws_thinking) / len(ws_thinking):,.0f} 字符"
          f" (max {max(ws_thinking):,})")
        w(f"  每搜总成本 ≈ {tot / ws_n:,.0f} 字符")
    w("  工具调用:", dict(tool_calls))
    if anomaly:
        w(f"  异常开发工具序列 ({len(anomaly)} 次):")
        for ts, nm, sz in anomaly[:20]:
            w(f"    {ts} {nm} args={sz}")


def section_thinking(transcript: Path | None, w, max_sentences: int):
    """5. thinking 决策句子（收/不收理由），按时间戳。"""
    w("=" * 24, "thinking 决策")
    if not (transcript and transcript.exists()):
        w("  未提供 --transcript")
        return
    shown = 0
    for line in open(transcript, encoding="utf-8"):
        if shown >= max_sentences:
            break
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") != "assistant":
            continue
        c = o.get("message", {}).get("content", [])
        if not isinstance(c, list):
            continue
        for b in c:
            if shown >= max_sentences:
                break
            if not (isinstance(b, dict) and b.get("type") == "thinking"):
                continue
            ts = (o.get("timestamp") or "")[11:19]
            for s in re.split(r"\n+", b.get("thinking", "")):
                if DECISION_PATTERN.search(s) and len(s) > 20:
                    w(f"  [{ts}] {s.strip()[:280]}")
                    shown += 1
                    if shown >= max_sentences:
                        break


def main():
    ap = argparse.ArgumentParser(description="运行复盘分析（开发侧）")
    ap.add_argument("run_dir", help="运行输出目录（如 outputs/交换机_2026-09-08-214051）")
    ap.add_argument("--transcript", help="会话 jsonl 路径（供模型/上下文/thinking 分析）")
    ap.add_argument("--out", help="输出到文件（默认 stdout）")
    ap.add_argument("--thinking", action="store_true", help="抽取 thinking 决策句子")
    ap.add_argument("--thinking-max", type=int, default=30, help="决策句子上限")
    args = ap.parse_args()

    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout

    def w(*a):
        print(" ".join(str(x) for x in a), file=out)

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        w(f"运行目录不存在: {run_dir}")
        sys.exit(1)
    transcript = Path(args.transcript) if args.transcript else None

    w(f"### 复盘: {run_dir}")
    section_model(transcript, run_dir, w)
    section_metrics(run_dir, w)
    section_zero_split(run_dir, w)
    section_delivery(run_dir, w)
    section_context(transcript, w)
    if args.thinking:
        section_thinking(transcript, w, args.thinking_max)
    if args.out:
        w(f"(已写入 {args.out})")


if __name__ == "__main__":
    main()
