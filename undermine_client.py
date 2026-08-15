"""Thin client for the Undermine Exchange API (https://undermine.exchange/api.html)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://api.undermine.exchange"
VALID_REGIONS = {"us", "eu", "tw", "kr"}


class UndermineApiError(RuntimeError):
    """Raised when the Undermine Exchange API returns an error response."""


@dataclass
class PriceQuote:
    item_id: int
    price_copper: int
    quantity: int
    scope: str  # "region" or a realm slug
    region: str
    last_updated: str | None = None
    last_seen: str | None = None

    @property
    def gold(self) -> int:
        return self.price_copper // 10_000

    @property
    def silver(self) -> int:
        return (self.price_copper % 10_000) // 100

    @property
    def copper(self) -> int:
        return self.price_copper % 100

    def formatted(self) -> str:
        return f"{self.gold}g {self.silver}s {self.copper}c"


@dataclass
class PriceSnapshot:
    """A single hourly price/quantity data point."""
    snapshot: str   # ISO 8601 UTC timestamp from Blizzard
    price_copper: int
    quantity: int

    @property
    def gold(self) -> float:
        return self.price_copper / 10_000


@dataclass
class DailySnapshot:
    """A single daily price/quantity data point."""
    day: str        # "YYYY-MM-DD"
    price_copper: int
    quantity: int

    @property
    def gold(self) -> float:
        return self.price_copper / 10_000


class UndermineClient:
    """Small wrapper around the Undermine Exchange API.

    Requires an API key obtained by signing in with Patreon at
    https://undermine.exchange/ and revealing it on
    https://undermine.exchange/api.html
    """

    def __init__(self, api_key: str | None = None, timeout: float = 15.0):
        self.api_key = api_key or os.environ.get("UNDERMINE_API_KEY")
        if not self.api_key:
            raise UndermineApiError(
                "No Undermine API key found. Set UNDERMINE_API_KEY in your .env file."
            )
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"ApiKey {self.api_key}",
                "Accept-Encoding": "gzip",
            }
        )

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{API_BASE}{path}"
        response = self._session.get(url, timeout=self.timeout)
        if response.status_code != 200:
            raise UndermineApiError(
                f"Undermine API request failed ({response.status_code}) for {path}: {response.text}"
            )
        return response.json()

    @staticmethod
    def _check_region(region: str) -> str:
        region = region.lower()
        if region not in VALID_REGIONS:
            raise ValueError(f"region must be one of {sorted(VALID_REGIONS)}, got {region!r}")
        return region

    # ------------------------------------------------------------------
    # Current price (now)
    # ------------------------------------------------------------------

    def commodity_now(self, region: str, item_id: int) -> PriceQuote:
        """Current region-wide price/quantity for a commodity (stackable) item."""
        region = self._check_region(region)
        data = self._get(f"/v1/region/{region}/commodities/{item_id}/now.json")
        result = data["result"]
        return PriceQuote(
            item_id=item_id,
            price_copper=result.get("price", 0),
            quantity=result.get("quantity", 0),
            scope="region",
            region=region,
            last_updated=result.get("lastUpdated"),
            last_seen=result.get("lastSeen"),
        )

    def item_now_on_realm(self, region: str, realm_slug: str, item_id: int) -> PriceQuote:
        """Current price/quantity for a non-commodity item on a specific realm."""
        region = self._check_region(region)
        data = self._get(f"/v1/realm/{region}/{realm_slug}/items/{item_id}/now.json")
        result = data["result"]
        return PriceQuote(
            item_id=item_id,
            price_copper=result.get("price", 0),
            quantity=result.get("quantity", 0),
            scope=realm_slug,
            region=region,
            last_updated=result.get("lastUpdated"),
            last_seen=result.get("lastSeen"),
        )

    def item_now_region(self, region: str, item_id: int) -> list[dict[str, Any]]:
        """Current price/quantity for a non-commodity item, broken out per realm group."""
        region = self._check_region(region)
        data = self._get(f"/v1/region/{region}/items/{item_id}/now.json")
        return data["result"]

    # ------------------------------------------------------------------
    # Hourly history (~14 days of hourly snapshots, free endpoint)
    # ------------------------------------------------------------------

    def item_hourly_on_realm(
        self, region: str, realm_slug: str, item_id: int
    ) -> list[PriceSnapshot]:
        """Hourly price/quantity history for a realm item (~14 days of data)."""
        region = self._check_region(region)
        data = self._get(f"/v1/realm/{region}/{realm_slug}/items/{item_id}/hourly.json")
        return [
            PriceSnapshot(
                snapshot=row["snapshot"],
                price_copper=row.get("price", 0),
                quantity=row.get("quantity", 0),
            )
            for row in data["result"].get("hourly", [])
        ]

    def commodity_hourly(self, region: str, item_id: int) -> list[PriceSnapshot]:
        """Hourly price/quantity history for a commodity item (~14 days of data)."""
        region = self._check_region(region)
        data = self._get(f"/v1/region/{region}/commodities/{item_id}/hourly.json")
        return [
            PriceSnapshot(
                snapshot=row["snapshot"],
                price_copper=row.get("price", 0),
                quantity=row.get("quantity", 0),
            )
            for row in data["result"].get("hourly", [])
        ]

    # ------------------------------------------------------------------
    # Daily history (all-time daily snapshots, free endpoint)
    # ------------------------------------------------------------------

    def item_daily_on_realm(
        self, region: str, realm_slug: str, item_id: int
    ) -> list[DailySnapshot]:
        """Daily price/quantity history for a realm item (all-time)."""
        region = self._check_region(region)
        data = self._get(f"/v1/realm/{region}/{realm_slug}/items/{item_id}/daily.json")
        return [
            DailySnapshot(
                day=row["day"],
                price_copper=row.get("price", 0),
                quantity=row.get("quantity", 0),
            )
            for row in data["result"].get("daily", [])
        ]

    def commodity_daily(self, region: str, item_id: int) -> list[DailySnapshot]:
        """Daily price/quantity history for a commodity item (all-time)."""
        region = self._check_region(region)
        data = self._get(f"/v1/region/{region}/commodities/{item_id}/daily.json")
        return [
            DailySnapshot(
                day=row["day"],
                price_copper=row.get("price", 0),
                quantity=row.get("quantity", 0),
            )
            for row in data["result"].get("daily", [])
        ]

    # ------------------------------------------------------------------
    # Static
    # ------------------------------------------------------------------

    def realms(self) -> list[dict[str, Any]]:
        """Static list of all supported regions/realms and their slugs."""
        data = self._get("/v1/static/realms.json")
        return data["result"]["realms"]
