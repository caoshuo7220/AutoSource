"""AutoSource 存储层：store.jsonl 追加日志 + 入库即验 + coverage 对账（docs/04）。

store 承载三类记录：增量发现条目（type=source）、搜索日志（type=search）、
清单核对结果（type=knowledge，2026-09-08 架构修订：验证结果随验证过程落库，
废除"会话暂存 + 阶段 6 一次性转写 manifest"——214051 实证漏写 60 个 verified
字段的事故类别；manifest 只承载阶段 0-2 声明态清单，收尾折叠时由 postprocess.fold
把 store 核对记录与声明对账并入）。
分工原则：LLM 只做语义判断，持久化与校验全部由本模块（脚本）保证。

- record_sources：批次入库即验——name/url 非空、granularity 枚举（缺省/非法
  归一化为合集级）、URL 走证据链边界校验（evidence.check_grounded，
  含 # 豁免与 ? 严格）；裸 URL 精确相等才幂等跳过并计数（不归一化——见
  docs/04 裁决 8.1）；单条被拒不阻断批次，其余照常入库
- record_search：搜索日志批量追加（查询词的证据比对在收尾折叠时做，现状机制）；
  zero_reason 可选——增量/扩量零提取的拒收理由（收尾护栏校验 2/3 校验）
- record_knowledge：清单核对结果批量入库（verified=true 必带 url 且过证据链；
  verified=false 带 note 不查证据；同名重录 = 状态更新，折叠取末次）
- coverage：每节点"已收 vs 提取"只读计数 + 每节点体裁分布（types）——脚本只供数据，薄弱判定仍由模型做

条目粒度契约（GRANULARITY_LEVELS）与节点推导（leaf_node）是存储层的契约工具——
入库校验与收尾统计共用，编排层（postprocess）从这里取（依赖方向：编排 → 存储，
单向向下；本模块不 import 编排层）。

store.jsonl 由脚本持有，模型不可见；每行一条（脚本盖 ts，模型无时钟——docs/04 §3.1）、
追加原子，崩溃最多丢最后一个批次。
"""
import difflib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from evidence import check_grounded, nearest_forms

# 垃圾域与低价值聚合平台黑名单（2026-09-10 收录政策收紧）：入库即拒 + 收尾过滤双层。
# 只收录"平台级"域名（反复出现的大平台，子串匹配一条覆盖全部子域），长尾单站垃圾
# 由判据①（机构发布的信息载体）覆盖，不进名单——名单收敛不膨胀；高频新平台补一行
# （治理同 SOURCE_TYPE_ALIASES：渐进收敛）。
GARBAGE_DOMAINS = [
    # 电商与消费平台
    "taobao", "tmall", "jd.com", "1688.com", "suning", "pinduoduo",
    # 注：amazon 条目不写 "amazon.com"——子串会误伤 docs.aws.amazon.com（合法
    # 厂商文档门户，SOM厂商轮交付物实证）；"www.amazon." 只匹配电商主站
    "ebay.com", "www.amazon.", "amazon.cn", "alibaba.com",
    # 内容与自媒体平台
    "zhihu", "baike.baidu", "csdn", "cnblogs", "jianshu", "51cto",
    "weibo", "douban", "toutiao",
    # 新闻门户
    "sina.com", "sohu.com", "163.com", "netease", "ifeng", "thepaper",
    # 科技媒体与导购
    "ithome", "smzdm", "36kr", "huxiu", "tmtpost", "donews",
    "zol.com", "pconline", "yesky", "it168",
    # 财经
    "eastmoney", "stockstar", "10jqka", "gelonghui", "dxpress",
    "cls.cn", "api3.cls", "xueqiu", "hexun",
    # 报告倒卖站群
    "sgpjbg", "168report", "qyresearch", "gminsights", "researchandmarkets",
    "giiresearch", "6wresearch", "marketresearch.com", "indexbox",
    "htfmarketintelligence", "straitsresearch", "marketresearchfuture", "worldic",
    # 文档分享与手册镜像
    "book118", "renrendoc", "docin", "doc88", ".wenku.", "zhidao", "scribd",
    "manualslib", "manualzz", "alldatasheet", "elcodis", "iczoom",
    # 图书平台
    "books.google", "worldofbooks", "alibris", "abebooks",
    # 招聘
    "zhaopin", "liepin", "51job", ".seek.", "indeed.com", ".job.",
]


def domain_of(url: str) -> str:
    """主机名（契约工具：入库闸门与收尾去重共用同一份实现）。

    无协议写法要认——结果的结构化链接带协议、摘要散文常不带，两种都是逐字照抄来的
    合法写法（2026-09-18 实证：不补协议则返回空串，垃圾域闸门形同虚设）。此前本模块
    与 postprocess 各有一份拷贝，只改其中一份不会有测试报红——收敛为单一实现。
    """
    return urlparse(url if "://" in url else "http://" + url).netloc


def is_garbage_domain(domain: str) -> bool:
    """域名命中垃圾域/低价值聚合平台名单（子串词形匹配，必要非充分——
    命中即拒收，不命中不保证收录：判据①仍是主过滤）。"""
    return any(g in domain for g in GARBAGE_DOMAINS)

# existing_urls 增量缓存：record_sources 每批全量读 store 建幂等集合，批数×记录数
# 增长时是 O(n²)；按 (路径, mtime) 缓存——文件被本进程以外改动（mtime 变化）时
# 自然失效重建（本服务是唯一写入方，正常场景命中缓存）。
_url_cache: dict[str, tuple[int, set[str]]] = {}


def leaf_node(category_path: str, nodes: list[str]) -> Optional[str]:
    """取 category_path 对应的叶子节点：节点名的最长后缀匹配（节点名本身可含 -）。"""
    matches = [n for n in nodes if category_path == n or category_path.endswith("-" + n)]
    return max(matches, key=len) if matches else None


# granularity 仅归一化与计数、不拒绝（产量优先，粒度/子站问题由后续
# "站点与子站合并"功能处理）；站点级已并入合集级，存量按非法值归一化。
GRANULARITY_LEVELS = ("合集级", "单篇级")

# 数据源类型封闭词表（12 类 + 其他兜底）：单字段、纯载体维度、互斥。
# 依据：45 轮历史共 332 个散词、头部 16 词覆盖 98%——长尾是噪声不是新形态
# （docs/01 决策 2 修订）。类名只回答"URL 背后是什么载体"，无主体/领域残留；
# 判定顺序与判据见 SKILL.md「数据源类型」段。
SOURCE_TYPES = (
    "文档", "官网", "标准", "报告", "文献", "专利", "数据集",
    "数据库", "代码仓库", "知识库", "社区", "媒体", "其他",
)

# 历史散词 → 标准词（同义词环，SKOS altLabel 机制）：覆盖历史 332 词，
# 新词先落「其他」+ unmapped 告警，人工补一行别名即收敛——词表治理是
# 渐进闭环，不是打补丁。
SOURCE_TYPE_ALIASES: dict[str, str] = {
    # → 文档（手册/指南/API/教程等教人使用的内容）
    "厂商文档": "文档", "官方文档": "文档", "厂商官方文档": "文档",
    "官方文档站": "文档", "厂商技术文档": "文档", "API 参考": "文档",
    "云厂商文档": "文档", "技术手册": "文档", "手册": "文档",
    "帮助中心": "文档", "API 文档": "文档", "开发者文档": "文档",
    "技术指南": "文档", "技术指南库": "文档", "软件文档": "文档",
    "产品手册": "文档", "产品指南": "文档", "厂商技术手册": "文档",
    "官方文档教程": "文档", "API 文档教程": "文档", "API 文档仓库": "文档",
    "开源 API 文档": "文档", "开放平台 API 文档": "文档", "开源文档": "文档",
    "数据模型文档": "文档", "科研机构官方文档": "文档", "政府官方文档": "文档",
    "厂商应用笔记": "文档", "厂商产品体系": "文档", "产品列表": "文档",
    "选型列表": "文档", "产品目录": "文档", "厂商产品清单": "文档",
    "厂商技术资源": "文档", "产品对照文档": "文档", "厂商文档体系": "文档",
    "厂商技术页": "文档", "厂商官方公开资料": "文档", "开源代码库文档": "文档",
    "开源代码库文档站": "文档", "开源教程": "文档", "教程站": "文档",
    "开源硬件文档": "文档",
    "技术教程文章": "文档", "测试仪器文档": "文档", "政府文件": "文档",
    "政府出版物": "文档", "政府政策": "文档", "法规文件": "文档",
    # → 官网（机构主页/门户首页：厂商/协会/政府/院校/机构）
    "厂商": "官网", "厂商官网": "官网", "行业协会": "官网",
    "评测机构": "官网", "评测认证": "官网", "评测": "官网",
    "研究机构": "官网", "标准组织": "官网", "大学": "官网",
    "行业组织": "官网", "基金会": "官网", "政府机构": "官网",
    "政府部门": "官网", "政府": "官网", "认证机构": "官网",
    "学术组织": "官网", "检测认证机构": "官网", "标准联盟": "官网",
    "产业联盟": "官网", "行业联盟": "官网", "国际组织": "官网", "开放组织": "官网",
    "监管机构": "官网", "产业组织": "官网", "媒体研究机构官网": "官网",
    "开源生态组织": "官网", "开源组织": "官网", "学术学会": "官网",
    "行业学会": "官网", "市场研究机构": "官网", "科研机构": "官网",
    "行业研究机构": "官网",
    "认可机构": "官网", "认证/评测机构": "官网", "评测平台": "官网",
    # → 标准（标准全文/规范/标准检索入口）
    "行业标准": "标准", "国家标准": "标准", "团体标准": "标准",
    "军用标准": "标准", "国际标准": "标准", "标准文件": "标准",
    "标准体系": "标准", "标准库": "标准", "技术标准": "标准", "航天行业标准": "标准",
    "标准文档": "标准", "开源标准": "标准", "开放标准": "标准", "评测标准": "标准",
    "开源硬件标准": "标准", "标准平台": "标准", "标准检索入口": "标准",
    "标准平台检索入口": "标准", "开放硬件标准": "标准", "开源硬件规范": "标准",
    "行业标准文件": "标准", "数据规范": "标准", "标准清单": "标准",
    "标准组织发布": "标准", "国家标准文件": "标准", "行业规范": "标准",
    "政府技术规范": "标准", "开源规范": "标准",
    # → 报告（市场研究/行业分析/白皮书/调查统计）
    "市场研究": "报告", "市场研究报告": "报告", "白皮书": "报告",
    "行业白皮书": "报告", "厂商白皮书": "报告", "技术白皮书": "报告", "行业报告": "报告",
    "研究报告": "报告", "评测报告": "报告", "白皮书库": "报告",
    "行业白皮书库": "报告", "行业研究报告": "报告", "行业研究": "报告",
    "权威评测机构研究报告": "报告", "权威调查机构报告": "报告",
    "国际组织报告": "报告", "政府报告": "报告", "政府技术路线图": "报告",
    "行业研究报告文章": "报告", "行业技术报告": "报告", "行业组织/报告": "报告",
    "行业统计": "报告", "行业资讯/报告": "报告", "评测/市场调研": "报告",
    "评测报告库": "报告", "测试信息": "报告", "测试结果": "报告",
    "测试评测报告": "报告", "测试报告": "报告",
    "研究机构报告": "报告", "研究机构发布": "报告",
    # → 文献（论文/期刊/文献库）
    "学术文献": "文献", "论文文献": "文献", "学术文献库": "文献",
    "文献库": "文献", "论文库": "文献", "论文": "文献", "学术论文": "文献", "综述论文": "文献",
    "学术期刊": "文献", "期刊论文": "文献", "技术期刊": "文献",
    "学术资源站": "文献", "论文合集仓库": "文献", "学术文献平台": "文献",
    "学术书目数据库领域入口": "文献", "学术会议论文集": "文献",
    "学术平台领域检索入口": "文献", "学术预印本平台领域入口": "文献",
    # → 专利
    "专利": "专利", "专利库": "专利", "专利文献": "专利", "专利数据库": "专利",
    # → 数据集（可整体下载/机读）
    "数据集": "数据集", "数据集目录": "数据集", "数据集镜像页面": "数据集",
    "公共数据集": "数据集", "政府统计数据": "数据集", "数据集仓库": "数据集",
    "数据集清单仓库": "数据集", "数据集官网": "数据集", "数据集平台页面": "数据集",
    "数据集镜像平台页面": "数据集", "开源数据集与模型库": "数据集",
    "开源数据集仓库": "数据集", "数据集合集": "数据集", "数据集基准页面": "数据集",
    "数据集平台": "数据集", "数据集检索仓库": "数据集", "数据集浏览器平台": "数据集",
    "数据集门户": "数据集", "数据集页面": "数据集", "仿真模型库": "数据集",
    "仿真数据模型": "数据集", "语料库官网": "数据集", "研究机构数据集": "数据集",
    "行业数据": "数据集", "开源基准数据集": "数据集", "开源数据模型": "数据集",
    "研究数据集/论文": "数据集", "研究数据/论文": "数据集", "公共数据": "数据集",
    "数据集集合/仓库": "数据集",
    # → 数据库（独立/跨厂商的条目检索平台）
    "公共数据平台": "数据库", "数据平台": "数据库", "数据库": "数据库",
    "垂直门户": "数据库", "行业门户": "数据库", "行业数据库": "数据库",
    "项目数据库": "数据库", "政府数据库": "数据库", "开源数据库": "数据库",
    "元器件目录平台": "数据库", "研究机构数据库": "数据库",
    "行业协会/产业数据库": "数据库", "数据库发布页": "数据库", "元件库": "数据库",
    "元件数据库": "数据库", "元器件数据库": "数据库", "IP 核数据库": "数据库",
    "产业数据库": "数据库", "厂商对比库": "数据库", "厂商目录数据库": "数据库",
    "器件参数数据库": "数据库", "安全数据库": "数据库", "材料数据库": "数据库",
    "监管机构资源库": "数据库", "研究数据库": "数据库", "研究数据库/模型库": "数据库",
    "研究数据库发布页": "数据库", "技术数据库": "数据库", "数据库目录": "数据库",
    "政府科技信息库": "数据库", "开放数据服务页": "数据库", "数据平台入口": "数据库",
    "数据平台领域入口": "数据库", "数据平台领域检索入口": "数据库",
    "市场数据平台": "数据库", "市场数据机构": "数据库", "行业数据机构": "数据库",
    "行业数据平台": "数据库", "认证数据库": "数据库", "研究数据平台": "数据库",
    "研究数据注册库": "数据库", "公共数据库/开源项目": "数据库",
    # → 代码仓库（Git 仓库/开源项目/代码托管）
    "仓库": "代码仓库", "开源仓库": "代码仓库", "开源代码库": "代码仓库",
    "开源项目": "代码仓库", "开源生态": "代码仓库", "开源硬件": "代码仓库",
    "开源工具": "代码仓库", "开源软件": "代码仓库", "开源工具库": "代码仓库",
    "开源工具合集": "代码仓库", "开源资源库": "代码仓库", "开源目录": "代码仓库",
    "开源符号库": "代码仓库", "开源软件库": "代码仓库", "开源 EDA 符号库": "代码仓库",
    "开源数据库/工具箱": "代码仓库", "开源资源目录": "代码仓库",
    "开源项目/协议实现": "代码仓库", "订阅源清单仓库": "代码仓库",
    "代码平台领域入口": "代码仓库", "竞赛方案汇总仓库": "代码仓库",
    "竞赛复现代码库": "代码仓库", "设计工具与参考设计": "代码仓库",
    "设计工具/参考设计库": "代码仓库", "厂商设计工具": "代码仓库",
    "厂商设计资源库": "代码仓库", "研究机构工具库": "代码仓库",
    "科研机构工具库": "代码仓库", "开源 API 服务": "代码仓库",
    # → 知识库（百科词条/Wiki/课程教材等体系化知识整理）
    "知识库": "知识库", "百科知识库": "知识库", "厂商知识库": "知识库",
    "技术图书": "知识库", "技术书籍": "知识库", "培训课程": "知识库",
    "教材页面": "知识库", "高校课程页面": "知识库", "在线课程页面": "知识库",
    "培训材料": "知识库", "培训教材": "知识库", "培训资料": "知识库",
    "教学平台": "知识库", "大学课程资料": "知识库", "行业培训": "知识库",
    "百科词条页面": "知识库", "开源标准知识库": "知识库", "技术资料库": "知识库",
    # → 社区（论坛/问答/开发者社区）
    "社区": "社区", "开源社区": "社区", "技术社区": "社区",
    "行业社区": "社区", "开发者社区": "社区", "社区论坛": "社区",
    "工程师社区": "社区", "厂商社区/论坛": "社区", "垂直门户/社区": "社区",
    "数据集社区": "社区",
    # → 媒体（新闻资讯/博客/行业媒体内容）
    "资讯平台": "媒体", "行业资讯": "媒体", "行业媒体": "媒体",
    "博客": "媒体", "技术博客": "媒体", "行业资讯平台": "媒体",
    "技术资源汇总文章": "媒体", "技术文章": "媒体", "行业政策文章": "媒体",
    "行业实践文章": "媒体", "工程博客文章": "媒体", "厂商官方技术博客": "媒体",
    "厂商技术文章": "媒体", "厂商资讯": "媒体", "评测媒体": "媒体",
    "评测文章": "媒体", "评测门户": "媒体", "媒体评测": "媒体",
    "专题门户": "媒体", "行业媒体门户": "媒体", "行业媒体专题": "媒体",
    "博客聚合": "媒体", "行业科普": "媒体", "行业标准文章": "媒体",
    "行业实践案例文章": "媒体", "行业技术播客": "媒体", "行业技术演讲页面": "媒体",
    "行业自律文件文章": "媒体", "行业协会/资讯": "媒体", "行业协会资讯": "媒体",
    "资讯平台专题": "媒体", "媒体研究平台": "媒体", "标准解读": "媒体",
    # → 其他（目录导航/排名列表/展会会议/竞赛）
    "排名": "其他", "列表": "其他", "行业展会": "其他", "行业会议": "其他",
    "学术会议": "其他", "学术会议官网": "其他", "学术评测活动官网": "其他",
    "竞赛官网": "其他", "竞赛平台赛题页": "其他", "书籍目录": "其他",
    "会议排名": "其他", "整理清单": "其他", "社区列表": "其他",
    "集采公告": "其他", "合集": "其他", "通用平台领域入口": "其他",
    "设计资源合集": "其他",
}


def canonicalize_source_type(raw) -> str:
    """归一到 SOURCE_TYPES：标准词自映射 → 别名映射 → 其他兜底（幂等）。

    非空输入恒返回词表内值——store/CSV/stats 口径一致；空/空白原样返回
    （模型违约未填的残缺如实保留，由 stats「未标注」口径承接，不混入其他）。
    落「其他」的非空原始词由调用方收集进 unmapped 告警（可补别名收敛）。
    """
    r = str(raw or "").strip()
    if not r:
        return ""
    if r in SOURCE_TYPES:
        return r
    return SOURCE_TYPE_ALIASES.get(r, "其他")


def normalize_entry_type(value) -> tuple[str, str, str]:
    """条目体裁归一的一次口径：→ (标准词, 原始词, 表外原始词或空串)。

    原始词为 strip 后的留痕（store 行 source_type_raw）；表外词（落「其他」
    且非「其他」本身）由第三个返回值带出——审计按原始词计数。写方（入库）
    与收尾三来源汇合共用本函数——多份口径漂移是已发生过的事故类型。
    """
    raw = str(value or "").strip()
    source_type = canonicalize_source_type(raw)
    unmapped = raw if source_type == "其他" and raw != "其他" else ""
    return source_type, raw, unmapped


def normalize_granularity(value) -> str:
    """粒度归一：非法/缺失一律「合集级」（P-002 默认，不拒绝）。"""
    return value if value in GRANULARITY_LEVELS else "合集级"


def read_manifest(run_dir: Path) -> Optional[dict]:
    """解析运行目录 manifest.json（全项目单一解析点）。

    缺失 → None（由调用方定策略：收尾报错 / 入库跳过对账 / 只读工具宽容）；
    损坏 / 非对象 → ValueError（文案与 fold 原有报错逐字一致）。nodes 是否
    列表等结构细节由调用方按需校验——解析与策略分离。
    """
    path = run_dir / "manifest.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"manifest.json 损坏: {e}")
    if not isinstance(data, dict):
        raise ValueError('manifest.json 结构错误：应为 {"domain", "nodes", ...} 对象')
    return data


def declared_items(manifest: dict) -> list[dict]:
    """manifest 两栏（vendors + knowledge）的声明条目——非列表/非对象项跳过。"""
    items: list[dict] = []
    for key in ("vendors", "knowledge"):
        value = manifest.get(key)
        if isinstance(value, list):
            items += [x for x in value if isinstance(x, dict)]
    return items


def declared_names_of(manifest: dict) -> set[str]:
    """声明名称集合：逐字口径（name 必须逐字照抄 manifest，与 URL 逐字校验同一纪律）。

    入库校验与收尾对账共用——此前两处各自 strip/不 strip，同一名称差异会
    "入库放过、收尾才判未核对"（2026-09-22 统一为逐字）。
    """
    return {str(x.get("name")) for x in declared_items(manifest) if x.get("name")}


# store 记录形状契约（接口契约第 1 批）：三种行的键集合声明——写方（record_*）
# 输出必须等于此处集合（由见证测试比对）；新增字段必须同步更新此处。
STORE_FIELDS = {
    "source": ("type", "node", "name", "category_path", "source_type",
               "source_type_raw", "granularity", "url", "description",
               "reason", "ts"),
    "search": ("type", "phase", "node", "query", "results", "extracted",
               "zero_reason", "ts"),
    "knowledge": ("type", "node", "name", "verified", "category_path",
                  "source_type", "source_type_raw", "granularity", "url",
                  "description", "reason", "note", "ts"),
}


# 失败分类表（2026-09-23）：拒收/阻断文案的单一事实源——文案模板一处定义，
# 出口只报 code（未登记即 KeyError）。**文案与既有散文逐字一致，模型可见面零变化**：
# code 与 actor 只进 run_dir/rejects.jsonl，不进工具返回值。
#   三元组 = (文案模板, 处置动作, 受话人)；受话人 model = 模型可自救，env = 环境异常。
# 两张表按可见性分：REJECTS 是 record_* 的逐条拒收（模型可见，通用纪律「工具契约」段覆盖）；
# BLOCKS 是 fold 的整轮阻断（异常文案，不属工具契约）。
REJECTS = {
    "MISSING_FIELD": ("缺 name/url", "补字段后重传", "model"),
    "GARBAGE_DOMAIN": ("垃圾域/低价值聚合平台，不收", "改换来源，勿重试同一域名", "model"),
    "NODE_NOT_DECLARED": ("category_path 未匹配任何声明节点——应填**节点名本身**"
                          "（如「工业以太网交换机」），不带条目名、不用 / 分隔"
                          "（172015 轮实测：填成「节点/条目名」致 108 条全部归不到节点）",
                          "改用 manifest 里的节点名重传", "model"),
    "EVIDENCE_LOG_MISSING": ("证据留痕不存在: {path}（PostToolUse hook 未生效？）",
                             "检查留痕是否被删；不可自行重建", "model"),
    "URL_NOT_IN_EVIDENCE": ("URL 不在证据留痕中{hint}",
                            "逐字照抄搜索结果原文重传", "model"),
    "MISSING_NAME_NODE": ("缺 name/node", "补字段后重传", "model"),
    "NAME_NOT_DECLARED": ("名称不在 manifest 声明清单中{near}"
                          "——name 必须逐字照抄 manifest",
                          "核对清单项名称后重传", "model"),
    "VERIFIED_WITHOUT_URL": ("verified=true 缺 url", "补 URL 或改 verified=false", "model"),
}

BLOCKS = {
    "QUOTA_SHORT": ("节点增量搜索未达标（每节点应 ≥{n} 次）：{detail}"
                    "——补搜并补录 record_search（phase 填「增量发现」）"
                    "后重跑 finalize（运行目录未被重命名）",
                    "补搜并补录 record_search 后重跑 finalize", "model"),
    "ZERO_REASON_MISSING": ("零提取留痕缺失或与证据矛盾（{n} 条）：拒收必须逐条说明理由且"
                            "可核对——该收的补 record_sources，确不收的补录 zero_reason（重传同"
                            "phase+node+query 行，末次覆盖）后重跑 finalize。**phase 必须与被拒行逐字"
                            "相同**——换成别的阶段等于新写一条，原件仍在、下次照样被拒：\n{detail}",
                            "补录 zero_reason 后重跑 finalize", "model"),
    "NO_CANDIDATES": ("搜索过（journal 非空）但候选为 0——疑似搜索工具异常，"
                      "按方案中止处理；raw.json 已保留供人工检查",
                      "中止并人工检查", "env"),
    "CHECKLIST_UNRECORDED": ("清单项未核对：{detail}",
                             "补 record_knowledge 后重跑 finalize", "model"),
    "SOURCES_EMPTY": ("来源未入库：增量/扩量搜索提取合计 {n} 条，但 store 中来源为 0——"
                      "疑似未调用 record_sources；修正后重跑 finalize（运行目录未被重命名）",
                      "先 record_sources 再重跑 finalize", "model"),
}

_FAILURES = {**REJECTS, **BLOCKS}   # 并集：渲染与查 actor 用


def render_failure(code: str, fmt: dict) -> str:
    """按表渲染失败文案（拒收与阻断共用）。缺参渲染成空串而**不抛**（失败路径的
    职责是不阻断）；未知 code 抛 KeyError——那是编程错误，当场暴露。"""
    return _FAILURES[code][0].format_map(defaultdict(str, fmt))


def reject_entry(code: str, *, index: int, name: str, url: Optional[str] = None,
                 **fmt) -> dict:
    """逐条拒收的返回体（模型可见）。键集合与文案与 2026-09-23 前逐字一致：
    record_sources 带 url 键、record_knowledge 不带（url=None 即不含该键）。
    code 与插值参数一律不进返回值。"""
    entry: dict = {"index": index, "name": name}
    if url is not None:
        entry["url"] = url
    entry["reason"] = render_failure(code, fmt)
    return entry


def append_records(store_path: Path, records: list[dict]) -> int:
    """追加 JSONL 记录（单次追加原子）；空批不创建文件。返回追加数。"""
    if not records:
        return 0
    with store_path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(records)


def load_store(store_path: Path) -> tuple[list[dict], int]:
    """读全部记录；坏行（非 JSON/非对象）跳过并计数。"""
    records: list[dict] = []
    bad = 0
    for line in store_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(payload, dict):
            records.append(payload)
        else:
            bad += 1
    return records, bad


def _existing_urls(store_path: Path) -> set[str]:
    """读 store 全部裸 URL（带 mtime 缓存）——幂等跳过的判断集合。"""
    key = str(store_path)
    if not store_path.exists():
        return set()
    mtime = store_path.stat().st_mtime_ns
    cached = _url_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    records, _ = load_store(store_path)
    urls = {r.get("url") for r in records}
    _url_cache[key] = (mtime, urls)
    return urls


def _grounded_hint(url: str, evidence: str) -> str:
    """证据拒收时的差异提示：留痕里同主机出现过的写法，或"该主机一条都没有"。

    前半句让模型照抄即可改对；后半句说明这条 URL 未被搜到、必须去搜（拼凑或凭
    记忆写的 URL 只落在这里）。2026-09-17 运行 115926 实证：拒收只报"不在留痕中"
    时，模型反复试错无效，最后去读 evidence.py 源码才定位差异。
    """
    forms = nearest_forms(url, evidence)
    if forms:
        return "；留痕里同一主机出现过的写法：" + " | ".join(f[:120] for f in forms)
    return "；留痕里没有该主机的任何 URL——不能凭记忆写，须搜到后再录"


def record_sources(store_path: Path, entries: list, evidence_path: Path,
                   nodes: list[str]) -> dict:
    """批次入库即验：逐条校验后追加进 store。返回 {accepted, skipped, rejected, unmapped}。

    rejected 每项 {index, name, url, reason}——单条被拒不阻断批次。
    幂等：裸 URL 精确相等才跳过并计数（真正去重由收尾的域名+名称联合完成）。
    node 字段由脚本按 category_path 对 nodes 的最长后缀匹配推导（模型零新增职责）。
    source_type 入库即归一到封闭词表（canonicalize_source_type）——store 内永远
    只存标准词；原始词留痕在 source_type_raw（审计零损失），unmapped 列出
    落「其他」的表外词（告警不阻断，可补别名收敛）。
    """
    accepted: list[dict] = []
    skipped = 0
    rejected: list[dict] = []
    reject_events: list[dict] = []
    unmapped: set[str] = set()
    evidence_ok = evidence_path.exists()
    evidence = evidence_path.read_text(encoding="utf-8", errors="replace") if evidence_ok else ""
    existing_urls = _existing_urls(store_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # 每批一个时间戳（脚本盖，模型无时钟）

    def _reject(code, i, e, name, url, **fmt):      # 拒收一处出口：返回值 + 失败事件
        node = leaf_node(str(e.get("category_path") or ""), nodes) if isinstance(e, dict) else ""
        rejected.append(reject_entry(code, index=i, name=name, url=url, **fmt))
        reject_events.append({"ts": ts, "code": code, "actor": _FAILURES[code][2],
                              "node": node or "", "name": name, "url": url})

    for i, e in enumerate(entries):
        name = str(e.get("name") or "") if isinstance(e, dict) else ""
        url = str(e.get("url") or "") if isinstance(e, dict) else ""
        if not isinstance(e, dict) or not name or not url:
            _reject("MISSING_FIELD", i, e, name, url)
            continue
        if is_garbage_domain(domain_of(url)):
            _reject("GARBAGE_DOMAIN", i, e, name, url)
            continue
        if nodes and leaf_node(str(e.get("category_path") or ""), nodes) is None:
            _reject("NODE_NOT_DECLARED", i, e, name, url)
            continue
        if not evidence_ok:
            _reject("EVIDENCE_LOG_MISSING", i, e, name, url, path=evidence_path)
            continue
        kept, rej = check_grounded([e], evidence)
        if rej:
            _reject("URL_NOT_IN_EVIDENCE", i, e, name, url,
                 hint=_grounded_hint(url, evidence))
            continue
        if url in existing_urls:
            skipped += 1
            continue
        source_type, raw_type, unmapped_word = normalize_entry_type(e.get("source_type"))
        if unmapped_word:
            unmapped.add(unmapped_word)
        accepted.append({
            "type": "source",
            "node": leaf_node(str(e.get("category_path") or ""), nodes) or "",
            "name": name,
            "category_path": e.get("category_path", ""),
            "source_type": source_type,
            "source_type_raw": raw_type,
            "granularity": normalize_granularity(e.get("granularity")),
            "url": url,
            "description": e.get("description", ""),
            "reason": e.get("reason", ""),
            "ts": ts,
        })
        existing_urls.add(url)
    append_records(store_path, accepted)
    if reject_events:
        append_records(store_path.parent / "rejects.jsonl", reject_events)
    if accepted:
        # 追加改变了文件 mtime——用新 mtime 更新缓存，保持幂等集合与磁盘一致
        _url_cache[str(store_path)] = (store_path.stat().st_mtime_ns, existing_urls)
    return {"accepted": len(accepted), "skipped": skipped, "rejected": rejected,
            "unmapped": sorted(unmapped)}


def record_search(store_path: Path, entries: list) -> int:
    """搜索日志批量追加；非 dict 条目跳过。返回追加数。

    zero_reason 可选（2026-09-09 收尾护栏配套）：增量/扩量搜索提取为 0 时的
    拒收理由（已收/垃圾域/无主题边界等）——收尾折叠时校验存在性与域名
    证据一致性，缺失/矛盾拒绝收尾；补录 = 重传同 phase+node+query 行（末次覆盖）。
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        rows.append({
            "type": "search",
            "phase": str(e.get("phase") or ""),
            "node": str(e.get("node") or ""),
            "query": str(e.get("query") or ""),
            "results": e.get("results", ""),
            "extracted": e.get("extracted", ""),
            "zero_reason": str(e.get("zero_reason") or ""),
            "ts": ts,
        })
    return append_records(store_path, rows)


def record_knowledge(store_path: Path, entries: list, evidence_path: Path) -> dict:
    """清单核对结果批量入库（2026-09-08 架构修订：清单核对结果不再"会话暂存 +
    阶段 6 一次性转写 manifest"——214051 实证漏写 60 个 verified 字段，65 条
    JSON 手工转写是必然出错的事故类型，且与压缩丢失风险同源；改为随验证过程
    分批落库，与增量条目/搜索日志同一机制）。

    每项 {name, node, verified, category_path?, source_type?, granularity?,
    url?, description?, reason?, note?}：
    - name/node 必填；verified=true 必带 url 且过证据链校验（当场拒绝+原因，
      可修正重传）；verified=false 带 note（"疑似无效机构"/"未找到官方入口"），
      不带 URL、不查证据
    - 同名重录 = 状态更新（append-only，收尾折叠按 name 取末次记录）
    - source_type 入库即归一（标准词 + raw 留痕），表外词进 unmapped
    返回 {accepted, rejected, unmapped}。
    """
    accepted: list[dict] = []
    rejected: list[dict] = []
    reject_events: list[dict] = []
    unmapped: set[str] = set()
    evidence_ok = evidence_path.exists()
    evidence = evidence_path.read_text(encoding="utf-8", errors="replace") if evidence_ok else ""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 名称对账前移（2026-09-17）：manifest 与 store 同在运行目录，收尾折叠按名精确
    # 对账——名称写错原本要等 finalize 才被点名（115926 实证：模型改写过名称，收尾
    # 漏录 9 项、整轮返工）。manifest 不存在时（阶段 1 的行前清单核对）不校验。
    _m = read_manifest(store_path.parent)
    declared: Optional[set] = declared_names_of(_m) if _m is not None else None

    def _reject(code, i, name, node="", url="", **fmt):   # 拒收一处出口：返回值 + 失败事件
        rejected.append(reject_entry(code, index=i, name=name, **fmt))
        reject_events.append({"ts": ts, "code": code, "actor": _FAILURES[code][2],
                              "node": node, "name": name, "url": url})

    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            _reject("MISSING_NAME_NODE", i, "")
            continue
        name = str(e.get("name") or "")
        node = str(e.get("node") or "")
        url_seen = str(e.get("url") or "")   # 事件用：条目自带的 url（verified 与否都可能带）
        if not name or not node:
            _reject("MISSING_NAME_NODE", i, name, node, url_seen)
            continue
        if declared is not None and name not in declared:
            near = difflib.get_close_matches(name, declared, n=1)
            _reject("NAME_NOT_DECLARED", i, name, node, url_seen,
                 near=f"，最相近的是「{near[0]}」" if near else "")
            continue
        verified = bool(e.get("verified"))
        url = str(e.get("url") or "") if verified else ""
        if verified and not url:
            _reject("VERIFIED_WITHOUT_URL", i, name, node)
            continue
        if verified and is_garbage_domain(domain_of(url)):
            _reject("GARBAGE_DOMAIN", i, name, node, url)
            continue
        if verified and not evidence_ok:
            _reject("EVIDENCE_LOG_MISSING", i, name, node, url, path=evidence_path)
            continue
        if verified:
            kept, rej = check_grounded([e], evidence)
            if rej:
                _reject("URL_NOT_IN_EVIDENCE", i, name, node, url,
                     hint=_grounded_hint(url, evidence))
                continue
        source_type, raw_type, unmapped_word = normalize_entry_type(e.get("source_type"))
        if unmapped_word:
            unmapped.add(unmapped_word)
        accepted.append({
            "type": "knowledge",
            "node": node,
            "name": name,
            "verified": verified,
            "category_path": e.get("category_path", ""),
            "source_type": source_type,
            "source_type_raw": raw_type,
            "granularity": normalize_granularity(e.get("granularity")),
            "url": url,
            "description": e.get("description", ""),
            "reason": e.get("reason", ""),
            "note": e.get("note", ""),
            "ts": ts,
        })
    append_records(store_path, accepted)
    if reject_events:
        append_records(store_path.parent / "rejects.jsonl", reject_events)
    return {"accepted": len(accepted), "rejected": rejected,
            "unmapped": sorted(unmapped)}


def coverage(store_path: Path, nodes: list[str]) -> list[dict]:
    """每节点 已收 vs 提取 的只读计数（missing = max(0, 提取-已收)）+ 体裁分布。

    missing 是粗略缺口信号（2026-09-02 评审）：提取数含去重前重复与跨节点
    顺路发现，已收数是幂等去重后的入库数，两口径天然有差——小额 missing 不
    视为遗漏，接近一整批提取量才值得怀疑漏调 record_sources。

    types 为每节点体裁分布（2026-09-18 补，docs/07 §五.3）：薄弱判定的"体裁/
    来源维度单一"原由模型按"刚提取的内容"判断，搜索下放子代理后主流程不再
    经过条目正文，改由本函数从 store 算分布供其判定；已收数为 0 的节点给空
    分布——"没有"与"单一"要能分开。角度级状态不可恢复（角度多样性脚本校验
    2026-08-31 裁决不做，该缺口以散文层治理维持，见 docs/02 讨论日志）。

    pending 为未核对的声明清单项名（2026-09-21 补）：阶段 5 与阶段 6 要判"两栏
    清单项全部核对完成"，此前只能自己去 store.jsonl 里 grep（180104 轮实测 9 次）。
    判定 = manifest 声明的名字 − store 里已有 knowledge 记录的名字——verified
    真假都算核对完成（定案后一律落 record_knowledge）。manifest 不存在时给空表。
    """
    records, _ = load_store(store_path) if store_path.exists() else ([], 0)
    declared: dict[str, list[str]] = {}
    try:
        m = read_manifest(store_path.parent)
    except (ValueError, OSError):
        m = None
    if m is not None:
        for item in declared_items(m):
            if item.get("name"):
                declared.setdefault(str(item.get("node") or ""), []).append(str(item["name"]))
    closed = {str(r.get("name") or "") for r in records
              if r.get("type") == "knowledge"}
    per: dict[str, dict] = {n: {"recorded": 0, "extracted": 0, "types": {}}
                            for n in nodes}
    extra: dict[str, dict] = {}

    def bucket(node: str) -> dict:
        if node in per:
            return per[node]
        return extra.setdefault(node, {"recorded": 0, "extracted": 0, "types": {}})

    for r in records:
        node = str(r.get("node") or "")
        if not node:
            continue
        if r.get("type") == "source":
            b = bucket(node)
            b["recorded"] += 1
            t = str(r.get("source_type") or "其他")
            b["types"][t] = b["types"].get(t, 0) + 1
        elif r.get("type") == "search":
            try:
                bucket(node)["extracted"] += int(r.get("extracted") or 0)
            except (TypeError, ValueError):
                pass
    result = []
    for node, c in {**per, **extra}.items():
        result.append({"node": node, "recorded": c["recorded"],
                       "extracted": c["extracted"],
                       "missing": max(0, c["extracted"] - c["recorded"]),
                       "types": c["types"],
                       "pending": [n for n in declared.get(node, []) if n not in closed]})
    return result
