"""SQLite store for the schema registry: installed packages and an audit trail (from ASTHRA, trimmed:
Procedure Studio keeps documents and revisions in its own run folders)."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS: list[str] = [
    """
    CREATE TABLE schema_package (
        id TEXT PRIMARY KEY,                 -- e.g. s1000d/4.2/acme-s1000d-4.2
        standard TEXT NOT NULL,
        issue TEXT NOT NULL,
        name TEXT NOT NULL,
        provenance TEXT NOT NULL,            -- official | oem | synthetic
        install_path TEXT NOT NULL,
        checksum TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        installed_at TEXT NOT NULL
    );
    CREATE INDEX ix_pkg_standard ON schema_package(standard, issue);
    CREATE TABLE audit_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        subject TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}'
    );
    """,
]


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            self._conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
            done = {r[0] for r in self._conn.execute("SELECT version FROM schema_migrations")}
            for i, sql in enumerate(MIGRATIONS, start=1):
                if i in done:
                    continue
                # executescript manages its own transaction; keep the migration
                # and its version record atomic inside one script.
                script = f"BEGIN;\n{sql}\nINSERT INTO schema_migrations(version) VALUES ({i});\nCOMMIT;"
                try:
                    self._conn.executescript(script)
                except Exception:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        self._conn.close()
