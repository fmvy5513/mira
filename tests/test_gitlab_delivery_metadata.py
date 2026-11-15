"""Enabled delivery sessions use the same verified metadata partition as normal reviews."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from mira.core.diff_parser import parse_diff
from mira.exceptions import ProviderError
from mira.models import ReviewResult
from mira.providers.gitlab_delivery import eligibility
from tests.test_gitlab_diff_completeness import BASE, HEAD, fragment, pr, provider_for, version


def enabled_provider(kind):
    data = version()
    metadata = {**fragment("asset.bin"), "new_file": False, "diff": ""}
    if kind == "rename":
        metadata.update(old_path="old.txt", new_path="renamed.txt", renamed_file=True)
    data["diffs"][1] = metadata
    provider, _ = provider_for(data)
    original = provider._request.side_effect
    writes = []

    async def request(method, url, **kwargs):
        if method == "POST":
            writes.append(kwargs["data"])
            return SimpleNamespace(json=lambda: {"id": 99})
        if "/pipelines?" in url:
            return SimpleNamespace(json=lambda: [{"id": 11, "sha": HEAD, "source": "push"}])
        if "/repository/branches/" in url:
            return SimpleNamespace(json=lambda: {"commit": {"id": BASE}})
        if method == "HEAD":
            path = unquote(urlsplit(url).path.split("/repository/files/", 1)[1])
            ref = parse_qs(urlsplit(url).query)["ref"][0]
            blob = "e" * 40 if kind == "rename" else ref
            return SimpleNamespace(
                headers={
                    "X-Gitlab-Blob-Id": blob,
                    "X-Gitlab-Size": "100",
                    "X-Gitlab-File-Path": path,
                    "X-Gitlab-Ref": ref,
                    "X-Gitlab-Commit-Id": ref,
                }
            )
        return await original(method, url, **kwargs)

    provider._request = AsyncMock(side_effect=request)
    provider._read_binary_prefix = AsyncMock(
        return_value=b"prefix\x00binary" if kind == "binary" else b"plain text"
    )
    return provider, metadata, writes


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["binary", "rename"])
async def test_enabled_session_reviews_text_but_cannot_claim_metadata_source_coverage(
    kind, monkeypatch
):
    monkeypatch.setenv("MIRA_GITLAB_DELIVERY_PROJECTS", "g/r")
    provider, metadata, writes = enabled_provider(kind)
    session = await provider.start_delivery_review(pr())
    text = await provider.get_pr_diff(pr())
    assert {file.path for file in parse_diff(text).files} == {"CAPABILITIES.md"}
    assert provider.get_unreviewable_paths(pr()) == [metadata["new_path"]]
    assert session.changed_paths == sorted(["CAPABILITIES.md", metadata["new_path"]])
    assert len((await provider._changes(pr()))["changes"]) == 2
    inputs = session.review_inputs()
    assert inputs["unreviewable_paths"] == [metadata["new_path"]]
    assert len(inputs["source_changes"]) == 1
    inputs["diff_refs"]["head_sha"] = "c" * 40
    inputs["changes"][0]["new_path"] = "mutated.txt"
    assert session.diff_refs["head_sha"] == HEAD
    assert session.changes[0]["new_path"] == "CAPABILITIES.md"
    result = ReviewResult(
        reviewed_paths=["CAPABILITIES.md"], skipped_paths=provider.get_unreviewable_paths(pr())
    )
    assert not eligibility(result, session.changed_paths, True, [])["eligible"]
    assert writes[0]["state"] == "pending"


@pytest.mark.asyncio
async def test_enabled_unknown_blank_text_fails_before_pending_status(monkeypatch):
    monkeypatch.setenv("MIRA_GITLAB_DELIVERY_PROJECTS", "g/r")
    provider, _, writes = enabled_provider("unknown")
    with pytest.raises(ProviderError, match="metadata-only"):
        await provider.start_delivery_review(pr())
    assert writes == []


@pytest.mark.asyncio
async def test_enabled_engine_runs_text_review_and_explicitly_denies_metadata_coverage(monkeypatch):
    from unittest.mock import MagicMock

    from mira.config import MiraConfig
    from mira.core.engine import ReviewEngine

    monkeypatch.setenv("MIRA_GITLAB_DELIVERY_PROJECTS", "g/r")
    provider, metadata, _ = enabled_provider("binary")
    provider.get_pr_info = AsyncMock(return_value=pr())
    session = await provider.start_delivery_review(pr())
    captured = {}

    async def review(self, diff_text, **kwargs):
        captured["text_paths"] = {file.path for file in parse_diff(diff_text).files}
        return ReviewResult(
            reviewed_paths=["CAPABILITIES.md"], reviewed_files=1, total_paths=["CAPABILITIES.md"]
        )

    monkeypatch.setattr(ReviewEngine, "_review_diff_internal", review)
    config = MiraConfig()
    config.review.auto_resolve_conversations = False
    config.review.overlap.enabled = False
    result = await ReviewEngine(
        config=config, llm=MagicMock(), provider=provider, bot_name="", dry_run=True
    ).review_pr("url")
    assert captured["text_paths"] == {"CAPABILITIES.md"}
    assert result.reviewed_paths == ["CAPABILITIES.md"]
    assert result.skipped_paths == [metadata["new_path"]]
    assert session.source_sha == HEAD and session.target_base_sha == BASE
    assert set(session.changed_paths) == {"CAPABILITIES.md", metadata["new_path"]}
    assert not eligibility(result, session.changed_paths, True, [])["eligible"]
