"""SQLite storage of event subscriptions, in one file that survives restarts and deploys.

The file holds signing secrets, so it is readable by the app user only.

Every operation opens its own short connection. That keeps the store free of thread affinity,
and the app and the `mattermost-mcp-events` command can share the file, which is what WAL mode
and the busy timeout are for. The queries are tiny, so they run directly on the event loop.
"""

import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_DATA_DIR = ".data"
FILE_NAME = "events.sqlite3"
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE subscriptions (
    id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    event_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    callback_url TEXT NOT NULL,
    secret TEXT NOT NULL,
    previous_secret TEXT,
    previous_secret_until REAL,
    created_at REAL NOT NULL,
    refreshed_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    verified_at REAL,
    last_delivery_at REAL,
    last_delivery_status TEXT
)
"""


class StoreError(Exception):
    """The database cannot be used. The message says which file and why."""


@dataclass(frozen=True)
class Subscription:
    """One row. Times are Unix seconds. repr() leaves out the secrets and the full URL."""

    id: str
    principal: str
    event_name: str
    arguments: str
    callback_url: str = field(repr=False)
    secret: str = field(repr=False)
    previous_secret: str | None = field(repr=False)
    previous_secret_until: float | None
    created_at: float
    refreshed_at: float
    expires_at: float
    verified_at: float | None
    last_delivery_at: float | None
    last_delivery_status: str | None


COLUMNS = [column.name for column in fields(Subscription)]
SELECT = f"SELECT {', '.join(COLUMNS)} FROM subscriptions"  # noqa: S608 - fixed column names
UPSERT = (
    f"INSERT OR REPLACE INTO subscriptions ({', '.join(COLUMNS)}) "  # noqa: S608 - fixed names
    f"VALUES ({', '.join('?' for _ in COLUMNS)})"
)


def database_path() -> Path:
    return Path(os.environ.get("MCP_DATA_DIR") or DEFAULT_DATA_DIR) / FILE_NAME


class Store:
    """Open the database, creating it if needed, or raise StoreError so startup can fail fast."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Created by hand, because SQLite would use the default mode and leave a moment in
            # which others could read the file. SQLite gives its -wal and -shm files this mode.
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            self._initialise()
        except (OSError, sqlite3.Error) as error:
            raise StoreError(f"cannot use the database {path}: {error}") from error

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: no implicit transactions, the code below says BEGIN itself.
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        connection = self._connect()
        try:
            # IMMEDIATE takes the write lock now, so a check and the write after it are atomic.
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    def _initialise(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        finally:
            connection.close()
        # Also the writability check: BEGIN IMMEDIATE fails on a read-only file or directory.
        with self._transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                connection.execute(SCHEMA)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise sqlite3.DatabaseError(f"unknown schema version {version}")

    def get_active(self, subscription_id: str, now: float) -> Subscription | None:
        connection = self._connect()
        try:
            row = connection.execute(
                f"{SELECT} WHERE id = ? AND expires_at > ?", (subscription_id, now)
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else Subscription(*row)

    def list_active(self, now: float) -> list[Subscription]:
        connection = self._connect()
        try:
            rows = connection.execute(
                f"{SELECT} WHERE expires_at > ? ORDER BY created_at, id", (now,)
            ).fetchall()
        finally:
            connection.close()
        return [Subscription(*row) for row in rows]

    def delete_expired(self, now: float) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM subscriptions WHERE expires_at <= ?", (now,))

    def save(self, subscription: Subscription, now: float, max_active: int) -> bool:
        """Insert or replace a row, or return False if the cap leaves no room for it.

        The caller checks the cap too, before it sends a verification request. This is the check
        that counts: verification takes seconds, so concurrent subscribes all pass the first one.
        The row itself is not counted, so a refresh at the cap still works.
        """
        with self._transaction() as connection:
            connection.execute("DELETE FROM subscriptions WHERE expires_at <= ?", (now,))
            others = connection.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE id != ?", (subscription.id,)
            ).fetchone()[0]
            if others >= max_active:
                return False
            connection.execute(UPSERT, tuple(getattr(subscription, column) for column in COLUMNS))
        return True

    def delete(self, subscription_id: str) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM subscriptions WHERE id = ?", (subscription_id,)
            )
            return cursor.rowcount > 0

    def delete_all(self) -> int:
        with self._transaction() as connection:
            return connection.execute("DELETE FROM subscriptions").rowcount

    def record_delivery(self, subscription_id: str, now: float, status: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE subscriptions SET last_delivery_at = ?, last_delivery_status = ? "
                "WHERE id = ?",
                (now, status, subscription_id),
            )
