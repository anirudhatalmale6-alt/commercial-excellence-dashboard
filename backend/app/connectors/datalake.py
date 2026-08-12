"""
Datalake connector — warehouse-shaped (Snowflake by default).

The Datalake supplies the things neither CRM nor ERP knows: quota/targets,
pre-aggregated activity, and model-scored account health. These are read-only
SELECTs against curated marts, never raw landing tables.

Live mode uses snowflake-connector-python with key-pair auth. Swapping to
BigQuery/Databricks means replacing `_extract_live()` and the QUERIES dict —
`normalize()` is unchanged as long as the marts keep the same column names.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone

from ..models import AccountHealth, ActivityRollup, Dataset, SourceSystem, Target
from .base import AuthError, Connector

QUERIES = {
    "fct_sales_target": """
        SELECT user_id, period_month, target_amount, currency
        FROM analytics.commercial.fct_sales_target
        WHERE period_month >= DATEADD(month, -14, CURRENT_DATE())
    """,
    "fct_activity_weekly": """
        SELECT user_id, week_start_date, call_count, meeting_count,
               email_count, demo_count
        FROM analytics.commercial.fct_activity_weekly
        WHERE week_start_date >= DATEADD(week, -16, CURRENT_DATE())
    """,
    "dim_account_health": """
        SELECT account_id, churn_risk_score, expansion_score,
               days_since_last_order, scored_at
        FROM analytics.commercial.dim_account_health
    """,
}


class SnowflakeDatalakeConnector(Connector):
    source = SourceSystem.DATALAKE
    display_name = "Datalake"
    vendor = "Snowflake"

    def authenticate(self) -> str:
        if self.mode == "mock":
            return "mock-datalake-token"

        account = os.getenv("SNOWFLAKE_ACCOUNT")
        user = os.getenv("SNOWFLAKE_USER")
        key_path = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH")
        if not all([account, user, key_path]):
            raise AuthError(
                "SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER / SNOWFLAKE_PRIVATE_KEY_PATH not set"
            )
        if not os.path.exists(key_path):
            raise AuthError(f"private key not found at {key_path}")
        return "keypair"

    def extract(self) -> dict[str, list[dict]]:
        if self.mode == "mock":
            from ..mock.generator import world

            return world().datalake_payload()
        return self._extract_live()

    def _extract_live(self) -> dict[str, list[dict]]:
        import snowflake.connector  # type: ignore
        from cryptography.hazmat.backends import default_backend  # type: ignore
        from cryptography.hazmat.primitives import serialization  # type: ignore

        with open(os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"], "rb") as fh:
            pkey = serialization.load_pem_private_key(
                fh.read(),
                password=(os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or "").encode() or None,
                backend=default_backend(),
            )
        pkb = pkey.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        conn = snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            private_key=pkb,
            warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "COMMERCIAL_WH"),
            database=os.getenv("SNOWFLAKE_DATABASE", "ANALYTICS"),
        )
        out: dict[str, list[dict]] = {}
        try:
            cur = conn.cursor(snowflake.connector.DictCursor)
            for name, sql in QUERIES.items():
                cur.execute(sql)
                out[name] = [{k.lower(): v for k, v in row.items()} for row in cur.fetchall()]
        finally:
            conn.close()
        return out

    def normalize(self, raw: dict[str, list[dict]], ds: Dataset) -> None:
        for r in raw.get("fct_sales_target", []):
            ds.targets.append(
                Target(
                    user_id=r["user_id"],
                    period=str(r["period_month"])[:7],
                    amount=float(r["target_amount"] or 0.0),
                    currency=r.get("currency") or "EUR",
                )
            )

        for r in raw.get("fct_activity_weekly", []):
            ds.activity.append(
                ActivityRollup(
                    user_id=r["user_id"],
                    week_start=_as_date(r["week_start_date"]),
                    calls=int(r.get("call_count") or 0),
                    meetings=int(r.get("meeting_count") or 0),
                    emails=int(r.get("email_count") or 0),
                    demos=int(r.get("demo_count") or 0),
                )
            )

        for r in raw.get("dim_account_health", []):
            ds.account_health.append(
                AccountHealth(
                    account_id=r["account_id"],
                    churn_risk=float(r.get("churn_risk_score") or 0.0),
                    expansion_score=float(r.get("expansion_score") or 0.0),
                    days_since_last_order=int(r.get("days_since_last_order") or 0),
                    last_scored_at=_as_dt(r.get("scored_at")),
                )
            )

    def simulated_latency_ms(self) -> int:
        return 620 if self.mode == "mock" else 0

    def freshness_minutes(self) -> int:
        return 240  # marts rebuild every 4h

    def status_message(self) -> str:
        return "marts rebuilt 4-hourly"


def _as_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def _as_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
