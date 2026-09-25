# embedding_calibration.py
"""
Embedding-model calibration for the memory relevance threshold.

Cosine-similarity scales differ per embedding model: the same
memory/query pairs score ~0.65-0.75 on nomic-embed-text but only
~0.18-0.28 on text-embedding-3-large (measured, ST5-S16). A single
fixed threshold therefore silently disables long-term memory retrieval
when the user switches embedding models (ST6 §7-4).

This module embeds a small fixed corpus of memory-like Japanese texts
with the selected model, measures the similarity distributions of
labeled relevant vs. irrelevant pairs, and derives a per-model
threshold. The settings UI runs this when the embedding model is saved;
the result is persisted via `backend.shared.api_settings` and resolved
at search time by `get_memory_relevance_threshold()`.

Provider dispatch (ollama sequential / openai+xai batch / other API
sequential) intentionally mirrors `MemoryManager._get_embeddings_batch`;
a per-character MemoryManager (SQLite store etc.) is too heavy to build
just to borrow that method.
"""

import logging
from typing import Any, Dict, List

import numpy as np

logger = logging.getLogger(__name__)

# Bump when the corpus or labels change (stored alongside each persisted
# threshold so a stale calibration can be recognized).
CORPUS_VERSION = 1

# Memory-like anchor texts. Entries 0-2 are the exact long-term memories
# used for the S16 real-embedding measurement (tests/baseline/scenarios
# longctx_01), keeping this calibration anchored to real measured data.
_CALIBRATION_MEMORIES: List[str] = [
    "ユーザーは和食、特に煮物が好きで、昆布とかつおの合わせだしを使う。",   # 0
    "ユーザーは和食器を集めている。",                                       # 1
    "ユーザーはお酒を普段あまり飲まない。",                                 # 2
    "ユーザーは猫を一匹飼っていて、名前はモモという。",                     # 3
    "ユーザーはホラー映画が苦手で、コメディ映画をよく観る。",               # 4
    "ユーザーの仕事はプログラマーで、在宅勤務が多い。",                     # 5
    "ユーザーは腰痛持ちで、長時間座ると調子が悪くなる。",                   # 6
    "ユーザーは冬より夏が好きで、海に行くのが楽しみ。",                     # 7
    "先月、ユーザーと花火大会の話で盛り上がった。",                         # 8
    "ユーザーには妹が一人いて、東京に住んでいる。",                         # 9
]

# User-utterance style queries (what get_memories_for_prompt receives).
_CALIBRATION_QUERIES: List[str] = [
    "これまでの話を踏まえて、今度の週末に作る一品を提案してくれる？",       # 0 (S16)
    "何か映画のおすすめある？今夜観たいんだけど。",                         # 1
    "うちの猫が最近ずっと寝てばかりなんだよね。",                           # 2
    "今日は一日中コード書いてたら腰が痛くなっちゃった。",                   # 3
    "夏になったらどこか遊びに行きたいなあ。",                               # 4
]

# query index -> memory indices that a working retrieval SHOULD surface.
# Every other (query, memory) pair is treated as irrelevant. Memory 9 is
# relevant to no query (pure noise anchor).
_RELEVANT_PAIRS: Dict[int, frozenset] = {
    0: frozenset({0, 1, 2}),
    1: frozenset({4}),
    2: frozenset({3}),
    3: frozenset({5, 6}),
    4: frozenset({7, 8}),
}

# Derived threshold is clamped to this sane range.
THRESHOLD_MIN = 0.02
THRESHOLD_MAX = 0.9

# The threshold is a noise gate, not a classifier: similarity ranking orders
# the results and the token budget caps volume, so a false positive costs a
# few prompt tokens while a false negative is the "long-term memory silently
# dead" failure mode this calibration exists to prevent. Real measurement
# (2026-07-04) showed the relevant/irrelevant distributions OVERLAP for both
# current models (same-domain persona sentences are never fully separable),
# so the gate is derived from the relevant distribution alone: keep every
# labeled-relevant pair, with a spread-proportional safety margin below the
# weakest one. The irrelevant side is measured and reported (noise_pass) for
# transparency only.
_MARGIN_RATIO = 0.15

# Per-call timeout. No retry loop: calibration is interactive (save button)
# and must fail fast rather than keep the UI spinning through retries.
_EMBED_TIMEOUT = 30.0


class CalibrationError(Exception):
    """Raised when calibration embeddings cannot be generated."""


def _embed_texts(provider: str, model_name: str, texts: List[str]) -> List[np.ndarray]:
    """Embed texts with the given model. Fails fast on the first error.

    openai/xai: single batch call. Other providers: sequential calls.
    """
    if provider in ("openai", "xai"):
        from backend.llm.api_integration import call_api_embedding_batch
        result = call_api_embedding_batch(
            provider=provider, model=model_name, texts=texts, timeout=_EMBED_TIMEOUT
        )
        if not result.get("success"):
            raise CalibrationError(result.get("error", "batch embedding failed"))
        return [np.array(e, dtype=np.float32) for e in result["embeddings"]]

    embeddings = []
    for text in texts:
        if provider == "ollama":
            from backend.llm.ollama_integration import call_ollama_embedding
            result = call_ollama_embedding(
                model=model_name, text=text, timeout=_EMBED_TIMEOUT
            )
        else:
            from backend.llm.api_integration import call_api_embedding
            result = call_api_embedding(
                provider=provider, model=model_name, text=text, timeout=_EMBED_TIMEOUT
            )
        if not result.get("success"):
            raise CalibrationError(result.get("error", "embedding failed"))
        embeddings.append(np.array(result["embedding"], dtype=np.float32))
    return embeddings


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    if norm == 0.0:
        return 0.0
    return float(np.clip(float(np.dot(a, b)) / norm, -1.0, 1.0))


def _derive_threshold(rel: List[float], irr: List[float]) -> Dict[str, Any]:
    """Derive a noise-gate threshold from relevant/irrelevant similarity samples.

    threshold = rel_min - _MARGIN_RATIO * (rel_max - rel_min), clamped.
    Every labeled-relevant pair passes; noise_pass reports the fraction of
    irrelevant pairs that also pass (the model's discriminative power for
    this domain — informational, not part of the derivation).
    """
    rel_min, rel_max = min(rel), max(rel)
    irr_min, irr_max = min(irr), max(irr)

    margin = _MARGIN_RATIO * (rel_max - rel_min)
    threshold = min(max(rel_min - margin, THRESHOLD_MIN), THRESHOLD_MAX)
    noise_pass = sum(1 for s in irr if s >= threshold) / len(irr)

    return {
        "threshold": round(float(threshold), 4),
        "noise_pass": round(noise_pass, 3),
        "rel_min": round(rel_min, 4),
        "rel_max": round(rel_max, 4),
        "irr_min": round(irr_min, 4),
        "irr_max": round(irr_max, 4),
    }


def calibrate_embedding_model(provider: str, model_name: str) -> Dict[str, Any]:
    """Measure the model's similarity scale and derive a relevance threshold.

    Returns:
        On success: {"success": True, "threshold": float, "noise_pass": float,
                     "rel_min"/"rel_max"/"irr_min"/"irr_max": float,
                     "pairs_relevant"/"pairs_irrelevant": int,
                     "corpus_version": int, "provider": str, "model": str}
        On failure: {"success": False, "error": str} (caller falls back to
                     the seeded/default threshold; retrying = re-save).
    """
    try:
        mem_embs = _embed_texts(provider, model_name, _CALIBRATION_MEMORIES)
        query_embs = _embed_texts(provider, model_name, _CALIBRATION_QUERIES)

        rel, irr = [], []
        for qi, q_emb in enumerate(query_embs):
            relevant_set = _RELEVANT_PAIRS.get(qi, frozenset())
            for mi, m_emb in enumerate(mem_embs):
                sim = _cosine(q_emb, m_emb)
                (rel if mi in relevant_set else irr).append(sim)

        derived = _derive_threshold(rel, irr)

        result = {
            "success": True,
            "pairs_relevant": len(rel),
            "pairs_irrelevant": len(irr),
            "corpus_version": CORPUS_VERSION,
            "provider": provider,
            "model": model_name,
        }
        result.update(derived)
        logger.info(
            f"[Calibration] {provider}::{model_name}: threshold="
            f"{result['threshold']} (rel {result['rel_min']}-{result['rel_max']} / "
            f"irr {result['irr_min']}-{result['irr_max']}, "
            f"noise_pass={result['noise_pass']})"
        )
        return result

    except CalibrationError as e:
        logger.warning(f"[Calibration] {provider}::{model_name} failed: {e}")
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[Calibration] {provider}::{model_name} unexpected error: {e}")
        return {"success": False, "error": str(e)}


def calibrate_and_save(encoded_value: str) -> Dict[str, Any]:
    """Calibrate the model given as "provider::model" and persist the result.

    On failure nothing is persisted (resolution falls back to seed/default)
    and the model choice itself stays saved — memory search keeps working.
    """
    from backend.shared.api_settings import decode_model_value, save_embedding_threshold

    provider, model_name = decode_model_value(encoded_value)
    result = calibrate_embedding_model(provider, model_name)
    if result.get("success"):
        save_embedding_threshold(f"{provider}::{model_name}", result)
    return result
