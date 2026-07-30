"""SQLite schema ownership for the rebuildable Wiki knowledge index."""

from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 3


def is_corruption_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "database disk image is malformed",
            "file is not a database",
            "database corrupt",
            "malformed database schema",
        )
    )


def reset_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP TRIGGER IF EXISTS chunks_ai;
        DROP TRIGGER IF EXISTS chunks_ad;
        DROP TRIGGER IF EXISTS chunks_au;
        DROP TABLE IF EXISTS chunks_fts;
        DROP TABLE IF EXISTS links;
        DROP TABLE IF EXISTS chunks;
        DROP TABLE IF EXISTS note_embeddings;
        DROP TABLE IF EXISTS events;
        DROP TABLE IF EXISTS runs;
        DROP TABLE IF EXISTS notes;
        DROP TABLE IF EXISTS meta;

        CREATE TABLE meta (
            schema_version INTEGER NOT NULL,
            built_at TEXT
        );
        CREATE TABLE notes (
            path TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            type TEXT,
            tags TEXT,
            created TEXT,
            updated TEXT,
            mtime INTEGER NOT NULL,
            content_hash TEXT NOT NULL
        );
        CREATE TABLE note_embeddings (
            path TEXT PRIMARY KEY REFERENCES notes(path) ON DELETE CASCADE,
            content_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            dimension INTEGER NOT NULL CHECK(dimension > 0),
            vector BLOB NOT NULL,
            embedded_at TEXT NOT NULL
        );
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('note', 'event')),
            source_id TEXT NOT NULL,
            ticket TEXT,
            title TEXT NOT NULL DEFAULT '',
            heading TEXT,
            text TEXT NOT NULL,
            pos INTEGER NOT NULL,
            UNIQUE(source_kind, source_id, pos)
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            text,
            title,
            heading,
            content='chunks',
            content_rowid='id',
            tokenize='porter unicode61'
        );
        CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, text, title, heading)
            VALUES (new.id, new.text, new.title, new.heading);
        END;
        CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text, title, heading)
            VALUES ('delete', old.id, old.text, old.title, old.heading);
        END;
        CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text, title, heading)
            VALUES ('delete', old.id, old.text, old.title, old.heading);
            INSERT INTO chunks_fts(rowid, text, title, heading)
            VALUES (new.id, new.text, new.title, new.heading);
        END;
        CREATE TABLE links (
            src_note TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
            dst_name TEXT NOT NULL,
            resolved_path TEXT REFERENCES notes(path) ON DELETE SET NULL
        );
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            ticket TEXT,
            provider TEXT,
            model TEXT,
            role TEXT,
            spawned_at TEXT,
            ended_at TEXT,
            outcome TEXT
        );
        CREATE TABLE events (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            type TEXT NOT NULL,
            ts TEXT,
            text_excerpt TEXT NOT NULL,
            PRIMARY KEY(run_id, seq)
        );
        CREATE INDEX chunks_source_idx ON chunks(source_kind, source_id, pos);
        CREATE INDEX chunks_ticket_idx ON chunks(ticket);
        CREATE INDEX note_embeddings_hash_idx ON note_embeddings(content_hash);
        CREATE INDEX links_dst_idx ON links(resolved_path);
        CREATE INDEX events_type_ts_idx ON events(type, ts);
        """
    )
    connection.execute(
        "INSERT INTO meta(schema_version, built_at) VALUES (?, NULL)",
        (SCHEMA_VERSION,),
    )
    connection.commit()
