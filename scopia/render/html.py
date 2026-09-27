"""Self-contained, interactive HTML report.

No CDN, no build step, no framework — the file opens from disk, from an email
attachment, or from a CI artifact years from now. Layout is computed in Python and
baked into the SVG, so the graph is fully readable before any script runs; the inline
script only adds selection and focus.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from ..graph.model import CALL, Graph, Node, is_uncertain
from . import layout as layout_mod

_HI_FAN_IN = 5
_MAX_LABEL = 40

_WHY = {
    "ambiguous": "several definitions share this name",
    "inferred": "receiver type unknown — matched by name only",
    "heuristic": "referenced as a string, not called directly",
    "static": "",
}


def render(graph: Graph, *, title: str = "scopia") -> str:
    lay = layout_mod.compute(graph)
    touched = graph.touched
    new_files = {n.path for n in touched if n.added}
    all_files = {n.path for n in touched}

    if new_files and len(new_files) == len(all_files) and all_files:
        subtitle = "Every file here is new — this is the shape of the code, not of a change."
    elif new_files:
        subtitle = f"{len(new_files)} of {len(all_files)} files are new."
    else:
        subtitle = "What changed, and what it reaches."

    return _PAGE.format(
        title=html.escape(title),
        subtitle=html.escape(subtitle),
        stats=_stats(graph, touched),
        sidebar=_sidebar(touched),
        svg=_svg(graph, lay),
        data=_payload(graph),
        stamp=datetime.now(timezone.utc).strftime("%d %b %Y · %H:%M UTC"),
        hot=_HI_FAN_IN,
        footnote=_footnote(graph),
        banner=_banner(graph),
    )


def _stats(graph: Graph, touched: list[Node]) -> str:
    items = [
        (len(touched), "changed"),
        (graph.files_changed, "files"),
        (len(graph.nodes) - len(touched), "context"),
    ]
    out = "".join(f"<span><b>{v}</b>{label}</span>" for v, label in items)
    if graph.uncertain_edges:
        out += f'<span class="warn"><b>{len(graph.uncertain_edges)}</b>uncertain</span>'
    return out


def _sidebar(touched: list[Node]) -> str:
    """Changed symbols, widest reach first — that is the order worth reading in."""
    rows = []
    for node in sorted(touched, key=lambda n: (-n.fan_in, n.label)):
        badge = "new" if node.added else str(node.changed_lines)
        badge_cls = "b-new" if node.added else "b-lines"
        reach = ""
        if node.fan_in:
            approx = "~" if node.fan_in_shared else ""
            hot = " hot" if node.fan_in >= _HI_FAN_IN else ""
            reach = f'<em class="reach{hot}">{approx}{node.fan_in}</em>'
        rows.append(
            f'<li><button type="button" data-goto="{html.escape(node.uid)}">'
            f'<span class="{badge_cls}">{html.escape(badge)}</span>'
            f'<span class="sym">{html.escape(_short(node.display))}</span>'
            f"{reach}</button></li>"
        )
    return "\n".join(rows) or '<li class="empty">nothing changed</li>'


def _banner(graph: Graph) -> str:
    """A cap that removes most of the graph has to announce itself.

    Buried in a footnote, a 75% cut reads as a missing edge rather than a hidden one.
    """
    if not graph.truncated:
        return ""
    total = len(graph.nodes) + graph.truncated
    return (
        f'<div class="banner">Showing <b>{len(graph.nodes)} of {total}</b> symbols. '
        "Edges to the rest are not drawn — raise <b>--max-nodes</b> for the full picture."
        "</div>"
    )


def _footnote(graph: Graph) -> str:
    bits = []
    if graph.unparsed:
        names = ", ".join(path for path, _ in graph.unparsed[:3])
        more = f" +{len(graph.unparsed) - 3} more" if len(graph.unparsed) > 3 else ""
        bits.append(f"Not analysed (no parser): {names}{more}.")
    if graph.utilities_hidden:
        bits.append(
            f"{graph.utilities_hidden} widely-called helper(s) hidden as plumbing."
        )
    if graph.uncertain_edges:
        bits.append("Dashed edges were matched by name — treat them as leads, not facts.")
    return " ".join(bits)


def _payload(graph: Graph) -> str:
    """Graph data for the inline script.

    `<` is escaped so a symbol named `</script>` cannot break out of the tag.
    """
    nodes = {
        n.uid: {
            "label": n.display,
            "path": n.path,
            "touched": n.touched,
            "added": n.added,
            "pre": n.pre_existing,
            "gone": n.gone,
            "lines": n.changed_lines,
            "fan": n.fan_in,
            "approx": n.fan_in_shared,
            "hin": n.hidden_in,
            "hout": n.hidden_out,
        }
        for n in graph.nodes
    }
    edges = [
        {
            "src": e.src,
            "dst": e.dst,
            "kind": e.kind,
            "why": _WHY.get(e.provenance, e.provenance),
        }
        for e in graph.edges
    ]
    return json.dumps({"nodes": nodes, "edges": edges}, separators=(",", ":")).replace(
        "<", "\\u003c"
    )


# --- svg -----------------------------------------------------------------------------


def _svg(graph: Graph, lay: layout_mod.Layout) -> str:
    parts = [
        f'<svg id="graph" viewBox="0 0 {lay.width:.0f} {lay.height:.0f}" '
        f'width="{lay.width:.0f}" height="{lay.height:.0f}" '
        'xmlns="http://www.w3.org/2000/svg">',
        "<defs>"
        '<marker id="a-solid" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
        'markerHeight="6" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" class="m-solid"/></marker>'
        '<marker id="a-dash" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
        'markerHeight="6" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" class="m-dash"/></marker>'
        "</defs>",
        '<g class="edges">',
    ]
    for edge in lay.edges:
        parts.append(_edge(edge, lay.placed[edge.src], lay.placed[edge.dst]))
    parts.append('</g><g class="nodes">')

    if lay.isolated_top is not None:
        parts.append(
            f'<text x="{layout_mod.MARGIN}" y="{lay.isolated_top + 12:.0f}" '
            'class="band">NO CONNECTIONS FOUND</text>'
        )
    for placed in lay.placed.values():
        parts.append(_node(placed))

    parts.append("</g></svg>")
    return "".join(parts)


def _edge(edge, src: layout_mod.Placed, dst: layout_mod.Placed) -> str:
    if dst.x >= src.right:
        x1, y1, x2, y2 = src.right, src.cy, dst.x, dst.cy
    elif dst.right <= src.x:
        x1, y1, x2, y2 = src.x, src.cy, dst.right, dst.cy
    else:
        x1, y1, x2, y2 = src.cx, src.bottom, dst.cx, dst.y

    if abs(y2 - y1) < 1 and abs(x2 - x1) < 1:
        return ""
    dx = max(30.0, abs(x2 - x1) / 2)
    d = f"M{x1:.1f},{y1:.1f} C{x1 + dx:.1f},{y1:.1f} {x2 - dx:.1f},{y2:.1f} {x2:.1f},{y2:.1f}"

    certain = edge.kind == CALL and not is_uncertain(edge.provenance)
    cls = "e-call" if certain else ("e-impl" if edge.kind != CALL else "e-soft")
    return (
        f'<path class="edge {cls}" d="{d}" '
        f'marker-end="url(#{"a-solid" if certain else "a-dash"})" '
        f'data-src="{html.escape(edge.src)}" data-dst="{html.escape(edge.dst)}"/>'
    )


def _node(placed: layout_mod.Placed) -> str:
    n = placed.node
    return (
        f'<g class="node {_tone(n)}" data-uid="{html.escape(n.uid)}" tabindex="0" '
        f'role="button" aria-label="{html.escape(n.display)}">'
        f'<rect x="{placed.x:.1f}" y="{placed.y:.1f}" width="{placed.w:.1f}" '
        f'height="{placed.h:.1f}" rx="7"/>'
        f'<text class="n-label" x="{placed.x + 13:.1f}" y="{placed.y + 19:.1f}">'
        f"{html.escape(_short(n.display))}</text>"
        f'<text class="n-meta" x="{placed.x + 13:.1f}" y="{placed.y + 34:.1f}">'
        f"{html.escape(_meta(n))}</text>"
        "</g>"
    )


def _tone(node: Node) -> str:
    """Two independent signals, two independent channels.

    Colour says *what happened* to the symbol — new, changed, or untouched context.
    Reach is a magnitude, not a category, so it rides on the border instead of
    replacing the fill; a widely-called new file then reads as both at once rather
    than forcing a choice between them.
    """
    state = "ctx" if not node.touched else ("new" if node.added else "chg")
    return f"{state} reach" if node.fan_in >= _HI_FAN_IN else state


def _meta(node: Node) -> str:
    """Terse on the node itself — the detail panel carries the full sentence."""
    bits = []
    if node.gone:
        bits.append("removed")
    elif node.added:
        bits.append("new")
    elif node.touched:
        bits.append(f"{node.changed_lines} changed")
        if node.pre_existing:
            bits.append("existing")
    if node.fan_in:
        approx = "~" if node.fan_in_shared else ""
        bits.append(f"{approx}{node.fan_in} callers")
    if not bits:
        bits.append("unchanged")
    hidden = node.hidden_in + node.hidden_out
    if hidden:
        bits.append(f"+{hidden} hidden")
    return " · ".join(bits)


def _short(text: str) -> str:
    """Elide the middle, never an end.

    `file.Class.method` carries its two most identifying parts at the edges, so cutting
    from the left throws away the filename the label exists to show. The class in the
    middle is what can go.
    """
    if len(text) <= _MAX_LABEL:
        return text
    keep = _MAX_LABEL - 1
    head = keep // 2
    return text[:head] + "…" + text[-(keep - head) :]


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg:#fbfcfd; --surface:#fff; --panel:#f3f6f9;
  --line:#dce3ea; --line-soft:#eaeff4;
  --ink:#16202b; --dim:#5d6c7b; --faint:#8b99a7;
  --accent:#0f766e; --accent-bg:#d8f0ec; --accent-line:#4f9f97;
  --hot:#b45309; --hot-bg:#fbeedd;
  --new:#15803d; --new-bg:#ddf3e3;
  --grid:rgba(15,118,110,.05);
  --shadow:0 1px 2px rgba(22,32,43,.06),0 6px 18px rgba(22,32,43,.07);
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}}
@media (prefers-color-scheme:dark) {{
  :root:not([data-theme="light"]) {{
    --bg:#0e1419; --surface:#151d24; --panel:#1a232b;
    --line:#2a3743; --line-soft:#212c35;
    --ink:#e5edf3; --dim:#95a6b5; --faint:#6d7f8e;
    --accent:#4fd1c5; --accent-bg:#113b37; --accent-line:#2f8d84;
    --hot:#f0a45a; --hot-bg:#382614;
    --new:#5ec97e; --new-bg:#11331f;
    --grid:rgba(79,209,197,.045);
    --shadow:0 1px 2px rgba(0,0,0,.4),0 6px 18px rgba(0,0,0,.35);
  }}
}}
*{{box-sizing:border-box}}
html,body{{height:100%}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans);
  -webkit-font-smoothing:antialiased}}

header{{padding:20px 24px 15px;border-bottom:1px solid var(--line);background:var(--surface)}}
h1{{margin:0 0 3px;font-size:18px;font-weight:650;letter-spacing:-.01em}}
.sub{{margin:0;color:var(--dim);font-size:13.5px}}
.stats{{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}}
.stats span{{font:11.5px var(--mono);color:var(--dim);background:var(--panel);
  border:1px solid var(--line-soft);border-radius:5px;padding:3px 8px}}
.stats b{{color:var(--ink);font-weight:600;margin-right:5px}}
.stats .warn{{color:var(--hot);background:var(--hot-bg);border-color:transparent}}
.stats .warn b{{color:var(--hot)}}
.banner{{margin-top:11px;padding:8px 11px;border-radius:7px;font-size:12.5px;
  background:var(--hot-bg);color:var(--hot);border:1px solid transparent}}
.banner b{{font-weight:650}}

.shell{{display:grid;grid-template-columns:286px 1fr;height:calc(100% - 128px);
  min-height:500px;transition:grid-template-columns .14s ease}}
/* The detail panel is a column, not an overlay — selecting a node must never
   cover the graph you are trying to read. */
.shell.open{{grid-template-columns:286px 1fr 340px}}
aside{{border-right:1px solid var(--line);background:var(--surface);overflow-y:auto;
  padding:13px 0}}
.aside-h{{font:10px var(--mono);letter-spacing:.1em;text-transform:uppercase;
  color:var(--faint);padding:0 16px 8px}}
aside ul{{list-style:none;margin:0;padding:0}}
aside li.empty{{padding:8px 16px;color:var(--faint);font-size:13px}}
aside button{{display:flex;align-items:center;gap:9px;width:100%;text-align:left;
  background:none;border:0;border-left:2px solid transparent;padding:6px 16px;
  cursor:pointer;font:inherit;color:var(--ink)}}
aside button:hover{{background:var(--panel)}}
aside button[aria-current="true"]{{background:var(--accent-bg);border-left-color:var(--accent)}}
.b-lines,.b-new{{flex:none;font:10px var(--mono);border-radius:4px;padding:2px 5px;
  min-width:28px;text-align:center}}
.b-lines{{background:var(--panel);color:var(--dim);border:1px solid var(--line-soft)}}
.b-new{{background:var(--new-bg);color:var(--new);font-weight:600}}
.sym{{flex:1;font:12.5px var(--mono);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}}
.reach{{flex:none;font:10.5px var(--mono);color:var(--faint);font-style:normal}}
.reach.hot{{color:var(--hot);font-weight:600}}

main{{position:relative;overflow:auto;
  background-image:linear-gradient(var(--grid) 1px,transparent 1px),
    linear-gradient(90deg,var(--grid) 1px,transparent 1px);background-size:24px 24px}}
svg{{display:block;padding:20px}}

.node{{cursor:pointer}}
.node rect{{fill:var(--surface);stroke:var(--line);stroke-width:1.5}}
.node:focus{{outline:none}}
.node:focus rect,.node:hover rect{{stroke:var(--accent)}}
.n-label{{font:600 12.5px var(--mono);fill:var(--ink)}}
.n-meta{{font:10.5px var(--mono);fill:var(--faint)}}
.node.chg rect{{stroke:var(--accent);stroke-width:2;fill:var(--accent-bg)}}
.node.new rect{{stroke:var(--new);stroke-width:2;fill:var(--new-bg)}}
.node.ctx rect{{stroke-dasharray:4 3}}
/* Reach layers on top of state: the fill still says new/changed, the amber ring
   says "and a lot depends on it". */
.node.reach rect{{stroke:var(--hot);stroke-width:3}}
.node.ctx .n-label{{font-weight:400;fill:var(--dim)}}
.band{{font:10px var(--mono);fill:var(--faint);letter-spacing:.1em}}

.edge{{fill:none}}
.e-call{{stroke:var(--accent-line);stroke-width:1.7}}
.e-soft{{stroke:var(--faint);stroke-width:1.5;stroke-dasharray:5 4}}
.e-impl{{stroke:var(--faint);stroke-width:1.3;stroke-dasharray:2 3}}
.m-solid{{fill:var(--accent-line)}}
.m-dash{{fill:var(--faint)}}

/* Focus recedes the unrelated rather than hiding it, so context is never lost. */
svg.focusing .node,svg.focusing .edge{{opacity:.12;transition:opacity .13s ease}}
svg.focusing .node.on,svg.focusing .edge.on{{opacity:1}}
svg.focusing .node.pick rect{{stroke-width:3}}

.detail{{display:none;background:var(--surface);border-left:1px solid var(--line);
  padding:16px 18px;overflow-y:auto;min-width:0}}
.shell.open .detail{{display:block}}
.d-top{{display:flex;justify-content:space-between;align-items:start;gap:10px}}
.d-name{{font:600 13.5px var(--mono);word-break:break-all;line-height:1.35}}
.d-close{{flex:none;background:none;border:0;color:var(--faint);cursor:pointer;
  font-size:18px;line-height:1;padding:0 2px}}
.d-close:hover{{color:var(--ink)}}
.d-path{{font:11px var(--mono);color:var(--faint);margin:4px 0 11px;word-break:break-all}}
.d-tags{{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:6px}}
.d-tags i{{font:10.5px var(--mono);font-style:normal;padding:2px 7px;border-radius:4px;
  background:var(--panel);color:var(--dim)}}
.d-tags i.hot{{background:var(--hot-bg);color:var(--hot);font-weight:600}}
.d-tags i.new{{background:var(--new-bg);color:var(--new);font-weight:600}}
.d-sec{{font:10px var(--mono);letter-spacing:.09em;text-transform:uppercase;
  color:var(--faint);margin:12px 0 5px}}
.d-list{{list-style:none;margin:0;padding:0}}
.d-list li{{padding:3px 0;border-bottom:1px solid var(--line-soft)}}
.d-list li:last-child{{border:0}}
.d-list button{{background:none;border:0;padding:0;cursor:pointer;text-align:left;
  font:11.5px var(--mono);color:var(--accent);width:100%}}
.d-list button:hover{{text-decoration:underline}}
.d-dir{{color:var(--faint);font-weight:600}}
.arrow-key b{{color:var(--dim);font:11px var(--mono)}}
.d-why{{display:block;font:10px var(--sans);color:var(--faint);margin-top:1px}}
.d-none{{font-size:12px;color:var(--faint);margin:0}}
.d-hidden{{font:11px var(--sans);color:var(--hot);margin:5px 0 0}}

footer{{padding:10px 24px;border-top:1px solid var(--line);background:var(--surface);
  font-size:11.5px;color:var(--faint);display:flex;justify-content:space-between;
  gap:14px;flex-wrap:wrap}}
.legend{{display:flex;gap:13px;flex-wrap:wrap}}
.legend i{{font-style:normal;display:inline-flex;align-items:center;gap:5px}}
.dot{{width:9px;height:9px;border-radius:3px;display:inline-block;border:1.5px solid}}
.dot.reach{{background:transparent;border-color:var(--hot);border-width:2.5px}}
.dot.chg{{background:var(--accent-bg);border-color:var(--accent)}}
.dot.new{{background:var(--new-bg);border-color:var(--new)}}
.dot.ctx{{background:transparent;border-color:var(--line);border-style:dashed}}

@media (max-width:820px) {{
  .shell,.shell.open{{grid-template-columns:1fr;height:auto}}
  aside{{border-right:0;border-bottom:1px solid var(--line);max-height:200px}}
  main{{min-height:62vh}}
  .detail{{border-left:0;border-top:1px solid var(--line);max-height:46vh}}
}}
</style></head><body>

<header>
  <h1>{title}</h1>
  <p class="sub">{subtitle}</p>
  <div class="stats">{stats}</div>
  {banner}
</header>

<div class="shell" id="shell">
  <aside>
    <div class="aside-h">Changed · widest reach first</div>
    <ul id="list">{sidebar}</ul>
  </aside>
  <main id="canvas">{svg}</main>
  <section class="detail" id="detail" aria-live="polite"></section>
</div>

<footer>
  <div class="legend">
    <i><span class="dot new"></span>new file</i>
    <i><span class="dot chg"></span>changed</i>
    <i><span class="dot ctx"></span>unchanged context</i>
    <i class="arrow-key"><b>existing</b> edits code that was already there</i>
    <i><span class="dot reach"></span>called from {hot}+ places</i>
    <i class="arrow-key"><b>A&nbsp;&rarr;&nbsp;B</b> A calls B</i>
  </div>
  <div>{footnote} Click a node to focus · Esc clears · {stamp}</div>
</footer>

<script type="application/json" id="data">{data}</script>
<script>
(function () {{
  var G = JSON.parse(document.getElementById("data").textContent);
  var svg = document.getElementById("graph");
  var detail = document.getElementById("detail");
  var list = document.getElementById("list");
  var shell = document.getElementById("shell");
  var current = null;
  var inbound = {{}}, outbound = {{}};

  G.edges.forEach(function (e) {{
    (outbound[e.src] = outbound[e.src] || []).push(e);
    (inbound[e.dst] = inbound[e.dst] || []).push(e);
  }});

  function q(sel, val) {{
    var all = svg.querySelectorAll(sel), i;
    for (i = 0; i < all.length; i++) {{
      if (all[i].getAttribute("data-uid") === val) return all[i];
    }}
    return null;
  }}
  function nodeEl(uid) {{ return q(".node", uid); }}

  function clear() {{
    current = null;
    svg.classList.remove("focusing");
    var marked = svg.querySelectorAll(".on, .pick"), i;
    for (i = 0; i < marked.length; i++) marked[i].classList.remove("on", "pick");
    shell.classList.remove("open");
    var cur = list.querySelectorAll("[aria-current]");
    for (i = 0; i < cur.length; i++) cur[i].removeAttribute("aria-current");
  }}

  function focus(uid) {{
    if (!G.nodes[uid]) return;
    if (current === uid) {{ clear(); return; }}
    clear();
    current = uid;

    var keep = {{}}, i;
    keep[uid] = true;
    (outbound[uid] || []).forEach(function (e) {{ keep[e.dst] = true; }});
    (inbound[uid] || []).forEach(function (e) {{ keep[e.src] = true; }});

    svg.classList.add("focusing");
    Object.keys(keep).forEach(function (k) {{
      var el = nodeEl(k);
      if (el) el.classList.add("on");
    }});
    var picked = nodeEl(uid);
    if (picked) picked.classList.add("pick");

    var paths = svg.querySelectorAll(".edge");
    for (i = 0; i < paths.length; i++) {{
      var p = paths[i];
      if (p.getAttribute("data-src") === uid || p.getAttribute("data-dst") === uid) {{
        p.classList.add("on");
      }}
    }}

    var buttons = list.querySelectorAll("[data-goto]");
    for (i = 0; i < buttons.length; i++) {{
      if (buttons[i].getAttribute("data-goto") === uid) {{
        buttons[i].setAttribute("aria-current", "true");
        buttons[i].scrollIntoView({{block: "nearest"}});
      }}
    }}
    if (picked && picked.scrollIntoView) {{
      picked.scrollIntoView({{block: "center", inline: "center", behavior: "smooth"}});
    }}
    render(uid);
  }}

  function esc(s) {{
    return String(s).replace(/[&<>"]/g, function (c) {{
      return {{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}}[c];
    }});
  }}

  function related(edges, dir) {{
    if (!edges || !edges.length) return '<p class="d-none">nothing</p>';
    var html = "";
    edges.forEach(function (e) {{
      var other = dir === "out" ? e.dst : e.src;
      var meta = G.nodes[other];
      if (!meta) return;
      var why = e.why ? '<span class="d-why">' + esc(e.why) + "</span>" : "";
      var arrow = dir === "out" ? "\u2192 " : "\u2190 ";
      var verb = e.kind === "call" ? "" : "implements ";
      html += '<li><button type="button" data-goto="' + esc(other) + '">' +
        '<span class="d-dir">' + arrow + "</span>" + verb + esc(meta.label) + why +
        "</button></li>";
    }});
    return html ? '<ul class="d-list">' + html + "</ul>" : '<p class="d-none">nothing</p>';
  }}

  function hiddenNote(count) {{
    if (!count) return "";
    return '<p class="d-hidden">' + count +
      " more not drawn \u2014 raise --max-nodes to see " +
      (count === 1 ? "it" : "them") + ".</p>";
  }}

  function render(uid) {{
    var n = G.nodes[uid], tags = [];
    if (n.gone) tags.push('<i class="hot">removed</i>');
    else if (n.added) tags.push('<i class="new">new file</i>');
    else if (n.touched) {{
      tags.push("<i>" + n.lines + " line" + (n.lines === 1 ? "" : "s") + " changed</i>");
      if (n.pre) tags.push('<i class="hot">edits existing code</i>');
    }} else tags.push("<i>unchanged — shown for context</i>");
    if (n.fan) {{
      tags.push('<i class="' + (n.fan >= 5 ? "hot" : "") + '">called from ' +
        (n.approx ? "~" : "") + n.fan + " place" + (n.fan === 1 ? "" : "s") + "</i>");
    }}

    detail.innerHTML =
      '<div class="d-top"><div class="d-name">' + esc(n.label) + "</div>" +
      '<button class="d-close" type="button" aria-label="Close">&times;</button></div>' +
      '<div class="d-path">' + esc(n.path) + "</div>" +
      '<div class="d-tags">' + tags.join("") + "</div>" +
      '<div class="d-sec">Calls out to &rarr;</div>' + related(outbound[uid], "out") +
      hiddenNote(n.hout) +
      '<div class="d-sec">&larr; Called by</div>' + related(inbound[uid], "in") +
      hiddenNote(n.hin);
    shell.classList.add("open");
    detail.querySelector(".d-close").addEventListener("click", clear);
  }}

  svg.addEventListener("click", function (ev) {{
    var t = ev.target, g = null;
    while (t && t !== svg) {{
      if (t.classList && t.classList.contains("node")) {{ g = t; break; }}
      t = t.parentNode;
    }}
    if (g) focus(g.getAttribute("data-uid")); else clear();
  }});
  svg.addEventListener("keydown", function (ev) {{
    if (ev.key !== "Enter" && ev.key !== " ") return;
    var t = ev.target;
    if (t && t.classList && t.classList.contains("node")) {{
      ev.preventDefault();
      focus(t.getAttribute("data-uid"));
    }}
  }});
  document.addEventListener("click", function (ev) {{
    var t = ev.target;
    while (t && t !== document) {{
      if (t.getAttribute && t.getAttribute("data-goto")) {{
        focus(t.getAttribute("data-goto"));
        return;
      }}
      t = t.parentNode;
    }}
  }});
  document.addEventListener("keydown", function (ev) {{
    if (ev.key === "Escape") clear();
  }});
}})();
</script>
</body></html>
"""
