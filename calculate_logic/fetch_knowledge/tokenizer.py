"""共享 jieba 分词模块 —— 入库端和检索端共用，保证 tsv 存储与查询分词完全一致。

入库端：tokenize_for_tsv(text) → 空格拼接 → to_tsvector('simple', 拼接文本)
检索端：tokenize_for_tsquery(text) → ' | ' 拼接 → to_tsquery('simple', 拼接文本)

自定义词典和停用词通过 .env 配置加载：
- JIEBA_CUSTOM_DICT_PATH：自定义词典文件路径（每行一个词，# 开头为注释）
- JIEBA_STOPWORDS_PATH：停用词文件路径（每行一个词，# 开头为注释）
- JIEBA_SINGLE_CHAR_WHITELIST_PATH：单字白名单文件路径（每行一个字）

未配置时使用内置通用默认词表。
"""

import os
import re
import logging
import jieba

logger = logging.getLogger("Tokenizer")

_PROJECT_ROOT = os.getenv("WORK_PATH", "")

# ==================== 通用停用词（领域无关） ====================
_DEFAULT_STOPWORDS = frozenset({
    "的", "了", "在", "是", "我", "你", "他", "她", "它", "们", "和", "与", "或",
    "怎么", "如何", "什么", "为什么", "哪里", "哪个", "怎样", "多少", "有没有",
    "可以", "能", "要", "想", "需要", "应该", "吗", "呢", "吧", "啊", "哦", "嗯",
    "请问", "咨询", "了解", "知道", "告诉", "一下", "这个", "那个", "这些", "那些",
    "a", "an", "the", "is", "are", "was", "were", "how", "what", "why", "where",
    "to", "do", "does", "did", "can", "could", "would", "should", "will",
})

# ==================== 通用单字白名单（领域无关高频有意义单字） ====================
_DEFAULT_SINGLE_CHAR_WHITELIST = frozenset({
    "人", "事", "时", "地", "物", "法", "情", "理", "利", "权", "钱", "数",
    "量", "率", "期", "费", "金", "类", "种", "分", "总", "标", "准", "则",
})


def _load_word_file(path: str) -> set[str]:
    """从文件加载词表，每行一个词，# 开头为注释，空白行跳过。"""
    words = set()
    if not path:
        return words
    if not os.path.isabs(path) and _PROJECT_ROOT:
        path = os.path.join(_PROJECT_ROOT, path)
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    words.add(line)
        logger.info(f"[Tokenizer] 从 {path} 加载了 {len(words)} 个词")
    except FileNotFoundError:
        logger.warning(f"[Tokenizer] 词表文件不存在: {path}")
    except Exception as e:
        logger.error(f"[Tokenizer] 加载词表失败 {path}: {e}")
    return words


def _load_stopwords() -> frozenset:
    """加载停用词：默认通用停用词 + 外部文件补充。"""
    stopwords = set(_DEFAULT_STOPWORDS)
    extra = _load_word_file(os.getenv("JIEBA_STOPWORDS_PATH", ""))
    stopwords |= extra
    return frozenset(stopwords)


def _load_single_char_whitelist() -> frozenset:
    """加载单字白名单：默认 + 外部文件补充。"""
    whitelist = set(_DEFAULT_SINGLE_CHAR_WHITELIST)
    extra = _load_word_file(os.getenv("JIEBA_SINGLE_CHAR_WHITELIST_PATH", ""))
    whitelist |= extra
    return frozenset(whitelist)


def _load_custom_dict() -> None:
    """加载 jieba 自定义词典：外部文件优先，未配置时不加载（依赖 jieba 默认词典）。"""
    dict_path = os.getenv("JIEBA_CUSTOM_DICT_PATH", "")
    if not dict_path:
        logger.info("[Tokenizer] 未配置 JIEBA_CUSTOM_DICT_PATH，使用 jieba 默认词典")
        return
    words = _load_word_file(dict_path)
    for w in words:
        jieba.add_word(w)
    logger.info(f"[Tokenizer] 已加载 {len(words)} 个自定义词典词")


# 初始化：加载自定义词典、停用词、单字白名单
_load_custom_dict()
_STOPWORDS = _load_stopwords()
_SINGLE_CHAR_WHITELIST = _load_single_char_whitelist()


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
