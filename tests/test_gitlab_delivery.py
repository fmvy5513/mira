import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from mira.models import PRInfo, ReviewComment, ReviewResult, Severity
from mira.providers.gitlab import GitLabProvider
from mira.providers.gitlab_delivery import eligibility


def valid_result():
    return ReviewResult(reviewed_paths=["a.py"], total_paths=["a.py"], reviewed_files=1)


def test_exact_full_review_without_findings_can_pass():
    assert eligibility(valid_result(), ["a.py"], True, [])["eligible"]


def test_completed_review_with_blocking_findings_is_not_eligibility():
    result = valid_result()
    result.comments = [
        ReviewComment(
            path="a.py",
            line=1,
            end_line=None,
            body="Defect",
            severity=Severity.WARNING,
            category="bug",
            title="Defect",
            confidence=1.0,
        )
    ]
    assert not eligibility(result, ["a.py"], True, [])["eligible"]


def test_stale_head_missing_coverage_and_unresolved_threads_fail_closed():
    assert not eligibility(valid_result(), ["a.py"], False, [])["eligible"]
    assert not eligibility(valid_result(), ["a.py", "b.py"], True, [])["eligible"]
    assert not eligibility(valid_result(), ["a.py"], True, ["thread-1"])["eligible"]


def test_incremental_skipped_and_dependency_failures_never_green():
    for name, value in [
        ("incremental_since", "a" * 40),
        ("skipped_paths", ["b.py"]),
        ("failed_paths", ["a.py"]),
        ("dependency_failures", [object()]),
    ]:
        result = valid_result()
        setattr(result, name, value)
        assert not eligibility(result, ["a.py"], True, [])["eligible"]


def pr():
    return PRInfo(
        title="Change",
        description="",
        base_branch="main",
        head_branch="feature/test",
        url="https://git.invalid/group/repo/-/merge_requests/7",
        number=7,
        owner="group",
        repo="repo",
        head_sha="a" * 40,
        platform="gitlab",
    )


class Response:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


@pytest.mark.parametrize(
    "current_sha,coverage,unresolved,expected_state",
    [
        ("a" * 40, ["a.py"], False, "success"),
        ("c" * 40, ["a.py"], False, "failed"),
        ("a" * 40, [], False, "failed"),
        ("a" * 40, ["a.py"], True, "failed"),
    ],
)
def test_pinned_input_and_eligibility_status_are_machine_bound(
    current_sha, coverage, unresolved, expected_state
):
    provider = GitLabProvider("synthetic")
    posted = []
    manifests = []

    async def request(method, url, **kwargs):
        if method == "POST" and "/statuses/" in url:
            posted.append(kwargs["data"])
            return Response({"id": 99, "pipeline_id": 11})
        if method == "PUT":
            manifests.append(json.loads(kwargs["content"]))
            return Response({})
        if "/versions?" in url:
            return Response([{"id": 5, "head_commit_sha": "a" * 40}])
        if url.endswith("/versions/5"):
            return Response(
                {
                    "head_commit_sha": "a" * 40,
                    "base_commit_sha": "b" * 40,
                    "start_commit_sha": "b" * 40,
                    "real_size": "1",
                    "diffs": [
                        {"new_path": "a.py", "old_path": "a.py", "diff": "@@ -1 +1 @@\n-old\n+new"}
                    ],
                }
            )
        if "/repository/branches/" in url:
            return Response({"commit": {"id": "b" * 40}})
        if "/pipelines?" in url:
            return Response([{"id": 11, "sha": "a" * 40, "source": "merge_request_event"}])
        if "/discussions?" in url:
            return Response(
                [
                    {
                        "id": "blocking-thread",
                        "notes": [{"author": {"id": 4}, "resolvable": True, "resolved": False}],
                    }
                ]
                if unresolved
                else []
            )
        if url.endswith("/user"):
            return Response({"username": "mira-bot", "id": 4})
        if url.endswith("/merge_requests/7"):
            return Response({"sha": current_sha, "target_branch": "main"})
        raise AssertionError((method, url))

    with (
        patch.dict(os.environ, {"MIRA_GITLAB_DELIVERY_PROJECTS": "group/repo"}),
        patch.object(provider, "_request", side_effect=request),
    ):
        handle = drive(provider.start_delivery_review(pr()))
        assert provider.requires_full_delivery_review(pr())
        pinned = drive(provider._changes(pr()))
        assert pinned["diff_refs"]["head_sha"] == "a" * 40
        result = valid_result()
        result.reviewed_paths = coverage
        drive(provider.finish_delivery_review(pr(), handle, result))
    assert posted[0]["state"] == "pending"
    assert posted[-1]["state"] == expected_state
    assert manifests[0]["source_sha"] == "a" * 40
    assert posted[0]["name"].startswith("panel/mira-eligibility/7/")
    assert "start=99;" in posted[-1]["description"]
    assert posted[-1]["pipeline_id"] == 11


def test_unconfigured_project_has_no_status_side_effect():
    provider = GitLabProvider("synthetic")
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(provider, "_request", new_callable=AsyncMock) as request,
    ):
        assert drive(provider.start_delivery_review(pr())) is None
    request.assert_not_called()


def drive(coroutine):
    """Protocol fakes complete without I/O; no real event loop or network is needed."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("Unexpected asynchronous I/O at fake provider boundary")


@pytest.mark.parametrize("overflow,source", [(True, "merge_request_event"), (False, "external")])
def test_incomplete_version_or_external_only_pipeline_cannot_start_gate(overflow, source):
    from mira.exceptions import ProviderError

    provider = GitLabProvider("synthetic")
    version = {
        "head_commit_sha": "a" * 40,
        "base_commit_sha": "b" * 40,
        "start_commit_sha": "b" * 40,
        "overflow": overflow,
        "real_size": "1",
        "diffs": [{"new_path": "a.py", "old_path": "a.py", "diff": "@@ -1 +1 @@\n-old\n+new"}],
    }
    replies = [
        Response([{"id": 11, "sha": "a" * 40, "source": source}]),
        Response([{"id": 5, "head_commit_sha": "a" * 40}]),
        Response(version),
        Response({"commit": {"id": "b" * 40}}),
    ]
    with (
        patch.dict(os.environ, {"MIRA_GITLAB_DELIVERY_PROJECTS": "group/repo"}),
        patch.object(provider, "_request", new=AsyncMock(side_effect=replies)) as request,
        pytest.raises(ProviderError),
    ):
        drive(provider.start_delivery_review(pr()))
    writes = [call.kwargs["data"] for call in request.call_args_list if call.args[0] == "POST"]
    assert not writes or all(write["state"] == "pending" for write in writes)


def test_engine_runs_actual_delivery_hooks_with_original_fetched_input():
    from mira.config import MiraConfig
    from mira.core.engine import ReviewEngine

    provider = GitLabProvider("synthetic")
    original = pr()
    result = valid_result()
    from mira.providers.base import DeliveryReviewSession

    handle = DeliveryReviewSession(source_sha=original.head_sha, target_base_sha="b" * 40)
    events = []

    async def start(value):
        events.append("start")
        assert value is original
        return handle

    async def analysis(value):
        events.append("analysis")
        assert value is original
        return result

    async def finish(value, record, output):
        events.append("finish")
        assert value is original
        assert record is handle
        assert output is result

    engine = ReviewEngine(MiraConfig(), object(), provider)
    with (
        patch.object(provider, "get_pr_info", new=AsyncMock(return_value=original)),
        patch.object(provider, "start_delivery_review", side_effect=start),
        patch.object(provider, "finish_delivery_review", side_effect=finish),
        patch.object(engine, "_review_pr", side_effect=analysis),
    ):
        assert drive(engine.review_pr(original.url)) is result
    assert events == ["start", "analysis", "finish"]
