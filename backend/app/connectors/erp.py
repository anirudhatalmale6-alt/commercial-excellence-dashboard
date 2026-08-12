"""
ERP connector — SAP-shaped (OData / RFC-style table names).

The ERP is the source of truth for *actuals*: booked and invoiced revenue,
cost, and receivables ageing. The CRM is never used for revenue in this model —
that is the single most common cause of "the dashboard disagrees with finance".

Join key: ERP customer number (KNA1-KUNNR) <-> CRM Account.ERP_Customer_No__c.
Accounts that fail to join are reported in `unjoined()` and surface on the
integration-status strip, because a silent join failure is a silent wrong number.
"""

from __future__ import annotations

import os
from datetime import datetime

from ..models import ConnectorStatus, Dataset, OrderLine, SourceSystem
from .base import AuthError, Connector

STATUS_MAP = {"C": "invoiced", "A": "open", "B": "open", "R": "credited"}


class SAPERPConnector(Connector):
    source = SourceSystem.ERP
    display_name = "ERP"
    vendor = "SAP S/4HANA"

    def __init__(self, settings=None) -> None:
        super().__init__(settings)
        self._unjoined: list[str] = []

    # ------------------------------------------------------------------ auth

    def authenticate(self) -> str:
        if self.mode == "mock":
            return "mock-erp-token"

        import base64

        user = os.getenv("SAP_USER")
        password = os.getenv("SAP_PASSWORD")
        if not (user and password):
            raise AuthError("SAP_USER / SAP_PASSWORD not set")
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    # --------------------------------------------------------------- extract

    def extract(self) -> dict[str, list[dict]]:
        if self.mode == "mock":
            from ..mock.generator import world

            return world().erp_payload()
        return self._extract_live()

    def _extract_live(self) -> dict[str, list[dict]]:
        import httpx

        base = os.getenv("SAP_ODATA_BASE")
        if not base:
            raise AuthError("SAP_ODATA_BASE not set")
        headers = {"Authorization": self.token(), "Accept": "application/json"}
        out: dict[str, list[dict]] = {}
        with httpx.Client(timeout=90) as client:
            for entity, path in (
                ("KNA1", "/API_BUSINESS_PARTNER/A_Customer"),
                ("VBAK", "/API_SALES_ORDER_SRV/A_SalesOrder"),
            ):
                records: list[dict] = []
                url = f"{base}{path}?$top=1000"
                while url:
                    r = client.get(url, headers=headers)
                    r.raise_for_status()
                    body = r.json()
                    d = body.get("d", body)
                    records.extend(d.get("results", d.get("value", [])))
                    url = d.get("__next") or body.get("@odata.nextLink")
                out[entity] = records
        return out

    # ------------------------------------------------------------- normalize

    def normalize(self, raw: dict[str, list[dict]], ds: Dataset) -> None:
        # CRM must run first — we join onto the accounts it produced.
        by_erp_no = {a.erp_customer_no: a for a in ds.accounts if a.erp_customer_no}

        for r in raw.get("KNA1", []):
            acc = by_erp_no.get(r.get("KUNNR"))
            if acc is None:
                self._unjoined.append(r.get("KUNNR", "?"))
                continue
            acc.credit_hold = bool(r.get("CREDIT_HOLD"))

        for r in raw.get("VBAK", []):
            acc = by_erp_no.get(r.get("KUNNR"))
            if acc is None:
                continue
            ds.orders.append(
                OrderLine(
                    id=f"erp:{r['VBELN']}",
                    account_id=acc.id,
                    account_name=acc.name,
                    owner_id=acc.owner_id,
                    order_date=_sap_date(r["ERDAT"]),
                    amount=float(r.get("NETWR") or 0.0),
                    cost=float(r.get("COST") or 0.0),
                    currency=r.get("WAERK") or "EUR",
                    product_family=r.get("SPART") or "Unclassified",
                    status=STATUS_MAP.get(r.get("GBSTK", "C"), "open"),
                    days_overdue=int(r.get("OVERDUE_DAYS") or 0),
                )
            )

    # ---------------------------------------------------------------- health

    def simulated_latency_ms(self) -> int:
        # The ERP is the slow one in nearly every real deployment; the mock
        # reflects that so the status strip has something honest to show.
        return 1450 if self.mode == "mock" else 0

    def freshness_minutes(self) -> int:
        return 35

    def derive_status(self, elapsed_ms: int) -> ConnectorStatus:
        if self._unjoined:
            return ConnectorStatus.DEGRADED
        return super().derive_status(elapsed_ms)

    def status_message(self) -> str:
        if self._unjoined:
            return f"{len(self._unjoined)} customer(s) not matched to a CRM account"
        return "nightly batch + hourly delta"

    def unjoined(self) -> list[str]:
        return list(self._unjoined)


def _sap_date(value: str) -> "datetime.date":
    return datetime.strptime(str(value)[:8], "%Y%m%d").date()
