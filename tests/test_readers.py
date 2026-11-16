"""CSV, XML, and PDF ingestion, plus fallback to the model path."""

from pathlib import Path

from invoice_pipeline import formats, readers
from invoice_pipeline.extraction import from_structured
from invoice_pipeline.llm.text_parser import parse_text_invoice

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "invoices"


def test_key_value_csv_layout():
    invoice = from_structured(formats.parse_csv((SAMPLES / "invoice_1006.csv").read_text()))
    assert invoice.invoice_number == "INV-1006"
    assert invoice.vendor == "Acme Industrial Supplies"
    assert [(li.name, li.quantity, li.unit_price) for li in invoice.line_items] == [
        ("WidgetA", 5, 250.0),
        ("WidgetB", 3, 500.0),
    ]
    assert invoice.total == 2750.0


def test_tabular_csv_layout():
    invoice = from_structured(formats.parse_csv((SAMPLES / "invoice_1007.csv").read_text()))
    assert invoice.invoice_number == "INV-1007"
    assert invoice.vendor == "MegaWidgets Corp"
    assert len(invoice.line_items) == 3
    assert invoice.tax == 885.0
    assert invoice.total == 15525.0


def test_unknown_csv_layout_falls_back_to_the_model():
    assert formats.parse_csv("alpha,beta,gamma\n1,2,3") is None


def test_xml_parses_items_and_currency():
    invoice = from_structured(formats.parse_xml((SAMPLES / "invoice_1014.xml").read_text()))
    assert invoice.currency == "EUR"
    assert invoice.total == 4125.0
    assert [(li.name, li.quantity) for li in invoice.line_items] == [("WidgetA", 4), ("WidgetB", 6)]


def test_malformed_xml_falls_back_to_the_model():
    assert formats.parse_xml("<invoice><unclosed>") is None


def test_tax_rate_captured_across_formats():
    assert from_structured(formats.parse_csv((SAMPLES / "invoice_1007.csv").read_text())).tax_rate == 0.06
    assert from_structured(formats.parse_xml((SAMPLES / "invoice_1014.xml").read_text())).tax_rate == 0.10
    assert parse_text_invoice("Subtotal: $100.00\nTax (7%): $7.00\nTotal: $107.00")["tax_rate"] == 0.07


def test_pdf_reader_extracts_text():
    file_format, content = readers.load(SAMPLES / "invoice_1011.pdf")
    assert file_format == "pdf"
    assert isinstance(content, str) and "INV-1011" in content
