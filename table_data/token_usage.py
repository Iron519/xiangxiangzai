"""
LLM Token 用量日志工具 —— 统一提取 LangChain response 中的 model 名称和 token 消耗，
以 INFO 级别写入日志，格式：[TokenUsage] model=xxx prompt=N completion=N total=N

支持两种 response 来源：
1. ChatOpenAI.ainvoke() / .invoke() 返回的 AIMessage（非流式）
2. ChatOpenAI.astream() 累加后的 AIMessage（流式末尾）

LangChain 的 token usage 存放在两个位置（不同版本/模式不同）：
- response_metadata.token_usage: {prompt_tokens, completion_tokens, total_tokens}
- usage_metadata: {input_tokens, output_tokens, total_tokens}

 additionally provides a per-request token accumulator so that
judgment-node calls (FAST_MODEL) and the main generation node (TEXT_MODEL)
can both contribute to the persisted token total.
"""

import logging
import contextvars

logger = logging.getLogger("TokenUsage")

# per-request 累加器，用 contextvars 隔离并发请求
_accumulator: contextvars.ContextVar = contextvars.ContextVar("_token_acc", default=None)


class TokenAccumulator:
    """单轮对话的 token 累加器，按模型名分别累计。"""

    def __init__(self):
        self.models: dict[str, dict] = {}  # {model_name: {prompt, completion, total}}

    def add(self, model: str, prompt: int, completion: int, total: int):
        entry = self.models.setdefault(model, {"prompt": 0, "completion": 0, "total": 0})
        if prompt: entry["prompt"] += prompt
        if completion: entry["completion"] += completion
        if total: entry["total"] += total

    @property
    def total_tokens(self) -> int:
        return sum(m["total"] for m in self.models.values())

    @property
    def prompt_tokens(self) -> int:
        return sum(m["prompt"] for m in self.models.values())

    @property
    def completion_tokens(self) -> int:
        return sum(m["completion"] for m in self.models.values())

    @property
    def primary_model(self) -> str:
        """返回 token 消耗最多的模型名。"""
        if not self.models:
            return ""
        return max(self.models, key=lambda k: self.models[k]["total"])


def init_accumulator() -> TokenAccumulator:
    """在每轮对话开始时调用，创建并绑定到当前 context。"""
    acc = TokenAccumulator()
    _accumulator.set(acc)
    return acc


def get_accumulator() -> TokenAccumulator | None:
    return _accumulator.get()



def log_token_usage(node: str, response, default_model: str = ""):
    """从 LangChain AIMessage 提取 model + token 用量并记录 INFO 日志。

    Args:
        node: 调用节点名称（如 "node_generate", "classify_turn", "query_rewrite"）
        response: LangChain ainvoke/invoke 返回的 AIMessage，或 astream 累加后的 AIMessage
        default_model: 兜底模型名（当 response 中无 model 信息时使用）
    """
    model = default_model
    prompt_tokens = None
    completion_tokens = None
    total_tokens = None

    if response is not None:
        # 提取 model 名称
        response_metadata = getattr(response, "response_metadata", None) or {}
        model = response_metadata.get("model_name") or response_metadata.get("model") or default_model

        # 提取 token 用量（优先 response_metadata.token_usage，其次 usage_metadata）
        token_usage = response_metadata.get("token_usage", {}) or {}
        prompt_tokens = token_usage.get("prompt_tokens")
        completion_tokens = token_usage.get("completion_tokens")
        total_tokens = token_usage.get("total_tokens")

        if total_tokens is None:
            um = getattr(response, "usage_metadata", None) or {}
            prompt_tokens = um.get("input_tokens")
            completion_tokens = um.get("output_tokens")
            total_tokens = um.get("total_tokens")

    logger.info(
        f"[{node}] model={model} "
        f"prompt={prompt_tokens} completion={completion_tokens} total={total_tokens}"
    )

    # 累加到 per-request accumulator
    acc = get_accumulator()
    if acc is not None and total_tokens:
        acc.add(model, prompt_tokens or 0, completion_tokens or 0, total_tokens or 0)
