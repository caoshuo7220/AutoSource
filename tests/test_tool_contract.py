"""工具契约表与 store.py 拒绝分支的一致性校验（2026-09-21）。

契约表在 references/通用纪律.md「工具契约」段——子代理填 record_* 时的唯一依据。
2026-09-21 实测：78 个子代理共 112 次去读 scripts/ 下的源码，动机就是查这份契约
（典型序列 `Grep def record_knowledge` → `Read store.py` → 调工具）；表与实现一旦
脱节，子代理只能再回去读源码，补契约这件事就白做了。

拦两个方向：
  ① 表里列了必填、实现并不校验——本轮实测踩中：`note` 曾被列为必填，实际缺了
     照样入库，表在此处撒谎（正是子代理不信任表的来源）
  ② 实现新增拒绝分支、表里没跟上

不做：不反向枚举"可选"字段——实现用 .get() 取全部可选值，反向枚举只会把测试
绑死在实现细节上。
"""
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "autosource"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from store import record_knowledge, record_sources   # noqa: E402

REF_DISCIPLINE = (SKILL_DIR / "references" / "通用纪律.md").read_text(encoding="utf-8")
STORE_SRC = (SKILL_DIR / "scripts" / "store.py").read_text(encoding="utf-8")

NODES = ["节点甲"]
DECLARED = "某机构"

# 契约表下方「缺失即拒」一句点名的字段（两工具入参的并集；node 由 category_path
# 推导，只是 record_knowledge 的入参）
ENFORCED = ("name", "url", "node", "category_path")
SOURCES_REQUIRED = ("name", "url", "category_path")
KNOWLEDGE_REQUIRED = ("name", "node", "url")   # url 仅在 verified=true 时必填

# store.py 的拒绝文案 → 契约表中应出现的对应描述（描述跨反引号取词，故取公共子串）
CODE_REASONS = {
    "缺 name/url": "缺 name/url",
    "缺 name/node": "缺 name/node",
    "垃圾域/低价值聚合平台，不收": "垃圾域",
    "category_path 未匹配任何声明节点": "未匹配任何声明节点",
    "证据留痕不存在": "证据留痕不存在",
    "URL 不在证据留痕中": "URL 不在留痕中",
    "名称不在 manifest 声明清单中": "不在 manifest 声明清单",
    "verified=true 缺 url": "缺 url",
}


def _contract_section() -> str:
    """契约表到「被拒处理」之间的正文。"""
    head = REF_DISCIPLINE.index("**工具契约**")
    return REF_DISCIPLINE[head:REF_DISCIPLINE.index("**被拒处理**", head)]


def _fixtures(tmp_path: Path) -> tuple[Path, Path]:
    """证据留痕（含候选 URL）与 manifest（含声明名）。"""
    ev = tmp_path / "evidence.jsonl"
    ev.write_text(json.dumps(
        {"tool_name": "WebSearch", "tool_input": {"query": "q"},
         "tool_response": {"results": [{"url": "https://a.com/"}]}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"nodes": NODES, "vendors": [],
         "knowledge": [{"name": DECLARED, "node": NODES[0]}]},
        ensure_ascii=False), encoding="utf-8")
    return ev, tmp_path / "store.jsonl"


def test_enforced_fields_match_the_declaring_line():
    """测试里写死的 ENFORCED 必须与契约表的声明同步——改一处漏一处即失败。"""
    line = next(ln for ln in _contract_section().splitlines() if "缺失即拒" in ln)
    assert all(f"`{f}`" in line for f in ENFORCED), f"契约表声明行与 ENFORCED 不符：{line}"


def test_enforced_fields_really_rejected(tmp_path):
    """表说必填即拒的字段，缺了必须真拒（`note` 类错误的拦截点）。"""
    assert set(SOURCES_REQUIRED) | set(KNOWLEDGE_REQUIRED) == set(ENFORCED)
    ev, store = _fixtures(tmp_path)

    for missing in SOURCES_REQUIRED:
        entry = {"name": "甲", "url": "https://a.com/", "category_path": NODES[0]}
        entry.pop(missing, None)
        result = record_sources(store, [entry], ev, NODES)
        assert result["accepted"] == 0, f"record_sources 缺 {missing} 却被收下"

    for missing in KNOWLEDGE_REQUIRED:
        entry = {"name": DECLARED, "node": NODES[0], "verified": True,
                 "url": "https://a.com/"}
        entry.pop(missing, None)
        result = record_knowledge(store, [entry], ev)
        assert result["accepted"] == 0, f"record_knowledge 缺 {missing} 却被收下"


def test_every_code_reject_reason_is_documented():
    """store.py 里每条拒绝文案都能在契约表里找到对应——实现加了口子就报错。"""
    literals = set(re.findall(r'"reason":\s*f?"([^"]+)"', STORE_SRC))
    assert literals, "未从 store.py 解析出任何拒绝文案，正则或实现结构已变"
    section = _contract_section()
    for literal in sorted(literals):
        matched = [doc for code, doc in CODE_REASONS.items() if literal.startswith(code)]
        assert matched, f"store.py 有未登记进契约表的拒绝文案：{literal!r}"
        assert matched[0] in section, f"契约表未描述拒绝理由：{matched[0]}"
