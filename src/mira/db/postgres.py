"""PostgreSQL connection helpers — stale-connection recovery."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)


def connect(url: str) -> Any:
    import psycopg

    return psycopg.connect(url, autocommit=True)


def reconnect(url: str, conn: Any | None) -> Any:
    """Close a stale handle and open a fresh connection."""
    logger.warning("PostgreSQL connection stale, reconnecting")
    if conn is not None:
        with suppress(Exception):
            conn.close()
    return connect(url)


def is_closed(conn: Any | None) -> bool:
    """True when there is no handle or psycopg has marked it closed.

    psycopg sets ``closed`` once the socket is gone (server restart, crash
    recovery, idle drop), and from then on ``conn.cursor()`` itself raises —
    before any retry wrapper can see the error.
    """
    return conn is None or bool(getattr(conn, "closed", False))


def open_cursor(conn: Any | None, on_reconnect: Callable[[], Any]) -> ReconnectingCursor:
    """Open a ``ReconnectingCursor``, replacing a closed handle first.

    Creating the cursor is part of the retry: a handle left closed by an
    earlier failure (e.g. a reconnect attempted while the server was still in
    crash recovery) is replaced instead of failing every later request until
    the process restarts. No liveness query is sent.
    """
    import psycopg

    if conn is None or is_closed(conn):
        conn = on_reconnect()
    try:
        cur = conn.cursor()
    except psycopg.OperationalError:
        cur = on_reconnect().cursor()
    return ReconnectingCursor(cur, on_reconnect=on_reconnect)


class ReconnectingCursor:
    """Cursor wrapper that reconnects once when the DB raises OperationalError."""

    def __init__(
        self,
        cur: Any,
        *,
        on_reconnect: Callable[[], Any],
    ) -> None:
        self._cur = cur
        self._on_reconnect = on_reconnect

    def _retry(self, method: str, *args: Any, **kwargs: Any) -> Any:
        import psycopg

        try:
            return getattr(self._cur, method)(*args, **kwargs)
        except psycopg.OperationalError:
            self._cur = self._on_reconnect().cursor()
            return getattr(self._cur, method)(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._retry("execute", *args, **kwargs)

    def fetchone(self, *args: Any, **kwargs: Any) -> Any:
        return self._cur.fetchone(*args, **kwargs)

    def fetchall(self, *args: Any, **kwargs: Any) -> Any:
        return self._cur.fetchall(*args, **kwargs)

    def fetchmany(self, *args: Any, **kwargs: Any) -> Any:
        return self._cur.fetchmany(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> Any:
        return self._retry("executemany", *args, **kwargs)

    def close(self) -> None:
        self._cur.close()

    def __enter__(self) -> ReconnectingCursor:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cur, name)
