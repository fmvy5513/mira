"""The manifest-identity rollback script must leave data the previous release
can write against: one row per (owner, repo, name, kind, file_path)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rollback_manifest_identity.sql"
LEGACY_KEY = "owner, repo, name, kind, file_path"


def _statements() -> list[str]:
    body = "\n".join(
        line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("--")
    )
    return [s.strip() for s in body.split(";") if s.strip()]


def _dedupe_statement() -> str:
    (delete,) = [s for s in _statements() if s.startswith("DELETE")]
    return delete


def test_script_restores_exactly_the_legacy_unique_key() -> None:
    (alter,) = [s for s in _statements() if s.startswith("ALTER TABLE")]
    assert re.search(r"UNIQUE\s*\(\s*" + re.escape(LEGACY_KEY) + r"\s*\)", alter)
    statements = _statements()
    assert statements[0] == "BEGIN" and statements[-1] == "COMMIT"
    assert statements.index(_dedupe_statement()) < statements.index(alter)


def test_dedupe_keeps_one_row_per_legacy_key_preferring_production_then_newest() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE package_manifests (id INTEGER PRIMARY KEY, owner TEXT, repo TEXT, "
        "name TEXT, kind TEXT, version TEXT, file_path TEXT, is_dev INTEGER, "
        "updated_at REAL, scan_eligible INTEGER)"
    )
    rows = [
        # Two versions of one package in one file: production row wins over dev.
        (1, "a", "r", "shared", "npm", "1.0.0", "package-lock.json", 1, 9.0, 1),
        (2, "a", "r", "shared", "npm", "2.0.0", "package-lock.json", 0, 1.0, 1),
        # Same production flag: the newest write wins.
        (3, "a", "r", "left", "npm", "1.0.0", "web/package-lock.json", 0, 1.0, 1),
        (4, "a", "r", "left", "npm", "1.1.0", "web/package-lock.json", 0, 5.0, 1),
        # Already unique: untouched.
        (5, "a", "r", "solo", "pypi", "3.0", "requirements.txt", 0, 1.0, 1),
    ]
    conn.executemany("INSERT INTO package_manifests VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    conn.execute(_dedupe_statement())

    kept = conn.execute("SELECT id FROM package_manifests ORDER BY id").fetchall()
    assert [k for (k,) in kept] == [2, 4, 5]
    conn.execute(f"CREATE UNIQUE INDEX legacy ON package_manifests ({LEGACY_KEY})")
