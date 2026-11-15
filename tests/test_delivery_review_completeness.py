"""Full delivery coverage includes every hunk and uses fresh review thresholds."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.config import MiraConfig
from mira.core.chunker import chunk_files
from mira.core.engine import ReviewEngine
from mira.models import BotThreadRecord, FileChangeType, FileDiff, HunkInfo, PRInfo, ReviewResult


def test_oversized_multi_hunk_file_keeps_every_hunk_in_separate_chunks():
    hunks = [HunkInfo(1, 1, 1, 1, "x" * 4000), HunkInfo(10, 1, 10, 1, "y" * 4000)]
    file = FileDiff(path="large.py", change_type=FileChangeType.MODIFIED, hunks=hunks)
    chunks = chunk_files([file], max_tokens=3500)
    assert [h.content for chunk in chunks for f in chunk.files for h in f.hunks] == [
        h.content for h in hunks
    ]
    assert len(chunks) == 2
    assert not any(chunk.incomplete_paths for chunk in chunks)


@pytest.mark.asyncio
async def test_unsplittable_hunk_is_explicitly_incomplete_and_never_sent_to_model():
    config = MiraConfig()
    config.llm.max_context_tokens = 2500
    for key in (
        "walkthrough",
        "security_pass",
        "dependency_overlap",
        "self_critique",
        "code_context",
        "include_summary",
        "secrets_scan",
        "osv_scan",
    ):
        setattr(config.review, key, False)
    llm = MagicMock()
    llm.count_tokens = lambda text: len(text) // 4
    llm.usage = {}
    llm.review = AsyncMock(
        return_value='{"comments":[],"summary":"","metadata":{"reviewed_files":1}}'
    )
    engine = ReviewEngine(config=config, llm=llm)
    diff = (
        "diff --git a/large.py b/large.py\n--- a/large.py\n+++ b/large.py\n@@ -1 +1 @@\n-old\n+"
        + "x" * 5000
        + "\n"
    )
    result = await engine.review_diff(diff)
    assert result.reviewed_paths == []
    assert result.failed_paths == ["large.py"]
    assert result.failed_chunks and "budget" in result.failed_chunks[0].reason
    llm.review.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_delivery_pass_has_fresh_round_and_no_resolved_suppression(monkeypatch):
    provider = MagicMock()
    provider.requires_full_delivery_review.return_value = True
    provider.get_pr_info = AsyncMock(
        return_value=PRInfo(
            title="t",
            description="",
            base_branch="main",
            head_branch="f",
            url="https://git.invalid/g/r/-/merge_requests/1",
            number=1,
            owner="g",
            repo="r",
            head_sha="a" * 40,
            platform="gitlab",
        )
    )
    provider.start_delivery_review = AsyncMock(return_value=None)
    provider.finish_delivery_review = AsyncMock()
    provider.get_pr_diff = AsyncMock(return_value="complete diff")
    provider.get_all_bot_threads = AsyncMock(
        return_value=[
            BotThreadRecord(
                thread_id="old",
                path="a.py",
                line=10,
                body="Previously resolved warning",
                is_resolved=True,
            )
        ]
    )
    provider.get_unresolved_bot_threads = AsyncMock(return_value=[])
    provider.find_bot_comment = AsyncMock(return_value=None)
    provider.start_check_run = AsyncMock(return_value=0)
    provider.finish_check_run = AsyncMock()
    captured = {}

    async def internal(self, diff_text, **kwargs):
        captured.update(kwargs)
        return ReviewResult()

    monkeypatch.setattr(ReviewEngine, "_review_diff_internal", internal)
    config = MiraConfig()
    config.review.auto_resolve_conversations = False
    await ReviewEngine(
        config=config, llm=MagicMock(), provider=provider, bot_name="mira", dry_run=True
    ).review_pr("url")
    assert captured["review_round"] == 1
    assert not captured["resolved_threads"]


@pytest.mark.asyncio
@pytest.mark.parametrize("second_chunk_fails", [False, True])
async def test_split_file_is_covered_only_when_every_hunk_chunk_succeeds(second_chunk_fails):
    config = MiraConfig()
    config.llm.max_context_tokens = 2500
    for key in (
        "walkthrough",
        "security_pass",
        "dependency_overlap",
        "self_critique",
        "code_context",
        "include_summary",
        "secrets_scan",
        "osv_scan",
    ):
        setattr(config.review, key, False)
    good = '{"comments":[],"summary":"","metadata":{"reviewed_files":1}}'
    llm = MagicMock()
    llm.count_tokens = lambda text: len(text) // 4
    llm.usage = {}
    llm.review = AsyncMock(
        side_effect=[good, RuntimeError("second hunk unavailable") if second_chunk_fails else good]
    )
    diff = (
        "diff --git a/large.py b/large.py\n--- a/large.py\n+++ b/large.py\n@@ -1 +1 @@\n-old\n+"
        + "x" * 1200
        + "\n@@ -10 +10 @@\n-old\n+"
        + "y" * 1200
        + "\n"
    )
    result = await ReviewEngine(config=config, llm=llm).review_diff(diff)
    assert result.total_chunks == 2
    assert llm.review.await_count == 2
    if second_chunk_fails:
        assert result.reviewed_paths == []
        assert result.failed_paths == ["large.py"]
    else:
        assert result.reviewed_paths == ["large.py"]
        assert not result.failed_paths


@pytest.mark.asyncio
async def test_chunk_limit_cannot_mark_a_partially_reviewed_split_file_complete():
    config = MiraConfig()
    config.llm.max_context_tokens = 2500
    config.review.max_chunks_per_review = 1
    for key in (
        "walkthrough",
        "security_pass",
        "dependency_overlap",
        "self_critique",
        "code_context",
        "include_summary",
        "secrets_scan",
        "osv_scan",
    ):
        setattr(config.review, key, False)
    llm = MagicMock()
    llm.count_tokens = lambda text: len(text) // 4
    llm.usage = {}
    llm.review = AsyncMock(
        return_value='{"comments":[],"summary":"","metadata":{"reviewed_files":1}}'
    )
    diff = (
        "diff --git a/large.py b/large.py\n--- a/large.py\n+++ b/large.py\n@@ -1 +1 @@\n-old\n+"
        + "x" * 1200
        + "\n@@ -10 +10 @@\n-old\n+"
        + "y" * 1200
        + "\n"
    )
    result = await ReviewEngine(config=config, llm=llm).review_diff(diff)
    assert llm.review.await_count == 1
    assert result.reviewed_paths == []
    assert result.failed_paths == ["large.py"]
