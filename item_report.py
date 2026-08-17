"""WoW AH Item Report — Drak'Thul EU (Undermine Exchange)

Generates a price analysis for a given item:
  • Current price, quantity, and data freshness
  • 24-hour price range (min / max)
  • 14-day daily price ranges (min and max per day, derived from hourly data)
  • Combo chart: hourly price line + volume bars for the last 7 days

Usage:
    # Non-commodity item (gear, mounts, pets — realm-specific)
    python item_report.py --item-id 118852 --name "Invincible's Reins"

    # Commodity item (ore, herbs, cloth, enchanting mats — EU region-wide)
    python item_report.py --item-id 251285 --name "Petrified Root" --commodity

    # Save chart to a specific path
    python item_report.py --item-id 251285 --name "Petrified Root" --commodity --out /tmp/chart.png

    # Print JSON for programmatic use
    python item_report.py --item-id 251285 --commodity --json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from html import escape as html_escape
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote as url_quote

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from undermine_client import (
    DailySnapshot,
    PriceQuote,
    PriceSnapshot,
    UndermineApiError,
    UndermineClient,
)

# ── defaults ──────────────────────────────────────────────────────────────────
DEFAULT_REALM = "drakthul"
DEFAULT_REGION = "eu"
CHART_DAYS = 7
DAILY_HISTORY_DAYS = 14
BASELINE_WINDOW_DAYS = 30
RECOMMENDATION_THRESHOLD_PCT = 0.10  # how far from baseline before Buy/Sell instead of Hold
AH_SALE_CUT_PCT = 0.05  # WoW auction house's cut taken on a successful sale

# 60 days rather than 30: the prediction's weekday-seasonality component needs
# several samples per weekday to be stable (60d ≈ 8-9 samples/weekday vs. ≈4 at
# 30d), and the extra history also gives the trend line more to work with.
PREDICTION_WINDOW_DAYS = 60
PREDICTION_MIN_WINDOW_DAYS = 14  # below this, both trend and seasonality are too noisy to trust
PREDICTION_HORIZON_DAYS = 14
PREDICTION_FLAT_THRESHOLD_PCT = 3.0  # +/- this over the horizon counts as "flat" not rising/falling

# ── copper helpers ─────────────────────────────────────────────────────────────

def copper_to_gold(copper: int) -> float:
    return copper / 10_000


def fmt_gold(copper: int) -> str:
    g = copper // 10_000
    s = (copper % 10_000) // 100
    c = copper % 100
    return f"{g:,}g {s:02d}s {c:02d}c"


# ── statistics ─────────────────────────────────────────────────────────────────

def parse_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def last_n_hours(snapshots: list[PriceSnapshot], hours: int) -> list[PriceSnapshot]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [s for s in snapshots if parse_dt(s.snapshot) >= cutoff]


def last_n_days(snapshots: list[PriceSnapshot], days: int) -> list[PriceSnapshot]:
    return last_n_hours(snapshots, days * 24)


WEEKDAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def daily_ranges(snapshots: list[PriceSnapshot]) -> dict[str, dict]:
    """Group hourly snapshots by UTC date and compute min/max/avg price per day."""
    by_day: dict[str, list[int]] = defaultdict(list)
    qty_by_day: dict[str, list[int]] = defaultdict(list)
    for s in snapshots:
        if s.price_copper <= 0:
            continue
        day = parse_dt(s.snapshot).strftime("%Y-%m-%d")
        by_day[day].append(s.price_copper)
        qty_by_day[day].append(s.quantity)
    result = {}
    for day in sorted(by_day):
        prices = by_day[day]
        result[day] = {
            "min": min(prices),
            "max": max(prices),
            "avg": sum(prices) / len(prices),
            "avg_qty": int(sum(qty_by_day[day]) / len(qty_by_day[day])),
        }
    return result


# ── baseline price, buy/sell recommendation & weekday heatmap ──────────────────

def fetch_daily_history(
    client: UndermineClient, commodity: bool, realm: str, region: str, item_id: int,
) -> list[DailySnapshot] | None:
    """Fetch Undermine's daily (all-time) price/quantity history — this goes back
    years, unlike the ~14-day hourly endpoint used for the rest of the report.
    Returns None if the lookup fails, so callers can degrade gracefully."""
    try:
        if commodity:
            return client.commodity_daily(region, item_id)
        return client.item_daily_on_realm(region, realm, item_id)
    except UndermineApiError:
        return None


def compute_baseline(
    daily: list[DailySnapshot] | None, window_days: int = BASELINE_WINDOW_DAYS,
) -> dict | None:
    """Average AH price over the last `window_days` of daily history. Returns None
    if there's too little history to be meaningful (e.g. a brand-new item)."""
    if not daily:
        return None

    window = [d for d in daily[-window_days:] if d.price_copper > 0]
    if len(window) < 3:
        return None

    prices = [d.price_copper for d in window]
    return {
        "window_days": len(window),
        "avg_copper": int(sum(prices) / len(prices)),
        "min_copper": min(prices),
        "max_copper": max(prices),
    }


def compute_recommendation(
    current_price_copper: int,
    baseline: dict | None,
    threshold_pct: float = RECOMMENDATION_THRESHOLD_PCT,
    sale_cut_pct: float = AH_SALE_CUT_PCT,
) -> dict | None:
    """Buy/Sell/Hold call based on how the current price compares to the baseline
    average.

    The AH takes a `sale_cut_pct` cut on every completed sale (paid by the seller,
    not the buyer) — a seller only nets `price * (1 - sale_cut_pct)`. So it takes a
    bigger premium over baseline to be worth *selling* than it takes a discount to
    be worth *buying*; the sell threshold is adjusted accordingly. Returns None if
    there's no baseline to compare against.
    """
    if not baseline or baseline["avg_copper"] <= 0:
        return None

    avg = baseline["avg_copper"]
    window_days = baseline["window_days"]
    pct_vs_baseline = (current_price_copper - avg) / avg * 100

    buy_cutoff = avg * (1 - threshold_pct)
    # Selling nets you price*(1 - sale_cut_pct), so the *listing* price has to clear
    # a higher bar than a plain "+threshold_pct" over baseline to actually net that
    # much after the AH's cut — hence dividing by (1 - sale_cut_pct) here too.
    sell_cutoff = avg * (1 + threshold_pct) / (1 - sale_cut_pct)
    # Price to list *this* item at right now so that, after the AH cut, you clear
    # threshold_pct profit over what you're paying/holding at today — independent of
    # the baseline, useful even on a Hold.
    target_sell_price = current_price_copper * (1 + threshold_pct) / (1 - sale_cut_pct)

    if current_price_copper <= buy_cutoff:
        action = "buy"
        label = "Good time to buy"
        detail = (
            f"{abs(pct_vs_baseline):.0f}% below the {window_days}d average "
            f"(buy cutoff: {fmt_gold(int(buy_cutoff))})"
        )
    elif current_price_copper >= sell_cutoff:
        action = "sell"
        label = "Good time to sell"
        net_pct = ((current_price_copper * (1 - sale_cut_pct)) - avg) / avg * 100
        detail = (
            f"nets +{net_pct:.0f}% over the {window_days}d average "
            f"after the {sale_cut_pct * 100:.0f}% AH cut"
        )
    else:
        action = "hold"
        label = "Fair price"
        detail = (
            f"between the buy cutoff ({fmt_gold(int(buy_cutoff))}) and "
            f"sell cutoff ({fmt_gold(int(sell_cutoff))})"
        )

    return {
        "action": action,
        "label": label,
        "detail": detail,
        "pct_vs_baseline": pct_vs_baseline,
        "buy_cutoff_copper": int(buy_cutoff),
        "sell_cutoff_copper": int(sell_cutoff),
        "target_sell_price_copper": int(target_sell_price),
        "profit_pct": threshold_pct * 100,
    }


# Flag an item's flip numbers as unreliable when its current actual price has
# drifted this far (%) from the window's fitted trend line — a sign of a recent
# shock (crash/spike) the historical pattern hasn't absorbed yet, e.g. a 30-day-old
# weekday average is meaningless the day after a 50% crash.
MAX_TREND_DEVIATION_PCT = 20.0


def is_price_off_trend(
    current_price_copper: int | None, trend_price_copper: int | None,
    max_deviation_pct: float = MAX_TREND_DEVIATION_PCT,
) -> bool:
    """True if `current_price_copper` has diverged from the fitted trend line by
    more than `max_deviation_pct` — i.e. the item just had a sharp price move the
    historical weekday/hourly pattern doesn't reflect yet, so a "buy now, sell
    later" call built from that pattern isn't trustworthy right now."""
    if not current_price_copper or current_price_copper <= 0 or not trend_price_copper:
        return False
    deviation_pct = abs(current_price_copper - trend_price_copper) / trend_price_copper * 100
    return deviation_pct > max_deviation_pct


def compute_weekday_heatmap(
    daily: list[DailySnapshot] | None, window_days: int = BASELINE_WINDOW_DAYS,
) -> dict | None:
    """Buy/sell strength and supply level by weekday, over the last `window_days`
    of daily history.

    Detrended: rather than comparing each weekday's raw average price to the
    window's raw overall average (which would mistake a multi-day price crash or
    spike for a weekday-specific pattern — e.g. "Tuesdays are cheap" really just
    meaning "the item crashed this week and happened to have more Tuesday
    samples"), this fits a linear trend across the window (same method as
    `compute_price_prediction`) and looks at each weekday's *median* deviation
    from that trend line (median, not mean, so 1-2 outlier days can't dominate a
    weekday that's otherwise flat). `avg_price_copper` is then that deviation
    projected onto today's trend level, so it stays a meaningful "what this
    weekday usually looks like right now" absolute price — cheaper-than-trend
    days are buy-strong, pricier days are sell-strong. Supply is reported the
    same way (below/above average AH quantity) without buy/sell framing, and
    isn't detrended (quantity doesn't compound the way price does).

    Returns None if there's too little history to fill out a meaningful weekly
    pattern."""
    if not daily:
        return None

    window = [d for d in daily[-window_days:] if d.price_copper > 0]
    if len(window) < 7:
        return None

    n = len(window)
    xs = list(range(n))
    ys = [float(d.price_copper) for d in window]
    dates = [datetime.strptime(d.day, "%Y-%m-%d") for d in window]
    slope, intercept = _linear_regression(xs, ys)
    trend_last = max(0.0, slope * (n - 1) + intercept)

    by_weekday_resid: dict[str, list[float]] = defaultdict(list)
    by_weekday_qty: dict[str, list[int]] = defaultdict(list)
    for x, y, dt, d in zip(xs, ys, dates, window):
        wd = dt.strftime("%a")
        by_weekday_resid[wd].append(y - (slope * x + intercept))
        by_weekday_qty[wd].append(d.quantity)

    overall_avg_qty = sum(d.quantity for d in window) / len(window)

    days = []
    for wd in WEEKDAY_ORDER:
        resids = by_weekday_resid.get(wd) or []
        qtys = by_weekday_qty.get(wd) or []
        if not resids:
            days.append({
                "weekday": wd, "samples": 0,
                "avg_price_copper": None, "price_pct": None,
                "avg_qty": None, "qty_pct": None,
            })
            continue
        avg_price = max(0.0, trend_last + _median(resids))
        avg_qty = sum(qtys) / len(qtys) if qtys else 0
        days.append({
            "weekday": wd,
            "samples": len(resids),
            "avg_price_copper": int(round(avg_price)),
            "price_pct": (avg_price - trend_last) / trend_last * 100 if trend_last else 0.0,
            "avg_qty": int(avg_qty),
            "qty_pct": (avg_qty - overall_avg_qty) / overall_avg_qty * 100 if overall_avg_qty else 0.0,
        })

    avg_samples = sum(d["samples"] for d in days) / len(days)
    return {
        "window_days": len(window),
        "avg_samples": avg_samples,
        "days": days,
        "trend_price_copper": int(round(trend_last)),
    }


def _heatmap_price_color(pct: float | None, cap: float = 25.0) -> str:
    """Map a weekday's % price deviation from the window average to a background
    color: green (cheaper/buy-strong) to pink (pricier/sell-strong), intensity
    scaled by magnitude (capped at `cap`%)."""
    if pct is None:
        return "rgba(255,255,255,0.03)"
    clamped = max(-cap, min(cap, pct))
    alpha = 0.10 + 0.55 * (abs(clamped) / cap)
    return f"rgba(34,197,94,{alpha:.2f})" if pct < 0 else f"rgba(236,72,153,{alpha:.2f})"


def _heatmap_supply_color(pct: float | None, cap: float = 40.0) -> str:
    """Map a weekday's % quantity deviation from the window average to a blue
    background color, intensity scaled by magnitude (either direction)."""
    if pct is None:
        return "rgba(255,255,255,0.03)"
    clamped = max(-cap, min(cap, pct))
    alpha = 0.08 + 0.45 * (abs(clamped) / cap)
    return f"rgba(58,123,213,{alpha:.2f})"


def _weekday_profit_tooltip_html(
    day: dict, current_price_copper: int | None, sale_cut_pct: float = AH_SALE_CUT_PCT,
) -> str:
    """Hover tooltip for a price-row heatmap cell: what buying now and selling on this
    weekday (at its historical average price, after the AH's sale cut) would net you
    per item. "" if there's no current price to compare against."""
    if not current_price_copper or current_price_copper <= 0 or day["avg_price_copper"] is None:
        return ""

    weekday_price = day["avg_price_copper"]
    net_if_sold = weekday_price * (1 - sale_cut_pct)
    profit_copper = int(round(net_if_sold - current_price_copper))
    profit_pct = profit_copper / current_price_copper * 100
    profit_class = "profit-pos" if profit_copper >= 0 else "profit-neg"
    sign = "+" if profit_copper >= 0 else "-"

    return (
        '<div class="heatmap-tooltip">'
        f'<div class="heatmap-tooltip-title">{day["weekday"]} avg price: {html_escape(fmt_gold(weekday_price))}</div>'
        f'<div class="heatmap-tooltip-row">Buy now at {html_escape(fmt_gold(current_price_copper))}, '
        f'sell {day["weekday"]} (after {sale_cut_pct * 100:.0f}% AH cut)</div>'
        f'<div class="heatmap-tooltip-profit {profit_class}">'
        f'{sign}{html_escape(fmt_gold(abs(profit_copper)))} per item ({profit_pct:+.0f}%)</div>'
        "</div>"
    )


def render_weekday_heatmap_html(heatmap: dict | None, current_price_copper: int | None = None) -> str:
    """Render the two-row weekday heatmap (price strength + supply level), or a
    fallback message if there isn't enough daily history to build one. Price-row
    cells get an extra hover tooltip showing the potential per-item profit/loss of
    buying now and selling on that weekday, when `current_price_copper` is given."""
    if not heatmap:
        return '<div class="recipes-empty">Not enough daily history yet to build a weekly pattern.</div>'

    price_cells = []
    supply_cells = []
    for d in heatmap["days"]:
        if d["samples"] == 0:
            price_cells.append(
                f'<div class="heatmap-cell" style="background:{_heatmap_price_color(None)};">'
                f'<div class="wd">{d["weekday"]}</div><div class="val">&mdash;</div></div>'
            )
            supply_cells.append(
                f'<div class="heatmap-cell" style="background:{_heatmap_supply_color(None)};">'
                f'<div class="wd">{d["weekday"]}</div><div class="val">&mdash;</div></div>'
            )
            continue
        tooltip_html = _weekday_profit_tooltip_html(d, current_price_copper)
        price_cells.append(
            f'<div class="heatmap-cell" style="background:{_heatmap_price_color(d["price_pct"])};" '
            f'title="{html_escape(fmt_gold(d["avg_price_copper"]))} avg">'
            f'<div class="wd">{d["weekday"]}</div>'
            f'<div class="val">{d["price_pct"]:+.0f}%</div>'
            f'{tooltip_html}'
            "</div>"
        )
        supply_cells.append(
            f'<div class="heatmap-cell" style="background:{_heatmap_supply_color(d["qty_pct"])};" '
            f'title="{d["avg_qty"]:,} avg on AH">'
            f'<div class="wd">{d["weekday"]}</div>'
            f'<div class="val">{d["qty_pct"]:+.0f}%</div></div>'
        )

    return (
        '<div class="heatmap-section">'
        '<div class="heatmap-row-label">Price vs. window avg &middot; green = buy-strong, pink = sell-strong</div>'
        f'<div class="heatmap-grid">{"".join(price_cells)}</div>'
        "</div>"
        '<div class="heatmap-section">'
        '<div class="heatmap-row-label">Supply vs. window avg &middot; darker = more on AH</div>'
        f'<div class="heatmap-grid">{"".join(supply_cells)}</div>'
        "</div>"
        f'<div class="heatmap-note">Based on ~{heatmap["avg_samples"]:.1f} days per weekday '
        f'over the last {heatmap["window_days"]}d.</div>'
    )


# ── price prediction (trend + weekday seasonality) ──────────────────────────────

def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least-squares slope/intercept for y = slope*x + intercept."""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom if denom else 0.0
    intercept = mean_y - slope * mean_x
    return slope, intercept


def _median(values: list[float]) -> float:
    """Median of `values` — used instead of a plain mean when aggregating
    per-weekday/per-hour residuals, since a mean lets 1-2 outlier days (e.g. a
    short-lived price spike) dominate an otherwise-flat pattern; a median is far
    more robust to that."""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5


def compute_price_prediction(
    daily: list[DailySnapshot] | None,
    window_days: int = PREDICTION_WINDOW_DAYS,
    horizon_days: int = PREDICTION_HORIZON_DAYS,
) -> dict | None:
    """Forecast the next `horizon_days` of daily average price from the last
    `window_days` of daily history (or fewer if that's all Undermine has).

    Method — deliberately simple/explainable rather than a black-box model,
    consistent with the baseline/heatmap logic above:
      1. Fit a linear trend (least-squares regression on day index) to capture
         overall drift over the window.
      2. Layer a per-weekday seasonal offset on top: each weekday's average
         deviation from its own trend value (same idea as `compute_weekday_heatmap`,
         applied forward instead of backward).
      3. Widen the confidence band by sqrt(days-ahead), since a random-walk-style
         forecast's uncertainty compounds the further out it goes.

    Returns None if there's too little daily history (< `PREDICTION_MIN_WINDOW_DAYS`
    valid days) for the trend/seasonality split to be meaningful.
    """
    if not daily:
        return None

    window = [d for d in daily[-window_days:] if d.price_copper > 0]
    if len(window) < PREDICTION_MIN_WINDOW_DAYS:
        return None

    n = len(window)
    xs = list(range(n))
    ys = [float(d.price_copper) for d in window]
    dates = [datetime.strptime(d.day, "%Y-%m-%d") for d in window]

    slope, intercept = _linear_regression(xs, ys)

    weekday_residuals: dict[str, list[float]] = defaultdict(list)
    for x, y, dt in zip(xs, ys, dates):
        weekday_residuals[dt.strftime("%a")].append(y - (slope * x + intercept))
    weekday_adj = {wd: sum(vals) / len(vals) for wd, vals in weekday_residuals.items()}

    residuals_flat = [
        y - (slope * x + intercept) - weekday_adj[dt.strftime("%a")]
        for x, y, dt in zip(xs, ys, dates)
    ]
    noise_std = _stdev(residuals_flat)

    last_date = dates[-1]
    forecast = []
    for step in range(1, horizon_days + 1):
        future_x = n - 1 + step
        future_date = last_date + timedelta(days=step)
        wd = future_date.strftime("%a")
        predicted = max(0.0, slope * future_x + intercept + weekday_adj.get(wd, 0.0))
        band = noise_std * (step ** 0.5)
        forecast.append({
            "date": future_date.strftime("%Y-%m-%d"),
            "price_copper": int(round(predicted)),
            "low_copper": int(round(max(0.0, predicted - band))),
            "high_copper": int(round(predicted + band)),
        })

    avg_price = sum(ys) / n
    last_actual = ys[-1]
    day_horizon_price = forecast[-1]["price_copper"]
    total_change_pct = (
        (day_horizon_price - last_actual) / last_actual * 100 if last_actual else 0.0
    )
    if total_change_pct >= PREDICTION_FLAT_THRESHOLD_PCT:
        trend_direction = "rising"
    elif total_change_pct <= -PREDICTION_FLAT_THRESHOLD_PCT:
        trend_direction = "falling"
    else:
        trend_direction = "flat"

    coeff_of_variation = (noise_std / avg_price) if avg_price else 1.0
    if n >= 45 and coeff_of_variation < 0.15:
        confidence = "high"
    elif n >= 21 and coeff_of_variation < 0.30:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "window_days": n,
        "horizon_days": horizon_days,
        "trend_direction": trend_direction,
        "total_change_pct": total_change_pct,
        "confidence": confidence,
        "history": [{"date": d.day, "price_copper": d.price_copper} for d in window],
        "forecast": forecast,
    }


_PREDICTION_TREND_META = {
    "rising": {"pill_class": "pill-sell", "label": "Rising", "arrow": "\u2191"},
    "falling": {"pill_class": "pill-buy", "label": "Falling", "arrow": "\u2193"},
    "flat": {"pill_class": "pill-hold", "label": "Flat", "arrow": "\u2192"},
}
_PREDICTION_CONFIDENCE_COLOR = {
    "high": "var(--green)", "medium": "var(--gold)", "low": "var(--pink)",
}


def _forecast_vs_current_html(predicted_copper: int, current_price_copper: int | None) -> str:
    """Small badge for the Day-by-Day Forecast table: is this forecasted day's price
    higher, lower, or about the same (within PREDICTION_FLAT_THRESHOLD_PCT) as the
    current price?"""
    if not current_price_copper or current_price_copper <= 0:
        return '<span style="color:var(--muted);">&mdash;</span>'

    pct = (predicted_copper - current_price_copper) / current_price_copper * 100
    if pct >= PREDICTION_FLAT_THRESHOLD_PCT:
        arrow, label, color = "&#9650;", "Higher", "var(--green)"
    elif pct <= -PREDICTION_FLAT_THRESHOLD_PCT:
        arrow, label, color = "&#9660;", "Lower", "var(--pink)"
    else:
        arrow, label, color = "&#8594;", "Same", "var(--muted)"
    return (
        f'<span style="color:{color};font-weight:600;">{arrow} {label}</span> '
        f'<span style="color:var(--muted);">({pct:+.1f}%)</span>'
    )


def render_prediction_tab_html(prediction: dict | None, current_price_copper: int | None = None) -> str:
    """Render the full contents of the Prediction tab: summary pills, a forecast
    chart (built client-side from `DATA.prediction`), and a day-by-day table — or
    a fallback message if there isn't enough daily history yet."""
    if not prediction:
        return (
            '<div class="card">'
            '<div class="recipes-empty">Not enough daily history yet to build a '
            f"{PREDICTION_HORIZON_DAYS}-day price prediction (need at least "
            f"{PREDICTION_MIN_WINDOW_DAYS} days of data for this item).</div>"
            "</div>"
        )

    forecast = prediction["forecast"]
    day_horizon = forecast[-1]
    direction = prediction["trend_direction"]
    meta = _PREDICTION_TREND_META[direction]
    confidence = prediction["confidence"]

    pills = (
        '<div class="headline" style="margin-bottom:18px;">'
        f'<div class="pill"><div class="label">In {prediction["horizon_days"]} Days</div>'
        f'<div class="value" style="color:var(--purple);">{html_escape(fmt_gold(day_horizon["price_copper"]))}</div>'
        f'<div class="pill-sub">{html_escape(fmt_gold(day_horizon["low_copper"]))} &ndash; '
        f'{html_escape(fmt_gold(day_horizon["high_copper"]))}</div></div>'
        f'<div class="pill {meta["pill_class"]}"><div class="label">Trend</div>'
        f'<div class="value">{meta["arrow"]} {meta["label"]}</div>'
        f'<div class="pill-sub">{prediction["total_change_pct"]:+.1f}% over {prediction["horizon_days"]}d</div></div>'
        '<div class="pill"><div class="label">Confidence</div>'
        f'<div class="value" style="color:{_PREDICTION_CONFIDENCE_COLOR[confidence]};">{confidence.title()}</div>'
        f'<div class="pill-sub">based on {prediction["window_days"]}d of history</div></div>'
        "</div>"
    )

    rows = []
    for f in forecast:
        dt = datetime.strptime(f["date"], "%Y-%m-%d")
        rows.append(
            "<tr>"
            f'<td>{dt.strftime("%a %b %d")}</td>'
            f'<td class="price-cell">{html_escape(fmt_gold(f["price_copper"]))}</td>'
            f'<td class="muted-cell">{html_escape(fmt_gold(f["low_copper"]))} &ndash; '
            f'{html_escape(fmt_gold(f["high_copper"]))}</td>'
            f'<td>{_forecast_vs_current_html(f["price_copper"], current_price_copper)}</td>'
            "</tr>"
        )
    table = (
        '<table class="recipes-table">'
        "<thead><tr><th>Date</th><th>Predicted Price</th><th>Range</th><th>vs Current</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )

    return (
        f"{pills}"
        '<div class="grid-bottom">'
        '<div class="card">'
        f'<h3>Price Forecast &middot; Next {prediction["horizon_days"]}d</h3>'
        '<div class="chart-wrap xl"><canvas id="chartPrediction"></canvas></div>'
        '<div class="heatmap-note">Linear trend + weekday seasonality projected from the last '
        f'{prediction["window_days"]}d of daily history. Shaded band = uncertainty range, '
        "widening the further out the forecast goes. Directional guidance only — not a guarantee."
        "</div>"
        "</div>"
        '<div class="card">'
        "<h3>Day-by-Day Forecast</h3>"
        f"{table}"
        "</div>"
        "</div>"
    )


# ── Midnight flask overview (webapp landing page) ───────────────────────────────

# The higher-quality ("Rank 2", ilvl 295) variant of each Midnight combat flask.
# Midnight's crafted consumables come in two ranks that share the same name and are
# one item ID apart (e.g. 241320 = Rank 2 / ilvl 295, 241321 = Rank 1 / ilvl 278,
# confirmed via Wowhead's tooltip endpoint) — the lower-ilvl Rank 1 id is intentionally
# excluded here since the ask was for higher-quality only.
MIDNIGHT_FLASKS = [
    {
        "item_id": 241320, "name": "Flask of Thalassian Resistance", "stat": "Versatility",
        "icon": "inv_12_profession_alchemy_flask_sindoreipotion_yellow",
    },
    {
        "item_id": 241322, "name": "Flask of the Magisters", "stat": "Mastery",
        "icon": "inv_12_profession_alchemy_flask_sindoreipotion_black",
    },
    {
        "item_id": 241324, "name": "Flask of the Blood Knights", "stat": "Haste",
        "icon": "inv_12_profession_alchemy_flask_sindoreipotion_white-",
    },
    {
        "item_id": 241326, "name": "Flask of the Shattered Sun", "stat": "Critical Strike",
        "icon": "inv_12_profession_alchemy_flask_sindoreipotion_red--",
    },
]

TODAY_SIGNAL_THRESHOLD_PCT = 5.0  # weekday price deviation needed to call today buy/sell-strong


def build_flask_overview(client: UndermineClient, region: str = "eu") -> list[dict]:
    """Price + "is today a good day to buy/sell" signal for each higher-quality Midnight
    combat flask, for the webapp landing page.

    Best-effort per flask: an item Undermine doesn't track yet, or one with too little
    daily history to build a weekday pattern, still comes back with whatever data is
    available (see "available" / "today_signal" in each row) rather than failing outright.
    """
    today_wd = datetime.now(timezone.utc).strftime("%a")
    rows = []
    for flask in MIDNIGHT_FLASKS:
        item_id = flask["item_id"]
        row = {
            **flask,
            "wowhead_url": f"https://www.wowhead.com/item={item_id}",
            "available": False,
            "price_copper": None,
            "quantity": None,
            "today_signal": None,
        }
        try:
            quote = client.commodity_now(region, item_id)
        except UndermineApiError:
            rows.append(row)
            continue

        row["available"] = True
        row["price_copper"] = quote.price_copper
        row["quantity"] = quote.quantity

        daily = fetch_daily_history(client, True, DEFAULT_REALM, region, item_id)
        heatmap = compute_weekday_heatmap(daily)
        today_cell = None
        if heatmap:
            today_cell = next(
                (d for d in heatmap["days"] if d["weekday"] == today_wd and d["samples"] > 0),
                None,
            )
        if today_cell:
            pct = today_cell["price_pct"]
            if pct <= -TODAY_SIGNAL_THRESHOLD_PCT:
                action, label = "buy", "Good day to buy"
            elif pct >= TODAY_SIGNAL_THRESHOLD_PCT:
                action, label = "sell", "Good day to sell"
            else:
                action, label = "neutral", "Average day"
            row["today_signal"] = {"action": action, "label": label, "pct": pct}

        rows.append(row)
    return rows


# ── Midnight short-flip ribbon (webapp landing page) ────────────────────────────

# A small, hand-picked basket of Midnight commodities that are liquid enough (high
# AH volume, short buy-now/sell-tomorrow holding period) to make sense for quick
# flips, spanning the item types the ribbon is meant to cover. Flasks reuse the
# higher-quality (ilvl 295) ids from MIDNIGHT_FLASKS; phials/potions likewise pick
# the higher-quality rank where the two ranks are distinct items. Materials are the
# five non-zone-locked base Midnight herbs (raw, ungathered — the most heavily
# traded herb tier). Leather/ore/crystal/gem entries are the base (Silver-rank,
# where two quality ranks exist as distinct item ids) Skinning, Mining, Enchanting,
# and Jewelcrafting gathering/prospecting outputs — the highest-volume commodity
# tier for each of those professions.
#
# `rank`: every raw-gathering reagent (herb/leather/ore/crystal/gem) in Midnight
# exists as TWO separate item ids sharing one name — a cheaper "lower quality" tier and
# a pricier "higher quality" tier (same terminology as the "Midnight Flasks · EU · higher
# quality" box above). This was verified by pulling every item id that shares each name
# from Wowhead's own search index (not just guessing nearby ids) and comparing live
# Undermine prices for each pair, e.g. Void-Tempered Leather is 238511 (lower) / 238512
# (higher), Tranquility Bloom is 236761 (lower) / 236767 (higher). We deliberately pick
# the lower-quality id everywhere — it's the highest-volume, most-traded tier — so the
# ribbon is comparing like-for-like, not accidentally quoting a rarer higher-quality
# listing. Dazzling Thorium is the one reagent here confirmed to only have a single
# item id/quality. Crafted consumables (flasks/phials/potions) work differently: they're
# a single item id whose ilvl/power comes from the alchemist's recipe rank, so we pick
# the higher-quality (highest ilvl) recipe rank instead, since that's the version most
# buyers want — same pick as MIDNIGHT_FLASKS above.
MIDNIGHT_TRADE_GOODS = [
    # Flasks
    {
        "item_id": 241320, "name": "Flask of Thalassian Resistance", "category": "flask",
        "icon": "inv_12_profession_alchemy_flask_sindoreipotion_yellow", "quality": 1, "rank": "Higher quality",
    },
    {
        "item_id": 241322, "name": "Flask of the Magisters", "category": "flask",
        "icon": "inv_12_profession_alchemy_flask_sindoreipotion_black", "quality": 1, "rank": "Higher quality",
    },
    {
        "item_id": 241324, "name": "Flask of the Blood Knights", "category": "flask",
        "icon": "inv_12_profession_alchemy_flask_sindoreipotion_white-", "quality": 1, "rank": "Higher quality",
    },
    {
        "item_id": 241326, "name": "Flask of the Shattered Sun", "category": "flask",
        "icon": "inv_12_profession_alchemy_flask_sindoreipotion_red--", "quality": 1, "rank": "Higher quality",
    },
    # Phials (profession-stat consumables)
    {
        "item_id": 241310, "name": "Haranir Phial of Finesse", "category": "phial",
        "icon": "inv_12_profession_alchemy_flask_haranirpotion_blue", "quality": 1, "rank": "Higher quality",
    },
    {
        "item_id": 241314, "name": "Haranir Phial of Concentrated Ingenuity", "category": "phial",
        "icon": "inv_12_profession_alchemy_flask_haranirpotion_orange", "quality": 1, "rank": "Higher quality",
    },
    {
        "item_id": 241316, "name": "Haranir Phial of Perception", "category": "phial",
        "icon": "inv_12_profession_alchemy_flask_haranirpotion_purple", "quality": 1, "rank": "Higher quality",
    },
    # Potions
    {
        "item_id": 241304, "name": "Silvermoon Health Potion", "category": "potion",
        "icon": "inv_12_profession_alchemy_lightpotion_orange", "quality": 1, "rank": "Higher quality",
    },
    {
        "item_id": 241306, "name": "Refreshing Serum", "category": "potion",
        "icon": "inv_alchemy_80_potion01purple", "quality": 1, "rank": "Higher quality",
    },
    # Materials (base Midnight herbs)
    {
        "item_id": 236761, "name": "Tranquility Bloom", "category": "material",
        "icon": "inv_misc_herb_peacebloom", "quality": 1, "rank": "Lower quality",
    },
    {
        "item_id": 236774, "name": "Azeroot", "category": "material",
        "icon": "inv_herb_earthroot", "quality": 2, "rank": "Lower quality",
    },
    {
        "item_id": 236776, "name": "Argentleaf", "category": "material",
        "icon": "inv_misc_herb_silverleaf", "quality": 2, "rank": "Lower quality",
    },
    {
        "item_id": 236778, "name": "Mana Lily", "category": "material",
        "icon": "inv_misc_herb_mageroyal", "quality": 2, "rank": "Lower quality",
    },
    {
        "item_id": 236770, "name": "Sanguithorn", "category": "material",
        "icon": "inv_herb_bloodthistle", "quality": 2, "rank": "Lower quality",
    },
    # Leather/scales (base Midnight skinning materials)
    {
        "item_id": 238511, "name": "Void-Tempered Leather", "category": "leather",
        "icon": "inv_12_profession_skinning_thalassianleather_brown", "quality": 1, "rank": "Lower quality",
    },
    {
        "item_id": 238513, "name": "Void-Tempered Scales", "category": "leather",
        "icon": "inv_12_profession_skinning_thalassianscale_violet", "quality": 1, "rank": "Lower quality",
    },
    # Ores (base Midnight mining materials)
    {
        "item_id": 237359, "name": "Refulgent Copper Ore", "category": "ore",
        "icon": "inv_ore_refulgentcopper", "quality": 1, "rank": "Lower quality",
    },
    {
        "item_id": 237362, "name": "Umbral Tin Ore", "category": "ore",
        "icon": "inv_ore_umbraltin", "quality": 2, "rank": "Lower quality",
    },
    {
        "item_id": 237364, "name": "Brilliant Silver Ore", "category": "ore",
        "icon": "inv_ore_brilliantsilver", "quality": 2, "rank": "Lower quality",
    },
    {
        "item_id": 237366, "name": "Dazzling Thorium", "category": "ore",
        "icon": "inv_12_profession_mining_dazzlingthorium-", "quality": 3, "rank": "Only quality",
    },
    # Enchanting crystals/shards/dust (disenchanting byproducts)
    {
        "item_id": 243599, "name": "Eversinging Dust", "category": "crystal",
        "icon": "inv_12_profession_enchanting_enchantingdust_green", "quality": 1, "rank": "Lower quality",
    },
    {
        "item_id": 243602, "name": "Radiant Shard", "category": "crystal",
        "icon": "inv_12_profession_enchanting_enchantingshard_blue", "quality": 3, "rank": "Lower quality",
    },
    {
        "item_id": 243605, "name": "Dawn Crystal", "category": "crystal",
        "icon": "inv_12_profession_enchanting_enchantingcrystal_orange", "quality": 4, "rank": "Lower quality",
    },
    # Gems (uncut Jewelcrafting prospecting output)
    {
        "item_id": 242553, "name": "Sanguine Garnet", "category": "gem",
        "icon": "inv_12_profession_jewelcrafting_uncommon_gem_uncut_red", "quality": 2, "rank": "Lower quality",
    },
    {
        "item_id": 242554, "name": "Amani Lapis", "category": "gem",
        "icon": "inv_12_profession_jewelcrafting_uncommon_gem_uncut_blue", "quality": 2, "rank": "Lower quality",
    },
    {
        "item_id": 242607, "name": "Harandar Peridot", "category": "gem",
        "icon": "inv_12_profession_jewelcrafting_uncommon_gem_uncut_green", "quality": 2, "rank": "Lower quality",
    },
    {
        "item_id": 242606, "name": "Tenebrous Amethyst", "category": "gem",
        "icon": "inv_12_profession_jewelcrafting_uncommon_gem_uncut_purple", "quality": 2, "rank": "Lower quality",
    },
]

# Every entry above is confirmed against live Wowhead item data (see the "quality"
# field on each, matching Wowhead's/Blizzard's own item-rarity scale below) as a
# Midnight-expansion item id — nothing here is reused/ported from Dragonflight.


def get_midnight_quality_rank(item_id: int) -> str | None:
    """The crafting-quality tier label ("Lower quality" / "Higher quality" /
    "Only quality") for a Midnight item, if known — same terminology used on the
    landing-page flip ribbons and the Midnight Flasks box. Looked up from
    `MIDNIGHT_TRADE_GOODS` (raw materials — most have a lower/higher quality pair
    of item ids, see the comment above that list) and `MIDNIGHT_FLASKS` (always
    the higher-quality/max recipe rank). Returns None if this item id isn't one
    we've researched a quality split for — e.g. it's not a Midnight item at all,
    or it's a Midnight item whose quality tiers we haven't looked up yet."""
    for good in MIDNIGHT_TRADE_GOODS:
        if good["item_id"] == item_id:
            return good["rank"]
    for flask in MIDNIGHT_FLASKS:
        if flask["item_id"] == item_id:
            return "Higher quality"
    return None


CATEGORY_LABELS = {
    "flask": "Flask", "phial": "Phial", "potion": "Potion", "material": "Herb",
    "leather": "Leather", "ore": "Ore", "crystal": "Enchanting", "gem": "Gem",
}

# WoW's standard item-rarity scale (color + label), keyed by the numeric "quality"
# Wowhead reports in its tooltip JSON — used to badge each flip-ribbon card so it's
# obvious at a glance whether an item is a common/white reagent or a rarer
# uncommon/green, rare/blue, or epic/purple one (crafting materials in Midnight
# don't all share one rarity — e.g. base ores are Common, some prospected gems and
# disenchanting shards are Uncommon/Rare/Epic).
QUALITY_META = {
    0: ("Poor", "#9d9d9d"),
    1: ("Common", "#ffffff"),
    2: ("Uncommon", "#1eff00"),
    3: ("Rare", "#0070dd"),
    4: ("Epic", "#a335ee"),
    5: ("Legendary", "#ff8000"),
}

# Below this net profit (after the AH cut), a buy-now/sell-tomorrow flip isn't worth
# the overnight price risk — filters noise-level "profit" out of the ribbon.
MIN_FLIP_PROFIT_PCT = 2.0


def compute_tomorrow_flip(
    current_price_copper: int | None,
    heatmap: dict | None,
    sale_cut_pct: float = AH_SALE_CUT_PCT,
) -> dict | None:
    """"Buy now, sell tomorrow" call for one item: compares today's price to
    tomorrow's historical average (from the weekday heatmap), net of the AH's
    sale_cut_pct cut. Returns None if there's no current price or no weekday
    history for tomorrow specifically (too few samples for that weekday)."""
    if not current_price_copper or current_price_copper <= 0 or not heatmap:
        return None

    tomorrow_wd = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%a")
    day = next(
        (d for d in heatmap["days"] if d["weekday"] == tomorrow_wd and d["samples"] > 0), None,
    )
    if not day or day["avg_price_copper"] is None:
        return None

    tomorrow_avg_copper = day["avg_price_copper"]
    net_sell_copper = tomorrow_avg_copper * (1 - sale_cut_pct)
    profit_copper = int(round(net_sell_copper - current_price_copper))
    profit_pct = profit_copper / current_price_copper * 100

    return {
        "tomorrow_weekday": tomorrow_wd,
        "tomorrow_avg_copper": tomorrow_avg_copper,
        "net_sell_copper": int(round(net_sell_copper)),
        "profit_copper": profit_copper,
        "profit_pct": profit_pct,
        "samples": day["samples"],
    }


def build_flip_ribbon(
    client: UndermineClient,
    region: str = "eu",
    limit: int = 8,
    min_profit_pct: float = MIN_FLIP_PROFIT_PCT,
) -> list[dict]:
    """Rank `MIDNIGHT_TRADE_GOODS` by "buy now, sell tomorrow" net profit (after the
    AH's sale cut) and return the top `limit` still-profitable picks, for the webapp
    landing page's short-flip ribbon.

    Best-effort per item: one Undermine doesn't track yet, or with too little daily
    history to know tomorrow's weekday pattern, is silently skipped rather than
    failing the whole ribbon. Items below `min_profit_pct` net profit are excluded
    outright (not worth the overnight price risk), as are items whose current
    price has diverged too far from the fitted trend line (see
    `is_price_off_trend` — a sign of a recent shock the weekday pattern doesn't
    reflect yet), so the result can be shorter than `limit` — or empty, if
    nothing clears the bar today.
    """
    rows = []
    for good in MIDNIGHT_TRADE_GOODS:
        item_id = good["item_id"]
        try:
            quote = client.commodity_now(region, item_id)
        except UndermineApiError:
            continue

        daily = fetch_daily_history(client, True, DEFAULT_REALM, region, item_id)
        heatmap = compute_weekday_heatmap(daily)
        if not heatmap or is_price_off_trend(
            quote.price_copper, heatmap.get("trend_price_copper")
        ):
            continue

        flip = compute_tomorrow_flip(quote.price_copper, heatmap)
        if not flip or flip["profit_pct"] < min_profit_pct:
            continue

        rows.append({
            **good,
            "wowhead_url": f"https://www.wowhead.com/item={item_id}",
            "price_copper": quote.price_copper,
            "quantity": quote.quantity,
            "sell_label": f"Sell {flip['tomorrow_weekday']}",
            **flip,
        })

    rows.sort(key=lambda r: r["profit_pct"], reverse=True)
    return rows[:limit]


# ── Midnight quick-flip ribbon (buy now, sell in ~1 hour) ───────────────────────

# Below this net profit (after the AH cut), a buy-now/sell-in-an-hour flip isn't
# worth the churn — hour-to-hour price swings are naturally smaller than
# day-to-day ones, so this bar is set lower than MIN_FLIP_PROFIT_PCT.
MIN_HOURLY_FLIP_PROFIT_PCT = 1.0

# A genuine hour-to-hour AH price move is small; a computed "profit" bigger than
# this is far more likely a couple of outlier days skewing one hour's median than
# a real, repeatable edge, so it's excluded rather than shown.
MAX_HOURLY_FLIP_PROFIT_PCT = 12.0

# Require at least this many same-hour samples (out of up to 14, one per day in the
# ~14-day hourly history window) before trusting that hour's pattern — otherwise a
# single unusual data point could masquerade as a reliable one.
MIN_HOURLY_SAMPLES = 5

# How many days of hourly snapshots to build the hour-of-day pattern from — matches
# the ~14-day window the free Undermine hourly endpoint actually provides.
HOURLY_WINDOW_DAYS = 14


def compute_hourly_heatmap(
    hourly: list[PriceSnapshot] | None, window_days: int = HOURLY_WINDOW_DAYS,
) -> dict | None:
    """Buy/sell strength by hour-of-day (UTC), over the last `window_days` of hourly
    history — same idea as `compute_weekday_heatmap` but at hourly granularity, to
    support "buy now, sell in about an hour" calls.

    Detrended the same way: fits a linear trend across the whole hourly series and
    looks at each hour-of-day's *median* deviation from that trend line (median,
    not mean — this matters even more here than for the weekday heatmap, since a
    commodity that spikes mid-window and settles back down would otherwise leave
    "hour 12" looking like a huge buy/sell opportunity purely because a couple of
    samples from the spike days happened to land in that bucket, not because of
    any real, repeatable intraday pattern).

    Returns None if there's too little history to fill out a meaningful pattern."""
    if not hourly:
        return None

    window = sorted(
        (s for s in last_n_days(hourly, window_days) if s.price_copper > 0),
        key=lambda s: s.snapshot,
    )
    if len(window) < 24:
        return None

    n = len(window)
    xs = list(range(n))
    ys = [float(s.price_copper) for s in window]
    hours_of_day = [parse_dt(s.snapshot).hour for s in window]
    slope, intercept = _linear_regression(xs, ys)
    trend_last = max(0.0, slope * (n - 1) + intercept)

    by_hour_resid: dict[int, list[float]] = defaultdict(list)
    for x, y, hr in zip(xs, ys, hours_of_day):
        by_hour_resid[hr].append(y - (slope * x + intercept))

    hours = []
    for hr in range(24):
        resids = by_hour_resid.get(hr) or []
        if not resids:
            hours.append({
                "hour": hr, "samples": 0, "median_resid_copper": None,
                "avg_price_copper": None, "price_pct": None,
            })
            continue
        median_resid = _median(resids)
        avg_price = max(0.0, trend_last + median_resid)
        hours.append({
            "hour": hr,
            "samples": len(resids),
            "median_resid_copper": median_resid,
            "avg_price_copper": int(round(avg_price)),
            "price_pct": (avg_price - trend_last) / trend_last * 100 if trend_last else 0.0,
        })

    avg_samples = sum(h["samples"] for h in hours) / len(hours)
    return {
        "window_days": window_days,
        "avg_samples": avg_samples,
        "hours": hours,
        "trend_price_copper": int(round(trend_last)),
    }


def compute_next_hour_flip(
    current_price_copper: int | None,
    heatmap: dict | None,
    sale_cut_pct: float = AH_SALE_CUT_PCT,
    min_samples: int = MIN_HOURLY_SAMPLES,
    max_profit_pct: float = MAX_HOURLY_FLIP_PROFIT_PCT,
) -> dict | None:
    """"Buy now, sell in about an hour" call for one item: projects the next UTC
    hour's price by shifting the *current actual price* by the typical (median)
    difference between this hour's and the next hour's residual-from-trend, net
    of the AH's sale_cut_pct cut.

    Anchoring on the current actual price (not the trend line's absolute level)
    means only the *relative* hour-to-hour shape of the pattern is used — a trend
    fit skewed by a mid-window spike can still distort that shape, so a result is
    also discarded outright if it implies a profit bigger than `max_profit_pct`
    (see MAX_HOURLY_FLIP_PROFIT_PCT) or too few historical samples for either
    hour."""
    if not current_price_copper or current_price_copper <= 0 or not heatmap:
        return None

    now = datetime.now(timezone.utc)
    current_hour = now.hour
    next_hour = (now + timedelta(hours=1)).hour
    cur_cell = next(
        (h for h in heatmap["hours"] if h["hour"] == current_hour and h["samples"] >= min_samples), None,
    )
    next_cell = next(
        (h for h in heatmap["hours"] if h["hour"] == next_hour and h["samples"] >= min_samples), None,
    )
    if (
        not cur_cell or not next_cell
        or cur_cell["median_resid_copper"] is None or next_cell["median_resid_copper"] is None
    ):
        return None

    next_hour_avg_copper = max(
        0.0, current_price_copper + (next_cell["median_resid_copper"] - cur_cell["median_resid_copper"])
    )
    net_sell_copper = next_hour_avg_copper * (1 - sale_cut_pct)
    profit_copper = int(round(net_sell_copper - current_price_copper))
    profit_pct = profit_copper / current_price_copper * 100
    if abs(profit_pct) > max_profit_pct:
        return None

    return {
        "next_hour": next_hour,
        "next_hour_avg_copper": int(round(next_hour_avg_copper)),
        "net_sell_copper": int(round(net_sell_copper)),
        "profit_copper": profit_copper,
        "profit_pct": profit_pct,
        "samples": min(cur_cell["samples"], next_cell["samples"]),
    }


def build_hourly_flip_ribbon(
    client: UndermineClient,
    region: str = "eu",
    limit: int = 8,
    min_profit_pct: float = MIN_HOURLY_FLIP_PROFIT_PCT,
) -> list[dict]:
    """Rank `MIDNIGHT_TRADE_GOODS` by "buy now, sell in about an hour" net profit
    (after the AH's sale cut) and return the top `limit` still-profitable picks,
    for the webapp landing page's quick-flip ribbon. Same approach as
    `build_flip_ribbon`, but comparing the current price against a (detrended)
    hour-of-day heatmap instead of a weekday one.

    Best-effort per item: one Undermine doesn't track yet, or with too little
    hourly history to know the next hour's pattern, is silently skipped, as are
    items whose current price has diverged too far from the fitted trend line
    (see `is_price_off_trend`) — same off-trend guard as the daily ribbon.
    """
    rows = []
    for good in MIDNIGHT_TRADE_GOODS:
        item_id = good["item_id"]
        try:
            quote = client.commodity_now(region, item_id)
            hourly = client.commodity_hourly(region, item_id)
        except UndermineApiError:
            continue

        heatmap = compute_hourly_heatmap(hourly)
        if not heatmap or is_price_off_trend(
            quote.price_copper, heatmap.get("trend_price_copper")
        ):
            continue

        flip = compute_next_hour_flip(quote.price_copper, heatmap)
        if not flip or flip["profit_pct"] < min_profit_pct:
            continue

        rows.append({
            **good,
            "wowhead_url": f"https://www.wowhead.com/item={item_id}",
            "price_copper": quote.price_copper,
            "quantity": quote.quantity,
            "sell_label": "Sell in ~1h",
            **flip,
        })

    rows.sort(key=lambda r: r["profit_pct"], reverse=True)
    return rows[:limit]


# ── recipes (Wowhead "reagent for" scrape) ──────────────────────────────────────

WOWHEAD_ITEM_URL = "https://www.wowhead.com/item={item_id}/x"
WOWHEAD_TOOLTIP_URL = "https://nether.wowhead.com/tooltip/item/{item_id}"
WOWHEAD_ICON_URL = "https://wow.zamimg.com/images/wow/icons/medium/{icon}.jpg"
_WOWHEAD_HEADERS = {
    # NOTE: a full, "realistic" modern Chrome UA string actually gets blocked by
    # Wowhead's WAF here (likely a UA/TLS-fingerprint mismatch heuristic, since curl's
    # TLS handshake doesn't match real Chrome). This short, generic UA is what works.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}
MAX_RECIPE_ITEMS = 60  # cap how many distinct crafted items we price-check


def _curl_get(
    url: str, timeout: float = 15.0, retries: int = 3, backoff: float = 5.0,
    min_len: int = 0, reject_marker: str | None = None,
) -> str:
    """Fetch a URL via curl (bytes, decoded as UTF-8).

    Wowhead sits behind bot-detection (Cloudflare/CloudFront) that fingerprints the
    TLS/HTTP client and blocks Python's `requests`/`urllib` even with a spoofed
    User-Agent, while plain `curl` gets through — so we shell out to curl instead.
    It also applies a short-lived per-IP rate limit that returns a small ~900-byte
    "403 Request blocked" page; a short retry-with-backoff clears it.
    """
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("curl not found on PATH — required to fetch Wowhead data")

    last_body = ""
    for attempt in range(retries):
        result = subprocess.run(
            [
                curl,
                "-s",
                "-L",  # Wowhead 301-redirects /item=<id>/<slug> to the canonical slug URL
                "-A", _WOWHEAD_HEADERS["User-Agent"],
                "--max-time", str(int(timeout)),
                url,
            ],
            capture_output=True,
            timeout=timeout + 5,
        )
        body = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
        blocked = reject_marker is not None and reject_marker in body
        if result.returncode == 0 and len(body) >= min_len and not blocked:
            return body
        last_body = body
        if attempt < retries - 1:
            time.sleep(backoff)

    raise RuntimeError(
        f"Wowhead request failed for {url} after {retries} attempts (got {len(last_body)} bytes back)"
    )


def fetch_wowhead_item_meta(item_id: int, timeout: float = 10.0) -> dict:
    """Fetch an item's name/icon via Wowhead's lightweight tooltip JSON endpoint."""
    body = _curl_get(
        WOWHEAD_TOOLTIP_URL.format(item_id=item_id), timeout=timeout, retries=2, min_len=10,
    )
    data = json.loads(body)
    return {"name_enus": data.get("name"), "icon": data.get("icon")}


WOWHEAD_SEARCH_URL = "https://www.wowhead.com/search?q={query}"
_ITEM_URL_ID_RE = re.compile(r"item[=/](\d+)")
_CANONICAL_ITEM_RE = re.compile(r'<link rel="canonical" href="[^"]*item[=/](\d+)')


def resolve_item_query(query: str) -> int:
    """Resolve a user-entered search box query to a numeric WoW item ID.

    Accepts, in order of preference (cheapest/most reliable first):
      1. A plain numeric item ID, e.g. "2770".
      2. A Wowhead item URL or slug, e.g. "https://www.wowhead.com/item=2770/copper-ore"
         or just "item=2770/copper-ore".
      3. A free-text item name, e.g. "Copper Ore" — resolved via a best-effort scrape
         of Wowhead's search results page.

    Raises ValueError if the query is empty or a name search finds no match — callers
    should tell the user to paste the numeric ID or a Wowhead link instead, since that
    path never depends on scraping and always works.
    """
    query = query.strip()
    if not query:
        raise ValueError("Please enter an item name, ID, or Wowhead link.")

    if query.isdigit():
        return int(query)

    url_match = _ITEM_URL_ID_RE.search(query)
    if url_match:
        return int(url_match.group(1))

    return _search_wowhead_by_name(query)


_SEARCH_TOP_RESULTS_MARKER = "WH.SearchPage.showTopResults("
_SEARCH_ITEM_TYPE = 3  # Wowhead's internal "type" id for items (vs. NPCs, quests, guides, ...)


def _search_wowhead_by_name(name: str) -> int:
    """Best-effort scrape of Wowhead's search results page for the first matching
    item ID.

    On an exact single match Wowhead 301-redirects straight to the item page (curl -L
    follows this, so we'd see its <link rel="canonical"> tag). Otherwise it serves a
    results page with an inline `WH.SearchPage.showTopResults([...])` JSON array —
    each entry has a "type" (3 == item) and "typeId" (the item ID) — we take the
    first item-typed result, which matches Wowhead's own result ranking.
    """
    html = _curl_get(
        WOWHEAD_SEARCH_URL.format(query=url_quote(name)),
        timeout=10.0, retries=2, min_len=500,
    )

    canonical_match = _CANONICAL_ITEM_RE.search(html)
    if canonical_match:
        return int(canonical_match.group(1))

    marker_idx = html.find(_SEARCH_TOP_RESULTS_MARKER)
    if marker_idx != -1:
        decoder = json.JSONDecoder()
        try:
            results, _ = decoder.raw_decode(html, marker_idx + len(_SEARCH_TOP_RESULTS_MARKER))
            for entry in results:
                if entry.get("type") == _SEARCH_ITEM_TYPE and entry.get("typeId") is not None:
                    return int(entry["typeId"])
        except (ValueError, json.JSONDecodeError, AttributeError, TypeError):
            pass

    raise ValueError(
        f"Couldn't find an item matching {name!r} on Wowhead. "
        "Try pasting the numeric item ID or a Wowhead item link instead."
    )


def fetch_wowhead_reagent_for(item_id: int, timeout: float = 15.0) -> tuple[dict[int, dict], list[dict]]:
    """Scrape an item's Wowhead page for its "Reagent For" recipe listview.

    Returns (items_meta, recipes) where items_meta maps itemId -> {"name_enus": ..., "icon": ...}
    for every item referenced on the page (crafted items included), and recipes is the raw
    list of recipe dicts from the "reagent-for" listview (each with "creates"/"reagents"/"name").
    """
    html = _curl_get(
        WOWHEAD_ITEM_URL.format(item_id=item_id),
        timeout=timeout, min_len=5000, reject_marker="Request blocked",
    )

    decoder = json.JSONDecoder()
    items_meta: dict[int, dict] = {}
    meta_match = re.search(r"WH\.Gatherer\.addData\(3,\s*1,\s*", html)
    if meta_match:
        try:
            obj, _ = decoder.raw_decode(html, meta_match.end())
            for key, val in obj.items():
                try:
                    items_meta[int(key)] = val
                except (TypeError, ValueError):
                    continue
        except (ValueError, json.JSONDecodeError):
            pass

    recipes: list[dict] = []
    marker_idx = html.find("id: 'reagent-for'")
    if marker_idx != -1:
        data_idx = html.find("data: ", marker_idx)
        if data_idx != -1:
            bracket_idx = data_idx + len("data: ")
            try:
                obj, _ = decoder.raw_decode(html, bracket_idx)
                if isinstance(obj, list):
                    recipes = obj
            except (ValueError, json.JSONDecodeError):
                pass

    return items_meta, recipes


def _cached_item_meta(
    item_id: int, page_meta: dict[int, dict], cache: dict[int, dict]
) -> dict:
    """Resolve an item's {"name_enus", "icon"}, preferring the page's metadata blob
    (free, already fetched) and falling back to Wowhead's tooltip endpoint. Cached
    per item_id so a reagent that repeats across recipes is only looked up once."""
    if item_id in cache:
        return cache[item_id]
    meta = page_meta.get(item_id)
    if not meta or not meta.get("name_enus"):
        try:
            meta = fetch_wowhead_item_meta(item_id)
        except (RuntimeError, ValueError, json.JSONDecodeError):
            meta = {}
    cache[item_id] = meta
    return meta


def _cached_item_price(
    client: UndermineClient, region: str, realm: str, item_id: int, cache: dict[int, tuple]
) -> tuple[int | None, int | None]:
    """Resolve an item's (price_copper, quantity_on_ah) via Undermine, commodity-first
    with realm fallback. Cached per item_id."""
    if item_id in cache:
        return cache[item_id]
    price_copper: int | None = None
    quantity_on_ah: int | None = None
    try:
        quote = client.commodity_now(region, item_id)
        price_copper, quantity_on_ah = quote.price_copper, quote.quantity
    except UndermineApiError:
        try:
            quote = client.item_now_on_realm(region, realm, item_id)
            price_copper, quantity_on_ah = quote.price_copper, quote.quantity
        except UndermineApiError:
            pass
    cache[item_id] = (price_copper, quantity_on_ah)
    return cache[item_id]


def build_recipe_rows(
    client: UndermineClient,
    region: str,
    realm: str,
    item_id: int,
    item_name: str,
    item_price_copper: int,
) -> list[dict]:
    """Look up every recipe that uses `item_id` as a reagent: the current AH price of
    the item each one crafts, plus the full materials list (with per-material price and
    the resulting profit = crafted item value - total materials cost). Returns rows
    sorted by crafted-item price, descending (rows with no AH data sort last)."""
    items_meta, recipes = fetch_wowhead_reagent_for(item_id)

    meta_cache: dict[int, dict] = {item_id: items_meta.get(item_id, {"name_enus": item_name})}
    price_cache: dict[int, tuple] = {item_id: (item_price_copper, None)}

    rows: list[dict] = []
    seen_recipe_ids: set[int] = set()
    for recipe in recipes[:MAX_RECIPE_ITEMS]:
        creates = recipe.get("creates")
        if not creates:
            continue
        recipe_id = recipe.get("id")
        if recipe_id is not None:
            if recipe_id in seen_recipe_ids:
                continue
            seen_recipe_ids.add(recipe_id)

        crafted_id = creates[0]
        qty_min = creates[1] if len(creates) > 1 else 1
        qty_max = creates[2] if len(creates) > 2 else qty_min

        crafted_meta = _cached_item_meta(crafted_id, items_meta, meta_cache)
        crafted_name = crafted_meta.get("name_enus") or f"Item {crafted_id}"
        icon = crafted_meta.get("icon")
        price_copper, quantity_on_ah = _cached_item_price(client, region, realm, crafted_id, price_cache)

        materials: list[dict] = []
        for reagent in recipe.get("reagents") or []:
            if not isinstance(reagent, list) or len(reagent) < 2:
                continue
            reagent_id, reagent_qty = reagent[0], reagent[1]
            r_meta = _cached_item_meta(reagent_id, items_meta, meta_cache)
            r_price, _ = _cached_item_price(client, region, realm, reagent_id, price_cache)
            materials.append({
                "item_id": reagent_id,
                "name": r_meta.get("name_enus") or f"Item {reagent_id}",
                "qty": reagent_qty,
                "price_copper": r_price,
                "subtotal_copper": (r_price * reagent_qty) if r_price is not None else None,
                "is_source": reagent_id == item_id,
            })

        materials_cost_copper: int | None = None
        if materials and all(m["subtotal_copper"] is not None for m in materials):
            materials_cost_copper = sum(m["subtotal_copper"] for m in materials)

        profit_copper: int | None = None
        if materials_cost_copper is not None and price_copper is not None:
            profit_copper = price_copper * qty_min - materials_cost_copper

        rows.append({
            "recipe_name": recipe.get("name") or recipe.get("displayName") or "Unknown Recipe",
            "crafted_item_id": crafted_id,
            "crafted_item_name": crafted_name,
            "icon": icon,
            "qty_min": qty_min,
            "qty_max": qty_max,
            "price_copper": price_copper,
            "quantity_on_ah": quantity_on_ah,
            "materials": materials,
            "materials_cost_copper": materials_cost_copper,
            "profit_copper": profit_copper,
        })

    rows.sort(key=lambda r: r["price_copper"] if r["price_copper"] is not None else -1, reverse=True)
    return rows


def _render_material_tooltip(row: dict) -> str:
    """Render the hover tooltip: full materials list (qty, price, subtotal) plus the
    resulting materials cost / crafted value / profit breakdown for one recipe row."""
    materials = row.get("materials") or []
    if not materials:
        return ""

    mat_rows = []
    for m in materials:
        price_str = fmt_gold(m["price_copper"]) if m["price_copper"] is not None else "?"
        subtotal_str = fmt_gold(m["subtotal_copper"]) if m["subtotal_copper"] is not None else "?"
        name_html = html_escape(m["name"])
        if m.get("is_source"):
            name_html = f'<span class="source-mat">{name_html} \u2605</span>'
        mat_rows.append(
            "<tr>"
            f"<td>{name_html}</td>"
            f'<td>&times;{m["qty"]}</td>'
            f"<td>{html_escape(price_str)}</td>"
            f"<td>{html_escape(subtotal_str)}</td>"
            "</tr>"
        )

    cost_str = fmt_gold(row["materials_cost_copper"]) if row.get("materials_cost_copper") is not None else "?"
    crafted_value = (
        row["price_copper"] * row["qty_min"] if row.get("price_copper") is not None else None
    )
    value_str = fmt_gold(crafted_value) if crafted_value is not None else "?"

    if row.get("profit_copper") is not None:
        profit = row["profit_copper"]
        profit_class = "profit-pos" if profit >= 0 else "profit-neg"
        profit_str = ("+" if profit >= 0 else "-") + fmt_gold(abs(profit))
    else:
        profit_class = ""
        profit_str = "?"

    return (
        '<div class="mat-tooltip">'
        f'<div class="mat-tooltip-title">Materials for {html_escape(row["crafted_item_name"])}</div>'
        "<table>"
        "<thead><tr><th>Material</th><th>Qty</th><th>Price</th><th>Subtotal</th></tr></thead>"
        f"<tbody>{''.join(mat_rows)}</tbody>"
        "<tfoot>"
        f'<tr class="total-row"><td colspan="3">Materials Cost</td><td>{html_escape(cost_str)}</td></tr>'
        f'<tr><td colspan="3">Crafted Value (&times;{row["qty_min"]})</td><td>{html_escape(value_str)}</td></tr>'
        f'<tr class="total-row"><td colspan="3">Profit</td><td class="{profit_class}">{html_escape(profit_str)}</td></tr>'
        "</tfoot>"
        "</table>"
        "</div>"
    )


def render_recipes_card(rows: list[dict] | None, item_name: str) -> str:
    """Render the whole "Recipes" card, or "" if the item isn't used in any known recipe.

    - rows is None: Wowhead lookup failed -> show the card with an error note.
    - rows is []: lookup succeeded but the item is used in no recipes -> omit the card entirely.
    - rows has entries: show the card with the recipes table.
    """
    if not rows and rows is not None:
        return ""

    if rows is None:
        content = '<div class="recipes-empty">Recipe data temporarily unavailable (Wowhead lookup failed).</div>'
    else:
        content = render_recipes_html(rows)

    return f'<div class="card">\n    <h3>Recipes Using {html_escape(item_name)}</h3>\n    {content}\n  </div>'


def render_recipes_html(rows: list[dict]) -> str:
    """Render the "Recipes" table as an HTML fragment."""
    row_html = []
    for r in rows:
        qty_label = f"×{r['qty_min']}" if r["qty_min"] == r["qty_max"] else f"×{r['qty_min']}-{r['qty_max']}"
        if r["price_copper"] is not None:
            price_cell = f'<td class="price-cell">{html_escape(fmt_gold(r["price_copper"]))}</td>'
            qty_ah_cell = f'<td class="muted-cell">{r["quantity_on_ah"]:,}</td>'
        else:
            price_cell = '<td class="muted-cell">no AH data</td>'
            qty_ah_cell = '<td class="muted-cell">&mdash;</td>'
        icon_html = (
            f'<img src="{WOWHEAD_ICON_URL.format(icon=r["icon"])}" alt="">' if r.get("icon") else ""
        )
        tooltip_html = _render_material_tooltip(r)
        row_html.append(
            "<tr>"
            f'<td><div class="item-cell">{icon_html}'
            f'<a href="https://www.wowhead.com/item={r["crafted_item_id"]}" target="_blank" rel="noopener noreferrer">'
            f'{html_escape(r["crafted_item_name"])}</a>'
            f"{tooltip_html}"
            "</div></td>"
            f'<td class="muted-cell">{html_escape(r["recipe_name"])}</td>'
            f'<td class="muted-cell">{qty_label}</td>'
            f"{price_cell}"
            f"{qty_ah_cell}"
            "</tr>"
        )

    return (
        '<table class="recipes-table">'
        "<thead><tr><th>Crafted Item</th><th>Recipe</th><th>Yield</th><th>AH Price</th><th>On AH</th></tr></thead>"
        f"<tbody>{''.join(row_html)}</tbody>"
        "</table>"
    )


# ── chart ──────────────────────────────────────────────────────────────────────

def _gold_formatter(value: float, _pos: int) -> str:
    """Format y-axis tick labels as e.g. '1,234g'."""
    return f"{int(value):,}g"


def render_chart(
    snapshots_7d: list[PriceSnapshot],
    item_name: str,
    item_id: int,
    out_path: Path,
    scope: str,
    region: str,
) -> None:
    """Render a combo bar+line chart: volume bars + price line, last 7 days."""
    valid = [s for s in snapshots_7d if s.price_copper > 0]
    if not valid:
        print("[chart] No data with price > 0 in the last 7 days — chart skipped.", file=sys.stderr)
        return

    x = [parse_dt(s.snapshot) for s in valid]
    prices_gold = [copper_to_gold(s.price_copper) for s in valid]
    quantities = [s.quantity for s in valid]

    fig, ax_vol = plt.subplots(figsize=(14, 5))
    ax_price = ax_vol.twinx()

    fig.patch.set_facecolor("#1a1a2e")
    for ax in (ax_vol, ax_price):
        ax.set_facecolor("#16213e")
        ax.tick_params(colors="#c8cdd6")
        ax.spines[:].set_color("#2a2a4a")

    # Volume bars (primary axis, left)
    bar_color = "#3a7bd5"
    bar_width = timedelta(minutes=40)
    ax_vol.bar(x, quantities, width=bar_width, color=bar_color, alpha=0.45, label="Volume")
    ax_vol.set_ylabel("Quantity on AH", color=bar_color, fontsize=10)
    ax_vol.tick_params(axis="y", labelcolor=bar_color)
    ax_vol.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))

    # Price line (secondary axis, right)
    line_color = "#f0c040"
    ax_price.plot(x, prices_gold, color=line_color, linewidth=1.6, label="Price", zorder=3)
    ax_price.set_ylabel("Price (gold)", color=line_color, fontsize=10)
    ax_price.tick_params(axis="y", labelcolor=line_color)
    ax_price.yaxis.set_major_formatter(mticker.FuncFormatter(_gold_formatter))

    # X-axis formatting
    ax_vol.xaxis.set_major_locator(mdates.DayLocator(interval=1, tz=timezone.utc))
    ax_vol.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=timezone.utc))
    ax_vol.xaxis.set_minor_locator(mdates.HourLocator(byhour=range(0, 24, 6), tz=timezone.utc))
    plt.setp(ax_vol.xaxis.get_majorticklabels(), rotation=30, ha="right", color="#c8cdd6")

    scope_label = "EU Region (Commodity)" if scope == "region" else f"{scope.title()} / {region.upper()}"
    ax_vol.set_title(
        f"{item_name}  (item {item_id})  ·  {scope_label}  ·  Last 7 days",
        color="#e0e0f0",
        fontsize=12,
        pad=12,
    )

    # Combined legend
    lines_vol, labels_vol = ax_vol.get_legend_handles_labels()
    lines_price, labels_price = ax_price.get_legend_handles_labels()
    ax_price.legend(
        lines_vol + lines_price,
        labels_vol + labels_price,
        loc="upper left",
        facecolor="#1a1a2e",
        edgecolor="#2a2a4a",
        labelcolor="#c8cdd6",
        fontsize=9,
    )

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)


# ── HTML dashboard report ───────────────────────────────────────────────────────

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #12142b;
    --card: #1a1d3a;
    --card-border: #2a2e56;
    --text: #e7e9f5;
    --muted: #9198c2;
    --pink: #ec4899;
    --teal: #14b8a6;
    --purple: #8b5cf6;
    --blue: #3a7bd5;
    --gold: #f0c040;
    --green: #22c55e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 28px;
    background: var(--bg);
    background-image: radial-gradient(circle at 20% 0%, #1e2350 0%, var(--bg) 55%);
    color: var(--text);
    font-family: "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 22px;
  }
  header h1 { margin: 0; font-size: 22px; font-weight: 600; }
  header h1 a { color: var(--text); text-decoration: none; border-bottom: 1px dashed var(--muted); }
  header h1 a:hover { color: var(--gold); border-bottom-color: var(--gold); }
  header .meta { color: var(--muted); font-size: 13px; }
  .header-title { display: flex; align-items: center; gap: 12px; }
  .quality-badge {
    font-size: 11px; font-weight: 600; color: var(--muted); white-space: nowrap;
    background: rgba(255,255,255,0.06); border-radius: 6px; padding: 4px 9px;
  }
  .back-btn {
    display: inline-flex; align-items: center; justify-content: center;
    width: 32px; height: 32px; flex-shrink: 0;
    border: 1px solid var(--card-border); border-radius: 8px;
    color: var(--muted); text-decoration: none; font-size: 17px; line-height: 1;
  }
  .back-btn:hover { color: var(--gold); border-color: var(--gold); background: rgba(240,192,64,0.08); }
  .headline {
    display: flex;
    gap: 14px;
    margin-bottom: 24px;
    flex-wrap: wrap;
  }
  .pill {
    background: var(--card);
    border: 1px solid var(--card-border);
    border-radius: 12px;
    padding: 12px 18px;
    min-width: 140px;
  }
  .pill .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .05em; }
  .pill .value { font-size: 19px; font-weight: 600; margin-top: 4px; }
  .pill .pill-sub { color: var(--muted); font-size: 11px; margin-top: 4px; }
  .pill.pill-buy { border-color: var(--green); background: rgba(34,197,94,0.10); }
  .pill.pill-buy .value { color: var(--green); }
  .pill.pill-sell { border-color: var(--pink); background: rgba(236,72,153,0.10); }
  .pill.pill-sell .value { color: var(--pink); }
  .pill.pill-hold .value { color: var(--muted); }
  .grid-top {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 18px;
    margin-bottom: 18px;
  }
  .grid-bottom {
    display: grid;
    grid-template-columns: 1fr;
    gap: 18px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--card-border);
    border-radius: 14px;
    padding: 16px 16px 10px;
  }
  .card h3 {
    margin: 0 0 10px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--muted);
  }
  .chart-wrap { position: relative; height: 190px; }
  .chart-wrap.tall { height: 240px; }
  .chart-wrap.xl { height: 300px; }
  .stat-row + .stat-row { margin-top: 8px; padding-top: 8px; }
  .stat-row {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--card-border);
  }
  .stat-row div { text-align: center; }
  .stat-row .stat-label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .04em; }
  .stat-row .stat-value { font-size: 15px; font-weight: 600; margin-top: 3px; }
  .recipes-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 4px; }
  .recipes-table th {
    text-align: left;
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .04em;
    font-weight: 600;
    padding: 6px 10px;
    border-bottom: 1px solid var(--card-border);
  }
  .recipes-table td { padding: 8px 10px; border-bottom: 1px solid var(--card-border); vertical-align: middle; }
  .recipes-table tr:last-child td { border-bottom: none; }
  .recipes-table tr:hover td { background: rgba(255,255,255,0.03); }
  .recipes-table .item-cell { position: relative; display: flex; align-items: center; gap: 8px; cursor: default; }
  .recipes-table .item-cell img { width: 22px; height: 22px; border-radius: 4px; border: 1px solid var(--card-border); }
  .recipes-table .item-cell a { color: var(--text); text-decoration: none; border-bottom: 1px dotted var(--muted); }
  .recipes-table .item-cell a:hover { color: var(--gold); border-bottom-color: var(--gold); }
  .recipes-table .price-cell { color: var(--gold); font-weight: 600; white-space: nowrap; }
  .recipes-table .muted-cell { color: var(--muted); }
  .recipes-empty { color: var(--muted); font-size: 13px; padding: 10px 2px; }
  .heatmap-section + .heatmap-section { margin-top: 14px; }
  .heatmap-row-label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .03em; margin-bottom: 6px; }
  .heatmap-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 5px; }
  .heatmap-cell {
    position: relative;
    border: 1px solid var(--card-border);
    border-radius: 8px;
    padding: 8px 2px;
    text-align: center;
    cursor: default;
  }
  .heatmap-cell .wd { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .03em; }
  .heatmap-cell .val { font-size: 13px; font-weight: 600; margin-top: 3px; }
  .heatmap-note { color: var(--muted); font-size: 11px; margin-top: 12px; }
  .heatmap-tooltip {
    display: none;
    position: absolute;
    bottom: 100%;
    left: 50%;
    transform: translateX(-50%);
    margin-bottom: 8px;
    z-index: 50;
    width: 220px;
    background: #0d0f26;
    border: 1px solid var(--card-border);
    border-radius: 10px;
    padding: 10px 12px;
    box-shadow: 0 10px 28px rgba(0,0,0,0.55);
    text-align: left;
    white-space: normal;
  }
  .heatmap-cell:hover .heatmap-tooltip { display: block; }
  .heatmap-tooltip-title { font-size: 12px; font-weight: 600; margin-bottom: 6px; color: var(--text); }
  .heatmap-tooltip-row { font-size: 11px; color: var(--muted); line-height: 1.4; }
  .heatmap-tooltip-profit { font-size: 13px; font-weight: 700; margin-top: 6px; }
  .heatmap-tooltip-profit.profit-pos { color: var(--green); }
  .heatmap-tooltip-profit.profit-neg { color: var(--pink); }
  .mat-tooltip {
    display: none;
    position: absolute;
    bottom: 100%;
    left: 0;
    margin-bottom: 8px;
    z-index: 50;
    min-width: 280px;
    background: #0d0f26;
    border: 1px solid var(--card-border);
    border-radius: 10px;
    padding: 10px 12px 8px;
    box-shadow: 0 10px 28px rgba(0,0,0,0.55);
  }
  .item-cell:hover .mat-tooltip { display: block; }
  .mat-tooltip-title { font-size: 12px; font-weight: 600; margin-bottom: 6px; color: var(--text); }
  .mat-tooltip table { width: 100%; border-collapse: collapse; font-size: 11px; }
  .mat-tooltip th {
    text-align: left; color: var(--muted); font-weight: 600; text-transform: uppercase;
    font-size: 10px; letter-spacing: .03em; padding: 3px 4px; border-bottom: 1px solid var(--card-border);
  }
  .mat-tooltip td { text-align: left; padding: 4px 4px; white-space: nowrap; }
  .mat-tooltip tfoot td { padding-top: 6px; }
  .mat-tooltip tr.total-row td { border-top: 1px solid var(--card-border); font-weight: 600; padding-top: 6px; }
  .mat-tooltip .source-mat { color: var(--gold); }
  .mat-tooltip .profit-pos { color: var(--green); font-weight: 700; }
  .mat-tooltip .profit-neg { color: var(--pink); font-weight: 700; }
  .tabs { display: flex; gap: 4px; margin-bottom: 20px; border-bottom: 1px solid var(--card-border); }
  .tab-btn {
    background: none; border: none; color: var(--muted); font-size: 13px; font-weight: 600;
    font-family: inherit; padding: 10px 6px; margin: 0 10px -1px 0; cursor: pointer;
    border-bottom: 2px solid transparent;
  }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active { color: var(--gold); border-bottom-color: var(--gold); }
  .tab-content.hidden { display: none; }
  footer { margin-top: 22px; color: var(--muted); font-size: 11px; }

  @media (max-width: 860px) {
    .grid-top { grid-template-columns: 1fr; }
  }
  @media (max-width: 640px) {
    body { padding: 16px; }
    header { flex-direction: column; align-items: flex-start; gap: 4px; }
    header h1 { font-size: 19px; }
    .headline { gap: 10px; }
    .pill { min-width: 0; flex: 1 1 130px; padding: 10px 14px; }
    .pill .value { font-size: 17px; }
    .card { padding: 14px 12px 8px; }
    .chart-wrap.xl { height: 260px; }
    .stat-row { gap: 6px; }
    .stat-row .stat-value { font-size: 13px; }
    .heatmap-cell { padding: 6px 2px; }
    .heatmap-cell .val { font-size: 11px; }
    .recipes-table { font-size: 12px; }
    .recipes-table th, .recipes-table td { padding: 6px 6px; }
  }
  @media (max-width: 480px) {
    .heatmap-grid { gap: 3px; }
    .heatmap-cell .wd { font-size: 9px; }
    .heatmap-cell .val { font-size: 10px; }
    .stat-row .stat-label { font-size: 9px; }
    /* drop the least essential columns so the table fits without horizontal
       scroll (which would also clip the above-row hover tooltip) */
    .recipes-table th:nth-child(2), .recipes-table td:nth-child(2),
    .recipes-table th:nth-child(5), .recipes-table td:nth-child(5) { display: none; }
    .mat-tooltip { min-width: 0; width: 220px; }
    .mat-tooltip table { font-size: 10px; }
    .heatmap-tooltip { width: 160px; padding: 8px 10px; }
    .heatmap-tooltip-row, .heatmap-tooltip-title { font-size: 10px; }
    .heatmap-tooltip-profit { font-size: 12px; }
  }
</style>
</head>
<body>
<header>
  <div class="header-title">
    <a class="back-btn" href="/" title="Back to search">&larr;</a>
    <h1><a href="__WOWHEAD_URL__" target="_blank" rel="noopener noreferrer">__ITEM_NAME__</a></h1>
    __QUALITY_BADGE__
  </div>
  <div class="meta">__SCOPE__ &middot; __UPDATED__</div>
</header>

<div class="headline">
  <div class="pill"><div class="label">Current Price</div><div class="value" style="color:var(--gold);">__CUR_PRICE__</div></div>
  <div class="pill"><div class="label">On AH Now</div><div class="value" style="color:var(--blue);">__CUR_QTY__</div></div>
  <div class="pill"><div class="label">24h Range</div><div class="value">__H24_MIN__ &ndash; __H24_MAX__</div></div>
  <div class="pill"><div class="label">14d Range</div><div class="value">__D14_MIN__ &ndash; __D14_MAX__</div></div>
  __RECOMMENDATION_PILL__
</div>

<div class="tabs">
  <button class="tab-btn active" data-tab="overview" type="button">Overview</button>
  <button class="tab-btn" data-tab="prediction" type="button">Forecast</button>
</div>

<div id="tab-overview" class="tab-content">
  <div class="grid-top">
    <div class="card">
      <h3>Daily Price Range &middot; 14d</h3>
      <div class="chart-wrap"><canvas id="chartDailyRange"></canvas></div>
    </div>
    <div class="card">
      <h3>Weekday Buy/Sell Pattern &middot; __BASELINE_WINDOW_DAYS__d</h3>
      __WEEKDAY_HEATMAP__
    </div>
  </div>

  <div class="grid-bottom">
    <div class="card">
      <h3>Price &amp; Volume Trend &middot; 7d</h3>
      <div class="chart-wrap xl"><canvas id="chartPriceVolume"></canvas></div>
      <div class="stat-row">
        <div><div class="stat-label">Current Price</div><div class="stat-value" style="color:var(--gold);">__CUR_PRICE__</div></div>
        <div><div class="stat-label">24h Min</div><div class="stat-value">__H24_MIN__</div></div>
        <div><div class="stat-label">24h Max</div><div class="stat-value">__H24_MAX__</div></div>
      </div>
      <div class="stat-row">
        <div><div class="stat-label">Current Qty</div><div class="stat-value" style="color:var(--blue);">__CUR_QTY__</div></div>
        <div><div class="stat-label">Avg Qty 7d</div><div class="stat-value">__AVG_QTY_7D__</div></div>
        <div><div class="stat-label">Avg Qty 14d</div><div class="stat-value">__AVG_QTY_14D__</div></div>
      </div>
    </div>
    __RECIPES_CARD__
  </div>
</div>

<div id="tab-prediction" class="tab-content hidden">
  __PREDICTION_TAB__
</div>

<footer>Data: Undermine Exchange API &middot; generated __GENERATED_AT__</footer>

<script>
const DATA = __DATA_JSON__;
Chart.defaults.color = "#9198c2";
Chart.defaults.borderColor = "#2a2e56";
Chart.defaults.font.family = "Segoe UI, Roboto, Helvetica, Arial, sans-serif";
Chart.defaults.animation = false;

new Chart(document.getElementById("chartDailyRange"), {
  type: "bar",
  data: {
    labels: DATA.dailyDates,
    datasets: [
      { label: "Min", data: DATA.dailyMin, backgroundColor: "#14b8a6" },
      { label: "Max", data: DATA.dailyMax, backgroundColor: "#ec4899" },
    ],
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { position: "top", labels: { boxWidth: 10, font: { size: 10 } } } },
    scales: { y: { ticks: { callback: (v) => v + "g" } }, x: { ticks: { maxRotation: 45, minRotation: 45, font: { size: 9 } } } },
  },
});

new Chart(document.getElementById("chartPriceVolume"), {
  type: "bar",
  data: {
    labels: DATA.hourlyLabels,
    datasets: [
      { type: "bar", label: "Quantity", data: DATA.hourlyQty, backgroundColor: "rgba(58,123,213,0.55)",
        yAxisID: "yVol", order: 2 },
      { type: "line", label: "Price (g)", data: DATA.hourlyPrice, borderColor: "#f0c040",
        backgroundColor: "rgba(240,192,64,0.15)", fill: false, tension: 0.25, pointRadius: 0,
        borderWidth: 2, yAxisID: "yPrice", order: 1 },
    ],
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { position: "top", labels: { boxWidth: 10, font: { size: 10 } } } },
    scales: {
      yVol: {
        position: "left",
        ticks: { color: "#3a7bd5", callback: (v) => v.toLocaleString() },
        grid: { drawOnChartArea: false },
        title: { display: true, text: "Quantity on AH", color: "#3a7bd5", font: { size: 10 } },
      },
      yPrice: {
        position: "right",
        ticks: { color: "#f0c040", callback: (v) => v + "g" },
        grid: { color: "#2a2e56" },
        title: { display: true, text: "Price (gold)", color: "#f0c040", font: { size: 10 } },
      },
      x: { ticks: { maxTicksLimit: 9, font: { size: 9 } } },
    },
  },
});

function createPredictionChart() {
  const p = DATA.prediction;
  const canvas = document.getElementById("chartPrediction");
  if (!p || !canvas) return;

  // Pad each series to the full (history + forecast) length so both lines share
  // one x-axis, and repeat the last historical point as the forecast's first
  // point so the dashed line connects to the solid one with no visual gap.
  const labels = p.historyLabels.concat(p.forecastLabels);
  const nHist = p.historyLabels.length;
  const lastActual = p.historyPrices[nHist - 1];

  const historyData = p.historyPrices.concat(Array(p.forecastLabels.length).fill(null));
  const forecastData = Array(nHist - 1).fill(null).concat([lastActual], p.forecastPrices);
  const upperData = Array(nHist - 1).fill(null).concat([lastActual], p.forecastHigh);
  const lowerData = Array(nHist - 1).fill(null).concat([lastActual], p.forecastLow);

  new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Upper bound", data: upperData, borderWidth: 0, pointRadius: 0,
          fill: false, tension: 0.2,
        },
        {
          label: "Lower bound", data: lowerData, borderWidth: 0, pointRadius: 0,
          backgroundColor: "rgba(139,92,246,0.15)", fill: "-1", tension: 0.2,
        },
        {
          label: "Historical", data: historyData, borderColor: "#f0c040",
          backgroundColor: "rgba(240,192,64,0.1)", fill: false, tension: 0.2,
          pointRadius: 0, borderWidth: 2,
        },
        {
          label: "Forecast", data: forecastData, borderColor: "#8b5cf6",
          borderDash: [6, 4], fill: false, tension: 0.2, pointRadius: 0, borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          position: "top", labels: { boxWidth: 10, font: { size: 10 } },
          filter: (item) => item.text !== "Upper bound" && item.text !== "Lower bound",
        },
      },
      scales: {
        y: { ticks: { callback: (v) => v + "g" } },
        x: { ticks: { maxTicksLimit: 10, font: { size: 9 } } },
      },
    },
  });
}

const tabBtns = document.querySelectorAll(".tab-btn");
let predictionChartCreated = false;
tabBtns.forEach((btn) => {
  btn.addEventListener("click", () => {
    tabBtns.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".tab-content").forEach((c) => c.classList.add("hidden"));
    document.getElementById("tab-" + btn.dataset.tab).classList.remove("hidden");
    if (btn.dataset.tab === "prediction" && !predictionChartCreated) {
      createPredictionChart();
      predictionChartCreated = true;
    }
  });
});

</script>
</body>
</html>
"""


def render_recommendation_pill(baseline: dict | None, recommendation: dict | None) -> str:
    """Render the headline 'Recommendation' pill (Buy/Sell/Hold vs. the baseline
    average), or "" if there wasn't enough daily history to compute one."""
    if not baseline or not recommendation:
        return ""
    action = recommendation["action"]
    value_label = {"buy": "BUY", "sell": "SELL", "hold": "HOLD"}[action]
    target_sell = recommendation.get("target_sell_price_copper")
    profit_pct = recommendation.get("profit_pct", RECOMMENDATION_THRESHOLD_PCT * 100)
    target_line = ""
    if target_sell:
        target_line = (
            '<div class="pill-sub">List at '
            f'<strong>{html_escape(fmt_gold(target_sell))}</strong> for +{profit_pct:.0f}% '
            "profit on today's price, after the AH cut</div>"
        )
    return (
        f'<div class="pill pill-{action}">'
        f'<div class="label">{baseline["window_days"]}d Baseline &middot; {html_escape(fmt_gold(baseline["avg_copper"]))}</div>'
        f'<div class="value">{value_label}</div>'
        f'<div class="pill-sub">{html_escape(recommendation["detail"])}</div>'
        f"{target_line}"
        "</div>"
    )


def render_html_report(
    item_name: str,
    item_id: int,
    commodity: bool,
    realm: str,
    region: str,
    current_price: int,
    current_qty: int,
    last_updated: str | None,
    snapshots_all: list[PriceSnapshot],
    out_path: Path | None = None,
    recipe_rows: list[dict] | None = None,
    baseline: dict | None = None,
    recommendation: dict | None = None,
    weekday_heatmap: dict | None = None,
    prediction: dict | None = None,
) -> str:
    """Build a self-contained Chart.js dashboard HTML report and return it as a string.

    If `out_path` is given, also writes it to disk (creating parent dirs as needed)."""
    now_utc = datetime.now(timezone.utc)
    scope = "EU Region (Commodity)" if commodity else f"{realm.title()} / {region.upper()}"

    h24 = [s for s in last_n_hours(snapshots_all, 24) if s.price_copper > 0]
    h24_min = min((s.price_copper for s in h24), default=current_price)
    h24_max = max((s.price_copper for s in h24), default=current_price)

    hist_14d = last_n_days(snapshots_all, DAILY_HISTORY_DAYS)
    ranges = daily_ranges(hist_14d)
    last_14 = sorted(ranges.items())[-DAILY_HISTORY_DAYS:]
    d14_min = min((v["min"] for _, v in last_14), default=current_price)
    d14_max = max((v["max"] for _, v in last_14), default=current_price)

    snapshots_7d = [s for s in last_n_days(snapshots_all, CHART_DAYS) if s.price_copper > 0]
    avg_qty_7d = int(sum(s.quantity for s in snapshots_7d) / len(snapshots_7d)) if snapshots_7d else current_qty
    avg_qty_14d = (
        int(sum(v["avg_qty"] for _, v in last_14) / len(last_14)) if last_14 else current_qty
    )

    quality_rank = get_midnight_quality_rank(item_id)
    quality_badge_html = (
        f'<span class="quality-badge">&#9670; {html_escape(quality_rank)}</span>' if quality_rank else ""
    )

    updated_str = "no data"
    if last_updated:
        try:
            age = now_utc - parse_dt(last_updated)
            mins = int(age.total_seconds() // 60)
            updated_str = f"updated {mins}m ago"
        except Exception:
            updated_str = f"updated {last_updated}"

    prediction_js = None
    if prediction:
        history = prediction["history"]
        forecast = prediction["forecast"]
        prediction_js = {
            "historyLabels": [
                datetime.strptime(h["date"], "%Y-%m-%d").strftime("%b %d") for h in history
            ],
            "historyPrices": [round(copper_to_gold(h["price_copper"]), 2) for h in history],
            "forecastLabels": [
                datetime.strptime(f["date"], "%Y-%m-%d").strftime("%b %d") for f in forecast
            ],
            "forecastPrices": [round(copper_to_gold(f["price_copper"]), 2) for f in forecast],
            "forecastLow": [round(copper_to_gold(f["low_copper"]), 2) for f in forecast],
            "forecastHigh": [round(copper_to_gold(f["high_copper"]), 2) for f in forecast],
        }

    data = {
        "dailyDates": [datetime.strptime(d, "%Y-%m-%d").strftime("%b %d") for d, _ in last_14],
        "dailyMin": [round(copper_to_gold(v["min"]), 2) for _, v in last_14],
        "dailyMax": [round(copper_to_gold(v["max"]), 2) for _, v in last_14],
        "hourlyLabels": [parse_dt(s.snapshot).strftime("%b %d %Hh") for s in snapshots_7d],
        "hourlyPrice": [round(copper_to_gold(s.price_copper), 2) for s in snapshots_7d],
        "hourlyQty": [s.quantity for s in snapshots_7d],
        "prediction": prediction_js,
    }

    html = (
        _HTML_TEMPLATE.replace("__TITLE__", f"{item_name} — AH Report")
        .replace("__ITEM_NAME__", item_name)
        .replace("__ITEM_ID__", str(item_id))
        .replace("__WOWHEAD_URL__", f"https://www.wowhead.com/item={item_id}")
        .replace("__QUALITY_BADGE__", quality_badge_html)
        .replace("__SCOPE__", scope)
        .replace("__UPDATED__", updated_str)
        .replace("__CUR_PRICE__", fmt_gold(current_price))
        .replace("__CUR_QTY__", f"{current_qty:,}")
        .replace("__H24_MIN__", fmt_gold(h24_min))
        .replace("__H24_MAX__", fmt_gold(h24_max))
        .replace("__D14_MIN__", fmt_gold(d14_min))
        .replace("__D14_MAX__", fmt_gold(d14_max))
        .replace("__AVG_QTY_7D__", f"{avg_qty_7d:,}")
        .replace("__AVG_QTY_14D__", f"{avg_qty_14d:,}")
        .replace("__GENERATED_AT__", now_utc.strftime("%Y-%m-%d %H:%M UTC"))
        .replace("__DATA_JSON__", json.dumps(data))
        .replace("__RECIPES_CARD__", render_recipes_card(recipe_rows, item_name))
        .replace("__RECOMMENDATION_PILL__", render_recommendation_pill(baseline, recommendation))
        .replace("__WEEKDAY_HEATMAP__", render_weekday_heatmap_html(weekday_heatmap, current_price))
        .replace("__PREDICTION_TAB__", render_prediction_tab_html(prediction, current_price))
        .replace(
            "__BASELINE_WINDOW_DAYS__",
            str(weekday_heatmap["window_days"] if weekday_heatmap else BASELINE_WINDOW_DAYS),
        )
    )

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")

    return html


# ── text report ────────────────────────────────────────────────────────────────

_BAR_CHARS = " ▁▂▃▄▅▆▇█"

def _sparkbar(value: int, max_value: int, width: int = 8) -> str:
    if max_value == 0:
        return _BAR_CHARS[0] * width
    ratio = value / max_value
    filled = round(ratio * width)
    idx = min(len(_BAR_CHARS) - 1, max(1, round(ratio * (len(_BAR_CHARS) - 1))))
    return _BAR_CHARS[idx] * filled + " " * (width - filled)


def print_report(
    item_name: str,
    item_id: int,
    commodity: bool,
    realm: str,
    region: str,
    current_price: int,
    current_qty: int,
    last_updated: str | None,
    snapshots_all: list[PriceSnapshot],
    chart_path: Path | None,
    baseline: dict | None = None,
    recommendation: dict | None = None,
    prediction: dict | None = None,
) -> None:
    now_utc = datetime.now(timezone.utc)
    scope = "EU Region (Commodity)" if commodity else f"{realm.title()} / {region.upper()}"

    # 24h range
    h24 = [s for s in last_n_hours(snapshots_all, 24) if s.price_copper > 0]
    h24_min = min((s.price_copper for s in h24), default=current_price)
    h24_max = max((s.price_copper for s in h24), default=current_price)

    # 14-day daily ranges (from hourly data)
    hist_14d = last_n_days(snapshots_all, DAILY_HISTORY_DAYS)
    ranges = daily_ranges(hist_14d)
    last_14 = sorted(ranges.items())[-DAILY_HISTORY_DAYS:]

    # max price across 14d for sparkline scaling
    max_14d = max((v["max"] for _, v in last_14), default=current_price) or 1

    W = 64
    sep = "─" * W

    updated_str = ""
    if last_updated:
        try:
            age = now_utc - parse_dt(last_updated)
            mins = int(age.total_seconds() // 60)
            updated_str = f"  updated {mins}m ago"
        except Exception:
            updated_str = f"  updated {last_updated}"

    print(f"\n{'═' * W}")
    print(f"  {item_name}  │  item {item_id}  │  {scope}{updated_str}")
    print(f"{'═' * W}")
    print(f"  Current price   {fmt_gold(current_price):<24}  ×{current_qty:,} on AH")
    print(f"  24h range       {fmt_gold(h24_min)}  –  {fmt_gold(h24_max)}")
    if baseline and recommendation:
        print(
            f"  {baseline['window_days']}d baseline    {fmt_gold(baseline['avg_copper']):<24}  "
            f"{recommendation['label'].upper()} ({recommendation['detail']})"
        )
        target_sell = recommendation.get("target_sell_price_copper")
        if target_sell:
            profit_pct = recommendation.get("profit_pct", RECOMMENDATION_THRESHOLD_PCT * 100)
            print(
                f"  {'':<16}  List at {fmt_gold(target_sell)} for +{profit_pct:.0f}% profit "
                "on today's price, after the AH cut"
            )
    if prediction:
        day_h = prediction["forecast"][-1]
        print(
            f"  {prediction['horizon_days']}d forecast   {fmt_gold(day_h['price_copper']):<24}  "
            f"{prediction['trend_direction'].upper()} ({prediction['total_change_pct']:+.1f}%, "
            f"{prediction['confidence']} confidence, {prediction['window_days']}d history)"
        )
    print(f"{sep}")
    print(f"  14-day daily price ranges  (hourly min – max)")
    print(f"  {'Date':<12}  {'Min':>14}  {'Max':>14}  {'Avg qty':>9}  Chart")

    for day, stats in last_14:
        bar_min = _sparkbar(stats["min"], max_14d, 6)
        bar_max = _sparkbar(stats["max"], max_14d, 6)
        try:
            dt = datetime.strptime(day, "%Y-%m-%d")
            day_label = dt.strftime("%a %b %d")
        except ValueError:
            day_label = day
        print(
            f"  {day_label:<12}  "
            f"{fmt_gold(stats['min']):>14}  "
            f"{fmt_gold(stats['max']):>14}  "
            f"{stats['avg_qty']:>9,}  "
            f"{bar_min}…{bar_max}"
        )

    print(f"{sep}")
    if chart_path and chart_path.exists():
        print(f"  Chart saved → {chart_path}")
    print(f"{'═' * W}\n")


# ── JSON output ─────────────────────────────────────────────────────────────────

def build_json(
    item_name: str,
    item_id: int,
    commodity: bool,
    realm: str,
    region: str,
    current_price: int,
    current_qty: int,
    last_updated: str | None,
    snapshots_all: list[PriceSnapshot],
    chart_path: Path | None,
    baseline: dict | None = None,
    recommendation: dict | None = None,
    weekday_heatmap: dict | None = None,
    prediction: dict | None = None,
) -> dict:
    h24 = [s for s in last_n_hours(snapshots_all, 24) if s.price_copper > 0]
    hist_14d = last_n_days(snapshots_all, DAILY_HISTORY_DAYS)
    ranges = daily_ranges(hist_14d)
    last_14 = {day: v for day, v in sorted(ranges.items())[-DAILY_HISTORY_DAYS:]}
    return {
        "item_id": item_id,
        "item_name": item_name,
        "commodity": commodity,
        "realm": realm if not commodity else None,
        "region": region,
        "current_price_copper": current_price,
        "current_price_gold": fmt_gold(current_price),
        "current_quantity": current_qty,
        "last_updated": last_updated,
        "h24_min_copper": min((s.price_copper for s in h24), default=current_price),
        "h24_max_copper": max((s.price_copper for s in h24), default=current_price),
        "daily_14d": {
            day: {
                "min_copper": v["min"],
                "max_copper": v["max"],
                "avg_copper": int(v["avg"]),
                "avg_qty": v["avg_qty"],
            }
            for day, v in last_14.items()
        },
        "chart_path": str(chart_path) if chart_path and chart_path.exists() else None,
        "baseline": baseline,
        "recommendation": recommendation,
        "weekday_heatmap": weekday_heatmap,
        "prediction": prediction,
    }


# ── scope auto-detection + report orchestration (shared by CLI and webapp) ─────

def detect_scope(
    client: UndermineClient,
    region: str,
    item_id: int,
    realm_override: str | None = None,
) -> tuple[bool, str, PriceQuote, list[PriceSnapshot]]:
    """Auto-detect whether an item is a region-wide commodity or a realm-specific
    item, by trying the commodity endpoint first and falling back to a realm lookup.

    Returns (commodity, realm, quote, hourly). `realm` is only meaningful when
    commodity is False. Raises UndermineApiError if the item isn't found via either
    endpoint (wrong ID, or simply not tracked by Undermine)."""
    try:
        quote_now = client.commodity_now(region, item_id)
        hourly = client.commodity_hourly(region, item_id)
        return True, DEFAULT_REALM, quote_now, hourly
    except UndermineApiError:
        pass

    realm = realm_override or DEFAULT_REALM
    quote_now = client.item_now_on_realm(region, realm, item_id)
    hourly = client.item_hourly_on_realm(region, realm, item_id)
    return False, realm, quote_now, hourly


def generate_report(
    item_id: int,
    item_name: str,
    commodity: bool,
    realm: str = DEFAULT_REALM,
    region: str = DEFAULT_REGION,
    include_recipes: bool = True,
    html_path: Path | None = None,
    chart_path: Path | None = None,
    client: UndermineClient | None = None,
    quote: PriceQuote | None = None,
    hourly: list[PriceSnapshot] | None = None,
) -> dict:
    """Fetch price data from Undermine, optionally look up recipes on Wowhead, and
    render the HTML dashboard. This is the single pipeline shared by the CLI
    (`main()`) and the Flask webapp (`webapp.py`).

    Pass `quote`/`hourly` if the caller already fetched them (e.g. via
    `detect_scope`) to avoid a redundant Undermine request. Returns
    {"html", "quote", "hourly", "recipe_rows"}. Raises UndermineApiError if the
    price/history fetch fails.
    """
    client = client or UndermineClient()

    if quote is None or hourly is None:
        if commodity:
            quote = client.commodity_now(region, item_id)
            hourly = client.commodity_hourly(region, item_id)
        else:
            quote = client.item_now_on_realm(region, realm, item_id)
            hourly = client.item_hourly_on_realm(region, realm, item_id)

    if chart_path is not None:
        snapshots_7d = last_n_days(hourly, CHART_DAYS)
        scope = "region" if commodity else realm
        render_chart(snapshots_7d, item_name, item_id, chart_path, scope, region)

    recipe_rows: list[dict] | None = None
    if include_recipes:
        try:
            recipe_rows = build_recipe_rows(
                client, region, realm, item_id, item_name, quote.price_copper
            )
        except (RuntimeError, UndermineApiError) as exc:
            print(f"[recipes] Skipped — Wowhead lookup failed: {exc}", file=sys.stderr)
            recipe_rows = None

    daily = fetch_daily_history(client, commodity, realm, region, item_id)
    baseline = compute_baseline(daily)
    recommendation = compute_recommendation(quote.price_copper, baseline)
    weekday_heatmap = compute_weekday_heatmap(daily)
    prediction = compute_price_prediction(daily)

    html = render_html_report(
        item_name, item_id, commodity, realm, region,
        quote.price_copper, quote.quantity, quote.last_updated,
        hourly, html_path, recipe_rows=recipe_rows,
        baseline=baseline, recommendation=recommendation,
        weekday_heatmap=weekday_heatmap, prediction=prediction,
    )

    return {
        "html": html,
        "quote": quote,
        "hourly": hourly,
        "recipe_rows": recipe_rows,
        "baseline": baseline,
        "recommendation": recommendation,
        "weekday_heatmap": weekday_heatmap,
        "prediction": prediction,
    }


# ── main ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="WoW AH item price report — Drak'Thul EU (Undermine Exchange)"
    )
    p.add_argument("--item-id", type=int, required=True, help="WoW item ID")
    p.add_argument("--name", default="", help="Item display name (cosmetic)")
    p.add_argument(
        "--commodity",
        action="store_true",
        help="Treat as a stackable commodity (EU region-wide AH)",
    )
    p.add_argument(
        "--realm",
        default=DEFAULT_REALM,
        help=f"Realm slug (default: {DEFAULT_REALM}; only used for non-commodities)",
    )
    p.add_argument(
        "--region",
        default=DEFAULT_REGION,
        choices=["us", "eu", "tw", "kr"],
        help=f"Region (default: {DEFAULT_REGION})",
    )
    p.add_argument(
        "--out",
        default="",
        help="Chart output path (default: item_<id>_<realm>.png next to this script)",
    )
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Print machine-readable JSON instead of the formatted report",
    )
    p.add_argument(
        "--no-chart",
        action="store_true",
        help="Skip chart generation",
    )
    p.add_argument(
        "--no-html",
        action="store_true",
        help="Skip HTML dashboard report generation",
    )
    p.add_argument(
        "--html-out",
        default="",
        help="HTML report output path (default: item_<id>_<realm>.html next to this script)",
    )
    p.add_argument(
        "--no-recipes",
        action="store_true",
        help="Skip the Recipes section (Wowhead lookup of what this item crafts into)",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()

    item_id: int = args.item_id
    item_name: str = args.name or f"Item {item_id}"
    commodity: bool = args.commodity
    realm: str = args.realm
    region: str = args.region

    # Chart output path
    if args.no_chart:
        chart_path: Path | None = None
    elif args.out:
        chart_path = Path(args.out)
    else:
        scope_slug = "commodity" if commodity else realm
        chart_path = Path(__file__).parent / f"item_{item_id}_{scope_slug}_{region}.png"

    # HTML dashboard output path
    if args.no_html:
        html_path: Path | None = None
    elif args.html_out:
        html_path = Path(args.html_out)
    else:
        scope_slug = "commodity" if commodity else realm
        html_path = Path(__file__).parent / f"item_{item_id}_{scope_slug}_{region}.html"

    try:
        result = generate_report(
            item_id, item_name, commodity, realm, region,
            include_recipes=not args.no_recipes,
            html_path=html_path,
            chart_path=chart_path,
        )
    except UndermineApiError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    now_quote = result["quote"]
    hourly = result["hourly"]
    baseline = result["baseline"]
    recommendation = result["recommendation"]
    weekday_heatmap = result["weekday_heatmap"]
    prediction = result["prediction"]

    if html_path is not None:
        print(f"HTML report saved -> {html_path}", file=sys.stderr)

    # Output
    if args.as_json:
        print(json.dumps(
            build_json(
                item_name, item_id, commodity, realm, region,
                now_quote.price_copper, now_quote.quantity,
                now_quote.last_updated, hourly, chart_path,
                baseline=baseline, recommendation=recommendation,
                weekday_heatmap=weekday_heatmap, prediction=prediction,
            ),
            indent=2,
        ))
    else:
        print_report(
            item_name, item_id, commodity, realm, region,
            now_quote.price_copper, now_quote.quantity,
            now_quote.last_updated, hourly, chart_path,
            baseline=baseline, recommendation=recommendation,
            prediction=prediction,
        )


if __name__ == "__main__":
    main()
