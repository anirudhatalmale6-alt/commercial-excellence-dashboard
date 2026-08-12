"""
CRM connector — Salesforce-shaped.

Mock mode serves records from app.mock.generator in Salesforce's native shape.
Live mode is wired for the Salesforce REST API (OAuth 2.0 client-credentials
flow + SOQL queries); fill in SFDC_* env vars and set CRM_MODE=live.

To point this at Dynamics/HubSpot instead: keep `normalize()` mostly as-is and
swap `_extract_live()` plus the STAGE_MAP. That is the only surface that knows
about the vendor.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from ..models import (
    Account,
    Dataset,
    Opportunity,
    OpportunityStage,
    Role,
    SourceSystem,
    User,
)
from .base import AuthError, Connector

# Vendor stage -> canonical stage. The one place funnel vocabulary is mapped.
STAGE_MAP: dict[str, OpportunityStage] = {
    "Qualification": OpportunityStage.QUALIFY,
    "Prospecting": OpportunityStage.QUALIFY,
    "Needs Analysis": OpportunityStage.DISCOVER,
    "Value Proposition": OpportunityStage.DISCOVER,
    "Proposal/Price Quote": OpportunityStage.PROPOSE,
    "Id. Decision Makers": OpportunityStage.PROPOSE,
    "Negotiation/Review": OpportunityStage.NEGOTIATE,
    "Closed Won": OpportunityStage.WON,
    "Closed Lost": OpportunityStage.LOST,
}

ROLE_MAP = {
    "Sales Manager": Role.MANAGER,
    "Regional Director": Role.MANAGER,
    "Sales Rep": Role.REP,
    "Account Executive": Role.REP,
}

SOQL = {
    "User": "SELECT Id,Name,Email,UserRole.Name,Region__c,Team__c,ManagerId,AnnualQuota__c FROM User WHERE IsActive=true",
    "Account": "SELECT Id,Name,Type,BillingCountryRegion,Industry,OwnerId,ERP_Customer_No__c FROM Account",
    "Opportunity": (
        "SELECT Id,AccountId,Account.Name,OwnerId,Name,StageName,Amount,CurrencyIsoCode,"
        "Probability,CreatedDate,CloseDate,LastActivityDate FROM Opportunity "
        "WHERE CreatedDate = LAST_N_DAYS:540"
    ),
}


class SalesforceCRMConnector(Connector):
    source = SourceSystem.CRM
    display_name = "CRM"
    vendor = "Salesforce"

    # ------------------------------------------------------------------ auth

    def authenticate(self) -> str:
        if self.mode == "mock":
            return "mock-crm-token"

        import httpx  # imported lazily so mock mode has no hard dependency

        domain = os.getenv("SFDC_DOMAIN")
        client_id = os.getenv("SFDC_CLIENT_ID")
        client_secret = os.getenv("SFDC_CLIENT_SECRET")
        if not all([domain, client_id, client_secret]):
            raise AuthError("SFDC_DOMAIN / SFDC_CLIENT_ID / SFDC_CLIENT_SECRET not set")

        resp = httpx.post(
            f"https://{domain}/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            raise AuthError(f"{resp.status_code} {resp.text[:200]}")
        body = resp.json()
        self.settings["instance_url"] = body["instance_url"]
        return body["access_token"]

    # --------------------------------------------------------------- extract

    def extract(self) -> dict[str, list[dict]]:
        if self.mode == "mock":
            from ..mock.generator import world

            return world().crm_payload()
        return self._extract_live()

    def _extract_live(self) -> dict[str, list[dict]]:
        import httpx

        base = self.settings["instance_url"]
        headers = {"Authorization": f"Bearer {self.token()}"}
        out: dict[str, list[dict]] = {}
        with httpx.Client(timeout=60) as client:
            for obj, query in SOQL.items():
                records: list[dict] = []
                url = f"{base}/services/data/v61.0/query/"
                params = {"q": query}
                while True:
                    r = client.get(url, headers=headers, params=params)
                    r.raise_for_status()
                    body = r.json()
                    records.extend(body["records"])
                    if body.get("done", True):
                        break
                    url = f"{base}{body['nextRecordsUrl']}"
                    params = None
                out[obj] = records
        return out

    # ------------------------------------------------------------- normalize

    def normalize(self, raw: dict[str, list[dict]], ds: Dataset) -> None:
        for r in raw.get("User", []):
            role_name = r.get("UserRole")
            if isinstance(role_name, dict):  # live SFDC returns a nested object
                role_name = role_name.get("Name")
            ds.users.append(
                User(
                    id=r["Id"],
                    name=r["Name"],
                    email=r.get("Email") or "",
                    role=ROLE_MAP.get(role_name or "", Role.REP),
                    team=r.get("Team__c") or "Unassigned",
                    region=r.get("Region__c") or "Unknown",
                    manager_id=r.get("ManagerId"),
                    quota_annual=float(r.get("AnnualQuota__c") or 0.0),
                )
            )

        for r in raw.get("Account", []):
            ds.accounts.append(
                Account(
                    id=r["Id"],
                    name=r["Name"],
                    segment=r.get("Type") or "SMB",
                    region=r.get("BillingCountryRegion") or "Unknown",
                    industry=r.get("Industry") or "Other",
                    owner_id=r["OwnerId"],
                    erp_customer_no=r.get("ERP_Customer_No__c"),
                )
            )

        for r in raw.get("Opportunity", []):
            account_name = r.get("AccountName")
            if not account_name and isinstance(r.get("Account"), dict):
                account_name = r["Account"].get("Name")
            stage = STAGE_MAP.get(r["StageName"])
            if stage is None:
                # Unknown vendor stage: park it in the earliest open stage
                # rather than dropping the record, and it shows up in the
                # unmapped-values report.
                stage = OpportunityStage.QUALIFY
                _UNMAPPED_STAGES.add(r["StageName"])

            prob = r.get("Probability")
            prob = float(prob) if prob is not None else 0.0
            if prob > 1:  # live SFDC gives 0-100
                prob = prob / 100.0

            ds.opportunities.append(
                Opportunity(
                    id=r["Id"],
                    account_id=r["AccountId"],
                    account_name=account_name or "Unknown account",
                    owner_id=r["OwnerId"],
                    name=r["Name"],
                    stage=stage,
                    amount=float(r.get("Amount") or 0.0),
                    currency=r.get("CurrencyIsoCode") or "EUR",
                    probability=prob,
                    created_at=_dt(r["CreatedDate"]),
                    close_date=_dt(r["CloseDate"]).date(),
                    last_activity_at=_dt(r["LastActivityDate"]) if r.get("LastActivityDate") else None,
                )
            )

    # ---------------------------------------------------------------- health

    def simulated_latency_ms(self) -> int:
        return 180 if self.mode == "mock" else 0

    def freshness_minutes(self) -> int:
        return 2

    def status_message(self) -> str:
        if _UNMAPPED_STAGES:
            return f"{len(_UNMAPPED_STAGES)} unmapped stage value(s)"
        return "synced"


_UNMAPPED_STAGES: set[str] = set()


def _dt(value: str) -> datetime:
    """Parse SFDC/ISO timestamps and plain dates into aware UTC datetimes."""
    v = value.replace("Z", "+00:00")
    if len(v) == 10:  # bare date
        v += "T00:00:00+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
