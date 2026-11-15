"""Normal GitLab reviews must not silently omit collapsed legacy diff fragments."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mira.core.diff_parser import parse_diff
from mira.exceptions import ProviderError
from mira.models import PRInfo
from mira.providers.gitlab import GitLabProvider

HEAD = "a" * 40
BASE = "b" * 40


def pr():
    return PRInfo(
        title="Docs",
        description="",
        base_branch="main",
        head_branch="docs",
        url="https://git.invalid/g/r/-/merge_requests/22",
        number=22,
        owner="g",
        repo="r",
        head_sha=HEAD,
        platform="gitlab",
    )


def fragment(path):
    return {
        "old_path": path,
        "new_path": path,
        "new_file": True,
        "collapsed": False,
        "too_large": False,
        "diff": "@@ -0,0 +1 @@\n+Reviewed documentation.\n",
    }


def version():
    return {
        "id": 923,
        "head_commit_sha": HEAD,
        "base_commit_sha": BASE,
        "start_commit_sha": BASE,
        "state": "collected",
        "real_size": "2",
        "diffs": [fragment("CAPABILITIES.md"), fragment("TDD.md")],
    }


def provider_for(data=None, final_head=HEAD, versions=None):
    provider = GitLabProvider("synthetic")
    payload = version() if data is None else data
    calls = []

    async def request(method, url, **kwargs):
        calls.append(url)
        if url.endswith("/versions/923"):
            value = payload
        elif "/versions?" in url:
            value = versions if versions is not None else [{"id": 923, "head_commit_sha": HEAD}]
        elif url.endswith("/changes"):
            # Exact MR22 failure: legacy endpoint silently collapses the TDD.
            value = {
                "sha": HEAD,
                "overflow": False,
                "changes": [
                    fragment("CAPABILITIES.md"),
                    {**fragment("TDD.md"), "diff": "", "collapsed": True},
                ],
            }
        elif url.endswith("/merge_requests/22"):
            count = sum(item.endswith("/merge_requests/22") for item in calls)
            value = {"sha": HEAD if count == 1 else final_head, "target_branch": "main"}
        else:
            raise AssertionError(url)
        return SimpleNamespace(json=lambda: copy.deepcopy(value))

    provider._request = AsyncMock(side_effect=request)
    return provider, calls


@pytest.mark.asyncio
async def test_normal_review_uses_exact_complete_version_not_collapsed_legacy_changes():
    provider, calls = provider_for()
    text = await provider.get_pr_diff(pr())
    assert {file.path for file in parse_diff(text).files} == {"CAPABILITIES.md", "TDD.md"}
    assert not any(url.endswith("/changes") for url in calls)
    refs = await provider._changes(pr())
    assert refs["diff_refs"]["head_sha"] == HEAD
    assert len(refs["changes"]) == 2
    assert not any(url.endswith("/changes") for url in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect", ["collapsed", "too_large", "empty", "count", "duplicate", "wrong_version", "overflow"]
)
async def test_incomplete_pinned_version_never_produces_partial_success(defect):
    data = version()
    if defect in {"collapsed", "too_large"}:
        data["diffs"][1][defect] = True
    elif defect == "empty":
        data["diffs"][1]["diff"] = ""
    elif defect == "count":
        data["real_size"] = "3"
    elif defect == "duplicate":
        data["diffs"][1] = copy.deepcopy(data["diffs"][0])
    elif defect == "wrong_version":
        data["head_commit_sha"] = "c" * 40
    elif defect == "overflow":
        data["overflow"] = True
    provider, _ = provider_for(data)
    with pytest.raises(ProviderError):
        await provider.get_pr_diff(pr())


@pytest.mark.asyncio
async def test_source_moving_during_fetch_fails_closed():
    provider, _ = provider_for(final_head="c" * 40)
    with pytest.raises(ProviderError, match="changed"):
        await provider.get_pr_diff(pr())


@pytest.mark.asyncio
async def test_missing_exact_version_does_not_fall_back_to_legacy_changes():
    provider, calls = provider_for(versions=[{"id": 922, "head_commit_sha": "c" * 40}])
    with pytest.raises(ProviderError, match="version"):
        await provider.get_pr_diff(pr())
    assert not any(url.endswith("/changes") for url in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["binary", "rename", "mode", "empty"])
async def test_verified_metadata_only_change_does_not_block_other_text_or_claim_source_coverage(
    kind,
):
    data = version()
    metadata = {
        **fragment("asset.bin"),
        "new_file": False,
        "diff": "",
        "a_mode": "100644",
        "b_mode": "100644",
    }
    if kind == "rename":
        metadata.update(old_path="old.txt", new_path="renamed.txt", renamed_file=True)
    elif kind == "mode":
        metadata["b_mode"] = "100755"
    elif kind == "empty":
        metadata["new_file"] = True
    data["diffs"][1] = metadata
    provider, calls = provider_for(data)
    original = provider._request.side_effect

    async def request(method, url, **kwargs):
        if method == "HEAD":
            from urllib.parse import parse_qs, unquote, urlsplit

            path = unquote(urlsplit(url).path.split("/repository/files/", 1)[1])
            ref = parse_qs(urlsplit(url).query)["ref"][0]
            blob = "d" * 40 if ref == HEAD and kind == "binary" else "e" * 40
            return SimpleNamespace(
                headers={
                    "X-Gitlab-Blob-Id": blob,
                    "X-Gitlab-Size": "0" if kind == "empty" else "100",
                    "X-Gitlab-File-Path": path,
                    "X-Gitlab-Ref": ref,
                    "X-Gitlab-Commit-Id": ref,
                }
            )
        return await original(method, url, **kwargs)

    provider._request = AsyncMock(side_effect=request)
    provider._read_binary_prefix = AsyncMock(
        return_value=b"blob\x00binary" if kind == "binary" else b"plain text"
    )
    text = await provider.get_pr_diff(pr())
    assert {file.path for file in parse_diff(text).files} == {"CAPABILITIES.md"}
    assert provider.get_unreviewable_paths(pr()) == [metadata["new_path"]]
    assert {row["new_path"] for row in (await provider._changes(pr()))["changes"]} == {
        "CAPABILITIES.md",
        metadata["new_path"],
    }
    from mira.models import ReviewResult
    from mira.providers.gitlab_delivery import eligibility

    result = ReviewResult(
        reviewed_paths=["CAPABILITIES.md"], skipped_paths=provider.get_unreviewable_paths(pr())
    )
    assert not eligibility(result, ["CAPABILITIES.md", metadata["new_path"]], True, [])["eligible"]


@pytest.mark.asyncio
async def test_rename_flag_without_equal_pinned_blobs_remains_incomplete():
    data = version()
    data["diffs"][1].update(diff="", new_file=False, renamed_file=True, old_path="old.txt")
    provider, _ = provider_for(data)
    original = provider._request.side_effect

    async def request(method, url, **kwargs):
        if method == "HEAD":
            from urllib.parse import parse_qs, unquote, urlsplit

            path = unquote(urlsplit(url).path.split("/repository/files/", 1)[1])
            ref = parse_qs(urlsplit(url).query)["ref"][0]
            return SimpleNamespace(
                headers={
                    "X-Gitlab-Blob-Id": ref,
                    "X-Gitlab-Size": "10",
                    "X-Gitlab-File-Path": path,
                    "X-Gitlab-Ref": ref,
                    "X-Gitlab-Commit-Id": ref,
                }
            )
        return await original(method, url, **kwargs)

    provider._request = AsyncMock(side_effect=request)
    provider._read_binary_prefix = AsyncMock(return_value=b"plain text")
    with pytest.raises(ProviderError):
        await provider.get_pr_diff(pr())


@pytest.mark.asyncio
async def test_engine_explicitly_skips_verified_metadata_and_never_credits_source_review(
    monkeypatch,
):
    from unittest.mock import MagicMock

    from mira.config import MiraConfig
    from mira.core.engine import ReviewEngine
    from mira.models import ReviewResult, WalkthroughResult

    provider = GitLabProvider("synthetic")
    provider.get_pr_info = AsyncMock(return_value=pr())
    provider.get_pr_diff = AsyncMock(return_value="complete text diff")
    provider._review_inputs = {provider._delivery_key(pr()): {"unreviewable_paths": ["asset.bin"]}}
    captured = {}

    async def review(self, diff_text, **kwargs):
        captured["diff"] = diff_text
        return ReviewResult(
            reviewed_paths=["CAPABILITIES.md", "asset.bin"],
            reviewed_files=2,
            total_paths=["CAPABILITIES.md"],
            walkthrough=WalkthroughResult(summary="Text reviewed"),
        )

    monkeypatch.setattr(ReviewEngine, "_review_diff_internal", review)
    monkeypatch.delenv("MIRA_GITLAB_DELIVERY_PROJECTS", raising=False)
    config = MiraConfig()
    config.review.overlap.enabled = False
    config.review.auto_resolve_conversations = False
    result = await ReviewEngine(
        config=config, llm=MagicMock(), provider=provider, dry_run=True
    ).review_pr("url")
    assert result.reviewed_paths == ["CAPABILITIES.md"]
    assert result.reviewed_files == 1
    assert result.skipped_paths == ["asset.bin"]
    assert "asset.bin" in result.total_paths
    assert "not source-reviewed" in result.summary
    assert "asset.bin" in result.walkthrough.summary


@pytest.mark.asyncio
async def test_binary_probe_stops_at_bounded_prefix_even_when_range_is_ignored(monkeypatch):
    from unittest.mock import patch

    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self, chunk_size):
            assert chunk_size == 8000
            yield b"x" * 16000
            raise AssertionError("Must not consume a full blob")

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 10, "follow_redirects": False}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, headers):
            assert headers["Range"] == "bytes=0-7999"
            assert f"ref={HEAD}" in url
            return Response()

    with patch("mira.providers.gitlab.httpx.AsyncClient", Client):
        assert (
            len(await GitLabProvider("synthetic")._read_binary_prefix(pr(), "image.png", HEAD))
            == 8000
        )


@pytest.mark.asyncio
async def test_metadata_verification_has_a_bounded_file_count():
    data = version()
    data["diffs"] = [{**fragment(f"file{i}.dat"), "diff": ""} for i in range(21)]
    data["real_size"] = "21"
    provider, calls = provider_for(data)
    with pytest.raises(ProviderError, match="bounded"):
        await provider.get_pr_diff(pr())
    assert not any("/repository/files/" in url for url in calls)


@pytest.mark.asyncio
async def test_binary_marker_is_explicitly_unreviewable_without_blob_probes():
    data = version()
    data["diffs"][1] = {
        **fragment("asset.bin"),
        "new_file": False,
        "diff": "Binary files a/asset.bin and b/asset.bin differ\n",
    }
    provider, calls = provider_for(data)
    text = await provider.get_pr_diff(pr())
    from mira.config import FilterConfig
    from mira.core.file_filter import filter_files

    assert {file.path for file in filter_files(parse_diff(text).files, FilterConfig())} == {
        "CAPABILITIES.md"
    }
    assert provider.get_unreviewable_paths(pr()) == ["asset.bin"]
    assert not any("/repository/files/" in url for url in calls)


@pytest.mark.asyncio
async def test_metadata_probe_requires_matching_immutable_ref_identity():
    provider = GitLabProvider("synthetic")
    provider._request = AsyncMock(
        return_value=SimpleNamespace(
            headers={
                "X-Gitlab-Blob-Id": "d" * 40,
                "X-Gitlab-Size": "0",
                "X-Gitlab-File-Path": "empty.txt",
                "X-Gitlab-Ref": "main",
                "X-Gitlab-Commit-Id": HEAD,
            }
        )
    )
    with pytest.raises(ProviderError, match="immutable"):
        await provider._pinned_blob_metadata(pr(), "empty.txt", HEAD)
