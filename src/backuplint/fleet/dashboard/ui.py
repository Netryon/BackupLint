"""HTML UI for the v0.6 read-only dashboard (visual redesign)."""

from __future__ import annotations

import html
from typing import Any
from urllib.parse import urlencode


# Official BackupLint matched-state mark — inline SVG (CSP: default-src 'none'
# blocks data: / external <img>; inline SVG is part of the HTML document).
# Geometry traced from the supplied 64×64 master PNG (two identical states).
def _logo_img(*, css_class: str = "logo-mark", size: int = 32) -> str:
    """Return official matched-state mark as inline SVG (white via currentColor)."""
    return (
        f'<svg class="{css_class}" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 64 64" width="{size}" height="{size}" '
        f'fill="currentColor" aria-hidden="true">'
        '<path d="M4 20h18v8H4zm38 0h18v8H42zM20 12h24v8H20z'
        'M22 20h4v8h-4zm16 0h4v8h-4z"/>'
        '<path d="M4 44h18v8H4zm38 0h18v8H42zM20 36h24v8H20z'
        'M22 44h4v8h-4zm16 0h4v8h-4z"/>'
        "</svg>"
    )


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _css() -> str:
    return """
:root {
  --bg: #171b21;
  --surface: #20252B;
  --surface-2: #262c34;
  --text: #FFFFFF;
  --muted: #9aa3ad;
  --line: #323941;
  --line-soft: #2a3038;
  --ok: #2fbf71;
  --ok-bg: rgba(47,191,113,.12);
  --warn: #e0a53a;
  --warn-bg: rgba(224,165,58,.12);
  --bad: #e55353;
  --bad-bg: rgba(229,83,83,.12);
  --info: #5b8def;
  --info-bg: rgba(49,85,231,.14);
  --accent: #3155E7;
  --accent-hover: #4066f0;
  --shadow: 0 1px 2px rgba(0,0,0,.28), 0 8px 24px rgba(0,0,0,.18);
  --radius: 10px;
  --sidebar-w: 248px;
  --font: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; }
body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  line-height: 1.45;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.app {
  display: grid;
  grid-template-columns: var(--sidebar-w) minmax(0, 1fr);
  min-height: 100vh;
}
.sidebar {
  background: var(--surface);
  border-right: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  padding: 1.1rem 0.9rem 1rem;
  position: sticky;
  top: 0;
  height: 100vh;
}
.brand-block {
  display: flex;
  align-items: center;
  gap: .75rem;
  padding: .35rem .55rem 1.15rem;
  border-bottom: 1px solid var(--line-soft);
  margin-bottom: .85rem;
  color: var(--text);
  text-decoration: none;
}
.brand-block:hover { text-decoration: none; }
.brand-avatar {
  width: 36px; height: 36px; border-radius: 8px;
  background: var(--accent);
  display: grid; place-items: center;
  flex: 0 0 auto;
  color: #fff;
}
.brand-avatar .logo-mark { width: 22px; height: 22px; display: block; object-fit: contain; }
.login-brand .brand-avatar .logo-mark { width: 30px; height: 30px; }
.brand-text { min-width: 0; }
.brand-text .wordmark {
  display: block;
  font-weight: 700;
  font-size: 1.05rem;
  letter-spacing: .01em;
  color: var(--text);
}
.brand-text .tag {
  display: block;
  color: var(--muted);
  font-size: .72rem;
  margin-top: .12rem;
}
.side-nav { display: flex; flex-direction: column; gap: .25rem; flex: 1; }
.side-nav a {
  display: flex; align-items: center; gap: .65rem;
  color: var(--muted);
  padding: .62rem .7rem;
  border-radius: 8px;
  font-weight: 500;
  font-size: .92rem;
  text-decoration: none;
}
.side-nav a:hover { background: var(--surface-2); color: var(--text); text-decoration: none; }
.side-nav a.active {
  background: var(--accent);
  color: #fff;
}
.side-nav .nav-ico {
  width: 18px; height: 18px; opacity: .9; flex: 0 0 auto;
}
.sidebar-foot {
  border-top: 1px solid var(--line-soft);
  padding-top: .85rem;
  margin-top: .75rem;
  font-size: .72rem;
  color: var(--muted);
}
.status-dot {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: var(--ok); margin-right: .35rem; vertical-align: middle;
}
.content {
  display: flex; flex-direction: column; min-width: 0;
}
.topbar {
  display: flex; justify-content: space-between; align-items: center;
  gap: 1rem; flex-wrap: wrap;
  padding: 1.15rem 1.5rem .35rem;
}
.topbar h1 { margin: 0; font-size: 1.45rem; font-weight: 650; }
.topbar .sub { color: var(--muted); font-size: .9rem; margin: .2rem 0 0; }
.topbar-actions { display: flex; align-items: center; gap: .6rem; }
form.logout { margin: 0; }
form.logout button, .btn {
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: .45rem .75rem;
  font: inherit;
  cursor: pointer;
}
form.logout button:hover, .btn:hover { border-color: var(--accent); }
main {
  padding: .75rem 1.5rem 2.5rem;
  max-width: 1280px;
  width: 100%;
}
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: .75rem;
}
.card {
  background: var(--surface);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius);
  padding: .95rem 1rem;
  box-shadow: var(--shadow);
}
.card .n { font-size: 1.55rem; font-weight: 700; letter-spacing: -.02em; }
.card .l { color: var(--muted); font-size: .82rem; margin-top: .15rem; }
.card.tone-ok .n { color: var(--ok); }
.card.tone-warn .n { color: var(--warn); }
.card.tone-bad .n { color: var(--bad); }
.card.tone-accent .n { color: #8ea4ff; }
.section-label {
  color: var(--muted);
  font-size: .75rem;
  text-transform: uppercase;
  letter-spacing: .06em;
  margin: 1.15rem 0 .55rem;
  font-weight: 600;
}
.panel {
  background: var(--surface);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius);
  padding: 1rem 1.05rem;
  box-shadow: var(--shadow);
  margin-top: .85rem;
}
.panel-head {
  display: flex; justify-content: space-between; align-items: baseline;
  gap: .75rem; margin-bottom: .55rem;
}
.panel-head h2 { margin: 0; font-size: 1rem; font-weight: 600; }
.charts {
  display: grid;
  grid-template-columns: 1.4fr 1fr;
  gap: .85rem;
  margin-top: .85rem;
}
.chart-bars {
  display: flex; align-items: flex-end; gap: .55rem;
  height: 140px; padding: .5rem 0 .25rem;
}
.chart-bars .bar-col {
  flex: 1; display: flex; flex-direction: column; align-items: center; gap: .35rem;
  min-width: 0; height: 100%; justify-content: flex-end;
}
.chart-bars .bar {
  width: 100%; max-width: 48px; border-radius: 6px 6px 2px 2px;
  min-height: 4px;
}
.chart-bars .bar-lbl { font-size: .72rem; color: var(--muted); }
.trend-stack {
  display: flex; align-items: flex-end; gap: .35rem; height: 140px;
  overflow-x: auto; padding-bottom: .25rem;
}
.trend-col {
  flex: 0 0 auto; width: 28px; display: flex; flex-direction: column;
  justify-content: flex-end; align-items: center; height: 100%; gap: .25rem;
}
.trend-col .stack {
  width: 100%; display: flex; flex-direction: column-reverse;
  border-radius: 4px 4px 2px 2px; overflow: hidden; min-height: 4px;
}
.trend-col .seg { width: 100%; min-height: 2px; }
.trend-col .lbl { font-size: .65rem; color: var(--muted); writing-mode: horizontal-tb; }
.time-range { margin: .5rem 0 1rem; }
.time-range select { min-width: 8rem; }
.alerts-table td { font-size: .85rem; }
.donut-wrap {
  display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;
  min-height: 140px;
}
.donut {
  width: 120px; height: 120px; border-radius: 50%;
  display: grid; place-items: center;
  position: relative;
  flex: 0 0 auto;
}
.donut::after {
  content: "";
  position: absolute; inset: 22px;
  background: var(--surface);
  border-radius: 50%;
}
.donut-center {
  position: relative; z-index: 1; text-align: center;
}
.donut-center .dn { font-weight: 700; font-size: 1.15rem; line-height: 1.1; }
.donut-center .dl { font-size: .68rem; color: var(--muted); }
.legend { font-size: .82rem; color: var(--muted); }
.legend div { margin: .25rem 0; }
.swatch {
  display: inline-block; width: 9px; height: 9px; border-radius: 2px;
  margin-right: .4rem; vertical-align: middle;
}
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th, td {
  text-align: left; padding: .62rem .5rem;
  border-bottom: 1px solid var(--line-soft); vertical-align: top;
}
th {
  color: var(--muted); font-weight: 600; font-size: .72rem;
  text-transform: uppercase; letter-spacing: .04em;
}
.pill {
  display: inline-block;
  padding: .12rem .5rem;
  border-radius: 999px;
  border: 1px solid transparent;
  font-size: .75rem;
  font-weight: 600;
  letter-spacing: .01em;
  background: var(--surface-2);
  color: var(--muted);
}
.pill.online, .pill.PASS {
  color: var(--ok); background: var(--ok-bg); border-color: rgba(47,191,113,.28);
}
.pill.stale, .pill.WARN {
  color: var(--warn); background: var(--warn-bg); border-color: rgba(224,165,58,.28);
}
.pill.offline, .pill.FAIL, .pill.ERROR, .pill.QUEUE_OVERFLOW {
  color: var(--bad); background: var(--bad-bg); border-color: rgba(229,83,83,.28);
}
form.filters {
  display: flex; flex-wrap: wrap; gap: .5rem;
  margin: .85rem 0; align-items: center;
}
input, select, button {
  background: var(--surface-2); color: var(--text);
  border: 1px solid var(--line); border-radius: 8px;
  padding: .48rem .65rem; font: inherit; max-width: 100%;
}
button { cursor: pointer; }
button[type="submit"], .btn-primary {
  background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600;
}
button[type="submit"]:hover, .btn-primary:hover { background: var(--accent-hover); }
.muted { color: var(--muted); }
.err { color: var(--bad); margin-bottom: .75rem; }
h1 { font-size: 1.4rem; margin: 0 0 .75rem; word-break: break-word; }
h2 { font-size: 1.02rem; margin: 1.35rem 0 .55rem; }
.pager { display: flex; gap: .75rem; flex-wrap: wrap; margin-top: 1rem; }
.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
td, .muted, h1 { overflow-wrap: anywhere; word-break: break-word; }
.readonly-banner {
  display: inline-flex; align-items: center; gap: .4rem;
  background: var(--info-bg); color: #a8b9ff;
  border: 1px solid rgba(49,85,231,.35);
  border-radius: 999px; padding: .2rem .65rem;
  font-size: .75rem; font-weight: 600; margin-bottom: .75rem;
}
/* Login (no sidebar) */
body.login-body {
  background:
    radial-gradient(900px 480px at 15% -10%, rgba(49,85,231,.22), transparent 55%),
    radial-gradient(700px 400px at 90% 110%, rgba(49,85,231,.12), transparent 50%),
    var(--bg);
  min-height: 100vh;
  display: grid; place-items: center; padding: 1.5rem;
}
.login {
  width: min(400px, 100%);
  background: var(--surface);
  border: 1px solid var(--line-soft);
  border-radius: 14px;
  padding: 2rem 1.75rem 1.75rem;
  box-shadow: var(--shadow);
}
.login-brand {
  display: flex; flex-direction: column; align-items: center; text-align: center;
  gap: .75rem; margin-bottom: 1.35rem;
}
.login-brand .brand-avatar {
  width: 52px; height: 52px; border-radius: 12px;
}
.login-brand .brand-avatar .logo-mark { width: 30px; height: 30px; }
.login-brand .wordmark {
  font-size: 1.35rem; font-weight: 700; letter-spacing: .01em;
}
.login h1 { text-align: center; font-size: 1.15rem; margin: 0 0 .35rem; }
.login .muted { text-align: center; margin: 0 0 1.1rem; }
.login label { display: block; font-size: .85rem; color: var(--muted); margin-bottom: .35rem; }
.login input[type="password"] { width: 100%; }
.login button[type="submit"] { width: 100%; margin-top: .35rem; padding: .65rem .8rem; }
@media (max-width: 900px) {
  .app { grid-template-columns: 1fr; }
  .sidebar {
    position: static; height: auto;
    border-right: none; border-bottom: 1px solid var(--line-soft);
  }
  .side-nav { flex-direction: row; flex-wrap: wrap; }
  .sidebar-foot { display: none; }
  .charts { grid-template-columns: 1fr; }
}
@media (max-width: 800px) {
  main, .topbar { padding-left: 1rem; padding-right: 1rem; }
  .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  table { font-size: .82rem; }
  form.filters > * { flex: 1 1 140px; }
}
@media (max-width: 420px) {
  .cards { grid-template-columns: 1fr 1fr; }
}
"""


def _nav_icon(name: str) -> str:
    icons = {
        "fleet": (
            '<svg class="nav-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.75"><rect x="3" y="3" width="7" height="7" rx="1.5"/>'
            '<rect x="14" y="3" width="7" height="7" rx="1.5"/>'
            '<rect x="3" y="14" width="7" height="7" rx="1.5"/>'
            '<rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>'
        ),
        "policy": (
            '<svg class="nav-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.75"><path d="M7 4h7l3 3v13H7z"/>'
            '<path d="M14 4v3h3M9 12h6M9 16h6"/></svg>'
        ),
        "history": (
            '<svg class="nav-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.75"><circle cx="12" cy="12" r="8"/>'
            '<path d="M12 8v5l3 2"/></svg>'
        ),
    }
    return icons.get(name, "")


def _layout(
    title: str,
    body: str,
    *,
    csrf: str = "",
    active: str = "",
    subtitle: str = "",
) -> str:
    logout = ""
    if csrf:
        logout = f"""
        <form class="logout" method="post" action="/dashboard/logout">
          <input type="hidden" name="csrf" value="{_e(csrf)}" />
          <input type="hidden" name="csrf_token" value="{_e(csrf)}" />
          <button type="submit">Log out</button>
        </form>"""
    fleet_cls = "active" if active == "fleet" else ""
    policy_cls = "active" if active == "policy" else ""
    history_cls = "active" if active == "history" else ""
    sub = f'<p class="sub">{_e(subtitle)}</p>' if subtitle else ""
    # Page bodies own the primary <h1>; topbar only carries logout + optional subtitle.
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_e(title)} · BackupLint</title>
<style>{_css()}</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <a class="brand brand-block" href="/dashboard/">
      <span class="brand-avatar">{_logo_img(size=22)}</span>
      <span class="brand-text">
        <span class="wordmark">BackupLint</span>
        <span class="tag">Verify today. Trust tomorrow.</span>
      </span>
    </a>
    <nav class="side-nav">
      <a class="{fleet_cls}" href="/dashboard/">{_nav_icon("fleet")} Fleet</a>
      <a class="{policy_cls}" href="/dashboard/policy">{_nav_icon("policy")} Policy</a>
      <a class="{history_cls}" href="/dashboard/history">{_nav_icon("history")} History</a>
    </nav>
    <div class="sidebar-foot">
      <div><span class="status-dot"></span>Read-only fleet view</div>
      <div style="margin-top:.35rem">Presence ≠ audit health</div>
    </div>
  </aside>
  <div class="content">
    <div class="topbar">
      <div>{sub}</div>
      <div class="topbar-actions">{logout}</div>
    </div>
    <main>
{body}
    </main>
  </div>
</div>
</body>
</html>"""


def page_login(*, error: str = "") -> str:
    err = f'<div class="err">{_e(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Login · BackupLint</title>
<style>{_css()}</style>
</head>
<body class="login-body">
<div class="login">
  <div class="login-brand">
    <span class="brand-avatar">{_logo_img(size=30)}</span>
    <div class="wordmark">BackupLint</div>
  </div>
  <h1>Dashboard login</h1>
  <p class="muted">Operator session for read-only fleet visibility.</p>
  {err}
  <form method="post" action="/dashboard/login">
    <label for="password">Password</label>
    <input id="password" type="password" name="password" autocomplete="current-password" required minlength="12"/>
    <p style="margin-top:1rem"><button type="submit">Sign in</button></p>
  </form>
</div>
</body>
</html>"""


def page_error(title: str, detail: str) -> str:
    body = f"<h1>{_e(title)}</h1><p class='muted'>{_e(detail)}</p>"
    return _layout(title, body)


def _presence_donut(online: int, stale: int, offline: int, total: int) -> str:
    if total <= 0:
        grad = "conic-gradient(var(--line) 0 100%)"
    else:
        o = online / total * 100
        s = stale / total * 100
        # offline fills the remainder
        grad = (
            f"conic-gradient(var(--ok) 0 {o:.2f}%, "
            f"var(--warn) {o:.2f}% {o + s:.2f}%, "
            f"var(--bad) {o + s:.2f}% 100%)"
        )
    return f"""
<div class="panel">
  <div class="panel-head"><h2>Agent presence</h2></div>
  <div class="donut-wrap">
    <div class="donut" style="background:{grad}">
      <div class="donut-center"><div class="dn">{_e(total)}</div><div class="dl">Agents</div></div>
    </div>
    <div class="legend">
      <div><span class="swatch" style="background:var(--ok)"></span>Online {_e(online)}</div>
      <div><span class="swatch" style="background:var(--warn)"></span>Stale {_e(stale)}</div>
      <div><span class="swatch" style="background:var(--bad)"></span>Offline {_e(offline)}</div>
    </div>
  </div>
  <p class="muted" style="margin:.5rem 0 0;font-size:.78rem">Heartbeat freshness only — not audit health.</p>
</div>
"""


def _time_range_select(
    current: str,
    *,
    action: str,
    hidden: dict[str, str],
    extra: str = "",
) -> str:
    hidden_inputs = "".join(
        f'<input type="hidden" name="{_e(k)}" value="{_e(v)}"/>'
        for k, v in hidden.items()
        if v
    )
    return f"""
<form class="filters time-range" method="get" action="{_e(action)}">
  <label class="muted">Time range</label>
  <select name="time_range">
    {_opt("1h", current)}{_opt("24h", current)}{_opt("7d", current)}
  </select>
  {hidden_inputs}
  {extra}
  <button type="submit">Apply</button>
</form>
"""


def _recent_alerts_panel(alerts: dict[str, Any]) -> str:
    rows = []
    for item in alerts.get("items") or []:
        kind = str(item.get("alert_kind") or item.get("status") or "")
        pill_cls = kind
        if kind in {"DATA_GAP", "QUEUE_OVERFLOW"}:
            pill_cls = "ERROR"
        elif kind == "PROTOCOL":
            pill_cls = "WARN"
        aid = item.get("agent_id")
        agent_cell = (
            f"<a href='/dashboard/agents/{_e(aid)}'>{_e(aid)}</a>"
            if aid
            else "—"
        )
        rows.append(
            "<tr>"
            f"<td>{_e(item.get('occurred_at'))}</td>"
            f"<td>{agent_cell}</td>"
            f"<td><span class='pill {_e(pill_cls)}'>{_e(kind)}</span></td>"
            f"<td>{_e(item.get('event_type'))}</td>"
            f"<td>{_e(item.get('summary'))}</td>"
            "</tr>"
        )
    body = (
        "".join(rows)
        if rows
        else "<tr><td colspan='5' class='muted'>No alerts in this range</td></tr>"
    )
    return f"""
<div class="panel">
  <div class="panel-head"><h2>Recent alerts</h2></div>
  <p class="muted" style="margin:0 0 .5rem;font-size:.78rem">
    FAIL, ERROR, DATA_GAP, queue overflow, and protocol compatibility — not generic WARN.
  </p>
  <div class="table-wrap alerts-table"><table><thead><tr>
    <th>Occurred</th><th>Agent</th><th>Kind</th><th>Type</th><th>Summary</th>
  </tr></thead><tbody>{body}</tbody></table></div>
</div>
"""


def _audit_trend_chart(trend: dict[str, Any]) -> str:
    buckets = trend.get("buckets") or []
    if not buckets:
        return """
<div class="panel">
  <div class="panel-head"><h2>Audit results over time</h2></div>
  <p class="muted">No PASS/WARN/FAIL/ERROR events in this range.</p>
</div>
"""
    colors = {  # nosec B105
        "PASS": "var(--ok)",
        "WARN": "var(--warn)",
        "FAIL": "var(--bad)",
        "ERROR": "var(--bad)",
    }
    peak = max(int(b.get("total") or 0) for b in buckets) or 1
    cols = []
    for item in buckets:
        counts = item.get("counts") or {}
        total = int(item.get("total") or 0)
        height_pct = max(6, int(100 * total / peak)) if total else 6
        segs = []
        for key in ("PASS", "WARN", "FAIL", "ERROR"):
            n = int(counts.get(key) or 0)
            if n <= 0:
                continue
            seg_h = max(2, int(height_pct * n / total))
            segs.append(
                f'<div class="seg" style="height:{seg_h}px;background:{colors[key]}"></div>'
            )
        label = str(item.get("bucket") or "")[-5:]
        cols.append(
            f'<div class="trend-col"><div class="stack" style="height:{height_pct}%">'
            f'{"".join(segs)}</div><div class="lbl">{_e(label)}</div></div>'
        )
    unit = trend.get("bucket_unit") or "bucket"
    return f"""
<div class="panel">
  <div class="panel-head"><h2>Audit results over time</h2></div>
  <div class="trend-stack">{"".join(cols)}</div>
  <p class="muted" style="margin:.35rem 0 0;font-size:.78rem">
    Stacked by occurred_at ({_e(unit)} buckets) · PASS / WARN / FAIL / ERROR only.
  </p>
</div>
"""


def _audit_bars(audit: dict[str, Any]) -> str:
    keys = ("PASS", "WARN", "FAIL", "ERROR")
    vals = [int(audit.get(k) or 0) for k in keys]
    peak = max(vals) if vals else 0
    colors = {  # nosec B105
        "PASS": "var(--ok)",
        "WARN": "var(--warn)",
        "FAIL": "var(--bad)",
        "ERROR": "var(--bad)",
    }
    cols = []
    for k, v in zip(keys, vals, strict=True):
        pct = 8 if peak == 0 and v == 0 else (max(8, int(100 * v / peak)) if peak else 8)
        if v == 0:
            pct = 6
        cols.append(
            f'<div class="bar-col"><div class="bar" style="height:{pct}%;background:{colors[k]}"></div>'
            f'<div class="bar-lbl">{_e(k)} {_e(v)}</div></div>'
        )
    return f"""
<div class="panel">
  <div class="panel-head"><h2>Current audit status</h2></div>
  <div class="chart-bars">{"".join(cols)}</div>
  <p class="muted" style="margin:.35rem 0 0;font-size:.78rem">Counts of agents by current audit result.</p>
</div>
"""


def page_fleet(
    overview: dict[str, Any],
    agents: dict[str, Any],
    alerts: dict[str, Any],
    trend: dict[str, Any],
    window: dict[str, str],
    *,
    csrf: str,
    qs: dict[str, list[str]],
) -> str:
    totals = overview.get("totals") or {}
    audit = totals.get("audit") or {}
    online = int(totals.get("online") or 0)
    stale = int(totals.get("stale") or 0)
    offline = int(totals.get("offline") or 0)
    agent_total = int(totals.get("agents") or 0)
    q = (qs.get("q") or [""])[0]
    presence = (qs.get("presence") or [""])[0]
    audit_status = (qs.get("audit_status") or [""])[0]
    capability = (qs.get("capability") or [""])[0]
    software_version = (qs.get("software_version") or [""])[0]
    protocol_version = (qs.get("protocol_version") or [""])[0]
    has_data_gap = (qs.get("has_data_gap") or [""])[0]
    limit = (qs.get("limit") or ["10"])[0]
    time_range = window.get("time_range") or "24h"
    time_bar = _time_range_select(
        time_range,
        action="/dashboard/",
        hidden={
            "q": q,
            "presence": presence,
            "audit_status": audit_status,
            "capability": capability,
            "software_version": software_version,
            "protocol_version": protocol_version,
            "has_data_gap": has_data_gap,
            "limit": limit,
            "offset": "0",
        },
    )
    cards = f"""
<div class="section-label">Presence</div>
<div class="cards">
  <div class="card tone-accent"><div class="n">{_e(agent_total)}</div><div class="l">Agents</div></div>
  <div class="card tone-ok"><div class="n">{_e(online)}</div><div class="l">Online</div></div>
  <div class="card tone-warn"><div class="n">{_e(stale)}</div><div class="l">Stale</div></div>
  <div class="card tone-bad"><div class="n">{_e(offline)}</div><div class="l">Offline</div></div>
</div>
<div class="section-label">Audit health</div>
<div class="cards">
  <div class="card tone-ok"><div class="n">{_e(audit.get('PASS', 0))}</div><div class="l">Audit PASS</div></div>
  <div class="card tone-warn"><div class="n">{_e(audit.get('WARN', 0))}</div><div class="l">Audit WARN</div></div>
  <div class="card tone-bad"><div class="n">{_e(audit.get('FAIL', 0))}</div><div class="l">Audit FAIL</div></div>
  <div class="card tone-bad"><div class="n">{_e(audit.get('ERROR', 0))}</div><div class="l">Audit ERROR</div></div>
  <div class="card"><div class="n">{_e(totals.get('data_gap_or_overflow_agents', 0))}</div><div class="l">DATA_GAP / overflow</div></div>
  <div class="card"><div class="n">{_e(totals.get('protocol_compatibility_warnings', 0))}</div><div class="l">Protocol warnings</div></div>
</div>
<p class="muted">Online/stale/offline is heartbeat freshness — separate from current audit health.</p>
<div class="charts">
{_audit_bars(audit if isinstance(audit, dict) else {})}
{_presence_donut(online, stale, offline, agent_total)}
</div>
{time_bar}
{_audit_trend_chart(trend)}
{_recent_alerts_panel(alerts)}
"""
    filters = f"""
<form class="filters" method="get" action="/dashboard/" id="fleet-filters">
  <input type="hidden" name="time_range" value="{_e(time_range)}"/>
  <input name="q" value="{_e(q)}" placeholder="Search id / label / host" maxlength="120"/>
  <select name="presence">
    <option value="">Presence (any)</option>
    {_opt("online", presence)}{_opt("stale", presence)}{_opt("offline", presence)}
  </select>
  <select name="audit_status">
    <option value="">Audit (any)</option>
    {_opt("PASS", audit_status)}{_opt("WARN", audit_status)}{_opt("FAIL", audit_status)}{_opt("ERROR", audit_status)}
  </select>
  <input name="capability" value="{_e(capability)}" placeholder="Capability key" maxlength="64"/>
  <input name="software_version" value="{_e(software_version)}" placeholder="Software version" maxlength="64"/>
  <input name="protocol_version" value="{_e(protocol_version)}" placeholder="Protocol" maxlength="8"/>
  <select name="has_data_gap">
    <option value="">DATA_GAP (any)</option>
    {_opt("true", has_data_gap)}{_opt("false", has_data_gap)}
  </select>
  <input type="hidden" name="limit" value="{_e(limit)}"/>
  <input type="hidden" name="offset" value="0"/>
  <button type="submit">Filter</button>
  <a href="/dashboard/">Clear</a>
</form>
"""
    rows = []
    for item in agents.get("items") or []:
        aid = item.get("agent_id")
        gap = "DATA_GAP" if item.get("has_data_gap") else ""
        warn = "proto!" if item.get("protocol_warning") else ""
        caps = item.get("capability_summary") or {}
        rows.append(
            "<tr>"
            f"<td><a href='/dashboard/agents/{_e(aid)}'>{_e(item.get('label') or aid)}</a>"
            f"<div class='muted'>{_e(aid)} · {_e(item.get('hostname'))}</div></td>"
            f"<td><span class='pill { _e(item.get('presence')) }'>{_e(item.get('presence'))}</span></td>"
            f"<td><span class='pill { _e(item.get('current_audit_status') or '') }'>{_e(item.get('current_audit_status') or '—')}</span></td>"
            f"<td>{_e(item.get('current_audit_occurred_at') or '—')}</td>"
            f"<td>{_e(item.get('last_heartbeat') or '—')}</td>"
            f"<td>{_e(item.get('software_version') or '—')}</td>"
            f"<td>{_e(item.get('protocol_version'))} {_e(warn)}</td>"
            f"<td>{_e(caps.get('available_count', 0))} avail {_e(gap)}</td>"
            "</tr>"
        )
    table = (
        "<div class='panel'><div class='panel-head'><h2>Agents</h2></div>"
        "<div class='table-wrap'><table><thead><tr>"
        "<th>Agent</th><th>Online</th><th>Current audit</th><th>Last audit</th>"
        "<th>Last heartbeat</th><th>Software</th><th>Protocol</th><th>Capabilities</th>"
        "</tr></thead><tbody>"
        + ("".join(rows) if rows else "<tr><td colspan='8' class='muted'>No agents</td></tr>")
        + "</tbody></table></div></div>"
    )
    matched = int(agents.get("matched") or 0)
    off = int(agents.get("offset") or 0)
    lim = int(agents.get("limit") or 10)
    prev_off = max(0, off - lim)
    next_off = off + lim
    qparams = {
        "q": q,
        "presence": presence,
        "audit_status": audit_status,
        "capability": capability,
        "software_version": software_version,
        "protocol_version": protocol_version,
        "has_data_gap": has_data_gap,
        "limit": str(lim),
        "time_range": time_range,
    }
    pager = (
        f"<div class='pager muted'>Showing { _e(agents.get('returned')) } of { _e(matched) }"
        f" (offset { _e(off) })</div><div class='pager'>"
    )
    if off > 0:
        pager += f"<a href='/dashboard/?{_e(urlencode({**qparams, 'offset': str(prev_off)}))}'>Previous</a>"
    if agents.get("has_more"):
        pager += f"<a href='/dashboard/?{_e(urlencode({**qparams, 'offset': str(next_off)}))}'>Next</a>"
    pager += "</div>"
    body = (
        f"<h1>Fleet overview</h1>"
        f'<p class="muted" style="margin-top:-.35rem">Overview of your backup environment.</p>'
        f"{cards}{filters}{table}{pager}"
    )
    return _layout("Fleet", body, csrf=csrf, active="fleet", subtitle="")


def page_agent(detail: dict[str, Any], *, csrf: str) -> str:
    ident = detail.get("identity") or {}
    audit = detail.get("current_audit") or {}
    caps = detail.get("capability_summary") or {}
    families = caps.get("families") or {}
    cap_rows = "".join(
        "<tr>"
        f"<td>{_e(name)}</td>"
        f"<td>{_e(state.get('installed'))}</td>"
        f"<td>{_e(state.get('supported'))}</td>"
        f"<td>{_e(state.get('available'))}</td>"
        f"<td>{_e(state.get('reason'))}</td>"
        "</tr>"
        for name, state in sorted(families.items())
        if isinstance(state, dict)
    )
    hist = "".join(
        "<tr>"
        f"<td>{_e(ev.get('occurred_at'))}</td>"
        f"<td>{_e(ev.get('event_type'))}</td>"
        f"<td><span class='pill {_e(ev.get('status'))}'>{_e(ev.get('status'))}</span></td>"
        f"<td>{_e(ev.get('summary'))}</td>"
        f"<td>{_e(ev.get('received_at'))}</td>"
        "</tr>"
        for ev in (detail.get("recent_history") or [])
    )
    title = str(ident.get("label") or ident.get("agent_id") or "Agent")
    body = f"""
<h1>{_e(ident.get('label') or ident.get('agent_id'))}</h1>
<p class="muted">{_e(ident.get('agent_id'))} · host {_e(ident.get('hostname'))} · registry {_e(ident.get('registry_status'))}</p>
<div class="cards">
  <div class="card"><div class="n"><span class="pill {_e(detail.get('presence'))}">{_e(detail.get('presence'))}</span></div><div class="l">Presence</div></div>
  <div class="card"><div class="n"><span class="pill {_e(audit.get('status') or '')}">{_e(audit.get('status') or '—')}</span></div><div class="l">Current audit</div></div>
  <div class="card"><div class="n">{_e(detail.get('software_version') or '—')}</div><div class="l">Software</div></div>
  <div class="card"><div class="n">{_e(detail.get('protocol_version'))}</div><div class="l">Protocol</div></div>
</div>
<div class="panel" style="margin-top:.85rem">
<p>Last heartbeat: {_e(detail.get('last_heartbeat') or 'never')} · Last received: {_e(detail.get('last_received_at') or '—')}</p>
<p>Current audit occurred_at: {_e(audit.get('occurred_at') or '—')} (independent of presence)</p>
</div>
<h2>Latest by check type</h2>
<div class="panel"><div class="table-wrap"><table><thead><tr><th>Check</th><th>Status</th><th>Occurred</th><th>Received</th><th>Summary</th></tr></thead>
<tbody>{_latest_check_rows(detail.get('latest_by_check_type') or {})}</tbody></table></div></div>
<h2>Capabilities</h2>
<p class="muted">NOT_INSTALLED / UNSUPPORTED / UNAVAILABLE are not FAIL.</p>
<div class="panel"><div class="table-wrap"><table><thead><tr><th>Family</th><th>Installed</th><th>Supported</th><th>Available</th><th>Reason</th></tr></thead>
<tbody>{cap_rows or "<tr><td colspan='5' class='muted'>No capabilities reported</td></tr>"}</tbody></table></div></div>
<h2>Recent history</h2>
<div class="panel"><div class="table-wrap"><table><thead><tr><th>Occurred</th><th>Type</th><th>Status</th><th>Summary</th><th>Received</th></tr></thead>
<tbody>{hist or "<tr><td colspan='5' class='muted'>No events</td></tr>"}</tbody></table></div></div>
"""
    return _layout(title, body, csrf=csrf, active="fleet")


def _latest_check_rows(latest: dict[str, Any]) -> str:
    if not latest:
        return "<tr><td colspan='5' class='muted'>No check results</td></tr>"
    rows = []
    for name, ev in latest.items():
        if ev is None:
            rows.append(
                f"<tr><td>{_e(name)}</td><td colspan='4' class='muted'>none</td></tr>"
            )
            continue
        rows.append(
            "<tr>"
            f"<td>{_e(name)}</td>"
            f"<td><span class='pill {_e(ev.get('status'))}'>{_e(ev.get('status'))}</span></td>"
            f"<td>{_e(ev.get('occurred_at'))}</td>"
            f"<td>{_e(ev.get('received_at'))}</td>"
            f"<td>{_e(ev.get('summary'))}</td>"
            "</tr>"
        )
    return "".join(rows)


def page_history(
    events: dict[str, Any],
    trend: dict[str, Any],
    window: dict[str, str],
    *,
    csrf: str,
    qs: dict[str, list[str]],
) -> str:
    agent_id = (qs.get("agent_id") or [""])[0]
    event_type = (qs.get("event_type") or [""])[0]
    status = (qs.get("status") or [""])[0]
    time_from = window.get("time_from") or (qs.get("time_from") or [""])[0]
    time_to = window.get("time_to") or (qs.get("time_to") or [""])[0]
    time_range = window.get("time_range") or (qs.get("time_range") or ["24h"])[0]
    limit = (qs.get("limit") or ["50"])[0]
    failures = (qs.get("failures_only") or [""])[0] in {"1", "true", "yes"}
    filters = (
        '<form class="filters" method="get" action="/dashboard/history">\n'  # nosec B608
        "  <select name=\"time_range\">\n"
        f"    {_opt('1h', time_range)}{_opt('24h', time_range)}{_opt('7d', time_range)}{_opt('custom', time_range)}\n"
        "  </select>\n"
        f'  <input name="agent_id" value="{_e(agent_id)}" placeholder="agent_id" maxlength="128"/>\n'
        f'  <input name="event_type" value="{_e(event_type)}" placeholder="event_type" maxlength="64"/>\n'
        f'  <input name="status" value="{_e(status)}" placeholder="status" maxlength="32"/>\n'
        f'  <input name="time_from" value="{_e(time_from)}" placeholder="time_from ISO" maxlength="64"/>\n'
        f'  <input name="time_to" value="{_e(time_to)}" placeholder="time_to ISO" maxlength="64"/>\n'
        f'  <input type="hidden" name="limit" value="{_e(limit)}"/>\n'
        f'  <label><input type="checkbox" name="failures_only" value="1" {"checked" if failures else ""}/> Failures only</label>\n'
        '  <button type="submit">Filter</button>\n'
        '  <a href="/dashboard/history">Clear</a>\n'
        "</form>\n"
    )
    rows = "".join(
        "<tr>"
        f"<td>{_e(ev.get('occurred_at'))}</td>"
        f"<td><a href='/dashboard/agents/{_e(ev.get('agent_id'))}'>{_e(ev.get('agent_id'))}</a></td>"
        f"<td>{_e(ev.get('event_type'))}</td>"
        f"<td><span class='pill {_e(ev.get('status'))}'>{_e(ev.get('status'))}</span></td>"
        f"<td>{_e(ev.get('summary'))}</td>"
        f"<td>{_e(ev.get('received_at'))}</td>"
        "</tr>"
        for ev in (events.get("items") or [])
    )
    matched = int(events.get("matched") or 0)
    off = int(events.get("offset") or 0)
    lim = int(events.get("limit") or 50)
    qparams = {
        "agent_id": agent_id,
        "event_type": event_type,
        "status": status,
        "time_from": time_from,
        "time_to": time_to,
        "time_range": time_range,
        "limit": str(lim),
    }
    if failures:
        qparams["failures_only"] = "1"
    pager = f"<div class='pager muted'>matched={_e(matched)} returned={_e(events.get('returned'))}</div><div class='pager'>"
    if off > 0:
        pager += (
            f"<a href='/dashboard/history?{_e(urlencode({**qparams, 'offset': str(max(0, off - lim))}))}'>"
            "Previous</a>"
        )
    if events.get("has_more"):
        pager += (
            f"<a href='/dashboard/history?{_e(urlencode({**qparams, 'offset': str(off + lim)}))}'>"
            "Next</a>"
        )
    pager += "</div>"
    body = f"""
<h1>History / failures</h1>
<p class="muted">Bounded page · occurred_at and received_at shown separately.</p>
{filters}
{_audit_trend_chart(trend)}
<div class="panel"><div class="table-wrap"><table><thead><tr><th>Occurred</th><th>Agent</th><th>Type</th><th>Status</th><th>Summary</th><th>Received</th></tr></thead>
<tbody>{rows or "<tr><td colspan='6' class='muted'>No events</td></tr>"}</tbody></table></div></div>
{pager}
"""
    return _layout("History", body, csrf=csrf, active="history")


def page_policy(
    policies: dict[str, Any],
    drift: dict[str, Any],
    rollouts: dict[str, Any],
    audit: dict[str, Any],
    *,
    csrf: str,
) -> str:
    policy_rows = ""
    for item in policies.get("items") or []:
        policy_rows += (
            "<tr>"
            f"<td>{_e(item.get('policy_id'))}</td>"
            f"<td>{_e(item.get('display_name'))}</td>"
            f"<td>{_e(item.get('latest_revision_id'))}</td>"
            f"<td>{_e(item.get('created_by'))}</td>"
            f"<td>{_e(item.get('created_at'))}</td>"
            "</tr>"
        )
    drift_rows = ""
    for item in drift.get("items") or []:
        drift_rows += (
            "<tr>"
            f"<td>{_e(item.get('agent_id'))}</td>"
            f"<td><span class='pill'>{_e(item.get('drift_status'))}</span></td>"
            f"<td>{_e(item.get('desired_revision_id'))}</td>"
            f"<td>{_e(item.get('applied_revision_id'))}</td>"
            f"<td>{_e(item.get('reason'))}</td>"
            f"<td>{_e(item.get('updated_at'))}</td>"
            "</tr>"
        )
    rollout_rows = ""
    for item in rollouts.get("items") or []:
        rollout_rows += (
            "<tr>"
            f"<td>{_e(item.get('rollout_id'))}</td>"
            f"<td>{_e(item.get('revision_id'))}</td>"
            f"<td><span class='pill'>{_e(item.get('status'))}</span></td>"
            f"<td>{_e(item.get('batch_size'))}/{_e(item.get('max_concurrent'))}</td>"
            f"<td>{_e(item.get('created_at'))}</td>"
            "</tr>"
        )
    audit_rows = ""
    for item in audit.get("items") or []:
        audit_rows += (
            "<tr>"
            f"<td>{_e(item.get('occurred_at'))}</td>"
            f"<td>{_e(item.get('actor'))}</td>"
            f"<td>{_e(item.get('action'))}</td>"
            f"<td>{_e(item.get('target'))}</td>"
            f"<td>{_e(item.get('result'))}</td>"
            f"<td>{_e(item.get('new_ref'))}</td>"
            "</tr>"
        )
    body = f"""
<h1>Policy</h1>
<span class="readonly-banner">READ ONLY</span>
<p class="muted">Read-only view. Mutations remain CLI/controller-admin only (no dashboard writes).</p>
<h2>Policies</h2>
<div class="panel"><div class="table-wrap"><table><thead><tr>
<th>Policy</th><th>Name</th><th>Latest revision</th><th>Created by</th><th>Created</th>
</tr></thead><tbody>{policy_rows or "<tr><td colspan='5' class='muted'>No policies</td></tr>"}</tbody></table></div></div>
<h2>Drift / apply</h2>
<div class="panel"><div class="table-wrap"><table><thead><tr>
<th>Agent</th><th>Drift</th><th>Desired</th><th>Applied</th><th>Reason</th><th>Updated</th>
</tr></thead><tbody>{drift_rows or "<tr><td colspan='6' class='muted'>No drift records</td></tr>"}</tbody></table></div></div>
<h2>Rollouts</h2>
<div class="panel"><div class="table-wrap"><table><thead><tr>
<th>Rollout</th><th>Revision</th><th>Status</th><th>Batch/concurrent</th><th>Created</th>
</tr></thead><tbody>{rollout_rows or "<tr><td colspan='5' class='muted'>No rollouts</td></tr>"}</tbody></table></div></div>
<h2>Audit</h2>
<div class="panel"><div class="table-wrap"><table><thead><tr>
<th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Result</th><th>New ref</th>
</tr></thead><tbody>{audit_rows or "<tr><td colspan='6' class='muted'>No audit records</td></tr>"}</tbody></table></div></div>
"""
    return _layout("Policy", body, csrf=csrf, active="policy")


def _opt(value: str, selected: str) -> str:
    sel = " selected" if value == selected else ""
    return f'<option value="{_e(value)}"{sel}>{_e(value)}</option>'
