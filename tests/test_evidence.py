"""evidence.py（1.0 复用模块）的边界匹配算法测试——实现规格第五章校验目标。

文件本身保留不改，测试钉住算法契约：候选 URL 必须作为完整 URL 出现在证据
留痕中（RFC 3986 字符集判定前后字符），`#` 通过、`?` 严格、截短不予通过。
"""
from evidence import check_grounded, strip_citation_anchors


def check(url: str, haystack: str) -> bool:
    """单 URL 证据校验的便捷入口。"""
    kept, _ = check_grounded([{"url": url}], haystack)
    return bool(kept)


def test_full_url_match_passes():
    haystack = '标题 "https://a.example/x" 结尾'
    assert check("https://a.example/x", haystack) is True


def test_prefix_of_longer_url_rejected():
    """截短：候选是更长 URL 的前缀（后邻字符属 URL 字符集）→ 拒绝。"""
    haystack = '{"url": "https://a.example/xyz"}'
    assert check("https://a.example/xy", haystack) is False


def test_bare_domain_rejected():
    """截短为仅域名本身：后邻 "/" 属 URL 字符集 → 拒绝。"""
    haystack = '{"url": "https://a.example/x"}'
    assert check("https://a.example", haystack) is False


def test_hash_fragment_exception_allowed():
    """`#` 后接 fragment 不改变资源主体 → 通过（实现规格第五章例外一）。"""
    haystack = '{"url": "https://a.example/x#3"}'
    assert check("https://a.example/x", haystack) is True


def test_question_mark_strict_rejected():
    """`?` 不豁免：query 改变内容 → 严格拒绝（实现规格第五章例外二）。"""
    haystack = '{"url": "https://a.example/x?y=1"}'
    assert check("https://a.example/x", haystack) is False


def test_url_not_in_evidence_rejected():
    assert check("https://a.example/x", "完全无关的文本") is False


def test_second_occurrence_boundary_ok_passes():
    """多个出现位置逐一判定：前一处是更长 URL 的前缀、后一处边界干净 → 通过。"""
    haystack = '{"url": "https://a.example/x/y"} 文本 https://a.example/x 结尾'
    assert check("https://a.example/x", haystack) is True


def test_before_boundary_url_char_rejected():
    """前邻字符属 URL 字符集（更长 URL 的片段）→ 拒绝。"""
    haystack = '{"url": "zzzhttps://a.example/x"}'
    assert check("https://a.example/x", haystack) is False


def test_empty_url_rejected():
    assert check("", "任意文本") is False


def test_snippet_text_also_evidence():
    """证据留痕含全部字段值：title/snippet 中的 URL 同样可作证据。"""
    haystack = '{"snippet": "参见 https://a.example/x 了解更多"}'
    assert check("https://a.example/x", haystack) is True


def test_strip_citation_anchors():
    """输出前处理：剥离尾部纯数字引用锚点，单词锚点保留（实现规格第五章）。"""
    assert strip_citation_anchors("https://a.example/x.pdf#3") == "https://a.example/x.pdf"
    assert strip_citation_anchors("https://a.example/x#3#1") == "https://a.example/x"
    assert strip_citation_anchors("https://a.example/x#content") == "https://a.example/x#content"
    assert strip_citation_anchors("https://a.example/x") == "https://a.example/x"
    assert strip_citation_anchors("https://a.example/x?y=1#3") == "https://a.example/x?y=1"
