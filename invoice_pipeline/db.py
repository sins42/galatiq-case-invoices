"""SQLite schema, seed data, and connection handling."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    item TEXT PRIMARY KEY,
    stock INTEGER NOT NULL,
    unit_price REAL
);

CREATE TABLE IF NOT EXISTS vendors (
    name TEXT PRIMARY KEY,
    approved INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS invoices_processed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    invoice_number TEXT,
    number_key TEXT,
    vendor TEXT,
    vendor_key TEXT,
    content_hash TEXT,
    bill_hash TEXT,
    total REAL,
    currency TEXT,
    status TEXT NOT NULL,
    reasons TEXT NOT NULL,
    payment_ref TEXT,
    invoice_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_ledger_number ON invoices_processed (number_key);
CREATE INDEX IF NOT EXISTS ix_ledger_bill ON invoices_processed (bill_hash);
CREATE INDEX IF NOT EXISTS ix_ledger_vendor_total ON invoices_processed (vendor_key, total);

CREATE TABLE IF NOT EXISTS payments (
    idempotency_key TEXT PRIMARY KEY,
    vendor TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# The four rows from the case brief, extended with catalog prices.
SEED_INVENTORY = [
    ("WidgetA", 15, 250.00),
    ("WidgetB", 10, 500.00),
    ("GadgetX", 5, 750.00),
    ("FakeItem", 0, None),
]

SEED_VENDORS = [
    "Widgets Inc.",
    "Precision Parts Ltd.",
    "Global Supply Chain Partners",
    "Acme Industrial Supplies",
    "MegaWidgets Corp",
    "Consolidated Materials Group",
    "Summit Manufacturing Co.",
    "QuickShip Distributers",
    "Atlas Industrial Supply",
    "TechParts International",
    "Reliable Components Inc.",
]

_TABLES = ("payments", "invoices_processed", "vendors", "inventory")


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection, reset: bool = False) -> None:
    """Create tables and seed reference data. With reset, the ledger and payments are cleared too."""
    if reset:
        for table in _TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA)
    conn.executemany("INSERT OR IGNORE INTO inventory VALUES (?, ?, ?)", SEED_INVENTORY)
    conn.executemany("INSERT OR IGNORE INTO vendors (name) VALUES (?)", [(v,) for v in SEED_VENDORS])
    conn.commit()


def load_catalog(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT item, stock, unit_price FROM inventory").fetchall()
