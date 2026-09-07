"""prompts.py 的单元测试：5 节点 prompt 模板与 JSON 解析。

模板内容以实现规格第六章为准；测试钉住关键注入点（运行时数据确实进入 prompt）。
"""
import pytest

from prompts import (EXTRACT_TMPL, INIT_TMPL, PLAN_TMPL, REPORT_TMPL, REVIEW_TMPL,
                     build_extract_prompt, build_init_prompt, build_plan_prompt,
                     build_report_prompt, build_review_prompt, fill, parse_json)


def test_fill_replaces_all_placeholders():
    assert fill("你好{{name}}，{{greeting}}！", {"name": "交换机", "greeting": "开工"}) \
        == "你好交换机，开工！"


def test_fill_missing_key_raises():
    with pytest.raises(KeyError):
        fill("{{不存在}}", {})


def test_parse_json_plain():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_fenced():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_leading_prose():
    """LLM 输出偶带引导语：剥围栏后从首个 { 截取解析。"""
    assert parse_json('好的，输出如下：\n{"a": 1}') == {"a": 1}


def test_parse_json_invalid_raises():
    with pytest.raises(ValueError):
        parse_json("不是JSON")


def test_init_prompt_carries_domain_description():
    prompt = build_init_prompt("AI 算力服务器")
    assert "AI 算力服务器" in prompt
    assert "只输出 JSON" in prompt
    assert "parent" in prompt


def test_plan_prompt_carries_structure_gaps_history():
    prompt = build_plan_prompt(
        [{"name": "交换机", "parent": "", "terms": [], "entities": [],
          "dims": [], "angles": []}],
        [{"description": "缺国内厂商指南", "node": "交换机"}],
        [{"batch": 1, "query": "交换机 文档", "node": "交换机"}],
    )
    assert '"交换机"' in prompt
    assert "缺国内厂商指南" in prompt
    assert "交换机 文档" in prompt
    assert "3-5" in prompt


def test_extract_prompt_carries_search_results():
    prompt = build_extract_prompt([
        {"query_id": 1, "query": "SONiC documentation",
         "results": [{"title": "SONiC", "url": "https://sonic-net.github.io/SONiC/",
                      "snippet": "..."}]},
    ])
    assert "SONiC documentation" in prompt
    assert "https://sonic-net.github.io/SONiC/" in prompt
    assert "new_entities" in prompt
    assert "evidence_urls" in prompt  # L2 修订：新节点提案须携带证据


def test_review_prompt_carries_structure_summary_history():
    prompt = build_review_prompt(
        [{"name": "交换机", "parent": "", "terms": [], "entities": [],
          "dims": [], "angles": []}],
        {"total": 3, "per_node": {"交换机": {"count": 3, "types": {"官方文档": 3}}}},
        [{"batch": 1, "query": "q", "node": "交换机"}],
        [{"revision_id": 1, "proposed": {"name": "待裁决节点"}}],
    )
    assert "交换机" in prompt
    assert '"官方文档": 3' in prompt
    assert '"converged"' in prompt or "converged" in prompt
    assert "待裁决节点" in prompt  # L2 修订：待裁决修订池注入 review prompt


def test_report_prompt_carries_sources_and_six_sections():
    prompt = build_report_prompt(
        [{"name": "交换机", "parent": "", "terms": [], "entities": [],
          "dims": [], "angles": []}],
        [{"name": "SONiC 文档", "url": "https://sonic-net.github.io/SONiC/"}],
        [],
    )
    assert "SONiC 文档" in prompt
    for section in ("概览", "技术格局", "产业生态", "标准体系", "中外对比", "趋势观察"):
        assert section in prompt
    assert "统计数字" in prompt


def test_templates_match_spec_chapter_six():
    """模板与实现规格第六章逐项对应：节点契约字段与写作约束不得漂移。"""
    assert "dims" in INIT_TMPL and "terms" in INIT_TMPL
    assert "角度池" in PLAN_TMPL or "dims" in PLAN_TMPL
    assert "granularity" in EXTRACT_TMPL and "source_type" in EXTRACT_TMPL
    assert "gaps" in REVIEW_TMPL and "converged" in REVIEW_TMPL
    assert "六板块" in REPORT_TMPL
    # L2 修订契约：extract 提案携带证据、review 输出裁决
    assert "evidence_urls" in EXTRACT_TMPL
    assert "revisions" in REVIEW_TMPL
    assert "pending_revisions" in REVIEW_TMPL
