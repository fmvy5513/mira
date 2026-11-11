"""Tests for the GitLab webhook route + event dispatch."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from mira.config import FilterConfig, MiraConfig
from mira.platforms.gitlab.auth import GitLabTokenAuth
from mira.platforms.server import create_app

GL_SECRET = "gl-secret"
BOT = "mira-bot"


@pytest.fixture
def gitlab_auth():  # noqa: ANN201
    auth = GitLabTokenAuth("tok")
    # Avoid a live /user call for self-author detection.
    auth.get_bot_identity = AsyncMock(return_value="mira-bot")  # type: ignore[method-assign]
    return auth


@pytest.fixture
def app(gitlab_auth):  # noqa: ANN001, ANN201
    return create_app(
        app_auth=None,
        webhook_secret=None,
        bot_name=BOT,
        gitlab_auth=gitlab_auth,
        gitlab_webhook_secret=GL_SECRET,
    )


@pytest.fixture
async def client(app) -> AsyncClient:  # noqa: ANN001
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _mr_payload(action="open", oldrev=None):
    attrs = {
        "iid": 7,
        "action": action,
        "source_branch": "f",
        "target_branch": "main",
        "url": "https://gitlab.com/g/p/-/merge_requests/7",
    }
    if oldrev:
        attrs["oldrev"] = oldrev
    return {
        "object_kind": "merge_request",
        "object_attributes": attrs,
        "project": {
            "path_with_namespace": "g/p",
            "web_url": "https://gitlab.com/g/p",
            "default_branch": "main",
            "visibility": "private",
        },
        "user": {"username": "alice"},
    }


@pytest.mark.asyncio
async def test_rejects_bad_token(client):
    resp = await client.post(
        "/gitlab/webhook",
        content=json.dumps(_mr_payload()),
        headers={"X-Gitlab-Token": "wrong", "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mr_open_triggers_review(client):
    with patch("mira.platforms.gitlab.webhook.handle_merge_request", new=AsyncMock()) as h:
        resp = await client.post(
            "/gitlab/webhook",
            content=json.dumps(_mr_payload(action="open")),
            headers={"X-Gitlab-Token": GL_SECRET, "X-Gitlab-Event": "Merge Request Hook"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "processing"


@pytest.mark.asyncio
async def test_mr_update_without_newcommits_ignored(client):
    with patch("mira.platforms.gitlab.webhook.handle_merge_request", new=AsyncMock()) as h:
        resp = await client.post(
            "/gitlab/webhook",
            content=json.dumps(_mr_payload(action="update")),  # no oldrev = no new commits
            headers={"X-Gitlab-Token": GL_SECRET, "X-Gitlab-Event": "Merge Request Hook"},
        )
    assert resp.json()["status"] == "ignored"
    h.assert_not_called()


@pytest.mark.asyncio
async def test_self_authored_event_ignored(client, gitlab_auth):
    payload = _mr_payload(action="open")
    payload["user"]["username"] = "mira-bot"  # the bot itself
    with patch("mira.platforms.gitlab.webhook.handle_merge_request", new=AsyncMock()) as h:
        resp = await client.post(
            "/gitlab/webhook",
            content=json.dumps(payload),
            headers={"X-Gitlab-Token": GL_SECRET, "X-Gitlab-Event": "Merge Request Hook"},
        )
    assert resp.json()["status"] == "ignored"
    h.assert_not_called()


def _note_payload(note="@mira-bot review"):
    return {
        "object_kind": "note",
        "object_attributes": {"note": note, "noteable_type": "MergeRequest"},
        "merge_request": {"iid": 7, "url": "https://gitlab.com/g/p/-/merge_requests/7"},
        "project": {"path_with_namespace": "g/p", "web_url": "https://gitlab.com/g/p"},
        "user": {"username": "alice"},
    }


@pytest.mark.asyncio
async def test_note_mention_triggers_command(client):
    with patch("mira.platforms.gitlab.webhook.handle_gitlab_note", new=AsyncMock()) as h:
        resp = await client.post(
            "/gitlab/webhook",
            content=json.dumps(_note_payload()),
            headers={"X-Gitlab-Token": GL_SECRET, "X-Gitlab-Event": "Note Hook"},
        )
    assert resp.json()["status"] == "processing"


@pytest.mark.asyncio
async def test_note_without_mention_ignored(client):
    with patch("mira.platforms.gitlab.webhook.handle_gitlab_note", new=AsyncMock()) as h:
        resp = await client.post(
            "/gitlab/webhook",
            content=json.dumps(_note_payload(note="looks good")),
            headers={"X-Gitlab-Token": GL_SECRET, "X-Gitlab-Event": "Note Hook"},
        )
    assert resp.json()["status"] == "ignored"
    h.assert_not_called()


@pytest.mark.asyncio
async def test_mr_merge_triggers_learning(client):
    with patch("mira.platforms.gitlab.webhook.handle_gitlab_merge", new=AsyncMock()) as h:
        resp = await client.post(
            "/gitlab/webhook",
            content=json.dumps(_mr_payload(action="merge")),
            headers={"X-Gitlab-Token": GL_SECRET, "X-Gitlab-Event": "Merge Request Hook"},
        )
    assert resp.json()["status"] == "processing"


@pytest.mark.asyncio
async def test_inline_mention_routes_to_thread_reply(gitlab_auth):
    """A free-form @-mention on a diff note → LLM intent classification."""
    from mira.platforms.gitlab import webhook as gw

    payload = {
        "object_attributes": {
            "note": "@mira-bot are you sure?",
            "noteable_type": "MergeRequest",
            "id": 99,
            "discussion_id": "abc123",
            "position": {"new_path": "a.py", "new_line": 4},
        },
        "merge_request": {"iid": 7, "url": "https://gitlab.com/g/p/-/merge_requests/7"},
        "project": {"path_with_namespace": "g/p", "web_url": "https://gitlab.com/g/p"},
        "user": {"username": "alice"},
    }
    prov = AsyncMock()
    prov.get_pr_info = AsyncMock(return_value=object())
    with (
        patch("mira.platforms.gitlab.webhook.create_provider", return_value=prov),
        patch("mira.platforms.handlers.run_thread_reply", new=AsyncMock()) as rtr,
        patch("mira.platforms.handlers.run_pr_command", new=AsyncMock()) as rpc,
    ):
        await gw.handle_gitlab_note(payload, gitlab_auth, "mira-bot")
    rtr.assert_awaited_once()
    rpc.assert_not_called()


@pytest.mark.asyncio
async def test_paused_label_skips_review(gitlab_auth):
    from mira.platforms.gitlab import webhook as gw

    payload = _mr_payload(action="open")
    payload["labels"] = [{"title": "mira-paused"}]
    with patch("mira.platforms.handlers.run_pr_review", new=AsyncMock()) as rpr:
        await gw.handle_merge_request(payload, gitlab_auth, "mira-bot")
    rpr.assert_not_called()


@pytest.mark.asyncio
async def test_ignore_in_description_skips_review(gitlab_auth):
    from mira.platforms.gitlab import webhook as gw

    payload = _mr_payload(action="open")
    payload["object_attributes"]["description"] = "wip @mira-bot ignore for now"
    with patch("mira.platforms.handlers.run_pr_review", new=AsyncMock()) as rpr:
        await gw.handle_merge_request(payload, gitlab_auth, "mira-bot")
    rpr.assert_not_called()


@pytest.mark.asyncio
async def test_unpaused_mr_runs_review(gitlab_auth):
    from mira.platforms.gitlab import webhook as gw

    payload = _mr_payload(action="open")
    with (
        patch("mira.platforms.index_handlers._get_app_db"),
        patch("mira.platforms.gitlab.webhook.create_provider", return_value=AsyncMock()),
        patch("mira.platforms.handlers.run_pr_review", new=AsyncMock()) as rpr,
    ):
        await gw.handle_merge_request(payload, gitlab_auth, "mira-bot")
    rpr.assert_awaited_once()


@pytest.mark.asyncio
async def test_github_route_404_when_not_configured(client):
    # GitLab-only deployment: the GitHub webhook route is disabled.
    resp = await client.post("/github/webhook", content="{}", headers={"X-GitHub-Event": "ping"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mr_open_blocked_author_filtered(client):
    payload = _mr_payload(action="open")
    payload["user"]["username"] = "dependabot"
    cfg = MiraConfig(filter=FilterConfig(blocked_authors=["dependabot"]))
    with (
        patch("mira.platforms.gitlab.webhook.load_config", return_value=cfg),
        patch("mira.platforms.gitlab.webhook.handle_merge_request", new=AsyncMock()) as h,
    ):
        resp = await client.post(
            "/gitlab/webhook",
            content=json.dumps(payload),
            headers={"X-Gitlab-Token": GL_SECRET, "X-Gitlab-Event": "Merge Request Hook"},
        )
    assert resp.json()["status"] == "ignored"
    h.assert_not_called()


@pytest.mark.asyncio
async def test_mr_open_allowed_author_not_filtered(client):
    payload = _mr_payload(action="open")
    payload["user"]["username"] = "alice"
    cfg = MiraConfig(filter=FilterConfig(blocked_authors=["dependabot"]))
    with (
        patch("mira.platforms.gitlab.webhook.load_config", return_value=cfg),
        patch("mira.platforms.gitlab.webhook.handle_merge_request", new=AsyncMock()) as h,
    ):
        resp = await client.post(
            "/gitlab/webhook",
            content=json.dumps(payload),
            headers={"X-Gitlab-Token": GL_SECRET, "X-Gitlab-Event": "Merge Request Hook"},
        )
    assert resp.json()["status"] == "processing"
    h.assert_awaited_once()


@pytest.mark.asyncio
async def test_mr_open_allowlist_filters_off_list(client):
    payload = _mr_payload(action="open")
    payload["user"]["username"] = "bob"
    cfg = MiraConfig(filter=FilterConfig(allowed_authors=["alice"]))
    with (
        patch("mira.platforms.gitlab.webhook.load_config", return_value=cfg),
        patch("mira.platforms.gitlab.webhook.handle_merge_request", new=AsyncMock()) as h,
    ):
        resp = await client.post(
            "/gitlab/webhook",
            content=json.dumps(payload),
            headers={"X-Gitlab-Token": GL_SECRET, "X-Gitlab-Event": "Merge Request Hook"},
        )
    assert resp.json()["status"] == "ignored"
    h.assert_not_called()


@pytest.mark.asyncio
async def test_note_review_ignores_author_filter(client):
    """Commands use the command policy, not the auto-review author filter."""
    payload = _note_payload(note="@mira-bot review")
    payload["user"]["username"] = "dependabot"
    cfg = MiraConfig(filter=FilterConfig(blocked_authors=["dependabot"]))
    with (
        patch("mira.platforms.gitlab.webhook.load_config", return_value=cfg),
        patch("mira.platforms.gitlab.webhook.handle_gitlab_note", new=AsyncMock()) as h,
    ):
        resp = await client.post(
            "/gitlab/webhook",
            content=json.dumps(payload),
            headers={"X-Gitlab-Token": GL_SECRET, "X-Gitlab-Event": "Note Hook"},
        )
    assert resp.json()["status"] == "processing"
    h.assert_awaited_once()


@pytest.mark.asyncio
async def test_note_non_review_ignores_author_filter(client):
    """Free-form command dispatch is separate from the auto-review author filter."""
    payload = _note_payload(note="@mira-bot pause")
    payload["user"]["username"] = "alice"
    cfg = MiraConfig(filter=FilterConfig(blocked_authors=["alice"]))
    with (
        patch("mira.platforms.gitlab.webhook.load_config", return_value=cfg),
        patch("mira.platforms.gitlab.webhook.handle_gitlab_note", new=AsyncMock()) as h,
    ):
        resp = await client.post(
            "/gitlab/webhook",
            content=json.dumps(payload),
            headers={"X-Gitlab-Token": GL_SECRET, "X-Gitlab-Event": "Note Hook"},
        )
    assert resp.json()["status"] == "processing"
    h.assert_awaited_once()


@pytest.mark.parametrize(
    "path,ref,after,expected",
    [
        ("rhet/physical-school-management-system", "refs/heads/dev", "a" * 40, "processing"),
        ("rhet/physical-school-management-system", "refs/heads/dev", "0" * 40, "ignored"),
        ("rhet/physical-school-management-system", "refs/heads/feature", "a" * 40, "ignored"),
        ("rhet/team/project", "refs/heads/dev", "a" * 40, "processing"),
        ("rhet-other/project", "refs/heads/dev", "a" * 40, "ignored"),
        ("other/physical-school-management-system", "refs/heads/dev", "a" * 40, "ignored"),
    ],
)
async def test_dev_push_scope(gitlab_auth, path, ref, after, expected):
    from unittest.mock import Mock

    from mira.platforms.gitlab.push_review import review_push
    from mira.platforms.gitlab.webhook import dispatch_gitlab_event

    tasks = Mock()
    payload = {
        "ref": ref,
        "before": "b" * 40,
        "after": after,
        "project": {"path_with_namespace": path},
    }
    assert await dispatch_gitlab_event("Push Hook", payload, gitlab_auth, BOT, tasks) == expected
    if expected == "processing":
        tasks.add_task.assert_called_once_with(review_push, payload, gitlab_auth, BOT)
    else:
        tasks.add_task.assert_not_called()


async def test_dev_push_reviews_range_posts_on_head_and_skips_replay(gitlab_auth):
    from unittest.mock import Mock

    from mira.models import ReviewResult
    from mira.platforms.gitlab import push_review as push

    before, head = "a" * 40, "b" * 40
    payload = {
        "before": before,
        "after": head,
        "project": {"path_with_namespace": "rhet/sub/app"},
        "ref": "refs/heads/main",
    }
    provider = Mock(_api="https://example.invalid/api/v4")
    provider._paginate = AsyncMock(return_value=[])
    provider._request = AsyncMock(
        return_value=Mock(
            json=lambda: {
                "diffs": [
                    {"old_path": "a.py", "new_path": "a.py", "diff": "@@ -1 +1 @@\n-old\n+new\n"}
                ]
            }
        )
    )
    engine = Mock(review_diff=AsyncMock(return_value=ReviewResult(summary="Reviewed the change.")))
    with (
        patch.object(push, "GitLabProvider", return_value=provider),
        patch.object(push, "ReviewEngine", return_value=engine),
        patch.object(push, "create_llm"),
        patch.object(push, "llm_config_for"),
        patch.object(push, "_record_push_review") as record,
    ):
        await push.review_push(payload, gitlab_auth, BOT)
        record.assert_called_once()
        get, post = provider._request.await_args_list
        assert get.kwargs["params"] == {"from": before, "to": head, "straight": "true"}
        assert post.args[0] == "POST" and post.args[1].endswith(f"/commits/{head}/comments")
        note = post.kwargs["json"]["note"]
        assert "rhet%2Fsub%2Fapp" in post.args[1]
        assert "main push review" in note
        assert "Reviewed the change." in note
        assert "+new" in engine.review_diff.await_args.args[0]
        provider._paginate.return_value = [{"author": {"username": BOT}, "note": note}]
        await push.review_push(payload, gitlab_auth, BOT)
        assert provider._request.await_count == 2


async def test_rhet_main_schedules_review_and_indexes_even_on_failure(gitlab_auth):
    from unittest.mock import Mock

    from mira.platforms.gitlab import webhook

    payload = {
        "ref": "refs/heads/main",
        "before": "a" * 40,
        "after": "b" * 40,
        "project": {"path_with_namespace": "rhet/sub/app", "default_branch": "main"},
    }
    tasks = Mock()
    assert (
        await webhook.dispatch_gitlab_event("Push Hook", payload, gitlab_auth, BOT, tasks)
        == "processing"
    )
    fn, *args = tasks.add_task.call_args.args
    with (
        patch(
            "mira.platforms.gitlab.push_review.review_push",
            new=AsyncMock(side_effect=RuntimeError("review")),
        ) as review,
        patch.object(webhook, "handle_gitlab_push", new=AsyncMock()) as index,
    ):
        with pytest.raises(RuntimeError, match="review"):
            await fn(*args)
        review.assert_awaited_once()
        index.assert_awaited_once_with(payload, gitlab_auth, BOT)


def test_push_review_is_recorded_for_dashboard_charts(tmp_path, monkeypatch):
    from mira.dashboard import api
    from mira.dashboard.db import AppDatabase
    from mira.index.store import IndexStore
    from mira.models import ReviewComment, ReviewResult, Severity
    from mira.platforms.gitlab import push_review as push

    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr(api, "_app_db", db)
    head = "c" * 40
    payload = {
        "after": head,
        "user_username": "alice",
        "project": {
            "path_with_namespace": "rhet/sub/app",
            "web_url": "https://gitlab.example/rhet/sub/app",
        },
    }
    result = ReviewResult(
        summary="ok",
        reviewed_files=1,
        token_usage={"total_tokens": 42},
        comments=[
            ReviewComment(
                path="a.py",
                line=1,
                end_line=None,
                severity=Severity.BLOCKER,
                category="bug",
                title="t",
                body="b",
                confidence=0.9,
            )
        ],
    )
    diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"

    push._record_push_review(payload, branch="dev", diff=diff, result=result, duration_ms=5)

    assert db.get_repo("rhet/sub", "app", platform="gitlab") is not None
    store = IndexStore.open("rhet/sub", "app", platform="gitlab")
    try:
        [event] = store.list_review_events()
    finally:
        store.close()
    db.close()
    assert event.pr_title == f"dev push {head[:8]}"
    assert event.pr_url.endswith(f"/-/commit/{head}")
    assert (event.blockers, event.lines_changed, event.tokens_used) == (1, 4, 42)
    assert event.author == "alice" and event.categories == "bug"
