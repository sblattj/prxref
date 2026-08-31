"""Render a JSONL run trace as a standalone HTML pipeline view.

The other four observability layers are text: logs, attempt records, a
heartbeat, a trace file. They answer questions you already know to ask. This
one is for the question you cannot phrase yet -- where did the time actually
go, and which node was still open when everything went quiet.

The output is a single file with no external references: no CDN, no fonts, no
network at all. An observability tool that only works when the network does is
useless in precisely the situation that motivated it, and a trace is most
often read from a laptop looking at a run that already failed.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

# The pipeline is a fixed DAG, so its shape is declared rather than inferred.
# A node that never emitted an event still renders -- greyed out -- which is
# the whole point: "this stage never ran" is a finding, and a graph built only
# from observed events cannot express it.
NODES: list[dict[str, Any]] = [
    {"id": "run", "label": "run", "sub": "review start", "col": 0},
    {"id": "forge.get_pr", "label": "get_pr", "sub": "forge metadata", "col": 1},
    {"id": "forge.get_diff", "label": "get_diff", "sub": "unified diff", "col": 2},
    {"id": "parse_diff", "label": "parse", "sub": "diff → files", "col": 3},
    {"id": "build_chunks", "label": "chunk", "sub": "files → chunks", "col": 4},
    {"id": "chunk", "label": "workers", "sub": "parallel review", "col": 5},
    {"id": "post", "label": "post", "sub": "summary + inline", "col": 6},
]

EDGES = [
    ("run", "forge.get_pr"),
    ("forge.get_pr", "forge.get_diff"),
    ("forge.get_diff", "parse_diff"),
    ("parse_diff", "build_chunks"),
    ("build_chunks", "chunk"),
    ("chunk", "post"),
]


def load_events(path: str | Path) -> list[dict]:
    """Read a JSONL trace, skipping any line that will not parse.

    A trace is appended to live and may be read while the process is still
    writing, so a torn final line is expected rather than exceptional. Dropping
    it is right; refusing to render the other 200 events because of it is not.
    """
    events: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def summarize(events: list[dict]) -> dict[str, Any]:
    """Fold events into per-node state, and surface what never closed.

    ``open`` nodes -- started with no matching ok/fail -- are the payload. A
    completed run has none; a hung one has exactly the node that wedged, which
    is the question the logs could not answer.
    """
    nodes: dict[str, dict[str, Any]] = {}
    for ev in events:
        node = ev.get("node", "?")
        phase = ev.get("phase", "")
        meta = ev.get("meta") or {}
        entry = nodes.setdefault(
            node, {"starts": 0, "ok": 0, "fail": 0, "skip": 0, "elapsed_ms": 0,
                   "first_t": ev.get("t_ms", 0), "last_t": ev.get("t_ms", 0), "meta": {}}
        )
        entry["last_t"] = ev.get("t_ms", entry["last_t"])
        if phase == "start":
            entry["starts"] += 1
        elif phase == "skip":
            entry["skip"] += 1
        elif phase in ("ok", "fail"):
            entry[phase] += 1
            entry["elapsed_ms"] = max(entry["elapsed_ms"], int(meta.get("elapsed_ms") or 0))
        entry["meta"].update({k: v for k, v in meta.items() if k != "elapsed_ms"})

    total_ms = max((e.get("t_ms", 0) for e in events), default=0)
    open_nodes = [n for n, e in nodes.items() if e["starts"] > e["ok"] + e["fail"]]
    return {
        "nodes": nodes,
        "total_ms": total_ms,
        "open_nodes": open_nodes,
        "event_count": len(events),
        "failed": [n for n, e in nodes.items() if e["fail"]],
    }


def render_html(events: list[dict], *, title: str = "prxref run") -> str:
    """Return one self-contained HTML document for this trace."""
    summary = summarize(events)
    payload = json.dumps(
        {"events": events, "summary": summary, "nodes": NODES, "edges": EDGES},
        ensure_ascii=False,
    )
    # </script> inside the data would close the tag early; escaping the slash
    # is the standard, encoding-safe fix and leaves the JSON valid.
    payload = payload.replace("</", "<\\/")
    return _TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__DATA__", payload)


def render_file(trace_path: str | Path, out_path: str | Path) -> Path:
    """Render ``trace_path`` to ``out_path`` and return the output path."""
    events = load_events(trace_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(events, title=Path(trace_path).name), encoding="utf-8")
    return out


_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · prxref pipeline</title>
<style>
  :root{
    --bg:#f6f7f9; --panel:#fff; --ink:#12151a; --muted:#5d6673; --line:#dfe3e8;
    --idle:#c8cdd4; --ok:#1f9d63; --run:#2f7de1; --fail:#d6403a; --warn:#c98a12;
    --accent:#2f7de1;
  }
  @media (prefers-color-scheme:dark){:root:not([data-theme=light]){
    --bg:#0e1116; --panel:#161b22; --ink:#e6edf3; --muted:#8b949e; --line:#2a313a;
    --idle:#39414c; --ok:#3fb950; --run:#58a6ff; --fail:#f85149; --warn:#d29922;
    --accent:#58a6ff;
  }}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.5 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:1180px;margin:0 auto;padding:28px 20px 64px}
  h1{font-size:19px;margin:0 0 2px;letter-spacing:-.01em}
  .sub{color:var(--muted);font-size:13px;margin-bottom:20px}
  .banner{padding:10px 14px;border-radius:8px;margin-bottom:18px;font-size:13px;
          border:1px solid var(--line);background:var(--panel)}
  .banner.bad{border-color:var(--fail);color:var(--fail)}
  .banner.good{border-color:var(--ok);color:var(--ok)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
        padding:16px;margin-bottom:18px}
  .scroll{overflow-x:auto}
  svg{display:block;min-width:900px}
  .nlabel{font:600 13px ui-sans-serif,sans-serif;fill:var(--ink)}
  .nsub{font:11px ui-monospace,Menlo,monospace;fill:var(--muted)}
  .nt{font:11px ui-monospace,Menlo,monospace;fill:var(--muted)}
  .bars{display:grid;grid-template-columns:150px 1fr 76px;gap:6px 12px;align-items:center;
        font:12px ui-monospace,Menlo,monospace}
  .bar{height:16px;border-radius:4px;background:var(--idle);position:relative}
  .bar>span{position:absolute;inset:0 auto 0 0;border-radius:4px}
  .right{text-align:right;color:var(--muted)}
  table{width:100%;border-collapse:collapse;font:12px ui-monospace,Menlo,monospace}
  th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);
        vertical-align:top}
  th{color:var(--muted);font-weight:600}
  tr.fail td{color:var(--fail)}
  .pill{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
        border:1px solid var(--line);color:var(--muted)}
  .legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-top:12px}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}
  details summary{cursor:pointer;color:var(--accent)}
</style></head><body><div class="wrap">
<h1>prxref pipeline · __TITLE__</h1>
<div class="sub" id="sub"></div>
<div id="banner"></div>
<div class="card scroll"><svg id="dag" height="94"></svg>
  <div class="legend">
    <span><i class="dot" style="background:var(--ok)"></i>completed</span>
    <span><i class="dot" style="background:var(--run)"></i>still open</span>
    <span><i class="dot" style="background:var(--fail)"></i>failed</span>
    <span><i class="dot" style="background:var(--warn)"></i>skipped</span>
    <span><i class="dot" style="background:var(--idle)"></i>never ran</span>
  </div>
</div>
<div class="card"><b style="font-size:13px">Where the time went</b>
  <div style="height:10px"></div><div class="bars" id="bars"></div></div>
<div class="card"><b style="font-size:13px">Events</b><div style="height:10px"></div>
  <div class="scroll"><table id="tbl"><thead><tr><th>t</th><th>node</th>
  <th>phase</th><th>detail</th></tr></thead><tbody></tbody></table></div></div>
</div>
<script id="trace-data" type="application/json">__DATA__</script>
<script>
(function(){
  var D = JSON.parse(document.getElementById('trace-data').textContent);
  var S = D.summary, NODES = D.nodes, EDGES = D.edges;
  var fmt = function(ms){ return ms >= 1000 ? (ms/1000).toFixed(1)+'s' : ms+'ms'; };

  document.getElementById('sub').textContent =
    D.events.length + ' events · ' + fmt(S.total_ms) + ' wall clock';

  // The banner states the one thing a reader needs before anything else: did
  // this run end, and if not, what was still open when it stopped talking.
  var b = document.getElementById('banner');
  if (S.open_nodes.length) {
    b.className = 'banner bad';
    b.textContent = 'Never completed: ' + S.open_nodes.join(', ') +
      ' started and never finished. This is where the run was when it went quiet.';
  } else if (S.failed.length) {
    b.className = 'banner bad';
    b.textContent = 'Failed nodes: ' + S.failed.join(', ');
  } else if (D.events.length) {
    b.className = 'banner good';
    b.textContent = 'Run completed; every node that started also finished.';
  } else {
    b.className = 'banner';
    b.textContent = 'Empty trace - no events were recorded.';
  }

  function state(id){
    var e = S.nodes[id];
    if (!e) return 'idle';
    if (e.fail) return 'fail';
    if (e.starts > e.ok + e.fail) return 'run';
    if (!e.starts && e.skip) return 'skip';
    return 'ok';
  }
  var COLOR = {idle:'var(--idle)', ok:'var(--ok)', run:'var(--run)',
               fail:'var(--fail)', skip:'var(--warn)'};

  // Fixed DAG, so lay it out arithmetically - no layout library needed, and
  // nothing to fail to load offline.
  var svg = document.getElementById('dag');
  var W = 128, H = 62, GAP = 20, X0 = 12, Y = 18, NS = 'http://www.w3.org/2000/svg';
  var pos = {};
  NODES.forEach(function(n){ pos[n.id] = {x: X0 + n.col*(W+GAP), y: Y}; });
  svg.setAttribute('width', X0*2 + NODES.length*(W+GAP));

  function el(name, attrs, text){
    var e = document.createElementNS(NS, name);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    svg.appendChild(e); return e;
  }

  EDGES.forEach(function(pair){
    var a = pos[pair[0]], c = pos[pair[1]];
    if (!a || !c) return;
    var lit = state(pair[0]) !== 'idle' && state(pair[1]) !== 'idle';
    el('line', {x1:a.x+W, y1:a.y+H/2, x2:c.x, y2:c.y+H/2,
                stroke: lit ? 'var(--accent)' : 'var(--line)', 'stroke-width': lit?2:1.5});
  });

  // ~17 chars is what W=128 holds at this font size; SVG text does not wrap.
  function clip(t){ return t.length > 17 ? t.slice(0, 16) + '\u2026' : t; }

  NODES.forEach(function(n){
    var p = pos[n.id], st = state(n.id), e = S.nodes[n.id];
    el('rect', {x:p.x, y:p.y, width:W, height:H, rx:9,
                fill:'var(--panel)', stroke:COLOR[st], 'stroke-width': st==='idle'?1.5:2.5});
    el('circle', {cx:p.x+13, cy:p.y+15, r:4, fill:COLOR[st]});
    el('text', {x:p.x+24, y:p.y+19, class:'nlabel'}, n.label);
    el('text', {x:p.x+11, y:p.y+35, class:'nsub'}, n.sub);
    var detail;
    if (!e) detail = 'not reached';
    else if (st === 'skip') detail = clip('skipped: ' + (e.meta.reason || 'by configuration'));
    else if (n.id === 'chunk') {
      var open = e.starts - e.ok - e.fail;
      detail = e.starts + '\u00d7 · ' + fmt(e.elapsed_ms || (e.last_t - e.first_t)) +
               (open ? ' · ' + open + ' open' : '');
    } else detail = fmt(e.elapsed_ms || (e.last_t - e.first_t));
    el('text', {x:p.x+11, y:p.y+51, class:'nt'}, detail);
  });

  // Duration bars, longest first: the eye should land on the expensive stage.
  var bars = document.getElementById('bars');
  var rows = Object.keys(S.nodes).map(function(id){
    var e = S.nodes[id];
    return {id:id, ms: e.elapsed_ms || (e.last_t - e.first_t), st: state(id)};
  }).filter(function(r){ return r.id !== 'heartbeat' && r.st !== 'skip'; })
    .sort(function(a,b){ return b.ms - a.ms; });
  var max = rows.reduce(function(m,r){ return Math.max(m, r.ms); }, 1);
  rows.forEach(function(r){
    var name = document.createElement('div'); name.textContent = r.id;
    var track = document.createElement('div'); track.className = 'bar';
    var fill = document.createElement('span');
    fill.style.width = Math.max(2, (r.ms/max)*100) + '%';
    fill.style.background = COLOR[r.st];
    track.appendChild(fill);
    var val = document.createElement('div'); val.className = 'right'; val.textContent = fmt(r.ms);
    bars.appendChild(name); bars.appendChild(track); bars.appendChild(val);
  });

  var tb = document.querySelector('#tbl tbody');
  D.events.forEach(function(ev){
    var tr = document.createElement('tr');
    if (ev.phase === 'fail') tr.className = 'fail';
    var meta = ev.meta ? JSON.stringify(ev.meta) : '';
    if (meta.length > 160) meta = meta.slice(0,160) + '…';
    [fmt(ev.t_ms), ev.node, ev.phase, meta].forEach(function(v, i){
      var td = document.createElement('td');
      if (i === 2) { var s = document.createElement('span'); s.className='pill'; s.textContent=v; td.appendChild(s); }
      else td.textContent = v;
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
})();
</script></body></html>
"""
