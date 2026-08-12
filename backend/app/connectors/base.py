"""
Connector contract.

Every source system gets one connector class. A connector does exactly three
things and nothing else:

    authenticate()  -> obtain/refresh a token or session
    extract()       -> pull raw records in the source's own shape
    normalize()     -> map those raw records onto app.models

`fetch()` runs the three in order, times it, and records health. Business logic
never lives here — if you find yourself computing a KPI inside a connector,
it belongs in services/ instead.

Adding a new source system = subclass Connector, implement the three methods,
register it in registry.py. Nothing downstream changes.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from ..models import ConnectorStatus, Dataset, IntegrationHealth, SourceSystem


class ConnectorError(RuntimeError):
    pass


class AuthError(ConnectorError):
    pass


class Connector(ABC):
    source: SourceSystem
    display_name: str
    vendor: str

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}
        self.mode: str = self.settings.get("mode", "mock")
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._health = IntegrationHealth(
            source=self.source,
            display_name=self.display_name,
            vendor=self.vendor,
            status=ConnectorStatus.DOWN,
            message="never synced",
            mode=self.mode,
        )

    # ------------------------------------------------------------- contract

    @abstractmethod
    def authenticate(self) -> str:
        """Return a valid access token. Cache it; respect expiry."""

    @abstractmethod
    def extract(self) -> dict[str, list[dict]]:
        """Pull raw records, keyed by source object/table name."""

    @abstractmethod
    def normalize(self, raw: dict[str, list[dict]], ds: Dataset) -> None:
        """Map raw records onto the canonical Dataset (mutates it in place)."""

    # ---------------------------------------------------------------- shared

    def token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        self._token = self.authenticate()
        self._token_expires_at = time.time() + float(self.settings.get("token_ttl", 3600))
        return self._token

    def fetch(self, ds: Dataset) -> IntegrationHealth:
        started = time.perf_counter()
        try:
            self.token()
            raw = self.extract()
            count = sum(len(v) for v in raw.values())
            self.normalize(raw, ds)
            elapsed = int((time.perf_counter() - started) * 1000) + self.simulated_latency_ms()
            self._health = IntegrationHealth(
                source=self.source,
                display_name=self.display_name,
                vendor=self.vendor,
                status=self.derive_status(elapsed),
                last_sync_at=datetime.now(timezone.utc),
                latency_ms=elapsed,
                records=count,
                freshness_minutes=self.freshness_minutes(),
                message=self.status_message(),
                mode=self.mode,
            )
        except AuthError as exc:
            self._health = IntegrationHealth(
                source=self.source,
                display_name=self.display_name,
                vendor=self.vendor,
                status=ConnectorStatus.DOWN,
                latency_ms=int((time.perf_counter() - started) * 1000),
                message=f"authentication failed: {exc}",
                mode=self.mode,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the status strip
            self._health = IntegrationHealth(
                source=self.source,
                display_name=self.display_name,
                vendor=self.vendor,
                status=ConnectorStatus.DOWN,
                latency_ms=int((time.perf_counter() - started) * 1000),
                message=f"{type(exc).__name__}: {exc}",
                mode=self.mode,
            )
        return self._health

    # ------------------------------------------------------- health details
    # Overridable so a connector can report vendor-specific nuance.

    def simulated_latency_ms(self) -> int:
        return 0

    def freshness_minutes(self) -> int:
        return 0

    def derive_status(self, elapsed_ms: int) -> ConnectorStatus:
        threshold = int(self.settings.get("degraded_latency_ms", 2500))
        return ConnectorStatus.DEGRADED if elapsed_ms > threshold else ConnectorStatus.OK

    def status_message(self) -> str:
        return "synced"

    @property
    def health(self) -> IntegrationHealth:
        return self._health
