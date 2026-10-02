"""``prxref eval dashboard``: the status math, the log reads, ``--once`` and a real server (#81)."""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from prxref import cli, eval_dashboard
from prxref.llm import ConfigError

T0 = "2026-10-02T10:00:00Z"
NOW = datetime(2026, 10, 2, 10, 10, 0, tzinfo=UTC)


def _pass(arm="a", repeat=1, state="running", total=10, ok=0, failed=0, rl=0, started=T0, finished=None, log=None):
    return {
        "arm": arm, "repeat": repeat, "state": state, "units_total": total, "units_ok": ok,
        "units_failed": failed, "units_rate_limited": rl, "attempt": 1, "started_at": started,
        "finished_at": finished, "log": log or f"logs/{arm}-r{repeat}.log", "error": None,
    }


def _campaign(tmp_path: Path, passes, started=T0) -> Path:
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "progress.json").write_text(json.dumps(
        {"version": 1, "started_at": started, "updated_at": "2026-10-02T10:09:59Z", "passes": passes}
    ))
    return tmp_path


class TestStatusMath:
    def test_pct_and_eta_from_the_passes_own_rate(self, tmp_path):
        d = _campaign(tmp_path, [_pass(total=10, ok=2, failed=1)])
        p = eval_dashboard.status(d, now=NOW)["passes"][0]
        assert p["pct"] == 30.0
        assert p["eta_s"] == 1400.0  # 3 done in 600 s, 7 left

    def test_eta_is_none_until_a_unit_is_done_then_a_number(self, tmp_path):
        d = _campaign(tmp_path, [_pass(total=10, ok=0)])
        st = eval_dashboard.status(d, now=NOW)
        assert st["passes"][0]["eta_s"] is None
        assert st["overall"]["eta_s"] is None
        d = _campaign(tmp_path, [_pass(total=10, ok=1)])
        st = eval_dashboard.status(d, now=NOW)
        assert st["passes"][0]["eta_s"] == 5400.0
        assert st["overall"]["eta_s"] == 5400.0

    def test_a_finished_pass_uses_its_finish_time_and_has_no_time_left(self, tmp_path):
        d = _campaign(tmp_path, [_pass(state="scored", total=4, ok=4, finished="2026-10-02T10:02:00Z")])
        p = eval_dashboard.status(d, now=NOW)["passes"][0]
        assert (p["pct"], p["eta_s"]) == (100.0, 0.0)

    def test_a_pass_finished_within_its_start_second_has_no_time_left_either(self, tmp_path):
        stamp = "2026-10-02T10:00:00Z"
        d = _campaign(tmp_path, [_pass(state="scored", total=2, ok=2, started=stamp, finished=stamp)])
        assert eval_dashboard.status(d, now=NOW)["passes"][0]["eta_s"] == 0.0

    def test_overall_sums_the_passes(self, tmp_path):
        d = _campaign(tmp_path, [
            _pass(arm="a", total=10, ok=5), _pass(arm="b", total=10, ok=2, failed=1),
            _pass(arm="c", total=20, state="pending", started=None),
        ])
        o = eval_dashboard.status(d, now=NOW)["overall"]
        assert (o["units_total"], o["units_done"], o["pct"], o["elapsed_s"]) == (40, 8, 20.0, 600.0)
        assert o["eta_s"] == 2400.0  # 8 done in 600 s, 32 left

    def test_a_pass_with_no_start_or_no_total_does_not_divide_by_zero(self, tmp_path):
        d = _campaign(tmp_path, [_pass(total=0, ok=0, started=None), _pass(arm="b", total=5, ok=1, started=None)])
        ps = eval_dashboard.status(d, now=NOW)["passes"]
        assert (ps[0]["pct"], ps[0]["eta_s"]) == (0.0, None)
        assert ps[1]["eta_s"] is None

    def test_campaign_json_is_optional(self, tmp_path):
        d = _campaign(tmp_path, [_pass()])
        assert eval_dashboard.status(d, now=NOW)["campaign"] is None
        (d / "campaign.json").write_text(json.dumps({"version": 1, "repeats": 3}))
        assert eval_dashboard.status(d, now=NOW)["campaign"] == {"version": 1, "repeats": 3}


class TestLogs:
    def test_tail_is_the_last_n_lines(self, tmp_path):
        d = _campaign(tmp_path, [_pass()])
        (d / "logs" / "a-r1.log").write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
        assert eval_dashboard.status(d, tail=3, now=NOW)["passes"][0]["log_tail"] == ["line 47", "line 48", "line 49"]
        assert eval_dashboard.status(d, tail=0, now=NOW)["passes"][0]["log_tail"] == []

    def test_rate_limit_lines_count_the_whole_log_not_the_tail(self, tmp_path):
        d = _campaign(tmp_path, [_pass()])
        lines = ["HTTP 429 from provider", "Rate-Limit hit", "Too Many Requests", "quota exceeded",
                 "session cap reached", "session limit", "port 4290 is fine", "all good"] + ["ok"] * 30
        (d / "logs" / "a-r1.log").write_text("\n".join(lines))
        st = eval_dashboard.status(d, tail=2, now=NOW)
        assert st["passes"][0]["rate_limit_lines"] == 6
        assert st["passes"][0]["log_tail"] == ["ok", "ok"]
        assert st["overall"]["rate_limited_passes"] == 1

    def test_a_missing_log_is_empty_and_a_log_outside_the_campaign_is_not_read(self, tmp_path):
        outside = tmp_path / "secret.log"
        outside.write_text("429 secret\n")
        (tmp_path / "c").mkdir()
        d = _campaign(tmp_path / "c", [_pass(), _pass(arm="b", log="../secret.log")])
        ps = eval_dashboard.status(d, now=NOW)["passes"]
        assert [(p["log_tail"], p["rate_limit_lines"]) for p in ps] == [([], 0), ([], 0)]

    def test_units_rate_limited_alone_flags_the_pass(self, tmp_path):
        d = _campaign(tmp_path, [_pass(rl=2), _pass(arm="b")])
        assert eval_dashboard.status(d, now=NOW)["overall"]["rate_limited_passes"] == 1


class TestRobustness:
    def test_a_missing_progress_json_is_a_config_error_naming_campaign(self, tmp_path):
        with pytest.raises(ConfigError, match="--campaign"):
            eval_dashboard.status(tmp_path)

    def test_invalid_json_keeps_the_last_good_status(self, tmp_path):
        d = _campaign(tmp_path, [_pass(total=10, ok=5)])
        good = eval_dashboard.status(d, now=NOW)
        (d / "progress.json").write_text('{"passes": [{"arm": "a", "units_t')
        assert eval_dashboard.status(d, now=NOW) == good

    def test_invalid_json_with_no_good_status_yet_is_a_config_error(self, tmp_path):
        (tmp_path / "progress.json").write_text("{")
        with pytest.raises(ConfigError, match="--campaign"):
            eval_dashboard.status(tmp_path)


class TestOnce:
    def test_prints_a_row_per_pass_and_exits_0(self, tmp_path, capsys):
        d = _campaign(tmp_path, [
            _pass(arm="base", total=10, ok=4), _pass(arm="cand", repeat=2, state="pending", total=10),
        ])
        (d / "logs" / "base-r1.log").write_text("429 Too Many Requests\n")
        assert cli.main(["eval", "dashboard", "--campaign", str(d), "--once"]) == 0
        out = capsys.readouterr().out.splitlines()
        assert out[0].split() == ["arm", "repeat", "state", "ok/total", "pct", "eta", "rate-limit"]
        assert out[1].split()[:5] == ["base", "1", "running", "4/10", "40.0%"]
        assert re.fullmatch(r"\d+:\d\d:\d\d", out[1].split()[5])
        assert out[1].split()[-1] == "RATE-LIMITED"
        assert out[2].split()[:6] == ["cand", "2", "pending", "0/10", "0.0%", "-"]
        assert out[2].split()[-1] == "-"
        assert out[3].startswith("overall: 4/20 units (20.0%)")

    def test_a_campaign_without_progress_json_exits_2_naming_campaign(self, tmp_path, capsys):
        assert cli.main(["eval", "dashboard", "--campaign", str(tmp_path), "--once"]) == 2
        err = capsys.readouterr().err
        assert err.startswith("configuration error:") and "--campaign" in err

    def test_a_campaign_that_is_not_a_directory_exits_2(self, tmp_path, capsys):
        assert cli.main(["eval", "dashboard", "--campaign", str(tmp_path / "nope"), "--once"]) == 2
        assert "--campaign" in capsys.readouterr().err

    def test_a_negative_tail_exits_2(self, tmp_path, capsys):
        d = _campaign(tmp_path, [_pass()])
        assert cli.main(["eval", "dashboard", "--campaign", str(d), "--once", "--tail", "-1"]) == 2
        assert "--tail" in capsys.readouterr().err


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read().decode("utf-8")


@pytest.fixture
def server(tmp_path):
    d = _campaign(tmp_path, [_pass(total=10, ok=3)])
    (d / "logs" / "a-r1.log").write_text("<script>alert(1)</script>\nplain line\n429 rate limit\n")
    srv = eval_dashboard.make_server(d, "127.0.0.1", 0, 20)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, d
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=10)


class TestServer:
    def test_serves_the_page_the_status_and_404(self, server):
        srv, _ = server
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        code, ctype, page = _get(base + "/")
        assert (code, ctype) == (200, "text/html; charset=utf-8")
        assert "/status.json" in page and "setInterval(poll,2000)" in page
        assert "http://" not in page and "https://" not in page and " src=" not in page and "<link" not in page
        code, ctype, body = _get(base + "/status.json")
        assert (code, ctype) == (200, "application/json")
        st = json.loads(body)
        assert st["overall"]["units_done"] == 3 and st["passes"][0]["rate_limit_lines"] == 1
        assert _get(base + "/nope")[0] == 404
        assert _get(base + "/status.json/extra")[0] == 404

    def test_every_log_line_is_html_escaped_in_the_page(self, server):
        srv, _ = server
        _, _, page = _get(f"http://127.0.0.1:{srv.server_address[1]}/")
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page

    def test_the_page_script_escapes_before_it_writes_innerhtml(self):
        page = eval_dashboard.render_page(None)
        assert "function esc(" in page and "p.log_tail.map(esc)" in page

    def test_a_vanished_progress_json_serves_the_last_good_status(self, server):
        srv, d = server
        url = f"http://127.0.0.1:{srv.server_address[1]}/status.json"
        assert _get(url)[0] == 200
        (d / "progress.json").write_text("{")
        code, _, body = _get(url)
        assert code == 200 and json.loads(body)["overall"]["units_done"] == 3

    def test_a_port_in_use_is_a_config_error_naming_port(self, server):
        srv, d = server
        with pytest.raises(ConfigError, match="--port"):
            eval_dashboard.make_server(d, "127.0.0.1", srv.server_address[1], 20)

    def test_a_non_loopback_host_logs_a_warning_and_loopback_does_not(self, tmp_path, caplog):
        d = _campaign(tmp_path, [_pass()])
        with caplog.at_level("WARNING", logger="prxref.eval_dashboard"):
            eval_dashboard.make_server(d, "127.0.0.1", 0, 20).server_close()
            assert not caplog.records
            eval_dashboard.make_server(d, "0.0.0.0", 0, 20).server_close()
        assert [r.levelname for r in caplog.records] == ["WARNING"]
        assert "0.0.0.0" in caplog.records[0].getMessage()


class TestServe:
    def test_prints_the_real_port_and_ctrl_c_exits_0(self, tmp_path, capsys, monkeypatch):
        d = _campaign(tmp_path, [_pass()])

        def _interrupt(self, poll_interval=0.5):
            raise KeyboardInterrupt

        monkeypatch.setattr(eval_dashboard.ThreadingHTTPServer, "serve_forever", _interrupt)
        assert cli.main(["eval", "dashboard", "--campaign", str(d), "--port", "0"]) == 0
        line = capsys.readouterr().out.strip()
        assert line.startswith("dashboard: http://127.0.0.1:") and line.endswith("/")
        assert line != "dashboard: http://127.0.0.1:0/"
