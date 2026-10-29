"""Dashboard index trigger resolves GitHub App tokens from the stored installation."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from mira.dashboard import api
from mira.dashboard.db import AppDatabase
from mira.dashboard.routers import repos


@pytest.fixture
def app_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[AppDatabase, None, None]:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("MIRA_GITHUB_APP_ID", "123")
    monkeypatch.setenv("MIRA_GITHUB_PRIVATE_KEY", "dummy")
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr(api, "_app_db", db)
    yield db
    db.close()


class _FakeAuth:
    """Stand-in for GitHubAppAuth with scripted installation data."""

    installations: list[dict[str, object]] = []
    installation_repos: dict[int, list[dict[str, object]]] = {}
    token_failures: set[int] = set()
    calls: list[str] = []

    def __init__(self, app_id: str, private_key: str) -> None:
        pass

    async def get_installation_token(self, installation_id: int) -> str:
        self.calls.append(f"token:{installation_id}")
        if installation_id in self.token_failures:
            raise RuntimeError(f"HTTP 404 for {installation_id}")
        return f"tok-{installation_id}"

    async def list_installations(self) -> list[dict[str, object]]:
        self.calls.append("list_installations")
        return self.installations

    async def list_installation_repos(self, installation_id: int) -> list[dict[str, object]]:
        self.calls.append(f"list_repos:{installation_id}")
        return self.installation_repos.get(installation_id, [])


@pytest.fixture
def fake_auth(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAuth]:
    import mira.platforms.github.auth as auth_mod

    _FakeAuth.installations = []
    _FakeAuth.installation_repos = {}
    _FakeAuth.token_failures = set()
    _FakeAuth.calls = []
    monkeypatch.setattr(auth_mod, "GitHubAppAuth", _FakeAuth)
    return _FakeAuth


async def test_github_token_env_wins(app_db: AppDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "pat")
    token, err = await repos._resolve_github_token("o/r", [])
    assert (token, err) == ("pat", "")


async def test_stored_installation_is_used_without_scanning(
    app_db: AppDatabase, fake_auth: type[_FakeAuth]
) -> None:
    record = app_db.register_repo("o", "r", installation_id=42)
    token, err = await repos._resolve_github_token("o/r", [record])
    assert (token, err) == ("tok-42", "")
    assert fake_auth.calls == ["token:42"]


async def test_falls_back_to_scan_and_persists_installation(
    app_db: AppDatabase, fake_auth: type[_FakeAuth]
) -> None:
    record = app_db.register_repo("o", "r", installation_id=7)
    fake_auth.token_failures = {7}
    fake_auth.installations = [{"id": 1}, {"id": 2}]
    fake_auth.installation_repos = {1: [{"full_name": "x/y"}], 2: [{"full_name": "o/r"}]}

    token, err = await repos._resolve_github_token("o/r", [record])
    assert (token, err) == ("tok-2", "")
    assert app_db.get_repo("o", "r").installation_id == 2


async def test_missing_installation_reports_clear_error(
    app_db: AppDatabase, fake_auth: type[_FakeAuth]
) -> None:
    fake_auth.installations = [{"id": 1}]
    fake_auth.installation_repos = {1: [{"full_name": "x/y"}]}
    token, err = await repos._resolve_github_token("o/r", [])
    assert token == ""
    assert "not installed on o/r" in err


async def test_no_installations_reports_credentials_hint(
    app_db: AppDatabase, fake_auth: type[_FakeAuth]
) -> None:
    token, err = await repos._resolve_github_token("o/r", [])
    assert token == ""
    assert "MIRA_GITHUB_APP_ID" in err
