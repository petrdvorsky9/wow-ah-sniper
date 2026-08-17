"""WoW AH Sniper — web app.

A search box in front of `item_report.py`'s report pipeline: type an item name, ID,
or Wowhead link, and get the same dashboard the CLI/skill generates as a `.html`
file — served live in your browser instead.

Local usage (binds to 127.0.0.1 by default):
    python webapp.py
    python webapp.py --port 8000

Then open http://127.0.0.1:5000 (or your chosen port) in your browser.

In production (e.g. behind gunicorn on Fly.io — see fly.toml/Dockerfile), this module
is imported directly for its `app` object and `main()`/argparse are never invoked.
"""

from __future__ import annotations

import argparse
import time
from html import escape as html_escape

from flask import Flask, Response, redirect, request, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from item_report import (
    CATEGORY_LABELS,
    DEFAULT_REALM,
    DEFAULT_REGION,
    MIN_FLIP_PROFIT_PCT,
    QUALITY_META,
    WOWHEAD_ICON_URL,
    build_flask_overview,
    build_flip_ribbon,
    detect_scope,
    fetch_wowhead_item_meta,
    fmt_gold,
    generate_report,
    resolve_item_query,
)
from undermine_client import UndermineApiError, UndermineClient

app = Flask(__name__)

# Every request shares one Undermine Exchange API key (and scrapes Wowhead under one
# IP), so once this is public we rate-limit per visitor to keep a single user from
# exhausting either. In-memory storage is fine for a single-instance deployment (the
# default on Fly's free tier); if you ever scale to multiple machines, point
# storage_uri at a shared Redis instance instead, or the limits won't be shared.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per hour", "2000 per day"],
    storage_uri="memory://",
)

REGIONS = ["eu", "us", "tw", "kr"]

# ── Midnight flask overview cache (avoids refetching on every landing-page hit) ─

FLASK_OVERVIEW_TTL_SECONDS = 600
_flask_overview_cache: dict = {"rows": None, "fetched_at": 0.0}


def get_flask_overview_cached() -> list[dict]:
    """Cached wrapper around `build_flask_overview` — refreshes at most once every
    `FLASK_OVERVIEW_TTL_SECONDS`. On a refresh failure, keeps serving the last good
    data rather than blanking out the landing page."""
    now = time.monotonic()
    if _flask_overview_cache["rows"] is not None and (
        now - _flask_overview_cache["fetched_at"] < FLASK_OVERVIEW_TTL_SECONDS
    ):
        return _flask_overview_cache["rows"]
    try:
        client = UndermineClient()
        rows = build_flask_overview(client, region="eu")
        _flask_overview_cache["rows"] = rows
        _flask_overview_cache["fetched_at"] = now
        return rows
    except Exception:
        return _flask_overview_cache["rows"] or []


# ── Midnight short-flip ribbon cache (avoids refetching on every landing-page hit) ─

FLIP_RIBBON_TTL_SECONDS = 600
_flip_ribbon_cache: dict = {"rows": None, "fetched_at": 0.0}


def get_flip_ribbon_cached() -> list[dict]:
    """Cached wrapper around `build_flip_ribbon` — refreshes at most once every
    `FLIP_RIBBON_TTL_SECONDS`. On a refresh failure, keeps serving the last good
    data rather than blanking out the landing page. Unlike the flask overview, an
    empty list here is also a valid (cacheable) result — it means nothing in the
    basket cleared the profit bar today, not that the fetch failed."""
    now = time.monotonic()
    if _flip_ribbon_cache["rows"] is not None and (
        now - _flip_ribbon_cache["fetched_at"] < FLIP_RIBBON_TTL_SECONDS
    ):
        return _flip_ribbon_cache["rows"]
    try:
        client = UndermineClient()
        rows = build_flip_ribbon(client, region="eu", limit=16)
        _flip_ribbon_cache["rows"] = rows
        _flip_ribbon_cache["fetched_at"] = now
        return rows
    except Exception:
        return _flip_ribbon_cache["rows"] or []

# ── shared dark theme (same palette as item_report.py's dashboard) ─────────────

_PAGE_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #12142b;
    --card: #1a1d3a;
    --card-border: #2a2e56;
    --text: #e7e9f5;
    --muted: #9198c2;
    --gold: #f0c040;
    --blue: #3a7bd5;
    --pink: #ec4899;
    --green: #22c55e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    display: flex;
    align-items: __ALIGN__;
    justify-content: center;
    padding: 40px 16px;
    background: var(--bg);
    background-image: radial-gradient(circle at 20% 0%, #1e2350 0%, var(--bg) 55%);
    color: var(--text);
    font-family: "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  .page-stack {
    width: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 20px;
  }
  .box {
    width: 100%;
    max-width: 480px;
    background: var(--card);
    border: 1px solid var(--card-border);
    border-radius: 16px;
    padding: 28px 28px 24px;
  }
  .box.box-wide { max-width: 620px; }
  .box.box-flip { max-width: 1200px; }
  @media (max-width: 480px) {
    body { padding: 20px 12px; align-items: flex-start; }
    .box { padding: 20px 18px 18px; border-radius: 14px; }
    .adv-row { flex-direction: column; gap: 12px; }
  }
  .flask-overview { margin-top: 26px; padding-top: 20px; border-top: 1px solid var(--card-border); }
  .flask-overview-title { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 12px; }
  .flask-row {
    display: flex; align-items: center; gap: 10px; padding: 9px 0;
    border-bottom: 1px solid var(--card-border); flex-wrap: wrap;
  }
  .flask-row:last-child { border-bottom: none; }
  .flask-id { display: flex; align-items: center; gap: 10px; flex: 1 1 160px; min-width: 0; }
  .flask-id img { width: 28px; height: 28px; border-radius: 6px; border: 1px solid var(--card-border); flex-shrink: 0; }
  .flask-name { min-width: 0; font-size: 13px; }
  .flask-name a { color: var(--text); text-decoration: none; border-bottom: 1px dotted var(--muted); }
  .flask-name a:hover { color: var(--gold); border-bottom-color: var(--gold); }
  .flask-stat { display: block; color: var(--muted); font-size: 11px; margin-top: 1px; }
  .flask-price { text-align: right; font-size: 13px; font-weight: 600; color: var(--gold); white-space: nowrap; }
  .flask-qty { display: block; color: var(--muted); font-size: 10px; font-weight: 400; margin-top: 1px; }
  .flask-badge {
    font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .03em;
    padding: 5px 9px; border-radius: 6px; white-space: nowrap; text-align: center;
  }
  .flask-badge-buy { background: rgba(34,197,94,0.15); color: var(--green); }
  .flask-badge-sell { background: rgba(236,72,153,0.15); color: var(--pink); }
  .flask-badge-neutral { background: rgba(255,255,255,0.06); color: var(--muted); }
  .flask-unavailable { color: var(--muted); }
  .flask-btn {
    display: inline-block;
    background: transparent;
    border: 1px solid var(--gold);
    color: var(--gold);
    font-size: 11px;
    font-weight: 600;
    text-decoration: none;
    padding: 6px 12px;
    border-radius: 8px;
    white-space: nowrap;
  }
  .flask-btn:hover { background: var(--gold); color: #1a1305; }
  @media (max-width: 480px) {
    .flask-row { justify-content: space-between; }
    .flask-id { flex-basis: 100%; }
  }
  .flip-ribbon-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
  .flip-ribbon-title { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 4px; }
  .flip-ribbon-sub { color: var(--muted); font-size: 11px; margin-bottom: 14px; }
  .flip-ribbon-nav { display: flex; gap: 6px; flex-shrink: 0; }
  .flip-nav-btn {
    width: 26px; height: 26px; border-radius: 8px;
    background: rgba(255,255,255,0.04); border: 1px solid var(--card-border); color: var(--text);
    font-size: 14px; line-height: 1; cursor: pointer; display: flex; align-items: center; justify-content: center;
    transition: background .15s, border-color .15s, opacity .15s;
  }
  .flip-nav-btn:hover { background: rgba(240,192,64,0.12); border-color: var(--gold); color: var(--gold); }
  .flip-nav-btn:disabled { opacity: .35; cursor: default; }
  .flip-nav-btn:disabled:hover { background: rgba(255,255,255,0.04); border-color: var(--card-border); color: var(--text); }
  .flip-viewport { position: relative; }
  .flip-scroll {
    display: flex; gap: 10px; overflow-x: auto; padding-bottom: 6px; margin: 0 -4px;
    scroll-snap-type: x mandatory; scroll-behavior: smooth;
    scrollbar-width: none;
  }
  .flip-scroll::-webkit-scrollbar { display: none; }
  .flip-card {
    flex: 0 0 190px;
    background: rgba(255,255,255,0.03);
    border: 1px solid var(--card-border);
    border-radius: 12px;
    padding: 12px 14px;
    margin: 0 4px;
    scroll-snap-align: start;
  }
  .flip-card-tags { display: flex; align-items: center; gap: 6px; margin-bottom: 7px; flex-wrap: wrap; }
  .flip-card-cat {
    display: inline-block; font-size: 9px; font-weight: 600; text-transform: uppercase;
    letter-spacing: .04em; color: var(--muted); background: rgba(255,255,255,0.06);
    border-radius: 5px; padding: 2px 6px;
  }
  .flip-card-quality { font-size: 9px; font-weight: 600; letter-spacing: .02em; }
  .flip-card-rank { font-size: 9px; color: var(--muted); margin-bottom: 8px; }
  .flip-card-id { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .flip-card-id img { width: 26px; height: 26px; border-radius: 6px; border: 1px solid var(--card-border); flex-shrink: 0; }
  .flip-card-name { font-size: 12px; line-height: 1.25; min-width: 0; }
  .flip-card-name a { color: var(--text); text-decoration: none; border-bottom: 1px dotted var(--muted); }
  .flip-card-name a:hover { color: var(--gold) !important; border-bottom-color: var(--gold); }
  .flip-card-row { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-top: 3px; }
  .flip-card-row .v { color: var(--text); font-weight: 600; }
  .flip-card-profit {
    margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--card-border);
    font-size: 13px; font-weight: 700; color: var(--green);
  }
  .flip-card-profit-sub { font-size: 10px; font-weight: 400; color: var(--muted); margin-top: 1px; }
  .flip-card-btn {
    display: block; text-align: center; margin-top: 9px;
    background: transparent; border: 1px solid var(--gold); color: var(--gold);
    font-size: 10px; font-weight: 600; text-decoration: none; padding: 5px 0; border-radius: 7px;
  }
  .flip-card-btn:hover { background: var(--gold); color: #1a1305; }
  .flip-empty { color: var(--muted); font-size: 13px; padding: 6px 2px; }
  h1 { margin: 0 0 6px; font-size: 22px; font-weight: 600; }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 22px; }
  form { display: flex; flex-direction: column; gap: 12px; }
  input[type=text] {
    background: #0d0f26;
    border: 1px solid var(--card-border);
    border-radius: 10px;
    padding: 12px 14px;
    color: var(--text);
    font-size: 14px;
  }
  input[type=text]:focus { outline: none; border-color: var(--gold); }
  details { color: var(--muted); font-size: 12px; }
  summary { cursor: pointer; padding: 4px 0; }
  .adv-row { display: flex; gap: 10px; margin-top: 10px; }
  .adv-row > div { flex: 1; }
  label { display: block; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 4px; }
  select, .adv-row input[type=text] { width: 100%; padding: 8px 10px; font-size: 13px; }
  .checkbox-row { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); }
  button {
    background: var(--gold);
    color: #1a1305;
    border: none;
    border-radius: 10px;
    padding: 12px 14px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    margin-top: 6px;
  }
  button:hover { filter: brightness(1.08); }
  .error {
    background: rgba(236,72,153,0.12);
    border: 1px solid var(--pink);
    color: #ffd6e8;
    border-radius: 10px;
    padding: 12px 14px;
    font-size: 13px;
    margin-bottom: 16px;
  }
  .back { color: var(--muted); font-size: 13px; text-decoration: none; border-bottom: 1px dashed var(--muted); }
  .back:hover { color: var(--gold); border-bottom-color: var(--gold); }
  .hint { color: var(--muted); font-size: 12px; margin-top: 14px; }
  .hint code { color: var(--text); }
  .notice {
    background: rgba(240,192,64,0.08);
    border: 1px solid rgba(240,192,64,0.35);
    color: var(--text);
    border-radius: 10px;
    padding: 10px 14px;
    font-size: 12px;
    line-height: 1.5;
    margin-top: 16px;
  }
</style>
</head>
<body>
<div class="page-stack">
__BODY__
</div>
</body>
</html>
"""


def _page(title: str, body: str, align: str = "center") -> str:
    return (
        _PAGE_SHELL.replace("__TITLE__", html_escape(title))
        .replace("__BODY__", body)
        .replace("__ALIGN__", align)
    )


def render_flask_overview() -> str:
    """Landing-page card: current price + "good day to buy/sell today" signal for
    each higher-quality Midnight combat flask. Returns "" if the cache has nothing
    to show yet (e.g. very first request while the initial fetch is in flight)."""
    rows = get_flask_overview_cached()
    if not rows:
        return ""

    row_html = []
    for r in rows:
        icon_img = f'<img src="{WOWHEAD_ICON_URL.format(icon=r["icon"])}" alt="">' if r.get("icon") else ""
        id_cell = (
            '<div class="flask-id">'
            f"{icon_img}"
            '<div class="flask-name">'
            f'<a href="{r["wowhead_url"]}" target="_blank" rel="noopener noreferrer">{html_escape(r["name"])}</a>'
            f'<span class="flask-stat">{html_escape(r["stat"])}</span>'
            "</div>"
            "</div>"
        )
        report_btn = (
            f'<a class="flask-btn" href="/report?q={r["item_id"]}&region=eu&recipes=1">Report</a>'
        )

        if not r["available"]:
            row_html.append(
                f'<div class="flask-row flask-unavailable">{id_cell}'
                f'<div class="flask-price muted-cell">no AH data</div>{report_btn}</div>'
            )
            continue

        price_cell = (
            f'<div class="flask-price">{html_escape(fmt_gold(r["price_copper"]))}'
            f'<span class="flask-qty">&times;{r["quantity"]:,} on AH</span></div>'
        )
        signal = r["today_signal"]
        if signal:
            badge = (
                f'<div class="flask-badge flask-badge-{signal["action"]}">'
                f'{html_escape(signal["label"])} ({signal["pct"]:+.0f}%)</div>'
            )
        else:
            badge = '<div class="flask-badge flask-badge-neutral">No weekday data yet</div>'
        row_html.append(
            f'<div class="flask-row">{id_cell}{price_cell}{badge}{report_btn}</div>'
        )

    return (
        '<div class="flask-overview">'
        '<div class="flask-overview-title">Midnight Flasks &middot; EU &middot; higher quality</div>'
        f'{"".join(row_html)}'
        "</div>"
    )


_FLIP_CAROUSEL_SCRIPT = """
<script>
(function () {
  function flipEls() {
    return {
      scroll: document.getElementById('flip-scroll'),
      prev: document.getElementById('flip-prev'),
      next: document.getElementById('flip-next'),
    };
  }
  window.flipScrollBy = function (dir) {
    var scroll = flipEls().scroll;
    if (!scroll) return;
    var card = scroll.querySelector('.flip-card');
    var step = card ? card.getBoundingClientRect().width + 18 : scroll.clientWidth * 0.8;
    scroll.scrollBy({ left: step * dir, behavior: 'smooth' });
  };
  function updateNav() {
    var els = flipEls();
    if (!els.scroll || !els.prev || !els.next) return;
    var max = els.scroll.scrollWidth - els.scroll.clientWidth - 1;
    els.prev.disabled = els.scroll.scrollLeft <= 0;
    els.next.disabled = els.scroll.scrollLeft >= max;
  }
  document.addEventListener('DOMContentLoaded', function () {
    var scroll = flipEls().scroll;
    if (!scroll) return;
    scroll.addEventListener('scroll', updateNav, { passive: true });
    window.addEventListener('resize', updateNav);
    updateNav();
  });
})();
</script>
"""


def render_flip_ribbon() -> str:
    """Standalone landing-page box (own row, above the search box, wider than it):
    top "buy now, sell tomorrow" picks across a basket of Midnight
    materials/flasks/potions/phials, ranked by net profit after the 5% AH cut.
    Returns "" if the cache hasn't fetched anything yet (e.g. very first request
    while the initial fetch is in flight) — a genuinely empty *result* (no
    profitable flips today) still renders the box with a friendly empty state."""
    rows = get_flip_ribbon_cached()
    if rows is None:
        return ""

    if not rows:
        cards_html = (
            '<div class="flip-empty">No short flips clear the '
            f'{MIN_FLIP_PROFIT_PCT:.0f}% profit bar right now '
            "&mdash; check back later today or tomorrow.</div>"
        )
    else:
        cards = []
        for r in rows:
            icon_img = f'<img src="{WOWHEAD_ICON_URL.format(icon=r["icon"])}" alt="">' if r.get("icon") else ""
            cat_label = CATEGORY_LABELS.get(r["category"], r["category"].title())
            quality_label, quality_color = QUALITY_META.get(r.get("quality", 1), QUALITY_META[1])
            rank_label = r.get("rank")
            sign = "+" if r["profit_copper"] >= 0 else "-"
            cards.append(
                '<div class="flip-card">'
                '<div class="flip-card-tags">'
                f'<span class="flip-card-cat">{html_escape(cat_label)}</span>'
                f'<span class="flip-card-quality" style="color:{quality_color}">'
                f'&#9679; {html_escape(quality_label)}</span>'
                "</div>"
                + (f'<div class="flip-card-rank">&#9670; {html_escape(rank_label)}</div>' if rank_label else "")
                + '<div class="flip-card-id">'
                f"{icon_img}"
                '<div class="flip-card-name">'
                f'<a href="{r["wowhead_url"]}" target="_blank" rel="noopener noreferrer" '
                f'style="color:{quality_color}">{html_escape(r["name"])}</a>'
                "</div>"
                "</div>"
                f'<div class="flip-card-row"><span>Buy now</span><span class="v">{html_escape(fmt_gold(r["price_copper"]))}</span></div>'
                f'<div class="flip-card-row"><span>Sell {html_escape(r["tomorrow_weekday"])}</span>'
                f'<span class="v">{html_escape(fmt_gold(r["net_sell_copper"]))}</span></div>'
                '<div class="flip-card-profit">'
                f'{sign}{html_escape(fmt_gold(abs(r["profit_copper"])))}'
                f'<div class="flip-card-profit-sub">{r["profit_pct"]:+.0f}% after 5% AH cut</div>'
                "</div>"
                f'<a class="flip-card-btn" href="/report?q={r["item_id"]}&region=eu&recipes=0">Report</a>'
                "</div>"
            )
        cards_html = (
            '<div class="flip-viewport">'
            '<div class="flip-scroll" id="flip-scroll">' + "".join(cards) + "</div>"
            "</div>"
        )

    nav_html = (
        '<div class="flip-ribbon-nav">'
        '<button type="button" class="flip-nav-btn" id="flip-prev" aria-label="Previous" onclick="flipScrollBy(-1)">&lsaquo;</button>'
        '<button type="button" class="flip-nav-btn" id="flip-next" aria-label="Next" onclick="flipScrollBy(1)">&rsaquo;</button>'
        "</div>"
        if rows else ""
    )

    return (
        '<div class="box box-flip">'
        '<div class="flip-ribbon-head">'
        '<div><div class="flip-ribbon-title">Midnight Short Flips &middot; Buy Today, Sell Tomorrow &middot; EU</div>'
        '<div class="flip-ribbon-sub">Ranked by net profit after the 5% AH cut, using each item\'s historical weekday price pattern. '
        '&#9679; dot = item quality (white/green/blue/purple = Common/Uncommon/Rare/Epic). '
        '&#9670; tag = crafting rank &mdash; raw materials show the cheaper Silver rank (Gold-rank listings of the same item cost more), '
        'flasks/phials/potions show the max recipe rank. Every item here is Midnight-only, none are ported from Dragonflight.</div></div>'
        f"{nav_html}"
        "</div>"
        f"{cards_html}"
        "</div>"
        + (_FLIP_CAROUSEL_SCRIPT if rows else "")
    )


def render_search_page(error: str | None = None, query: str = "") -> str:
    error_html = f'<div class="error">{html_escape(error)}</div>' if error else ""
    region_options = "".join(
        f'<option value="{r}">{r.upper()}</option>' for r in REGIONS
    )
    body = f"""
{render_flip_ribbon()}
<div class="box box-wide">
<h1>WoW AH Sniper</h1>
<div class="sub">Search any auction house item to see its live price dashboard.</div>
{error_html}
<form action="/report" method="get">
  <input type="text" name="q" placeholder="Item name, ID, or Wowhead link" value="{html_escape(query)}" autofocus required>
  <details>
    <summary>Advanced</summary>
    <div class="adv-row">
      <div>
        <label>Region</label>
        <select name="region">{region_options}</select>
      </div>
      <div>
        <label>Realm override (non-commodity items)</label>
        <input type="text" name="realm" placeholder="{DEFAULT_REALM}">
      </div>
    </div>
    <div class="checkbox-row" style="margin-top:10px;">
      <input type="checkbox" id="recipes" name="recipes" value="1" checked>
      <label for="recipes" style="margin:0;">Include recipes section (slower)</label>
    </div>
  </details>
  <button type="submit">Look up price</button>
</form>
<div class="hint">
  Commodity vs. realm scope is auto-detected. Examples: <code>2770</code>,
  <code>Copper Ore</code>, <code>wowhead.com/item=23540/felsteel-longblade</code>.
</div>
<div class="notice">
  This app is currently under active development. You may encounter bugs, unexpected
  behavior, or incomplete features. Thanks for your patience while improvements are
  being made!
</div>
{render_flask_overview()}
</div>
"""
    return _page("WoW AH Sniper", body)


def render_error_page(message: str, query: str = "") -> str:
    body = f"""
<div class="box">
<h1>Couldn't load that item</h1>
<div class="error">{html_escape(message)}</div>
<a class="back" href="/{'?q=' + query if query else ''}">&larr; Back to search</a>
</div>
"""
    return _page("WoW AH Sniper — Error", body)


def _resolve_display_name(item_id: int) -> str:
    """Best-effort item display name for the header/title; falls back to a generic
    label if the Wowhead tooltip lookup fails (never blocks the report)."""
    try:
        meta = fetch_wowhead_item_meta(item_id)
        name = meta.get("name_enus")
        if name:
            return name
    except Exception:
        pass
    return f"Item {item_id}"


# ── routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_search_page(query=request.args.get("q", ""))


@app.route("/report")
@limiter.limit("30 per hour")
def report():
    query = request.args.get("q", "").strip()
    if not query:
        return redirect(url_for("index"))

    region = (request.args.get("region") or DEFAULT_REGION).strip().lower()
    if region not in REGIONS:
        return render_error_page(f"Unsupported region '{region}'.", query), 400
    realm_override = (request.args.get("realm") or "").strip() or None
    include_recipes = request.args.get("recipes", "1") != "0"

    try:
        item_id = resolve_item_query(query)
    except ValueError as exc:
        return render_error_page(str(exc), query), 400

    try:
        client = UndermineClient()
    except UndermineApiError as exc:
        return render_error_page(str(exc), query), 500

    try:
        commodity, realm, quote, hourly = detect_scope(client, region, item_id, realm_override)
    except UndermineApiError:
        realm_desc = realm_override or DEFAULT_REALM
        return (
            render_error_page(
                f"Item {item_id} isn't tracked by Undermine Exchange on {region.upper()} "
                f"— checked both the region-wide commodity market and realm '{realm_desc}'. "
                "Double-check the item ID/realm, or it may simply not be sold on the AH.",
                query,
            ),
            404,
        )

    item_name = _resolve_display_name(item_id)

    try:
        result = generate_report(
            item_id, item_name, commodity, realm, region,
            include_recipes=include_recipes,
            client=client, quote=quote, hourly=hourly,
        )
    except UndermineApiError as exc:
        return render_error_page(str(exc), query), 500

    return Response(result["html"], mimetype="text/html")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WoW AH Sniper — local web app")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1, local only)")
    p.add_argument("--port", type=int, default=5000, help="Port (default: 5000)")
    p.add_argument("--debug", action="store_true", help="Enable Flask debug/auto-reload")
    return p


def main() -> None:
    args = build_parser().parse_args()
    print(f"WoW AH Sniper running at http://{args.host}:{args.port} — Ctrl+C to stop.")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
