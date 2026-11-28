"""The admin register-and-index task reports how it ended, not a job that is gone."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mira.dashboard import api
from mira.dashboard.db import AppDatabase
from mira.dashboard.routers import admin
from mira.index import indexer
from mira.index.indexer import IndexingRecoveryPending
from mira.index.store import IndexStore


class _Body:
    project = "grp/proj"


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("MIRA_GITLAB_TOKEN", "synthetic-token")
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr(api, "_app_db", db)
    return db


async def _register_and_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    tasks: list[asyncio.Task] = []
    create_task = asyncio.create_task

    def capture(coro):
        task = create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(admin.asyncio, "create_task", capture)
    await admin._register_and_index_repo("gitlab", "MIRA_GITLAB_TOKEN", _Body())
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_unfinished_recovery_is_reported_as_retryable_failure(
    db: AppDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def recovery_pending(**kwargs):
        raise IndexingRecoveryPending("Index reconciliation incomplete: synthetic")

    monkeypatch.setattr(indexer, "index_repo", recovery_pending)

    await _register_and_wait(monkeypatch)

    record = db.get_repo("grp", "proj", platform="gitlab")
    assert record is not None
    assert record.status == "failed"
    assert "Retry" in (record.error or "")
    # The persisted run stays pending, so the retry still reconciles.
    store = IndexStore.open("grp", "proj", platform="gitlab", allow_fallback=False)
    try:
        assert store.claim_index_run().needs_reconciliation
    finally:
        store.close()
