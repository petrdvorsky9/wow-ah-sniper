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

# Shown instead of the generic "isn't tracked" message when Undermine returns a 429 —
# the shared API key has a fixed points/hour budget, and a burst of traffic (or the
# landing-page ribbons refreshing) can exhaust it for everyone until it replenishes.
_RATE_LIMITED_MESSAGE = (
    "Undermine Exchange's API key is temporarily rate-limited (this app shares one key "
    "across all visitors, and it has an hourly quota). This is not an error with your "
    "item — please wait a few minutes and try again."
)


def _cache_age_label(fetched_at: float) -> str:
    """Minutes since this box's data was last successfully (re)fetched from
    Undermine. Deliberately worded "checked", not "updated", to avoid implying
    this is the same thing as the item report page's "updated Xm ago" — that one
    is Undermine's own per-item last-price-change timestamp (`PriceQuote.last_updated`),
    which can be much older than this even right after a successful refetch, since
    re-polling doesn't make Undermine's underlying snapshot any newer. A box here
    covering ~20 items also has no single well-defined "last updated" moment the
    way one report page (one item) does."""
    mins = int((time.monotonic() - fetched_at) // 60)
    return f"checked {mins}m ago" if mins > 0 else "checked just now"


# ── Midnight flask overview cache (avoids refetching on every landing-page hit) ─
# Kept in lockstep with FLIP_RIBBON_TTL_SECONDS below so every landing-page ribbon
# refreshes on the same cadence.

FLASK_OVERVIEW_TTL_SECONDS = 1200
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
# Kept in lockstep with FLASK_OVERVIEW_TTL_SECONDS above.

FLIP_RIBBON_TTL_SECONDS = 1200
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
  .box.box-wide { max-width: 760px; }
  .box.box-flip { max-width: 1200px; }
  @media (max-width: 480px) {
    body { padding: 20px 12px; align-items: flex-start; }
    .box { padding: 20px 18px 18px; border-radius: 14px; }
    .adv-row { flex-direction: column; gap: 12px; }
  }
  .flask-overview { margin-top: 26px; padding-top: 20px; border-top: 1px solid var(--card-border); }
  .flask-overview-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
  .flask-overview-title { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .05em; }
  .flask-overview-updated { color: var(--muted); font-size: 10px; flex-shrink: 0; }
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
  .flask-badge-hold, .flask-badge-neutral { background: rgba(255,255,255,0.06); color: var(--muted); }
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
  .flip-ribbon-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .flip-ribbon-title { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .05em; }
  .flip-ribbon-updated { color: var(--muted); font-size: 10px; margin-top: 3px; }
  .flip-ribbon-body { margin-top: 12px; }
  .box-flip.flip-collapsed .flip-ribbon-body { display: none; }
  .flip-ribbon-sub { color: var(--muted); font-size: 11px; margin-bottom: 14px; }
  .flip-ribbon-controls { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  .flip-ribbon-nav { display: flex; gap: 6px; flex-shrink: 0; }
  .flip-nav-btn, .flip-toggle-btn {
    width: 26px; height: 26px; border-radius: 8px;
    background: rgba(255,255,255,0.04); border: 1px solid var(--card-border); color: var(--text);
    font-size: 14px; line-height: 1; cursor: pointer; display: flex; align-items: center; justify-content: center;
    transition: background .15s, border-color .15s, opacity .15s;
  }
  .flip-nav-btn:hover, .flip-toggle-btn:hover { background: rgba(240,192,64,0.12); border-color: var(--gold); color: var(--gold); }
  .flip-nav-btn:disabled { opacity: .35; cursor: default; }
  .flip-nav-btn:disabled:hover { background: rgba(255,255,255,0.04); border-color: var(--card-border); color: var(--text); }
  .flip-toggle-btn .chevron { display: inline-block; transition: transform .2s ease; }
  .box-flip.flip-collapsed .flip-toggle-btn .chevron { transform: rotate(-90deg); }
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
  .flip-card-quality { font-size: 13px; line-height: 1; }
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
    updated_label = _cache_age_label(_flask_overview_cache["fetched_at"])

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
            f'<a class="flask-btn" href="/report?q={r["item_id"]}&region=eu">Report</a>'
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
                f'<div class="flask-badge flask-badge-{signal["action"]}" '
                f'title="% vs. typical {signal["weekday"]} price, the same reference the report\'s '
                'Buy/Sell/Hold call uses">'
                f'{html_escape(signal["label"])} ({signal["pct"]:+.0f}% vs typical {signal["weekday"]})</div>'
            )
        else:
            badge = '<div class="flask-badge flask-badge-neutral">No weekday data yet</div>'
        row_html.append(
            f'<div class="flask-row">{id_cell}{price_cell}{badge}{report_btn}</div>'
        )

    return (
        '<div class="flask-overview">'
        '<div class="flask-overview-head">'
        '<div class="flask-overview-title">Midnight Flasks &middot; EU &middot; higher quality</div>'
        f'<div class="flask-overview-updated">{updated_label}</div>'
        "</div>"
        f'{"".join(row_html)}'
        "</div>"
    )


_FLIP_CAROUSEL_SCRIPT = """
<script>
(function () {
  function stepFor(scroll) {
    var card = scroll.querySelector('.flip-card');
    return card ? card.getBoundingClientRect().width + 18 : scroll.clientWidth * 0.8;
  }
  function scrollOf(ribbonId) {
    return document.querySelector('.flip-scroll[data-ribbon="' + ribbonId + '"]');
  }
  function updateNav(scroll) {
    var ribbonId = scroll.getAttribute('data-ribbon');
    var prev = document.querySelector('.flip-nav-btn[data-ribbon="' + ribbonId + '"][data-dir="-1"]');
    var next = document.querySelector('.flip-nav-btn[data-ribbon="' + ribbonId + '"][data-dir="1"]');
    if (!prev || !next) return;
    var max = scroll.scrollWidth - scroll.clientWidth - 1;
    prev.disabled = scroll.scrollLeft <= 0;
    next.disabled = scroll.scrollLeft >= max;
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.flip-nav-btn');
    if (!btn) return;
    var scroll = scrollOf(btn.getAttribute('data-ribbon'));
    if (!scroll) return;
    var dir = parseInt(btn.getAttribute('data-dir'), 10);
    scroll.scrollBy({ left: stepFor(scroll) * dir, behavior: 'smooth' });
  });
  document.addEventListener('click', function (e) {
    var toggle = e.target.closest('.flip-toggle-btn');
    if (!toggle) return;
    var box = toggle.closest('.box-flip');
    if (!box) return;
    var collapsed = box.classList.toggle('flip-collapsed');
    toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    toggle.setAttribute('aria-label', collapsed ? 'Expand' : 'Collapse');
    if (!collapsed) {
      var scroll = scrollOf(box.getAttribute('data-ribbon-box'));
      if (scroll) updateNav(scroll);
    }
  });
  document.addEventListener('DOMContentLoaded', function () {
    var scrolls = document.querySelectorAll('.flip-scroll');
    scrolls.forEach(function (scroll) {
      scroll.addEventListener('scroll', function () { updateNav(scroll); }, { passive: true });
      updateNav(scroll);
    });
    window.addEventListener('resize', function () {
      scrolls.forEach(updateNav);
    });
  });
})();
</script>
"""


def _render_flip_card(r: dict) -> str:
    icon_img = f'<img src="{WOWHEAD_ICON_URL.format(icon=r["icon"])}" alt="">' if r.get("icon") else ""
    cat_label = CATEGORY_LABELS.get(r["category"], r["category"].title())
    _, quality_color = QUALITY_META.get(r.get("quality", 1), QUALITY_META[1])
    rank_label = r.get("rank")
    sign = "+" if r["profit_copper"] >= 0 else "-"
    return (
        '<div class="flip-card">'
        '<div class="flip-card-tags">'
        f'<span class="flip-card-cat">{html_escape(cat_label)}</span>'
        f'<span class="flip-card-quality" style="color:{quality_color}">&#9679;</span>'
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
        f'<div class="flip-card-row"><span>{html_escape(r["sell_label"])}</span>'
        f'<span class="v">{html_escape(fmt_gold(r["net_sell_copper"]))}</span></div>'
        '<div class="flip-card-profit">'
        f'{sign}{html_escape(fmt_gold(abs(r["profit_copper"])))}'
        f'<div class="flip-card-profit-sub">{r["profit_pct"]:+.0f}% after 5% AH cut</div>'
        "</div>"
        f'<a class="flip-card-btn" href="/report?q={r["item_id"]}&region=eu">Report</a>'
        "</div>"
    )


def _render_flip_ribbon_box(
    rows: list[dict] | None,
    *,
    ribbon_id: str,
    title: str,
    subtitle: str,
    empty_pct: float,
    empty_hint: str,
    updated_label: str,
) -> str:
    """Shared renderer for both flip ribbons (buy-tomorrow and buy-in-an-hour) —
    same card layout, carousel behavior, and collapse/expand behavior, just
    different data/copy. `ribbon_id` must be unique per box so the shared script
    (`_FLIP_CAROUSEL_SCRIPT`, included once for the whole page) can wire up each
    box's own nav buttons and collapse toggle.

    Collapsed by default when there's nothing worth showing (empty picks), so the
    landing page isn't cluttered with a box that's just an explanatory sentence;
    expanded by default whenever there are actual picks. Either way the user can
    still toggle it manually via the chevron button."""
    if rows is None:
        return ""

    expanded = bool(rows)

    # Shown right under the header, outside the collapsible body, so it's visible
    # even while the box is collapsed — no need to expand an empty box just to
    # find out *why* it's empty.
    empty_note_html = (
        f'<div class="flip-empty">No flips clear the {empty_pct:.0f}% profit bar '
        f"right now &mdash; {empty_hint}.</div>"
        if not rows else ""
    )

    if rows:
        cards_html = (
            '<div class="flip-viewport">'
            f'<div class="flip-scroll" data-ribbon="{ribbon_id}">'
            + "".join(_render_flip_card(r) for r in rows)
            + "</div></div>"
        )
    else:
        cards_html = ""

    nav_html = (
        '<div class="flip-ribbon-nav">'
        f'<button type="button" class="flip-nav-btn" data-ribbon="{ribbon_id}" data-dir="-1" aria-label="Previous">&lsaquo;</button>'
        f'<button type="button" class="flip-nav-btn" data-ribbon="{ribbon_id}" data-dir="1" aria-label="Next">&rsaquo;</button>'
        "</div>"
        if rows else ""
    )

    toggle_btn = (
        f'<button type="button" class="flip-toggle-btn" data-ribbon-toggle="{ribbon_id}" '
        f'aria-expanded="{"true" if expanded else "false"}" '
        f'aria-label="{"Collapse" if expanded else "Expand"}">'
        '<span class="chevron">&#9662;</span></button>'
    )

    return (
        f'<div class="box box-flip{"" if expanded else " flip-collapsed"}" data-ribbon-box="{ribbon_id}">'
        '<div class="flip-ribbon-head">'
        '<div>'
        f'<div class="flip-ribbon-title">{title}</div>'
        f'<div class="flip-ribbon-updated">{updated_label}</div>'
        "</div>"
        f'<div class="flip-ribbon-controls">{nav_html}{toggle_btn}</div>'
        "</div>"
        f"{empty_note_html}"
        f'<div class="flip-ribbon-body" data-ribbon-body="{ribbon_id}">'
        f'<div class="flip-ribbon-sub">{subtitle}</div>'
        f"{cards_html}"
        "</div>"
        "</div>"
    )


def render_flip_ribbon() -> str:
    """Standalone landing-page box (own row, above the search box, wider than it):
    top "buy now, sell tomorrow" picks across a basket of Midnight
    materials/flasks/potions/phials, ranked by net profit after the 5% AH cut.
    Returns "" if the cache hasn't fetched anything yet (e.g. very first request
    while the initial fetch is in flight) — a genuinely empty *result* (no
    profitable flips today) still renders the box with a friendly empty state."""
    rows = get_flip_ribbon_cached()
    return _render_flip_ribbon_box(
        rows,
        ribbon_id="daily",
        title="Midnight Short Flips &middot; Buy Today, Sell Tomorrow &middot; EU",
        subtitle=(
            "Ranked by net profit after the 5% AH cut, using each item's historical weekday price pattern. "
            "&#9679; dot = item quality (white/green/blue/purple = Common/Uncommon/Rare/Epic). "
            "&#9670; tag = crafting quality tier (same meaning as the Midnight Flasks box below) &mdash; raw materials show "
            "the lower-quality tier (the higher-quality tier of the same item costs more), flasks/phials/potions always show "
            "the higher-quality recipe rank. Every item here is Midnight-only, none are ported from Dragonflight."
        ),
        empty_pct=MIN_FLIP_PROFIT_PCT,
        empty_hint="check back later today or tomorrow",
        updated_label=_cache_age_label(_flip_ribbon_cache["fetched_at"]),
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
{_FLIP_CAROUSEL_SCRIPT}
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
    except UndermineApiError as exc:
        if exc.status_code == 429:
            return render_error_page(_RATE_LIMITED_MESSAGE, query), 429
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
            include_recipes=False,
            client=client, quote=quote, hourly=hourly,
        )
    except UndermineApiError as exc:
        if exc.status_code == 429:
            return render_error_page(_RATE_LIMITED_MESSAGE, query), 429
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
