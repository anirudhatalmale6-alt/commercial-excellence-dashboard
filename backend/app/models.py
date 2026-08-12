"""
Canonical (normalized) data model.

Every connector — CRM, ERP, Datalake — maps its own payload into these shapes.
Nothing downstream (KPI engine, action engine, API, UI) ever sees a
source-specific field name. That is the whole point of this module: swapping
Salesforce for Dynamics means writing a new mapping in the connector, not
touching anything below it.

Field naming convention:
    *_id        canonical id, always "<source>:<native id>" so ids stay unique
                across systems
    amount      always in `currency`, already FX-converted to the reporting
                currency by the normalizer
    *_at        timezone-aware UTC datetimes
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SourceSystem(str, Enum):
    CRM = "crm"
    ERP = "erp"
    DATALAKE = "datalake"


class Role(str, Enum):
    REP = "rep"
    MANAGER = "manager"
    ADMIN = "admin"


class OpportunityStage(str, Enum):
    """Canonical funnel. Connectors map their own stage names onto these."""

    QUALIFY = "qualify"
    DISCOVER = "discover"
    PROPOSE = "propose"
    NEGOTIATE = "negotiate"
    WON = "won"
    LOST = "lost"


OPEN_STAGES = {
    OpportunityStage.QUALIFY,
    OpportunityStage.DISCOVER,
    OpportunityStage.PROPOSE,
    OpportunityStage.NEGOTIATE,
}

# Ordered for funnel rendering.
FUNNEL_ORDER = [
    OpportunityStage.QUALIFY,
    OpportunityStage.DISCOVER,
    OpportunityStage.PROPOSE,
    OpportunityStage.NEGOTIATE,
    OpportunityStage.WON,
]


class User(BaseModel):
    """A person who logs into the dashboard. Sourced from CRM user objects."""

    id: str
    name: str
    email: str
    role: Role
    team: str
    region: str
    manager_id: Optional[str] = None
    quota_annual: float = 0.0


class Account(BaseModel):
    id: str
    name: str
    segment: str  # Enterprise / Mid-Market / SMB
    region: str
    industry: str
    owner_id: str
    source: SourceSystem = SourceSystem.CRM
    # ERP-enriched
    erp_customer_no: Optional[str] = None
    credit_hold: bool = False


class Opportunity(BaseModel):
    id: str
    account_id: str
    account_name: str
    owner_id: str
    name: str
    stage: OpportunityStage
    amount: float
    currency: str = "EUR"
    probability: float = 0.0  # 0..1
    created_at: datetime
    close_date: date
    last_activity_at: Optional[datetime] = None
    source: SourceSystem = SourceSystem.CRM

    @property
    def is_open(self) -> bool:
        return self.stage in OPEN_STAGES


class OrderLine(BaseModel):
    """Invoiced/booked revenue out of the ERP. This is the 'actuals' truth."""

    id: str
    account_id: str
    account_name: str
    owner_id: str
    order_date: date
    amount: float
    cost: float
    currency: str = "EUR"
    product_family: str
    status: str  # invoiced / open / credited
    days_overdue: int = 0
    source: SourceSystem = SourceSystem.ERP

    @property
    def margin(self) -> float:
        return self.amount - self.cost

    @property
    def margin_pct(self) -> float:
        return 0.0 if self.amount == 0 else (self.amount - self.cost) / self.amount


class Target(BaseModel):
    """Quota per user per period. Lives in the Datalake in most orgs."""

    user_id: str
    period: str  # "2026-08" or "2026-Q3"
    amount: float
    currency: str = "EUR"
    source: SourceSystem = SourceSystem.DATALAKE


class ActivityRollup(BaseModel):
    """Weekly activity counters, pre-aggregated in the Datalake."""

    user_id: str
    week_start: date
    calls: int = 0
    meetings: int = 0
    emails: int = 0
    demos: int = 0
    source: SourceSystem = SourceSystem.DATALAKE


class AccountHealth(BaseModel):
    """Datalake-computed churn/expansion scoring per account."""

    account_id: str
    churn_risk: float  # 0..1
    expansion_score: float  # 0..1
    days_since_last_order: int
    last_scored_at: datetime
    source: SourceSystem = SourceSystem.DATALAKE


class Dataset(BaseModel):
    """Everything the engines need, normalized, in one object."""

    users: list[User] = Field(default_factory=list)
    accounts: list[Account] = Field(default_factory=list)
    opportunities: list[Opportunity] = Field(default_factory=list)
    orders: list[OrderLine] = Field(default_factory=list)
    targets: list[Target] = Field(default_factory=list)
    activity: list[ActivityRollup] = Field(default_factory=list)
    account_health: list[AccountHealth] = Field(default_factory=list)


class ConnectorStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class IntegrationHealth(BaseModel):
    """What the 'can I trust this data?' strip on the dashboard renders."""

    source: SourceSystem
    display_name: str
    vendor: str
    status: ConnectorStatus
    last_sync_at: Optional[datetime] = None
    latency_ms: int = 0
    records: int = 0
    freshness_minutes: int = 0
    message: str = ""
    mode: str = "mock"  # mock | live
