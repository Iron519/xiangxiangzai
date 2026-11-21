"""共享 jieba 分词模块 —— 入库端和检索端共用，保证 tsv 存储与查询分词完全一致。

入库端：tokenize_for_tsv(text) → 空格拼接 → to_tsvector('simple', 拼接文本)
检索端：tokenize_for_tsquery(text) → ' | ' 拼接 → to_tsquery('simple', 拼接文本)

使用 jieba 默认词典分词，内置通用停用词和单字白名单过滤。
"""

import re
import logging
import jieba

logger = logging.getLogger("Tokenizer")

# ==================== 通用停用词（领域无关） ====================
_STOPWORDS = frozenset({
    "的", "了", "在", "是", "我", "你", "他", "她", "它", "们", "和", "与", "或",
    "怎么", "如何", "什么", "为什么", "哪里", "哪个", "怎样", "多少", "有没有",
    "可以", "能", "要", "想", "需要", "应该", "吗", "呢", "吧", "啊", "哦", "嗯",
    "请问", "咨询", "了解", "知道", "告诉", "一下", "这个", "那个", "这些", "那些",
    "a", "an", "the", "is", "are", "was", "were", "how", "what", "why", "where",
    "to", "do", "does", "did", "can", "could", "would", "should", "will",
})

# ==================== 通用单字白名单（领域无关高频有意义单字） ====================
_SINGLE_CHAR_WHITELIST = frozenset({
    "人", "事", "时", "地", "物", "法", "情", "理", "利", "权", "钱", "数",
    "量", "率", "期", "费", "金", "类", "种", "分", "总", "标", "准", "则",
})


def _filter_tokens(tokens: list[str]) -> list[str]:
    """过滤停用词、纯标点/数字、无意义单字。"""
    result = []
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if tok in _STOPWORDS:
            continue
        if re.fullmatch(r"[\s\W]+", tok):
            continue
        if len(tok) == 1 and tok not in _SINGLE_CHAR_WHITELIST:
            continue
        result.append(tok)
    return result


def tokenize_for_tsv(text: str) -> str:
    """入库用：jieba 分词 → 过滤 → 空格拼接，供 to_tsvector('simple', ...) 使用。

    simple 配置不二次分词，按空格切分，所以每个 token 保持原样存入 tsv。
    """
    if not text or not text.strip():
        return ""
    tokens = jieba.lcut(text.strip(), cut_all=False)
    filtered = _filter_tokens(tokens)
    return " ".join(filtered)


def tokenize_for_tsquery(text: str) -> str:
    """检索用：jieba 分词 → 过滤 → ' | ' 拼接，供 to_tsquery('simple', ...) 使用。

    返回空串表示无有效关键词，调用方应跳过关键词检索。
    """
    if not text or not text.strip():
        return ""
    tokens = jieba.lcut(text.strip(), cut_all=False)
    filtered = _filter_tokens(tokens)
    return " | ".join(filtered)
