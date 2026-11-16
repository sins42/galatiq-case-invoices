"""Recording model responses to disk and replaying them offline."""

import dataclasses

import pytest
from conftest import SAMPLES

from invoice_pipeline import db
from invoice_pipeline.graph import InvoicePipeline
from invoice_pipeline.llm import MockProvider
from invoice_pipeline.llm.base import LLMError, LLMRequest
from invoice_pipeline.llm.replay import RecordingProvider, ReplayProvider


def test_replay_returns_recorded_then_raises_when_missing(tmp_path):
    path = tmp_path / "rec.json"
    recorder = RecordingProvider(MockProvider(script={"extract": ["recorded text"]}), path)
    request = LLMRequest(task="extract", role="extraction", system="s", user="u")

    assert recorder.complete(request).text == "recorded text"
    assert path.exists()

    replay = ReplayProvider(path)
    assert replay.complete(request).text == "recorded text"
    with pytest.raises(LLMError):
        replay.complete(request)  # the one recorded response for this key is used up


def test_record_then_replay_reproduces_a_run(settings, conn, tmp_path):
    path = tmp_path / "run.json"
    recorder = RecordingProvider(MockProvider(), path)
    original = InvoicePipeline(settings, recorder, conn).run(SAMPLES / "invoice_1010.txt", run_id="rec")

    replay_db = tmp_path / "replay.db"
    conn2 = db.connect(replay_db)
    db.init_db(conn2)
    replay = ReplayProvider(path)
    replayed = InvoicePipeline(dataclasses.replace(settings, db_path=replay_db), replay, conn2).run(
        SAMPLES / "invoice_1010.txt", run_id="rep"
    )
    conn2.close()

    assert replayed.outcome is original.outcome          # same decision, no API key
    assert replayed.invoice.total == original.invoice.total
    assert replay.usage                                   # usage was replayed from the recording
