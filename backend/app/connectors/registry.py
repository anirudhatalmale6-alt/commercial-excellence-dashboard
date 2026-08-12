"""
Connector registry + the sync orchestrator.

Order matters: CRM first (it defines accounts and users), then ERP (joins onto
accounts), then Datalake (joins onto users and accounts). If you add a source,
put it wherever its joins are satisfied.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

from ..models import Dataset, IntegrationHealth
from .base import Connector
from .crm import SalesforceCRMConnector
from .datalake import SnowflakeDatalakeConnector
from .erp import SAPERPConnector


def _mode(var: str) -> str:
    return os.getenv(var, "mock").lower()


def build_connectors() -> list[Connector]:
    return [
        SalesforceCRMConnector({"mode": _mode("CRM_MODE")}),
        SAPERPConnector({"mode": _mode("ERP_MODE")}),
        SnowflakeDatalakeConnector({"mode": _mode("DATALAKE_MODE")}),
    ]


class SyncEngine:
    """Runs every connector into one Dataset and keeps the last good result."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.connectors = build_connectors()
        self.dataset = Dataset()
        self.health: list[IntegrationHealth] = []
        self.last_sync_at: datetime | None = None
        self.sync_count = 0

    def sync(self) -> Dataset:
        ds = Dataset()
        health: list[IntegrationHealth] = []
        # Connectors are rebuilt per sync so per-run state (unjoined rows,
        # unmapped stages) does not accumulate across syncs.
        connectors = build_connectors()
        for c in connectors:
            health.append(c.fetch(ds))
        with self._lock:
            self.connectors = connectors
            self.dataset = ds
            self.health = health
            self.last_sync_at = datetime.now(timezone.utc)
            self.sync_count += 1
        return ds

    def current(self) -> Dataset:
        with self._lock:
            if not self.dataset.users:
                pass
            return self.dataset
