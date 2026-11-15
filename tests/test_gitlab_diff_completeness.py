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
