"""Blizzard Game Data API client for WoW Auction House data.

Requires a client ID and secret from https://develop.battle.net/
(create an application, no special permissions needed for AH data).

Set BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET in your .env file.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests
from dotenv import load_dotenv

from undermine_client import PriceQuote

load_dotenv()

VALID_REGIONS = {"us", "eu", "tw", "kr"}

# Commodity auction data and full realm dumps are large; cache them to avoid
# fetching on every poll cycle (Blizzard updates AH snapshots ~hourly anyway).
_CACHE_TTL = 300  # seconds


class BlizzardApiError(RuntimeError):
    """Raised when the Blizzard Game Data API returns an error response."""


class BlizzardClient:
    """Client for Blizzard's WoW Auction House Game Data API.

    Uses the OAuth2 client_credentials flow — no user login required.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout: float = 20.0,
    ):
        self.client_id = client_id or os.environ.get("BLIZZARD_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("BLIZZARD_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise BlizzardApiError(
                "Blizzard API credentials not found. "
                "Set BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET in your .env file. "
                "Create an app at https://develop.battle.net/"
            )
        self.timeout = timeout
        self._session = requests.Session()

        # token cache: region -> (access_token, expiry_epoch)
        self._tokens: dict[str, tuple[str, float]] = {}
        # auction data cache: cache_key -> (data, expiry_epoch)
        self._auction_cache: dict[str, tuple[list[dict], float]] = {}
        # connected realm slug -> id cache (permanent; IDs don't change)
        self._realm_id_cache: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _token(self, region: str) -> str:
        token, expiry = self._tokens.get(region, ("", 0.0))
        if time.monotonic() < expiry - 60:
            return token
        resp = self._session.post(
            f"https://{region}.battle.net/oauth/token",
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise BlizzardApiError(
                f"Blizzard OAuth failed ({resp.status_code}): {resp.text}"
            )
        data = resp.json()
        token = data["access_token"]
        self._tokens[region] = (token, time.monotonic() + data.get("expires_in", 86400))
        return token

    # ------------------------------------------------------------------
    # Raw HTTP
    # ------------------------------------------------------------------

    def _get(self, region: str, path: str, namespace_prefix: str = "dynamic") -> dict[str, Any]:
        url = f"https://{region}.api.blizzard.com{path}"
        resp = self._session.get(
            url,
            headers={"Authorization": f"Bearer {self._token(region)}"},
            params={"namespace": f"{namespace_prefix}-{region}", "locale": "en_US"},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise BlizzardApiError(
                f"Blizzard API error ({resp.status_code}) for {path}: {resp.text}"
            )
        return resp.json()

    # ------------------------------------------------------------------
    # Realm helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_region(region: str) -> str:
        region = region.lower()
        if region not in VALID_REGIONS:
            raise ValueError(f"region must be one of {sorted(VALID_REGIONS)}, got {region!r}")
        return region

    def connected_realm_id(self, region: str, realm_slug: str) -> int:
        """Resolve a realm slug to its connected-realm ID (cached permanently)."""
        cache_key = f"{region}/{realm_slug}"
        if cache_key in self._realm_id_cache:
            return self._realm_id_cache[cache_key]

        token = self._token(region)
        url = f"https://{region}.api.blizzard.com/data/wow/search/connected-realm"
        resp = self._session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={
                "namespace": f"dynamic-{region}",
                "realms.slug": realm_slug,
                "_pageSize": 1,
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise BlizzardApiError(
                f"Connected-realm lookup failed for {realm_slug!r} in {region}: {resp.text}"
            )
        results = resp.json().get("results", [])
        if not results:
            raise BlizzardApiError(
                f"No connected realm found for slug {realm_slug!r} in {region}. "
                "Check the slug at https://develop.battle.net/documentation/world-of-warcraft"
            )
        cr_id: int = results[0]["data"]["id"]
        self._realm_id_cache[cache_key] = cr_id
        return cr_id

    # ------------------------------------------------------------------
    # Raw auction fetches (with in-process cache)
    # ------------------------------------------------------------------

    def _commodity_auctions(self, region: str) -> list[dict]:
        cache_key = f"commodity/{region}"
        auctions, expiry = self._auction_cache.get(cache_key, ([], 0.0))
        if auctions and time.monotonic() < expiry:
            return auctions
        data = self._get(region, "/data/wow/auctions/commodities")
        auctions = data.get("auctions", [])
        self._auction_cache[cache_key] = (auctions, time.monotonic() + _CACHE_TTL)
        return auctions

    def _realm_auctions(self, region: str, connected_realm_id: int) -> list[dict]:
        cache_key = f"realm/{region}/{connected_realm_id}"
        auctions, expiry = self._auction_cache.get(cache_key, ([], 0.0))
        if auctions and time.monotonic() < expiry:
            return auctions
        data = self._get(region, f"/data/wow/connected-realm/{connected_realm_id}/auctions")
        auctions = data.get("auctions", [])
        self._auction_cache[cache_key] = (auctions, time.monotonic() + _CACHE_TTL)
        return auctions

    # ------------------------------------------------------------------
    # Public interface (matches UndermineClient's method signatures)
    # ------------------------------------------------------------------

    def commodity_now(self, region: str, item_id: int) -> PriceQuote:
        """Lowest unit price and total quantity for a commodity item across the region."""
        region = self._check_region(region)
        auctions = self._commodity_auctions(region)
        matching = [a for a in auctions if a["item"]["id"] == item_id]
        if not matching:
            raise BlizzardApiError(
                f"No commodity listings found for item {item_id} in {region}"
            )
        matching.sort(key=lambda a: a["unit_price"])
        lowest_price: int = matching[0]["unit_price"]
        total_quantity: int = sum(a["quantity"] for a in matching)
        return PriceQuote(
            item_id=item_id,
            price_copper=lowest_price,
            quantity=total_quantity,
            scope="region",
            region=region,
        )

    def item_now_on_realm(self, region: str, realm_slug: str, item_id: int) -> PriceQuote:
        """Cheapest per-item buyout price and total quantity for a realm item."""
        region = self._check_region(region)
        cr_id = self.connected_realm_id(region, realm_slug)
        auctions = self._realm_auctions(region, cr_id)
        # Only consider buyout listings (buyout == 0 means bid-only)
        matching = [
            a for a in auctions
            if a["item"]["id"] == item_id and a.get("buyout", 0) > 0
        ]
        if not matching:
            raise BlizzardApiError(
                f"No buyout listings found for item {item_id} on {realm_slug}-{region}"
            )
        # Per-item buyout price (buyout covers the whole stack)
        per_item = sorted(
            matching, key=lambda a: a["buyout"] // max(a.get("quantity", 1), 1)
        )
        cheapest = per_item[0]
        price_per_item = cheapest["buyout"] // max(cheapest.get("quantity", 1), 1)
        total_quantity = sum(a.get("quantity", 1) for a in matching)
        return PriceQuote(
            item_id=item_id,
            price_copper=price_per_item,
            quantity=total_quantity,
            scope=realm_slug,
            region=region,
        )
