"""
本地 BGE embedding + reranker 后端（sentence-transformers）。

接口兼容 CustomEmbeddingClient / CustomRerankClient，通过 EMBEDDING_BACKEND / RERANK_BACKEND
环境变量切换。Docker 容器内使用 CPU 模式（无 MPS/CUDA）。

依赖：sentence-transformers + torch（CPU 版），仅 local 后端需要安装。
模型需提前下载到本地路径（通过 modelscope 或 huggingface 镜像）。
"""

import os
import asyncio
import logging
from typing import List, Dict, Any

import numpy as np

logger = logging.getLogger(__name__)

# ── 环境变量 ──
BGE_EMBEDDING_MODEL = os.getenv("BGE_EMBEDDING_MODEL", "BAAI/bge-m3")
BGE_RERANKER_MODEL = os.getenv("BGE_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
BGE_DEVICE = os.getenv("BGE_DEVICE", "cpu")  # Docker 内用 cpu
BGE_MAX_TEXT_LENGTH = 2048  # 与 CustomEmbeddingClient 一致

# 本地模型缓存路径（huggingface_hub 缓存结构）
_HF_CACHE = os.path.expanduser("~/.cache/huggingface/hub")


def _resolve_model_path(model_name: str) -> str:
    """将 HuggingFace 模型名解析为本地缓存路径。

    优先使用本地缓存路径，避免 huggingface_hub 远程检查（容器内可能无法访问 HF）。
    """
    # 模型名格式: BAAI/bge-m3 → 本地路径: models--BAAI--bge-m3/snapshots/main
    folder = model_name.replace("/", "--")
    local_path = os.path.join(_HF_CACHE, f"models--{folder}", "snapshots", "main")
    if os.path.isdir(local_path) and os.path.exists(os.path.join(local_path, "config.json")):
        return local_path
    # 回退到模型名（让 sentence-transformers 自己解析）
    return model_name


class LocalBGEEmbedding:
    """本地 BGE embedding 后端，接口兼容 CustomEmbeddingClient。

    使用 sentence-transformers 加载 bge-m3，懒加载模式。
    """

    def __init__(self, model_name: str = "", device: str = "cpu") -> None:
        self.model_name = model_name or BGE_EMBEDDING_MODEL
        self.device = device or BGE_DEVICE
        self._model = None
        logger.info(f"[BGE Embedding] 初始化: model={self.model_name}, device={self.device}")

    def _ensure_model(self):
        """懒加载模型（首次调用时加载，约 2GB 内存）。"""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                raise RuntimeError(
                    "sentence-transformers 未安装。请执行: pip install sentence-transformers torch，"
                    "或将 EMBEDDING_BACKEND=api 使用云端 API。"
                )
            model_path = _resolve_model_path(self.model_name)
            logger.info(f"[BGE Embedding] 正在加载模型 {model_path} ...")
            self._model = SentenceTransformer(model_path, device=self.device)
            logger.info(f"[BGE Embedding] 模型已加载: {self.model_name} (dim={self._model.get_sentence_embedding_dimension()})")

    async def text_embedding(self, texts: List[str]) -> List[np.ndarray]:
        """向量化，返回 np.ndarray 列表（float32），与 CustomEmbeddingClient.text_embedding 一致。"""
        clean = [t[:BGE_MAX_TEXT_LENGTH] for t in texts if t and t.strip()]
        if not clean:
            return []
        try:
            self._ensure_model()
            # sentence-transformers encode 是同步阻塞的，用 run_in_executor
            loop = asyncio.get_event_loop()
            embeddings = await loop.run_in_executor(
                None,
                lambda: self._model.encode(clean, convert_to_numpy=True),
            )
            return [np.array(v, dtype=np.float32) for v in embeddings]
        except Exception as e:
            logger.error(f"[BGE Embedding] 向量化失败: {e}")
            return []

    async def close(self) -> None:
        """释放模型引用。"""
        if self._model is not None:
            self._model = None
            logger.info("[BGE Embedding] 模型已卸载")


class LocalBGEReranker:
    """本地 BGE reranker 后端，接口兼容 CustomRerankClient。

    使用 sentence-transformers CrossEncoder 加载 bge-reranker-v2-m3，懒加载模式。
    """

    def __init__(self, model_name: str = "", device: str = "cpu") -> None:
        self.model_name = model_name or BGE_RERANKER_MODEL
        self.device = device or BGE_DEVICE
        self._model = None
        logger.info(f"[BGE Reranker] 初始化: model={self.model_name}, device={self.device}")

    def _ensure_model(self):
        """懒加载模型（首次调用时加载，约 1GB 内存）。"""
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError:
                raise RuntimeError(
                    "sentence-transformers 未安装。请执行: pip install sentence-transformers torch，"
                    "或将 RERANK_BACKEND=api 使用云端 API。"
                )
            model_path = _resolve_model_path(self.model_name)
            logger.info(f"[BGE Reranker] 正在加载模型 {model_path} ...")
            self._model = CrossEncoder(model_path, device=self.device)
            logger.info(f"[BGE Reranker] 模型已加载: {self.model_name}")

    async def arerank(self, query: str, texts: List[str]) -> Dict[int, float]:
        """异步 rerank，返回 {index: score} 映射，与 CustomRerankClient.arerank 一致。"""
        if not texts:
            return {}
        try:
            self._ensure_model()
            pairs = [[query, t] for t in texts]
            # CrossEncoder.predict 是同步阻塞的，用 run_in_executor
            loop = asyncio.get_event_loop()
            scores = await loop.run_in_executor(
                None,
                lambda: self._model.predict(pairs),
            )
            # scores 是 numpy array，归一化到 0-1（sigmoid）
            import numpy as np
            scores = 1.0 / (1.0 + np.exp(-np.array(scores, dtype=float)))  # sigmoid
            return {i: float(s) for i, s in enumerate(scores)}
        except Exception as e:
            logger.error(f"[BGE Reranker] rerank 失败: {e}")
            return {}

    async def close(self) -> None:
        """释放模型引用。"""
        if self._model is not None:
            self._model = None
            logger.info("[BGE Reranker] 模型已卸载")
