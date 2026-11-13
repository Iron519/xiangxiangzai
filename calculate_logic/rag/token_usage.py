"""
LLM Token 用量日志工具 —— 统一提取 LangChain response 中的 model 名称和 token 消耗，
以 INFO 级别写入日志，格式：[TokenUsage] model=xxx prompt=N completion=N total=N

支持两种 response 来源：
1. ChatOpenAI.ainvoke() / .invoke() 返回的 AIMessage（非流式）
2. ChatOpenAI.astream() 累加后的 AIMessage（流式末尾）

LangChain 的 token usage 存放在两个位置（不同版本/模式不同）：
- response_metadata.token_usage: {prompt_tokens, completion_tokens, total_tokens}
- usage_metadata: {input_tokens, output_tokens, total_tokens}
"""

import logging

logger = logging.getLogger("TokenUsage")


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
