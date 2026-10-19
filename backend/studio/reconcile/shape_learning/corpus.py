"""
SQLite-backed corpus of confirmed (shape descriptor -> real identity)
pairs. Deliberately NOT a vector database — at the realistic scale of a
confirmed-parts corpus for this tool (hundreds to low-thousands of
entries), brute-force NumPy distance search is fast enough (single-digit
milliseconds) and needs zero extra infrastructure beyond Python's
built-in sqlite3.
"""

import json
import sqlite3
import time
from pathlib import Path

import os
DB_PATH = Path(os.environ.get("STUDIO_SHAPE_CORPUS", Path(os.environ.get("STUDIO_DATA", Path.home() / ".techpub-studio")) / "shape_corpus.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_connection():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shape_corpus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            part_number TEXT,
            description TEXT,
            descriptors_json TEXT NOT NULL,
            source_file TEXT,
            confidence_source TEXT,
            timestamp REAL
        )
    """)
    return conn


def add_entry(part_number: str, description: str, descriptors: dict,
              source_file: str, confidence_source: str):
    conn = get_connection()
    conn.execute(
        "INSERT INTO shape_corpus (part_number, description, descriptors_json, "
        "source_file, confidence_source, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (part_number, description, json.dumps(descriptors), source_file,
         confidence_source, time.time()),
    )
    conn.commit()
    conn.close()


def get_all_entries() -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT part_number, description, descriptors_json, source_file, confidence_source FROM shape_corpus"
    ).fetchall()
    conn.close()
    return [
        {
            "part_number": r[0], "description": r[1],
            "descriptors": json.loads(r[2]),
            "source_file": r[3], "confidence_source": r[4],
        }
        for r in rows
    ]


def corpus_size() -> int:
    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) FROM shape_corpus").fetchone()[0]
    conn.close()
    return n

