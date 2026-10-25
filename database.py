"""Database connection shared by local SQLite and hosted Supabase Postgres."""
import os
import sqlite3
from pathlib import Path


class Row(dict):
    """Keep SQLite-style numeric and named column access for existing queries."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class Result:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self):
        value = self.cursor.fetchone()
        return Row(value) if value is not None else None

    def fetchall(self):
        return [Row(value) for value in self.cursor.fetchall()]

    def __iter__(self):
        return (Row(value) for value in self.cursor)


class PostgresConnection:
    def __init__(self, url):
        import psycopg
        from psycopg.rows import dict_row

        self.connection = psycopg.connect(
            url,
            autocommit=True,
            prepare_threshold=None,
            row_factory=dict_row,
            connect_timeout=10,
            sslmode="require",
        )
        self.in_transaction = False

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        try:
            if self.in_transaction:
                self.connection.execute("ROLLBACK" if error_type else "COMMIT")
                self.in_transaction = False
        finally:
            self.connection.close()

    def commit(self):
        if self.in_transaction:
            self.connection.execute("COMMIT")
            self.in_transaction = False

    def execute(self, statement, values=()):
        sql = statement.strip()
        if sql == "BEGIN IMMEDIATE":
            self.connection.execute("BEGIN")
            self.in_transaction = True
            # Serialize all booking and schedule changes across Vercel instances.
            self.connection.execute("SELECT pg_advisory_xact_lock(683179042)")
            return None
        if sql == "SELECT last_insert_rowid()":
            sql = "SELECT LASTVAL() AS id"
        elif sql.startswith("INSERT OR IGNORE INTO "):
            sql = sql.replace("INSERT OR IGNORE INTO ", "INSERT INTO ", 1) + " ON CONFLICT DO NOTHING"
        sql = sql.replace("?", "%s")
        return Result(self.connection.execute(sql, values))


class SqliteConnection:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=15, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=15000")

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        try:
            self.connection.__exit__(error_type, error, traceback)
        finally:
            self.connection.close()

    def execute(self, statement, values=()):
        return self.connection.execute(statement, values)

    def executescript(self, script):
        return self.connection.executescript(script)

    def commit(self):
        return self.connection.commit()


def connect():
    url = os.environ.get("DATABASE_URL")
    if url:
        return PostgresConnection(url)
    path = os.environ.get("DATABASE_PATH", str(Path(__file__).parent / "bookings.sqlite3"))
    return SqliteConnection(path)
