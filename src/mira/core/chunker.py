"""Token-aware chunking for large PRs."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mira.models import FileDiff, ReviewChunk

if TYPE_CHECKING:
    from mira.llm.base import LLMProviderProtocol


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


def _file_token_estimate(file_diff: FileDiff) -> int:
    """Estimate tokens for a file diff."""
    total = len(file_diff.path) // 4 + 20  # path + overhead
    for hunk in file_diff.hunks:
        total += _estimate_tokens(hunk.content)
    return total


def chunk_files(
    files: list[FileDiff],
    max_tokens: int,
    provider: LLMProviderProtocol | None = None,
) -> list[ReviewChunk]:
    """Split files into chunks that fit within token limits.

    Uses greedy first-fit-decreasing: sort files by estimated token count
    (largest first), then fit each into the first chunk with room.
    Oversized files are split only between complete hunks. An indivisible hunk
    exceeding the budget stays intact and is explicitly marked incomplete.
    """
    if not files:
        return []

    # Reserve tokens for system prompt and response
    prompt_overhead = 2000
    available = max_tokens - prompt_overhead

    # Compute estimates
    estimates: list[tuple[FileDiff, int]] = []
    for f in files:
        est = _tokens(f, provider)
        estimates.append((f, est))

    # Sort largest first
    estimates.sort(key=lambda x: x[1], reverse=True)

    chunks: list[ReviewChunk] = []

    for file_diff, est in estimates:
        # Handle oversized files
        if est > available:
            chunks.extend(_split_file(file_diff, available, provider))
            continue

        # Find first chunk with room
        placed = False
        for chunk in chunks:
            if chunk.token_estimate + est <= available:
                chunk.files.append(file_diff)
                chunk.token_estimate += est
                placed = True
                break

        if not placed:
            chunks.append(
                ReviewChunk(
                    files=[file_diff],
                    token_estimate=est,
                )
            )

    return chunks


def _tokens(file_diff: FileDiff, provider: LLMProviderProtocol | None) -> int:
    if provider is None:
        return _file_token_estimate(file_diff)
    return (
        provider.count_tokens("\n".join(h.content for h in file_diff.hunks))
        + len(file_diff.path) // 4
        + 20
    )


def _split_file(
    file_diff: FileDiff, available: int, provider: LLMProviderProtocol | None
) -> list[ReviewChunk]:
    """Preserve every hunk; never pretend an over-budget fragment was reviewed."""
    chunks = []
    pending = []

    def emit(hunks, incomplete=False):
        fragment = replace(file_diff, hunks=list(hunks))
        chunks.append(
            ReviewChunk(
                files=[fragment],
                token_estimate=_tokens(fragment, provider),
                incomplete_paths=[file_diff.path] if incomplete else [],
            )
        )

    for hunk in file_diff.hunks:
        trial = replace(file_diff, hunks=[*pending, hunk])
        if pending and _tokens(trial, provider) > available:
            emit(pending)
            pending = []
        if _tokens(replace(file_diff, hunks=[hunk]), provider) > available:
            emit([hunk], incomplete=True)
        else:
            pending.append(hunk)
    if pending:
        emit(pending)
    if not chunks:
        emit([], incomplete=True)
    return chunks
