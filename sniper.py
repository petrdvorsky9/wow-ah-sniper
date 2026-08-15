"""WoW Auction House sniper — Undermine Exchange (default) or Blizzard AH API.

Usage:
    # One-off price check via Undermine (default)
    python sniper.py check --item-id 251285 --region eu --commodity

    # One-off price check via Blizzard's own API
    python sniper.py check --item-id 251285 --region eu --commodity --source blizzard

    # Realm item
    python sniper.py check --item-id 118852 --region eu --realm drakthul

    # Watch everything in watchlist.yaml, polling every 5 minutes.
    # Undermine data refreshes ~hourly (Blizzard's AH snapshot cadence), so
    # polling much faster just burns your API rate limit budget.
    python sniper.py watch --interval 300
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from blizzard_client import BlizzardApiError, BlizzardClient
from undermine_client import PriceQuote, UndermineApiError, UndermineClient

WATCHLIST_PATH = Path(__file__).parent / "watchlist.yaml"

SOURCE_UNDERMINE = "undermine"
SOURCE_BLIZZARD = "blizzard"
VALID_SOURCES = (SOURCE_UNDERMINE, SOURCE_BLIZZARD)
DEFAULT_SOURCE = SOURCE_UNDERMINE


# ---------------------------------------------------------------------------
# Client registry — initialised lazily per source on first use
# ---------------------------------------------------------------------------

class _Clients:
    def __init__(self) -> None:
        self._undermine: UndermineClient | None = None
        self._blizzard: BlizzardClient | None = None

    def undermine(self) -> UndermineClient:
        if self._undermine is None:
            self._undermine = UndermineClient()
        return self._undermine

    def blizzard(self) -> BlizzardClient:
        if self._blizzard is None:
            self._blizzard = BlizzardClient()
        return self._blizzard

    def for_source(self, source: str) -> UndermineClient | BlizzardClient:
        if source == SOURCE_BLIZZARD:
            return self.blizzard()
        return self.undermine()


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def load_watchlist(path: Path = WATCHLIST_PATH) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Watchlist not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("items", [])


def fetch_quote(
    clients: _Clients, entry: dict, default_source: str = DEFAULT_SOURCE
) -> PriceQuote:
    source = entry.get("source", default_source)
    client = clients.for_source(source)
    region = entry["region"]
    item_id = entry["item_id"]
    if entry.get("commodity"):
        return client.commodity_now(region, item_id)
    realm = entry.get("realm")
    if not realm:
        raise ValueError(
            f"Item {entry.get('name', item_id)} needs a 'realm' when commodity: false"
        )
    return client.item_now_on_realm(region, realm, item_id)


def print_quote(name: str, quote: PriceQuote, max_price_gold: float | None = None) -> bool:
    is_deal = max_price_gold is not None and quote.gold <= max_price_gold
    marker = " <<< DEAL!" if is_deal else ""
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] "
        f"{name} ({quote.scope}/{quote.region}): "
        f"{quote.formatted()}  x{quote.quantity}{marker}"
    )
    return is_deal


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> None:
    clients = _Clients()
    entry = {
        "item_id": args.item_id,
        "region": args.region,
        "commodity": args.commodity,
        "realm": args.realm,
        "source": args.source,
    }
    try:
        quote = fetch_quote(clients, entry, default_source=args.source)
    except (UndermineApiError, BlizzardApiError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print_quote(f"item {args.item_id}", quote)


def cmd_watch(args: argparse.Namespace) -> None:
    clients = _Clients()
    watchlist = load_watchlist(Path(args.watchlist))
    if not watchlist:
        print("Watchlist is empty. Add items to watchlist.yaml first.", file=sys.stderr)
        sys.exit(1)

    print(
        f"Watching {len(watchlist)} item(s) "
        f"[default source: {args.source}], "
        f"polling every {args.interval}s. Ctrl+C to stop.\n"
    )
    try:
        while True:
            for entry in watchlist:
                name = entry.get("name", str(entry["item_id"]))
                source = entry.get("source", args.source)
                try:
                    quote = fetch_quote(clients, entry, default_source=args.source)
                except (UndermineApiError, BlizzardApiError, ValueError) as exc:
                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] "
                        f"{name} [{source}]: ERROR - {exc}"
                    )
                    continue
                is_deal = print_quote(name, quote, entry.get("max_price_gold"))
                if is_deal:
                    print("\a", end="")  # terminal bell
            print("-" * 60)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="WoW Auction House sniper (Undermine Exchange API or Blizzard AH API)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="One-off price lookup for a single item")
    check.add_argument("--item-id", type=int, required=True, help="WoW item ID")
    check.add_argument("--region", required=True, choices=["us", "eu", "tw", "kr"])
    check.add_argument("--commodity", action="store_true", help="Item is a stackable commodity")
    check.add_argument("--realm", help="Realm slug (required if not --commodity)")
    check.add_argument(
        "--source",
        choices=VALID_SOURCES,
        default=DEFAULT_SOURCE,
        help=f"Data source (default: {DEFAULT_SOURCE})",
    )
    check.set_defaults(func=cmd_check)

    watch = sub.add_parser("watch", help="Poll watchlist.yaml on a loop and alert on deals")
    watch.add_argument("--interval", type=int, default=300, help="Seconds between polls (default 300)")
    watch.add_argument("--watchlist", default=str(WATCHLIST_PATH), help="Path to watchlist YAML")
    watch.add_argument(
        "--source",
        choices=VALID_SOURCES,
        default=DEFAULT_SOURCE,
        help=f"Default data source for items that don't specify one (default: {DEFAULT_SOURCE})",
    )
    watch.set_defaults(func=cmd_watch)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
