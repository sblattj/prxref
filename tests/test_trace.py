"""Tests for prxref.trace: the JSONL run trace behind the pipeline view."""
from __future__ import annotations

import json
import threading

import pytest

from prxref.trace import SCHEMA_VERSION, Tracer, get_tracer


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestDisabledByDefault:
    def test_no_trace_file_means_a_no_op_tracer(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PRXREF_TRACE_FILE", raising=False)
        tracer = get_tracer()
        tracer.event("node", "start", a=1)
        with tracer.span("other"):
            pass
        assert list(tmp_path.iterdir()) == []

    def test_a_whitespace_only_value_reads_as_unset(self, monkeypatch):
        """Matches the whole config surface: blank means default, not a path."""
        monkeypatch.setenv("PRXREF_TRACE_FILE", "   ")
        assert get_tracer().path == ""

    def test_the_env_var_enables_it(self, monkeypatch, tmp_path):
        target = tmp_path / "run.jsonl"
        monkeypatch.setenv("PRXREF_TRACE_FILE", str(target))
        get_tracer().event("run", "start")
        assert _events(target)[0]["node"] == "run"


class TestEventShape:
    def test_every_event_carries_version_seq_time_node_phase(self, tmp_path):
        target = tmp_path / "t.jsonl"
        Tracer(str(target)).event("forge.get_pr", "ok", elapsed_ms=12)
        (ev,) = _events(target)
        assert ev["v"] == SCHEMA_VERSION
        assert ev["seq"] == 1
        assert ev["node"] == "forge.get_pr"
        assert ev["phase"] == "ok"
        assert ev["t_ms"] >= 0
        assert ev["meta"]["elapsed_ms"] == 12

    def test_a_span_emits_start_then_ok_with_elapsed(self, tmp_path):
        target = tmp_path / "t.jsonl"
        with Tracer(str(target)).span("parse_diff") as sp:
            sp["files"] = 3
        start, ok = _events(target)
        assert (start["node"], start["phase"]) == ("parse_diff", "start")
        assert (ok["node"], ok["phase"]) == ("parse_diff", "ok")
        assert ok["meta"]["files"] == 3
        assert "elapsed_ms" in ok["meta"]

    def test_a_raising_span_emits_fail_and_reraises(self, tmp_path):
        target = tmp_path / "t.jsonl"
        tracer = Tracer(str(target))
        with pytest.raises(ValueError):
            with tracer.span("build_chunks"):
                raise ValueError("boom")
        _, fail = _events(target)
        assert fail["phase"] == "fail"
        assert fail["meta"]["error"] == "ValueError"

    def test_an_unserializable_value_is_coerced_not_dropped(self, tmp_path):
        target = tmp_path / "t.jsonl"
        Tracer(str(target)).event("n", "ok", obj=object())
        assert isinstance(_events(target)[0]["meta"]["obj"], str)


class TestOperationalGuarantees:
    def test_events_are_readable_while_the_run_is_still_in_flight(self, tmp_path):
        """The whole point: a hung run must be diagnosable from a partial file.

        A trace flushed only at exit cannot explain the hang it exists for, so
        this reads the file BEFORE the run would have ended.
        """
        target = tmp_path / "t.jsonl"
        tracer = Tracer(str(target))
        tracer.event("chunk", "start", index=1)
        assert _events(target)[0]["meta"]["index"] == 1

    def test_concurrent_writers_produce_whole_lines(self, tmp_path):
        """Chunks are reviewed on a thread pool, so events arrive concurrently."""
        target = tmp_path / "t.jsonl"
        tracer = Tracer(str(target))

        def emit(i):
            for _ in range(25):
                tracer.event("chunk", "start", index=i, payload="x" * 200)

        threads = [threading.Thread(target=emit, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = _events(target)  # json.loads would raise on an interleaved line
        assert len(events) == 200
        assert sorted(e["seq"] for e in events) == list(range(1, 201))

    def test_tracing_never_raises_into_the_review(self, tmp_path):
        """An unwritable path must not fail a review that was otherwise fine."""
        tracer = Tracer(str(tmp_path / "no-such-dir" / "t.jsonl"))
        tracer.event("run", "start")
        with tracer.span("forge.get_pr"):
            pass


class TestPipelineView:
    """The rendered view must be readable offline and must not invent a story."""

    def _events(self, *triples):
        return [{"v": 1, "seq": i + 1, "t_ms": t, "node": n, "phase": p}
                for i, (t, n, p) in enumerate(triples)]

    # The SVG namespace is a URI that identifies; it is never dereferenced, and
    # createElementNS requires it verbatim. Everything else that looks like a URL
    # in this document would be a fetch.
    SVG_NS = "http://www.w3.org/2000/svg"

    def test_the_html_has_no_external_references(self):
        """A viewer that needs the network fails exactly when it is needed."""
        from prxref.viz import render_html

        doc = render_html(self._events((0, "run", "start"), (5, "run", "ok")))
        fetchable = doc.replace(self.SVG_NS, "")
        for forbidden in ("<script src", "<link ", "@import", "https://", "http://"):
            assert forbidden not in fetchable, f"external reference: {forbidden}"

    def test_the_no_external_reference_check_can_fail(self):
        """Control: the check above must not be satisfied by its own exemption."""
        doc = f'<img src="{self.SVG_NS}/x.png">https://cdn.example/a.js'
        fetchable = doc.replace(self.SVG_NS, "")
        assert "https://" in fetchable

    def test_a_node_that_never_ran_is_still_described(self):
        """"This stage never started" is a finding a graph built only from
        observed events cannot express."""
        from prxref.viz import NODES, render_html

        doc = render_html(self._events((0, "run", "start")))
        assert '"post"' in doc
        assert any(n["id"] == "post" for n in NODES)

    def test_an_unclosed_node_is_reported_as_open(self):
        from prxref.viz import summarize

        s = summarize(self._events((0, "chunk", "start")))
        assert s["open_nodes"] == ["chunk"]

    def test_a_closed_node_is_not_reported_as_open(self):
        """Control: the open-node signal must be able to come out empty."""
        from prxref.viz import summarize

        s = summarize(self._events((0, "chunk", "start"), (9, "chunk", "ok")))
        assert s["open_nodes"] == []

    def test_a_torn_final_line_does_not_lose_the_whole_trace(self, tmp_path):
        """A trace is read while it is still being appended to."""
        from prxref.viz import load_events

        f = tmp_path / "t.jsonl"
        f.write_text('{"v":1,"seq":1,"t_ms":0,"node":"run","phase":"start"}\n{"v":1,"seq":2,"t_')
        assert len(load_events(f)) == 1

    def test_a_script_tag_in_the_data_cannot_break_out(self, tmp_path):
        from prxref.viz import render_html

        doc = render_html([{"v": 1, "seq": 1, "t_ms": 0, "node": "run",
                            "phase": "start", "meta": {"x": "</script><b>pwned"}}])
        assert "</script><b>pwned" not in doc
        assert "<\\/script>" in doc
