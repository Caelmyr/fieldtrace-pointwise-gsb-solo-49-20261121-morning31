"""Atomic local SQLite records with content-addressed image and report blobs."""

from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import secrets
import sqlite3

from .contracts import Conflict, Missing, now_text

DOCUMENT_TABLES = {
    "stations", "deployments", "frames", "events", "marks", "outages", "glances",
    "models", "measures", "identities", "portraits", "paths", "packets", "samples",
    "reports", "calibrations",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS stations(
    id TEXT PRIMARY KEY, version INT NOT NULL, name TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS station_names ON stations(lower(name));
CREATE TABLE IF NOT EXISTS deployments(
    id TEXT PRIMARY KEY, station_id TEXT NOT NULL REFERENCES stations(id),
    version INT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS deployments_station ON deployments(station_id, created_at);
CREATE TABLE IF NOT EXISTS frames(
    id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL REFERENCES deployments(id),
    version INT NOT NULL, name TEXT NOT NULL, stamp_utc TEXT,
    illumination TEXT NOT NULL, review TEXT NOT NULL, origin TEXT NOT NULL,
    digest TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS frames_sequence ON frames(deployment_id, stamp_utc, id);
CREATE INDEX IF NOT EXISTS frames_review ON frames(review, illumination, stamp_utc);
CREATE UNIQUE INDEX IF NOT EXISTS frames_same_source ON frames(deployment_id, digest);
CREATE TABLE IF NOT EXISTS events(
    id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL REFERENCES deployments(id),
    version INT NOT NULL, name TEXT NOT NULL, stamp_utc TEXT,
    state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_sequence ON events(deployment_id, stamp_utc);
CREATE TABLE IF NOT EXISTS event_frames(
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    frame_id TEXT NOT NULL UNIQUE REFERENCES frames(id),
    ordinal INT NOT NULL, PRIMARY KEY(event_id, frame_id), UNIQUE(event_id, ordinal)
);
CREATE TABLE IF NOT EXISTS marks(
    id TEXT PRIMARY KEY, frame_id TEXT NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    version INT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS marks_frame ON marks(frame_id, kind);
CREATE TABLE IF NOT EXISTS outages(
    id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL REFERENCES deployments(id),
    version INT NOT NULL, name TEXT NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outages_time ON outages(deployment_id, start_utc, end_utc);
CREATE TABLE IF NOT EXISTS glances(
    id TEXT PRIMARY KEY, frame_id TEXT NOT NULL REFERENCES frames(id),
    version INT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS glances_frame ON glances(frame_id, kind, created_at);
CREATE TABLE IF NOT EXISTS models(
    id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL REFERENCES deployments(id),
    frame_id TEXT NOT NULL REFERENCES frames(id), version INT NOT NULL, name TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS measures(
    id TEXT PRIMARY KEY, frame_id TEXT NOT NULL REFERENCES frames(id), model_id TEXT NOT NULL REFERENCES models(id),
    version INT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identities(
    id TEXT PRIMARY KEY, version INT NOT NULL, name TEXT NOT NULL, taxon TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS identities_taxon ON identities(taxon, name);
CREATE TABLE IF NOT EXISTS portraits(
    id TEXT PRIMARY KEY, identity_id TEXT NOT NULL REFERENCES identities(id), frame_id TEXT NOT NULL REFERENCES frames(id),
    version INT NOT NULL, name TEXT NOT NULL, pose TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS portraits_subject ON portraits(identity_id, pose);
CREATE TABLE IF NOT EXISTS paths(
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id),
    version INT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packets(
    id TEXT PRIMARY KEY, version INT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples(
    id TEXT PRIMARY KEY, version INT NOT NULL, name TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports(
    id TEXT PRIMARY KEY, version INT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibrations(
    id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL REFERENCES deployments(id),
    version INT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calibrations_deployment ON calibrations(deployment_id, created_at);
CREATE TABLE IF NOT EXISTS assets(
    owner_id TEXT NOT NULL, label TEXT NOT NULL, digest TEXT NOT NULL, media_type TEXT NOT NULL,
    bytes INT NOT NULL, PRIMARY KEY(owner_id, label)
);
CREATE TABLE IF NOT EXISTS changes(
    serial INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, owner_id TEXT NOT NULL,
    version INT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS changes_record ON changes(owner_id, serial DESC);
CREATE TABLE IF NOT EXISTS preferences(
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""
INDEX_COLUMNS = {
    "stations": {"name"},
    "deployments": {"station_id", "name"},
    "frames": {"deployment_id", "name", "stamp_utc", "illumination", "review", "origin", "digest"},
    "events": {"deployment_id", "name", "stamp_utc", "state"},
    "marks": {"frame_id", "name", "kind"},
    "outages": {"deployment_id", "name", "start_utc", "end_utc"},
    "glances": {"frame_id", "kind", "name"},
    "models": {"deployment_id", "frame_id", "name"},
    "measures": {"frame_id", "model_id", "name"},
    "identities": {"name", "taxon"},
    "portraits": {"identity_id", "frame_id", "name", "pose"},
    "paths": {"event_id", "name"},
    "packets": {"kind", "name"},
    "samples": {"name"},
    "reports": {"kind", "name"},
    "calibrations": {"deployment_id", "name", "state"},
}


def unique_id():
    return secrets.token_hex(12)


def encoded(document):
    return json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def decoded(row):
    return json.loads(row["doc"]) if row is not None else None


def table_name(table):
    if table not in DOCUMENT_TABLES:
        raise ValueError(f"Unknown record category: {table}")
    return table


def projected_fields(table, doc):
    values = sorted(INDEX_COLUMNS[table])
    return values, [doc[name] for name in values]


class BlobStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, digest):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise Missing("file")
        return self.root / digest[:2] / digest

    def put(self, binary):
        digest = hashlib.sha256(binary).hexdigest()
        final = self.path(digest)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.is_file():
            return digest
        temporary = final.with_suffix(f".{secrets.token_hex(6)}.partial")
        try:
            with temporary.open("xb") as handle:
                handle.write(binary)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, final)
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def read(self, digest):
        try:
            return self.path(digest).read_bytes()
        except FileNotFoundError as exc:
            raise Missing("file") from exc


class Access:
    def __init__(self, connection, blob_store):
        self.conn = connection
        self.blobs = blob_store

    def one(self, table, key, *, optional=False):
        row = self.conn.execute(f"SELECT doc FROM {table_name(table)} WHERE id=?", (key,)).fetchone()
        if row is None and not optional:
            raise Missing(table.rstrip("s"))
        return decoded(row)

    def matching(self, table, *, where=(), order=("created_at", "DESC"), limit=None, offset=0):
        columns = INDEX_COLUMNS[table_name(table)] | {"id", "created_at", "updated_at"}
        terms, args = [], []
        for column, operator, value in where:
            if column not in columns or operator not in {"=", "!=", ">=", ">", "<", "<=", "IS", "IS NOT"}:
                raise ValueError("Unsupported field filter")
            terms.append(f"{column} {operator} ?")
            args.append(value)
        column, direction = order
        if column not in columns or direction not in {"ASC", "DESC"}:
            raise ValueError("Unsupported ordering")
        clause = " WHERE " + " AND ".join(terms) if terms else ""
        query = f"SELECT doc FROM {table}{clause} ORDER BY {column} {direction}, id {direction}"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            args.extend((limit, offset))
        return [decoded(row) for row in self.conn.execute(query, args)]

    def documents(self, table, keys):
        table_name(table)
        if not keys:
            return []
        slots = ",".join("?" for _ in keys)
        result = self.conn.execute(f"SELECT id,doc FROM {table} WHERE id IN ({slots})", tuple(keys))
        by_key = {row["id"]: decoded(row) for row in result}
        try:
            return [by_key[key] for key in keys]
        except KeyError as exc:
            raise Missing(table.rstrip("s")) from exc

    def count(self, table, *, equal=None):
        equal = equal or {}
        columns = INDEX_COLUMNS[table_name(table)]
        if not set(equal).issubset(columns):
            raise ValueError("Unsupported count filter")
        where = " WHERE " + " AND ".join(f"{k}=?" for k in equal) if equal else ""
        return self.conn.execute(f"SELECT count(*) FROM {table}{where}", tuple(equal.values())).fetchone()[0]

    def append(self, table, values, *, action="created"):
        table_name(table)
        timestamp = now_text()
        document = dict(values)
        document.update(id=unique_id(), version=1, created_at=timestamp, updated_at=timestamp)
        if "name" not in document:
            raise ValueError("Missing record name")
        fields, args = projected_fields(table, document)
        fields.extend(("id", "version", "created_at", "updated_at", "doc"))
        args.extend((document["id"], 1, timestamp, timestamp, encoded(document)))
        try:
            slots = ",".join("?" for _ in fields)
            self.conn.execute(f"INSERT INTO {table}({','.join(fields)}) VALUES ({slots})", args)
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"A {table.rstrip('s')} with the same identifying values already exists.") from exc
        self.log(table, document, action)
        return document

    def replace(self, table, key, expected, transform, *, action="modified"):
        table_name(table)
        original = self.one(table, key)
        if original["version"] != expected:
            raise Conflict("The record changed while it was open. Reload its latest version.",
                           facts={"version": original["version"]})
        updated = transform(dict(original))
        if updated["id"] != key:
            raise ValueError("Record identifiers cannot be modified")
        updated["version"] = original["version"] + 1
        updated["updated_at"] = now_text()
        fields, args = projected_fields(table, updated)
        fields.extend(("version", "updated_at", "doc"))
        args.extend((updated["version"], updated["updated_at"], encoded(updated), key, expected))
        setters = ",".join(f"{column}=?" for column in fields)
        try:
            changed = self.conn.execute(f"UPDATE {table} SET {setters} WHERE id=? AND version=?", args)
        except sqlite3.IntegrityError as exc:
            raise Conflict("The new values cannot coexist with an existing record.") from exc
        if changed.rowcount != 1:
            raise Conflict("The record changed while it was open.")
        self.log(table, updated, action)
        return updated

    def remove(self, table, key, expected):
        table_name(table)
        document = self.one(table, key)
        if document["version"] != expected:
            raise Conflict("Use the latest version before removing this record.")
        try:
            self.conn.execute(f"DELETE FROM {table} WHERE id=?", (key,))
        except sqlite3.IntegrityError as exc:
            raise Conflict("This record still has dependent survey evidence.") from exc
        self.conn.execute("DELETE FROM assets WHERE owner_id=?", (key,))
        self.log(table, document, "removed")

    def log(self, table, doc, action):
        self.conn.execute(
            "INSERT INTO changes(table_name,owner_id,version,action,created_at,doc) VALUES (?,?,?,?,?,?)",
            (table, doc["id"], doc["version"], action, now_text(), encoded(doc)),
        )

    def history(self, key, *, limit=50):
        source = self.conn.execute(
            "SELECT table_name,version,action,created_at,doc FROM changes WHERE owner_id=? ORDER BY serial DESC LIMIT ?",
            (key, limit),
        )
        return [
            {"type": row["table_name"], "version": row["version"], "action": row["action"],
             "at": row["created_at"], "snapshot": decoded(row)}
            for row in source
        ]

    def asset(self, owner, label):
        row = self.conn.execute("SELECT * FROM assets WHERE owner_id=? AND label=?", (owner, label)).fetchone()
        if row is None:
            raise Missing("image or report file")
        return dict(row)

    def add_asset(self, owner, label, data, media_type):
        digest = self.blobs.put(data)
        items = (owner, label, digest, media_type, len(data))
        self.conn.execute(
            "INSERT INTO assets(owner_id,label,digest,media_type,bytes) VALUES (?,?,?,?,?) "
            "ON CONFLICT(owner_id,label) DO UPDATE SET digest=excluded.digest,media_type=excluded.media_type,bytes=excluded.bytes",
            items,
        )
        return self.asset(owner, label)

    def assets(self, owner):
        rows = self.conn.execute(
            "SELECT label,digest,media_type,bytes FROM assets WHERE owner_id=? ORDER BY label", (owner,),
        )
        return [dict(row) for row in rows]

    def event_members(self, event):
        rows = self.conn.execute(
            "SELECT frames.doc FROM event_frames JOIN frames ON frame_id=frames.id "
            "WHERE event_id=? ORDER BY ordinal", (event,),
        )
        return [decoded(row) for row in rows]

    def membership(self, frame_ids):
        if not frame_ids:
            return {}
        slots = ",".join("?" for _ in frame_ids)
        rows = self.conn.execute(f"SELECT frame_id,event_id FROM event_frames WHERE frame_id IN ({slots})", frame_ids)
        return {row["frame_id"]: row["event_id"] for row in rows}

    def set_event_members(self, event, frame_ids):
        self.conn.execute("DELETE FROM event_frames WHERE event_id=?", (event,))
        try:
            self.conn.executemany(
                "INSERT INTO event_frames(event_id,frame_id,ordinal) VALUES (?,?,?)",
                ((event, key, index) for index, key in enumerate(frame_ids)),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("At least one frame is already attached to another event.") from exc

    def preference(self, key, default=None):
        row = self.conn.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_preference(self, key, value):
        self.conn.execute(
            "INSERT INTO preferences(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, encoded(value)),
        )


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "survey.sqlite3"
        self.blobs = BlobStore(self.root / "blobs")
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def use(self, *, write=False):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield Access(conn, self.blobs)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
