import os
from unittest.mock import AsyncMock, patch

import pytest

from mira.exceptions import ProviderError
from mira.providers.gitlab import GitLabProvider
from tests.test_gitlab_delivery import Response, drive, pr


@pytest.mark.parametrize("failure", ["missing", "overflow", "target"])
def test_start_rejection_does_not_leave_pending_status(failure):
    provider = GitLabProvider("synthetic")

    async def request(method, path, **kwargs):
        if method == "POST":
            return Response({"id": 99})
        if "/pipelines?" in path:
            return Response([{"id": 11, "sha": "a" * 40, "source": "push"}])
        if "/versions?" in path:
            return Response(
                [] if failure == "missing" else [{"id": 5, "head_commit_sha": "a" * 40}]
            )
        if path.endswith("/versions/5"):
            return Response(
                {
                    "head_commit_sha": "a" * 40,
                    "base_commit_sha": "b" * 40,
                    "real_size": "1",
                    "state": "collected",
                    "overflow": failure == "overflow",
                    "diffs": [{"new_path": "a.py", "diff": "@@ -1 +1 @@\n-old\n+new"}],
                }
            )
        if "/repository/branches/" in path:
            return Response({"commit": {"id": "c" * 40}})
        raise AssertionError(path)

    with (
        patch.dict(os.environ, {"MIRA_GITLAB_DELIVERY_PROJECTS": "group/repo"}),
        patch.object(provider, "_request", new=AsyncMock(side_effect=request)) as mocked,
        pytest.raises(ProviderError),
    ):
        drive(provider.start_delivery_review(pr()))
    writes = [call.kwargs["data"] for call in mocked.call_args_list if call.args[0] == "POST"]
    assert not writes or writes[-1]["state"] == "failed"


def test_delivery_completion_rejects_untyped_handle():
    provider = GitLabProvider("synthetic")
    with pytest.raises(ProviderError, match="session"):
        drive(provider.finish_delivery_review(pr(), {"source_sha": "a" * 40}, None))
