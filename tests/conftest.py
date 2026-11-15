import json
from pathlib import Path

import pytest

from invoice_pipeline import db
from invoice_pipeline.config import load_settings
from invoice_pipeline.graph import InvoicePipeline
from invoice_pipeline.llm import MockProvider

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "invoices"

CLEAN_HIGH_VALUE = {
    "invoice_number": "INV-9001",
    "vendor": {"name": "Widgets Inc."},
    "date": "2026-01-20",
    "due_date": "2026-02-20",
    "currency": "USD",
    "line_items": [
        {"item": "WidgetA", "quantity": 8, "unit_price": 250.00},
        {"item": "WidgetB", "quantity": 10, "unit_price": 500.00},
        {"item": "GadgetX", "quantity": 5, "unit_price": 750.00},
    ],
    "tax_amount": 0.0,
    "total": 10750.00,
}


@pytest.fixture
def settings(tmp_path):
    return load_settings(db_path=tmp_path / "test.db", runs_dir=tmp_path / "runs", xai_api_key=None)


@pytest.fixture
def conn(settings):
    connection = db.connect(settings.db_path)
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def run(settings, conn):
    """Run one invoice. Accepts a sample file name or a path."""

    def runner(invoice, provider=None, gateway=None):
        path = invoice if isinstance(invoice, Path) else SAMPLES / invoice
        kwargs = {"gateway": gateway} if gateway else {}
        pipeline = InvoicePipeline(settings, provider or MockProvider(), conn, **kwargs)
        return pipeline.run(path, run_id="test")

    return runner


@pytest.fixture
def write_invoice(tmp_path):
    def writer(data, name="invoice.json"):
        path = tmp_path / name
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    return writer
