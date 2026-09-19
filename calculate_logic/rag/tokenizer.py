"""共享 jieba 分词模块 —— 入库端和检索端共用，保证 tsv 存储与查询分词完全一致。

入库端：tokenize_for_tsv(text) → 空格拼接 → to_tsvector('simple', 拼接文本)
检索端：tokenize_for_tsquery(text) → ' | ' 拼接 → to_tsquery('simple', 拼接文本)
"""

import re
import jieba

# ==================== jieba 自定义词典（HR 领域复合词） ====================
_JIEBA_CUSTOM_WORDS = [
    "陪产假", "产假", "年假", "婚假", "育儿假", "哺乳假", "病假", "事假",
    "生育津贴", "社保停保", "五险一金", "弹性福利", "加保", "停保",
    "考勤", "差旅费", "报销", "离职", "入职", "试用期", "加班",
    "绩效", "晋升", "薪酬", "个税", "专项附加扣除",
    "探亲假", "工伤", "医疗期", "停工留薪", "经济补偿金",
    "养老金储蓄", "档案调出", "离职证明",
]

for _w in _JIEBA_CUSTOM_WORDS:
    jieba.add_word(_w)

# ==================== 停用词 ====================
_STOPWORDS = frozenset({
    "的", "了", "在", "是", "我", "你", "他", "她", "它", "们", "和", "与", "或",
    "怎么", "如何", "什么", "为什么", "哪里", "哪个", "怎样", "多少", "有没有",
    "可以", "能", "要", "想", "需要", "应该", "吗", "呢", "吧", "啊", "哦", "嗯",
    "请问", "咨询", "了解", "知道", "告诉", "一下", "这个", "那个", "这些", "那些",
    "a", "an", "the", "is", "are", "was", "were", "how", "what", "why", "where",
    "to", "do", "does", "did", "can", "could", "would", "should", "will",
})

# 有意义的单字白名单
_SINGLE_CHAR_WHITELIST = frozenset({
    "假", "休", "薪", "税", "险", "班", "贴",
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
        if re.fullmatch(r"[\d\s\W]+", tok):
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
