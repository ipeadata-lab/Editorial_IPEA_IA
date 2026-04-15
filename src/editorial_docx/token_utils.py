from __future__ import annotations

from dataclasses import dataclass

from .config import DEFAULT_REVIEW_MAX_BATCH_CHUNKS

try:
    import tiktoken
except Exception:  # pragma: no cover - fallback when optional dependency is absent
    tiktoken = None


def _get_encoding():
    if tiktoken is None:
        return None
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None


def count_tokens(text: str) -> int:
    encoding = _get_encoding()
    if encoding is None:
        return max(1, len(text or "") // 4)
    return len(encoding.encode(text or ""))


def truncate_text(text: str, max_tokens: int) -> str:
    encoding = _get_encoding()
    if encoding is None:
        return (text or "")[: max_tokens * 4]
    tokens = encoding.encode(text or "")[:max_tokens]
    return encoding.decode(tokens)


@dataclass(slots=True)
class TokenChunkConfig:
    max_tokens: int = 3200
    overlap_tokens: int = 240
    max_items: int = DEFAULT_REVIEW_MAX_BATCH_CHUNKS


def chunk_index_windows(
    items: list[tuple[int, str]],
    *,
    config: TokenChunkConfig | None = None,
) -> list[list[int]]:
    """Agrupa índices com orçamento de tokens e overlap entre lotes consecutivos."""
    if not items:
        return []

    cfg = config or TokenChunkConfig()
    batches: list[list[int]] = []
    current_indexes: list[int] = []
    current_token_counts: list[int] = []
    current_total = 0

    def flush_current() -> list[tuple[int, int]]:
        nonlocal current_indexes, current_token_counts, current_total
        snapshot = list(zip(current_indexes, current_token_counts))
        if current_indexes:
            batches.append(current_indexes[:])
        carry: list[tuple[int, int]] = []
        carry_tokens = 0
        for index, token_count in reversed(snapshot):
            if carry and carry_tokens + token_count > cfg.overlap_tokens:
                break
            carry.append((index, token_count))
            carry_tokens += token_count
        carry.reverse()
        current_indexes = [index for index, _ in carry]
        current_token_counts = [token_count for _, token_count in carry]
        current_total = carry_tokens
        return snapshot

    for index, text in items:
        token_count = max(1, count_tokens(text))
        if token_count > cfg.max_tokens:
            token_count = cfg.max_tokens

        if current_indexes and (
            len(current_indexes) >= cfg.max_items or current_total + token_count > cfg.max_tokens
        ):
            flush_current()

        current_indexes.append(index)
        current_token_counts.append(token_count)
        current_total += token_count

    if current_indexes:
        batches.append(current_indexes[:])

    deduped: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()
    for batch in batches:
        key = tuple(batch)
        if not batch or key in seen:
            continue
        seen.add(key)
        deduped.append(batch)
    return deduped


__all__ = ["TokenChunkConfig", "chunk_index_windows", "count_tokens", "truncate_text"]
