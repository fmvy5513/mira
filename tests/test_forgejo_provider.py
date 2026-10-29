"""Tests for the Forgejo provider API (permissions and check runs).

Forgejo has no Checks API; check runs are adapted onto commit statuses.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mira.exceptions import ProviderError
from mira.models import PRInfo
from mira.providers.forgejo import ForgejoProvider


class _FakeResp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data
        self.text = ""

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kw):
        return self._handler(method, url, **kw)


def _patch(handler):
    return patch("mira.providers.forgejo.httpx.AsyncClient", lambda *a, **k: _FakeClient(handler))


class TestGetRepoPermission:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw", "normalized"),
        [
            ("admin", "write"),
            ("owner", "write"),
            ("write", "write"),
            ("read", "read"),
            ("none", "none"),
        ],
    )
    async def test_normalizes_permission_levels(self, raw, normalized):
        def handler(method, url, **kw):
            assert "/collaborators/alice/permission" in url
            return _FakeResp(json_data={"permission": raw})

        with _patch(handler):
            permission = await ForgejoProvider("tok").get_repo_permission(
                "acme", "widgets", "alice"
            )
        assert permission == normalized

    @pytest.mark.asyncio
    async def test_missing_collaborator_is_none(self):
        def handler(method, url, **kw):
            return _FakeResp(status=404, json_data={})

        with _patch(handler):
            permission = await ForgejoProvider("tok").get_repo_permission(
                "acme", "widgets", "alice"
            )
        assert permission == "none"

    @pytest.mark.asyncio
    async def test_unknown_permission_is_none(self):
        def handler(method, url, **kw):
            return _FakeResp(json_data={"permission": "mystery"})

        with _patch(handler):
            permission = await ForgejoProvider("tok").get_repo_permission(
                "acme", "widgets", "alice"
            )
        assert permission is None

    @pytest.mark.asyncio
    async def test_forbidden_lookup_explains_admin_requirement(self):
        def handler(method, url, **kw):
            return _FakeResp(status=403)

        with _patch(handler), pytest.raises(ProviderError, match="repository admin/owner"):
            await ForgejoProvider("tok").get_repo_permission("acme", "widgets", "alice")


class TestCheckRuns:
    """Forgejo has no Checks API: the provider adapts the check lifecycle onto
    commit statuses (same context per SHA → the status is replaced in place)."""

    def _pr_info(self, head_sha: str = "deadbeef") -> PRInfo:
        return PRInfo(
            title="t",
            description="d",
            base_branch="main",
            head_branch="h",
            url="https://codeberg.example.org/o/r/pulls/1",
            number=1,
            owner="o",
            repo="r",
            head_sha=head_sha,
        )

    @pytest.mark.asyncio
    async def test_start_posts_pending_status(self):
        provider = ForgejoProvider("tok")
        pr_info = self._pr_info()
        captured = {}

        def handler(method, url, **kw):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = kw.get("json")
            return _FakeResp(status=201, json_data={})

        with _patch(handler):
            run_id = await provider.start_check_run(
                pr_info,
                title="Code Review In Progress",
                summary="Mira is analyzing your code changes…",
                details_url=pr_info.url,
            )
        assert run_id == 0  # posted sentinel
        assert captured["method"] == "POST"
        assert captured["url"].endswith("/repos/o/r/statuses/deadbeef")
        assert captured["json"] == {
            "state": "pending",
            "context": "Mira",
            "description": "Mira is analyzing your code changes…",
            "target_url": "https://codeberg.example.org/o/r/pulls/1",
        }

    @pytest.mark.asyncio
    async def test_finish_posts_terminal_status(self):
        provider = ForgejoProvider("tok")
        pr_info = self._pr_info()
        captured = {}

        def handler(method, url, **kw):
            captured["url"] = url
            captured["json"] = kw.get("json")
            return _FakeResp(status=201, json_data={})

        with _patch(handler):
            await provider.finish_check_run(
                pr_info,
                0,
                "success",
                title="Code Review Complete",
                summary="Review finished successfully.",
            )
        assert captured["url"].endswith("/repos/o/r/statuses/deadbeef")
        assert captured["json"]["state"] == "success"
        assert captured["json"]["context"] == "Mira"

    @pytest.mark.asyncio
    async def test_finish_failure_and_neutral_states(self):
        provider = ForgejoProvider("tok")
        pr_info = self._pr_info()
        seen: list[dict] = []

        def handler(method, url, **kw):
            seen.append(kw.get("json") or {})
            return _FakeResp(status=201, json_data={})

        with _patch(handler):
            await provider.finish_check_run(pr_info, 0, "failure", summary="It failed")
            await provider.finish_check_run(pr_info, 0, "neutral", summary="Warnings")
        # Forgejo statuses have no neutral state: neutral renders as success.
        assert [j["state"] for j in seen] == ["failure", "success"]
        assert all(j["context"] == "Mira" for j in seen)
        assert seen[0]["description"] == "It failed"

    @pytest.mark.asyncio
    async def test_finish_without_run_id_does_nothing(self):
        provider = ForgejoProvider("tok")
        called = []

        def handler(method, url, **kw):
            called.append(url)
            return _FakeResp(status=201, json_data={})

        with _patch(handler):
            await provider.finish_check_run(self._pr_info(), None, "success")
        assert called == []

    @pytest.mark.asyncio
    async def test_forbidden_start_raises_provider_error(self):
        provider = ForgejoProvider("tok")

        def handler(method, url, **kw):
            return _FakeResp(status=403)

        with _patch(handler), pytest.raises(ProviderError, match="403"):
            await provider.start_check_run(self._pr_info())

    @pytest.mark.asyncio
    async def test_no_head_sha_performs_no_request(self):
        provider = ForgejoProvider("tok")
        called = []

        def handler(method, url, **kw):
            called.append(url)
            return _FakeResp(status=201, json_data={})

        with _patch(handler):
            assert await provider.start_check_run(self._pr_info(head_sha="")) is None
            assert called == []
