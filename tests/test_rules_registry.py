"""规则登记表一致性校验（2026-09-15，docs/06 规则强制力重构方案 · 第 1 期）。

只做正向校验：锚点可解析、执行点真实存在、用例字段只接受机制类用例。
不做关键词反向覆盖（关键词法漏报与误报都高，见 docs/06 §7）；
不做口吻判断——机械只能查引用完整性，语义判断留给人。

机制类用例 = 驱动真实模块的用例（tests/test_store.py、test_postprocess.py、
test_mcp_server.py）。tests/test_skill_structure.py 断言的是 SKILL.md 里的原话，
拿它当"有机制"的证据会形成闭环（登记表从文本抄规则、用例从文本抄断言），故不计入。
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".claude" / "skills" / "autosource"
REGISTRY = SKILL_DIR / "rules.json"
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPTS = SKILL_DIR / "scripts"
SETTINGS = ROOT / ".claude" / "settings.json"

VALID_LEVELS = {"enforced", "enforced-no-test", "advisory"}
VALID_IMPL_KINDS = {"python", "settings", "hook", "store", "mcp"}
MECHANISM_TEST_FILES = {
    "tests/test_store.py",
    "tests/test_postprocess.py",
    "tests/test_mcp_server.py",
}

REGISTRY_DATA = json.loads(REGISTRY.read_text(encoding="utf-8"))
ENTRIES = REGISTRY_DATA["entries"]
SKILL_TEXT = SKILL_MD.read_text(encoding="utf-8")
SECTIONS = {
    re.sub(r"^#+\s*", "", line).strip()
    for line in SKILL_TEXT.splitlines()
    if line.startswith("#")
}


def _defined_symbols(path: Path) -> set:
    return set(re.findall(r"^(?:def|class)\s+(\w+)", path.read_text(encoding="utf-8"), re.M))


def _collected_test_names() -> set:
    names = set()
    for rel in MECHANISM_TEST_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        names.update(re.findall(r"^\s*def\s+(test_\w+)", text, re.M))
    return names


def test_registry_levels_and_required_fields():
    ids = [e["id"] for e in ENTRIES]
    assert len(ids) == len(set(ids)), "id 重复"
    for e in ENTRIES:
        assert e["level"] in VALID_LEVELS, e["id"]
        assert e["summary"].strip(), e["id"]
        assert e["anchors"], f"{e['id']} 没有锚点"
        if e["level"] in ("enforced", "enforced-no-test"):
            assert e["impl"], f"{e['id']} 标了 {e['level']} 却没有执行点"
        if e["level"] == "enforced":
            assert e["test"], f"{e['id']} 标了 enforced 却没有机制类用例"
        if e["level"] in ("advisory", "enforced-no-test"):
            assert not e["test"], f"{e['id']} 标了 {e['level']}，不该填用例"


def test_anchors_resolve_in_skill_md():
    for e in ENTRIES:
        for a in e["anchors"]:
            assert a["section"] in SECTIONS, f"{e['id']}：SKILL.md 里没有章节「{a['section']}」"
            assert a["marker"] in SKILL_TEXT, f"{e['id']}：找不到特征词「{a['marker']}」"


def test_impl_targets_exist():
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    for e in ENTRIES:
        for ref in e["impl"]:
            kind, _, rest = ref.partition(":")
            assert kind in VALID_IMPL_KINDS, f"{e['id']}：未知执行点类型 {kind}"
            if kind == "python":
                fname, _, symbol = rest.partition(":")
                path = SCRIPTS / fname
                assert path.exists(), f"{e['id']}：{fname} 不存在"
                assert symbol in _defined_symbols(path), f"{e['id']}：{fname} 里没有 {symbol}"
            elif kind == "settings":
                node = settings
                for key in rest.split("."):
                    assert isinstance(node, dict) and key in node, f"{e['id']}：settings.json 里没有 {rest}"
                    node = node[key]
            else:
                assert rest in _defined_symbols(SCRIPTS / "mcp_server.py"), f"{e['id']}：mcp_server 里没有 {rest}"


def test_test_field_is_mechanism_class():
    collected = _collected_test_names()
    for e in ENTRIES:
        if not e["test"]:
            continue
        rel, _, rest = e["test"].partition("::")
        name = rest.split("::")[-1]
        assert rel in MECHANISM_TEST_FILES, f"{e['id']}：{rel} 不在机制类用例清单里"
        assert name in collected, f"{e['id']}：{rel} 里没有用例 {name}"