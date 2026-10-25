"""Database connection shared by local SQLite and hosted Supabase Postgres."""
import os
import re
import sqlite3
import urllib.parse
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


def normalize_database_url(url):
    """Remove the brackets left around a substituted Supabase password."""
    if not url.startswith(("postgres://", "postgresql://")) or "@" not in url:
        return url
    prefix, host_and_path = url.rsplit("@", 1)
    if ":" not in prefix:
        return url
    user_prefix, password = prefix.rsplit(":", 1)
    if password.startswith("[") and password.endswith("]"):
        return user_prefix + ":" + password[1:-1] + "@" + host_and_path
    return url


class PostgresConnection:
    def __init__(self, url):
        import psycopg
        from psycopg.rows import dict_row

        uri = urllib.parse.urlsplit(normalize_database_url(url))
        if uri.scheme not in ("postgres", "postgresql") or not uri.hostname or not uri.username or not uri.password:
            raise ValueError("DATABASE_URL must be a complete PostgreSQL connection URI")
        try:
            passwords = [urllib.parse.unquote(uri.password, errors="strict")]
        except UnicodeDecodeError:
            # Keep malformed %XX sequences literal while decoding valid ASCII escapes.
            partial = re.sub(r"%([0-7][0-9a-fA-F])", lambda match: chr(int(match.group(1), 16)), uri.password)
            passwords = [partial, uri.password] if partial != uri.password else [uri.password]
        for password in passwords:
            try:
                self.connection = psycopg.connect(
                    host=uri.hostname,
                    port=uri.port or 5432,
                    dbname=uri.path.lstrip("/") or "postgres",
                    user=urllib.parse.unquote(uri.username),
                    password=password,
                    autocommit=True,
                    prepare_threshold=None,
                    row_factory=dict_row,
                    connect_timeout=10,
                    sslmode="require",
                )
                break
            except psycopg.OperationalError as error:
                if password != passwords[-1] and "password authentication failed" in str(error):
                    continue
                if "password authentication failed" in str(error):
                    raise ValueError("Supabase rejected the database password in DATABASE_URL") from None
                raise ValueError("Could not connect to the Supabase database") from None
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
