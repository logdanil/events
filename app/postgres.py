"""PostgreSQL connections and lossless, verified bootstrap from SQLite."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

# Keep timestamp strings and numeric precision identical to the SQLite database.
UTC_NOW = "to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
_pool: ConnectionPool | None = None
_pool_url: str | None = None
_pool_lock = Lock()


class Row(Mapping[str, Any]):
    def __init__(self, names: Sequence[str], values: Sequence[Any]):
        self._data = dict(zip(names, values))
        self._values = values

    def __getitem__(self, key: str | int) -> Any:
        return self._values[key] if isinstance(key, int) else self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def row_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


def configure_connection(conn: psycopg.Connection) -> None:
    conn.execute("SET TIME ZONE 'UTC'")
    conn.commit()


def statement(query: str) -> str:
    """Only adapt bind markers and UTC timestamps, never parameter values."""
    parts = re.split(r"('(?:[^']|'')*')", query)
    for i in range(0, len(parts), 2):
        parts[i] = parts[i].replace("%", "%%").replace("?", "%s")
        parts[i] = re.sub(r"\bCURRENT_TIMESTAMP\b", UTC_NOW, parts[i])
    for i in range(1, len(parts), 2):
        parts[i] = parts[i].replace("%", "%%")
    return "".join(parts)


class Connection:
    def __init__(self, raw: psycopg.Connection):
        self.raw = raw

    def execute(self, query: str, params: Sequence[Any] = ()):
        return self.raw.execute(statement(query), params)

    def executemany(self, query: str, params: Sequence[Sequence[Any]]):
        cursor = self.raw.cursor()
        cursor.executemany(statement(query), params)
        return cursor

    def executescript(self, script: str) -> None:
        for query in script.split(";"):
            if query.strip():
                self.execute(query)


def pool(url: str) -> ConnectionPool:
    global _pool, _pool_url
    with _pool_lock:
        if _pool is None or _pool_url != url:
            if _pool is not None:
                _pool.close()
            _pool = ConnectionPool(
                url, min_size=1, max_size=8, timeout=30, open=False,
                kwargs={"row_factory": row_factory, "connect_timeout": 15},
                configure=configure_connection,
            )
            _pool.open(wait=True)
            _pool_url = url
        return _pool


def close_pool() -> None:
    global _pool, _pool_url
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _pool_url = None


def schema_for_postgres(schema: str) -> str:
    schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    schema = schema.replace("chat_id INTEGER", "chat_id BIGINT")
    schema = schema.replace("source_id INTEGER", "source_id BIGINT")
    schema = schema.replace("TEXT NOT NULL COLLATE NOCASE", "CITEXT NOT NULL")
    schema = schema.replace("TEXT PRIMARY KEY COLLATE NOCASE", "CITEXT PRIMARY KEY")
    schema = re.sub(r"\bREAL\b", "DOUBLE PRECISION", schema)
    schema = schema.replace("CREATE TABLE IF NOT EXISTS events (",
                            "CREATE TABLE IF NOT EXISTS events (\n  rowid BIGSERIAL UNIQUE,")
    return schema


def columns(conn: Connection, table: str) -> set[str]:
    return {row["column_name"] for row in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name=?", (table,))}


def _digest(rows) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        encoded = json.dumps(list(row), ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.hexdigest()


def migrate_sqlite(conn: Connection, path: Path) -> dict[str, int]:
    """Copy and compare every value in one PostgreSQL transaction before cutover.

    Call only after the old worker has stopped writing. The original SQLite and
    a consistent backup remain untouched for rollback; a nonempty target fails closed.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sqlite_migrations ("
        "source_path TEXT PRIMARY KEY, migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "backup_path TEXT NOT NULL, manifest_json TEXT NOT NULL)")
    if conn.execute("SELECT 1 FROM sqlite_migrations").fetchone():
        return {}
    if not path.exists():
        raise RuntimeError(
            f"SQLite migration source does not exist: {path}. "
            "Restore the original volume before enabling DATABASE_URL.")
    source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY rowid")]
        if "settings" not in tables:
            raise RuntimeError("SQLite source is not an initialized monitor database")
        for table in tables:
            if not conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_name=?", (table,)).fetchone():
                raise RuntimeError(f"Unknown SQLite table: {table}; migration would lose data")
            if conn.raw.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(
                    sql.Identifier(table))).fetchone():
                raise RuntimeError(f"PostgreSQL table {table} is not empty; refusing to overwrite")
        backup = path.with_name(path.name + ".pre-postgres-" +
                                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        with backup.open("xb"):
            pass
        backup.chmod(0o600)
        snapshot = sqlite3.connect(backup)
        try:
            source.backup(snapshot)
            if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite backup integrity check failed")
            if snapshot.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("SQLite backup contains foreign key violations")
            manifest = {}
            for table in tables:
                names = [r[1] for r in snapshot.execute(f'PRAGMA table_info("{table}")')]
                if table == "events":
                    names = ["rowid", *names]
                unknown = set(names) - columns(conn, table)
                if unknown:
                    raise RuntimeError(f"Unmapped columns in {table}: {sorted(unknown)}")
                selected = ", ".join(f'"{name}"' for name in names)
                insert = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(map(sql.Identifier, names)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in names),
                )
                cursor = snapshot.execute(f'SELECT {selected} FROM "{table}" ORDER BY rowid')
                with conn.raw.cursor() as target:
                    while batch := cursor.fetchmany(500):
                        target.executemany(insert, batch)
                # Compare all columns, including nullable fields, JSON, timestamps and IDs.
                info = list(snapshot.execute(f'PRAGMA table_info("{table}")'))
                types = {r[1]: r[2] for r in info}
                types["rowid"] = "INTEGER"
                order = ["rowid"] if table == "events" else (
                    [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]] or names)
                source_order = ", ".join(
                    f'"{name}" COLLATE BINARY' if types[name] == "TEXT" else f'"{name}"'
                    for name in order)
                source_rows = snapshot.execute(
                    f'SELECT {selected} FROM "{table}" ORDER BY {source_order}')
                target_order = sql.SQL(", ").join(
                    sql.SQL("{}::text COLLATE \"C\" NULLS FIRST").format(sql.Identifier(name))
                    if types[name] == "TEXT"
                    else sql.SQL("{} NULLS FIRST").format(sql.Identifier(name))
                    for name in order)
                target_query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                    sql.SQL(", ").join(map(sql.Identifier, names)),
                    sql.Identifier(table), target_order)
                with conn.raw.cursor(row_factory=psycopg.rows.tuple_row) as target:
                    target.execute(target_query)
                    expected = _digest(source_rows)
                    actual = _digest(target)
                if expected != actual:
                    raise RuntimeError(f"Data verification failed for {table}: {expected[0]} / {actual[0]}")
                manifest[table] = {"rows": expected[0], "sha256": expected[1]}
                serial = "rowid" if table == "events" else "id"
                if serial in names:
                    sequence = conn.execute("SELECT pg_get_serial_sequence(?, ?)",
                                            (table, serial)).fetchone()[0]
                    if sequence:
                        highest = snapshot.execute(
                            f'SELECT COALESCE(MAX("{serial}"), 0) FROM "{table}"').fetchone()[0]
                        if table != "events":
                            old_sequence = snapshot.execute(
                                "SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
                            if old_sequence:
                                highest = max(highest, old_sequence[0])
                        conn.execute("SELECT setval(?::regclass, ?, ?)",
                                     (sequence, max(highest, 1), highest > 0))
                log.info("SQLite → PostgreSQL: %s, %d rows verified", table, expected[0])
            conn.execute(
                "INSERT INTO sqlite_migrations(source_path,backup_path,manifest_json) VALUES(?,?,?)",
                (str(path.resolve()), str(backup), json.dumps(manifest, sort_keys=True)))
            log.info("SQLite migration verified; rollback backup: %s", backup)
            return {table: data["rows"] for table, data in manifest.items()}
        finally:
            snapshot.close()
    finally:
        source.close()
