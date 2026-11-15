from unittest.mock import AsyncMock

import pytest

from mira.exceptions import ProviderError
from mira.providers.gitlab import GitLabProvider
from tests.test_gitlab_delivery import Response, drive, pr, valid_result


def fixture(monkeypatch, state="collected", change_at=None):
    monkeypatch.setenv("MIRA_GITLAB_DELIVERY_PROJECTS", "group/repo")
    provider = GitLabProvider("synthetic")
    changed = False
    statuses = []

    async def request(method, url, **kwargs):
        nonlocal changed
        if method == "POST":
            statuses.append(kwargs["data"])
            return Response({"id": 99})
        if method == "PUT":
            changed = change_at == "upload"
            return Response({})
        if "/pipelines?" in url:
            return Response([{"id": 11, "sha": "a" * 40, "source": "push"}])
        if "/versions?" in url:
            return Response([{"id": 5, "head_commit_sha": "a" * 40}])
        if "/versions/5" in url:
            data = {
                "head_commit_sha": "a" * 40,
                "base_commit_sha": "b" * 40,
                "start_commit_sha": "b" * 40,
                "real_size": "1",
                "diffs": [
                    {"new_path": "a.py", "old_path": "a.py", "diff": "@@ -1 +1 @@\n-old\n+new"}
                ],
            }
            if state is not None:
                data["state"] = state
            return Response(data)
        if "/discussions?" in url:
            changed = change_at == "discussions"
            return Response([])
        if url.endswith("/user"):
            return Response({"id": 4})
        if "/repository/branches/" in url:
            return Response({"commit": {"id": "b" * 40}})
        if url.endswith("/merge_requests/7"):
            return Response({"sha": ("c" if changed else "a") * 40, "target_branch": "main"})
        raise AssertionError((method, url))

    provider._request = AsyncMock(side_effect=request)
    return provider, statuses


@pytest.mark.parametrize("state", [None, "collecting", "without_files", "overflow"])
def test_delivery_rejects_uncollected_version_before_status(monkeypatch, state):
    provider, statuses = fixture(monkeypatch, state)
    with pytest.raises(ProviderError, match="incomplete"):
        drive(provider.start_delivery_review(pr()))
    assert statuses == []


@pytest.mark.parametrize("change_at", ["discussions", "upload"])
def test_publication_rechecks_revision_after_slow_operations(monkeypatch, change_at):
    provider, statuses = fixture(monkeypatch, change_at=change_at)
    session = drive(provider.start_delivery_review(pr()))
    drive(provider.finish_delivery_review(pr(), session, valid_result()))
    assert statuses[-1]["state"] == "failed"
