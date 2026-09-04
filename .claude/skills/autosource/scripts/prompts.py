"""AutoSource 2.0 LLM 节点 prompt 模板：init / plan / extract / review / report 五节点。

模板内容以《AutoSource2.0实现规格.md》第六章为准（逐字搬运，`{{...}}` 为注入的
运行时数据）。init / plan / extract / review 输出合法 JSON（脚本解析），report
输出 Markdown 正文。
"""
import json
import re

INIT_TMPL = """你是数据源发现系统的领域拆解器。给定领域描述，把领域拆解为树状分类结构。

领域描述：{{domain_description}}

拆解原则：
- 层级清晰：上下级严格包含，同级互斥不重叠；
- 符合领域常识：分类体系与该领域公认知识体系一致；
- 深度自适应：不同领域层级数可不同；
- 覆盖完整：覆盖该领域主要方向，无明显遗漏。

对每个叶子节点，补充：
- terms：核心搜索词（中文名、英文名、常见缩写、行业术语、细分场景词）；
- dims：该节点适合搜索的探索维度（从来源类型/来源角色中选，如"官方文档""行业标准""开源社区""论文""数据集"）。

只输出 JSON，不要其他文字：
{"nodes":[{"name":"...","parent":"...","terms":["..."],"dims":["..."]}]}
根节点 parent 为空字符串。"""

PLAN_TMPL = """你是数据源发现系统的搜索规划器。基于当前领域结构、缺口清单与搜索历史，生成下一批 3-5 个查询词。

领域结构：{{structure}}
缺口清单：{{gaps}}
搜索历史（最近几批）：{{recent_search_history}}

规划规则：
- 优先针对缺口清单中的开放缺口定向搜索；
- 查询词角度取自对应节点的 dims（优先搜索尚未覆盖的角度），允许使用搜索中新发现的维度；
- 查询词构造词类：
  1. 组织形式词：数据库、排名、列表、标准、仓库、数据集、文献库、合集等；
  2. 体裁/入口词：官方文档、开发者文档、technical documentation、手册、知识库、API 参考、帮助中心、白皮书、论文文献、专利、市场研究等；
  3. 来源角色词：标准组织、监管机构、政府部门、行业协会、厂商、研究机构、大学、评测机构、基金会、公共数据平台等；
  4. 厂商/产品名规则：不单独搜产品名、不搜规格参数页；搜文档站、知识库、API、帮助中心入口；
- 实体选题：对新发现的实体（机构/厂商/产品/规范），用"实体名 + 入口词"构造查询，可不含领域词；
- 易失效角度：论文/专利/标准必须组合"领域词 + 入口词"；英文查询避免未组合泛化后缀（list/directory/registry/collection）。

只输出 JSON，不要其他文字：
{"queries":[{"query":"...","node":"...","angle":"...","reason":"..."}]}"""

EXTRACT_TMPL = """你是数据源发现系统的提取器。对搜索结果逐条判断是否收录，并提取新实体、术语与子方向。

搜索结果：{{search_results}}

逐条判断（禁止整行拒绝）：
- 每条结果独立判断收或不收；
- 标注 granularity：合集级 / 单篇级；同一来源同时有合集入口与单篇时，优先收合集入口；
- 语言版本偏好：同一内容多语言版本只收一个，优先级 中文 > 英文 > 其他；
- 平台准入：知网、专利库、百科、标准平台首页等跨领域通用平台不收录；平台的领域专属入口可收；
- source_type 从词类词汇（组织形式词/体裁词/来源角色词）中选取，不另造同义新词；
- URL 必须从搜索结果中逐字复制，禁止规范化、截短、凭先验知识补 URL。

只输出 JSON，不要其他文字：
{"sources":[{"name":"...","url":"...","source_type":"...","granularity":"...","node":"...","description":"..."}],
 "new_entities":[{"name":"...","kind":"机构|厂商|产品|项目|规范","node":"..."}],
 "new_terms":[{"term":"...","node":"..."}],
 "new_nodes":[{"name":"...","parent":"...","terms":["..."],"dims":["..."]}]}
new_entities 与 new_terms 必须带 node（归属节点）；new_nodes 必须给完整可搜索字段（terms/dims）。"""

REVIEW_TMPL = """你是数据源发现系统的评审器。基于当前状态，生成开放缺口清单与覆盖充分性判断。

领域结构：{{structure}}
源集合统计：{{source_summary}}
搜索历史：{{search_history}}

评审规则：
- 开放缺口清单：只列出当前仍未覆盖的方向/维度/实体，每条标注归属节点（已覆盖的不再列出，缺口被补齐后自然消失）；
- 覆盖充分性判断：领域的主要子方向是否都已有数据源覆盖、是否还有明显未探索的维度；
- 客观覆盖条件（脚本另行校验，此处只做语义判断）：各节点适用维度是否都已搜索、是否有明显遗漏。

只输出 JSON，不要其他文字：
{"gaps":[{"description":"...","node":"..."}],
 "converged":true或false,
 "reason":"..."}
gaps 是"当前全部开放缺口"——脚本用它整体替换 state.exploration.gaps。"""

REPORT_TMPL = """你是数据源发现系统的分析报告撰写器。基于领域结构、已收录数据源清单与搜索历史，按六板块撰写分析报告正文。

领域结构：{{structure}}
已收录数据源清单：{{sources}}
搜索历史：{{search_history}}

六板块：概览 / 技术格局 / 产业生态 / 标准体系 / 中外对比 / 趋势观察。

写作要求：
- 依据锚定已收录的数据源，不空谈、不编造；
- 统计数字一律不写（"数据总览"节由脚本注入）。

输出 Markdown 正文（六板块，不含"数据总览"节）。"""

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def fill(template: str, mapping: dict) -> str:
    """替换模板中的 {{key}} 占位符；缺 key 抛出 KeyError（模板漂移显式化）。"""

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in mapping:
            raise KeyError(f"prompt 模板占位符 {{{{{key}}}}} 缺少注入值")
        return str(mapping[key])

    return _PLACEHOLDER_RE.sub(_sub, template)


def parse_json(content: str) -> dict:
    """解析 LLM 的 JSON 输出：剥 ```json 围栏，从首个 { 截取；顶层须为对象。"""
    text = str(content).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("响应中找不到 JSON 对象")
    try:
        data = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层应为对象")
    return data


def build_init_prompt(domain_description: str) -> str:
    return fill(INIT_TMPL, {"domain_description": domain_description})


def build_plan_prompt(structure: list[dict], gaps: list[dict],
                      recent_search_history: list[dict]) -> str:
    return fill(PLAN_TMPL, {
        "structure": json.dumps(structure, ensure_ascii=False),
        "gaps": json.dumps(gaps, ensure_ascii=False),
        "recent_search_history": json.dumps(recent_search_history, ensure_ascii=False),
    })


def build_extract_prompt(search_results: list[dict]) -> str:
    return fill(EXTRACT_TMPL, {
        "search_results": json.dumps(search_results, ensure_ascii=False)})


def build_review_prompt(structure: list[dict], source_summary: dict,
                        search_history: list[dict]) -> str:
    return fill(REVIEW_TMPL, {
        "structure": json.dumps(structure, ensure_ascii=False),
        "source_summary": json.dumps(source_summary, ensure_ascii=False),
        "search_history": json.dumps(search_history, ensure_ascii=False),
    })


def build_report_prompt(structure: list[dict], sources: list[dict],
                        search_history: list[dict]) -> str:
    return fill(REPORT_TMPL, {
        "structure": json.dumps(structure, ensure_ascii=False),
        "sources": json.dumps(sources, ensure_ascii=False),
        "search_history": json.dumps(search_history, ensure_ascii=False),
    })
