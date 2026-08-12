"""
Priority action engine.

Rules are declarative and independent: each one scans the normalized dataset for
its own condition and emits Action objects with an impact value and an urgency
weight. The engine then scores, sorts, dedupes per entity, and returns the top N
for whoever is looking.

score = impact_normalised * urgency_weight * recency_multiplier

Rules are registered in RULES at the bottom. Adding a rule is one function; the
UI needs no change. Manager-only rules are marked so a rep never sees "coach
your rep" items.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from ..models import Dataset, OpportunityStage, Role
from .kpi import KpiEngine, month_bounds, month_key
from .scope import Scope


@dataclass
class Action:
    id: str
    title: str
    detail: str
    category: str  # deal | pipeline | cash | account | coaching
    urgency: str  # critical | high | medium
    impact: float  # euros at stake
    score: float = 0.0
    entity_type: str = ""
    entity_id: str = ""
    owner_id: str = ""
    owner_name: str = ""
    due_in_days: int | None = None
    sources: list[str] = field(default_factory=list)


URGENCY_WEIGHT = {"critical": 3.0, "high": 2.0, "medium": 1.0}

# Tunables, kept together so the client can adjust the engine without hunting
# through the rules.
THRESHOLDS = {
    "stale_days_open_deal": 21,
    "stale_days_late_stage": 10,
    "closing_soon_days": 14,
    "coverage_floor": 3.0,
    "overdue_days": 45,
    "low_margin_pct": 0.20,
    "churn_risk": 0.65,
    "coaching_attainment": 0.70,
    "min_deal_amount": 5_000,
}

Rule = Callable[["ActionContext"], list[Action]]


@dataclass
class ActionContext:
    ds: Dataset
    scope: Scope
    today: date
    kpi: KpiEngine
    owner_names: dict[str, str]


# --------------------------------------------------------------------- rules


def rule_stalled_deals(ctx: ActionContext) -> list[Action]:
    """Open deals with no logged activity — the classic silent pipeline leak."""
    out: list[Action] = []
    for o in ctx.ds.opportunities:
        if not (ctx.scope.owns(o.owner_id) and o.is_open):
            continue
        if o.amount < THRESHOLDS["min_deal_amount"]:
            continue
        last = o.last_activity_at.date() if o.last_activity_at else o.created_at.date()
        idle = (ctx.today - last).days
        late_stage = o.stage in (OpportunityStage.PROPOSE, OpportunityStage.NEGOTIATE)
        limit = THRESHOLDS["stale_days_late_stage"] if late_stage else THRESHOLDS["stale_days_open_deal"]
        if idle < limit:
            continue
        out.append(
            Action(
                id=f"stall:{o.id}",
                title=f"Re-engage {o.name}",
                detail=(
                    f"{_money(o.amount)} in {o.stage.value} with no activity for {idle} days. "
                    f"Close date {o.close_date.isoformat()}."
                ),
                category="deal",
                urgency="critical" if late_stage and idle > 20 else "high",
                impact=o.amount * max(o.probability, 0.15),
                entity_type="opportunity",
                entity_id=o.id,
                owner_id=o.owner_id,
                owner_name=ctx.owner_names.get(o.owner_id, ""),
                due_in_days=(o.close_date - ctx.today).days,
                sources=["crm"],
            )
        )
    return out


def rule_closing_this_period(ctx: ActionContext) -> list[Action]:
    """Deals due inside the window that are not yet in a closing stage."""
    out: list[Action] = []
    horizon = ctx.today + timedelta(days=THRESHOLDS["closing_soon_days"])
    for o in ctx.ds.opportunities:
        if not (ctx.scope.owns(o.owner_id) and o.is_open):
            continue
        if not (ctx.today <= o.close_date <= horizon):
            continue
        if o.stage in (OpportunityStage.NEGOTIATE,) and o.probability >= 0.6:
            continue  # already where it should be
        days = (o.close_date - ctx.today).days
        out.append(
            Action(
                id=f"closing:{o.id}",
                title=f"Advance {o.name}",
                detail=(
                    f"Due in {days} day{'s' if days != 1 else ''} but still in "
                    f"{o.stage.value} at {o.probability*100:.0f}%. Confirm the date or move the stage."
                ),
                category="deal",
                urgency="critical" if days <= 5 else "high",
                impact=o.amount * o.probability,
                entity_type="opportunity",
                entity_id=o.id,
                owner_id=o.owner_id,
                owner_name=ctx.owner_names.get(o.owner_id, ""),
                due_in_days=days,
                sources=["crm"],
            )
        )
    return out


def rule_slipped_deals(ctx: ActionContext) -> list[Action]:
    """Open deals whose close date is already in the past."""
    out: list[Action] = []
    for o in ctx.ds.opportunities:
        if not (ctx.scope.owns(o.owner_id) and o.is_open and o.close_date < ctx.today):
            continue
        overdue = (ctx.today - o.close_date).days
        out.append(
            Action(
                id=f"slip:{o.id}",
                title=f"Re-date or close {o.name}",
                detail=f"Close date passed {overdue} days ago and the deal is still open at {_money(o.amount)}.",
                category="pipeline",
                urgency="high" if overdue > 14 else "medium",
                impact=o.amount * o.probability,
                entity_type="opportunity",
                entity_id=o.id,
                owner_id=o.owner_id,
                owner_name=ctx.owner_names.get(o.owner_id, ""),
                due_in_days=-overdue,
                sources=["crm"],
            )
        )
    return out


def rule_coverage_gap(ctx: ActionContext) -> list[Action]:
    """Not enough pipeline to make the remaining quarter number."""
    target, revenue = ctx.kpi.quarter_progress(ctx.scope)
    if target <= 0:
        return []
    remaining = target - revenue
    if remaining <= 0:
        return []
    pipeline = sum(o.amount for o in ctx.kpi.open_opps(ctx.scope))
    coverage = pipeline / remaining
    if coverage >= THRESHOLDS["coverage_floor"]:
        return []
    needed = remaining * THRESHOLDS["coverage_floor"] - pipeline
    who = ctx.scope.label
    return [
        Action(
            id=f"coverage:{ctx.scope.label}",
            title="Build pipeline - coverage below 3x",
            detail=(
                f"{who} has {coverage:.1f}x cover on {_money(remaining)} still to close this quarter. "
                f"Roughly {_money(needed)} of new qualified pipeline closes the gap."
            ),
            category="pipeline",
            urgency="critical" if coverage < 2 else "high",
            impact=remaining,
            entity_type="scope",
            entity_id=ctx.scope.label,
            sources=["crm", "erp", "datalake"],
        )
    ]


def rule_overdue_receivables(ctx: ActionContext) -> list[Action]:
    """ERP-side: cash the org has earned but not collected."""
    by_account: dict[str, dict] = {}
    for o in ctx.ds.orders:
        if not (ctx.scope.owns(o.owner_id) and o.status == "open"):
            continue
        if o.days_overdue < THRESHOLDS["overdue_days"]:
            continue
        row = by_account.setdefault(
            o.account_id,
            {"name": o.account_name, "amount": 0.0, "max_days": 0, "owner": o.owner_id},
        )
        row["amount"] += o.amount
        row["max_days"] = max(row["max_days"], o.days_overdue)
    out = []
    for account_id, row in by_account.items():
        out.append(
            Action(
                id=f"ar:{account_id}",
                title=f"Chase payment - {row['name']}",
                detail=f"{_money(row['amount'])} unpaid, oldest {row['max_days']} days past due.",
                category="cash",
                urgency="critical" if row["max_days"] > 90 else "high",
                impact=row["amount"],
                entity_type="account",
                entity_id=account_id,
                owner_id=row["owner"],
                owner_name=ctx.owner_names.get(row["owner"], ""),
                sources=["erp"],
            )
        )
    return out


def rule_margin_erosion(ctx: ActionContext) -> list[Action]:
    """Accounts billing below the margin floor over the last 60 days."""
    cutoff = ctx.today - timedelta(days=60)
    agg: dict[str, dict] = {}
    for o in ctx.ds.orders:
        if not (ctx.scope.owns(o.owner_id) and o.order_date >= cutoff and o.status != "credited"):
            continue
        row = agg.setdefault(
            o.account_id, {"name": o.account_name, "rev": 0.0, "cost": 0.0, "owner": o.owner_id}
        )
        row["rev"] += o.amount
        row["cost"] += o.cost
    out = []
    for account_id, row in agg.items():
        if row["rev"] < 25_000:
            continue
        margin = (row["rev"] - row["cost"]) / row["rev"]
        if margin >= THRESHOLDS["low_margin_pct"]:
            continue
        uplift = row["rev"] * (0.30 - margin)
        out.append(
            Action(
                id=f"margin:{account_id}",
                title=f"Review pricing - {row['name']}",
                detail=(
                    f"Margin {margin*100:.0f}% on {_money(row['rev'])} billed in 60 days. "
                    f"Getting to 30% is worth about {_money(uplift)}."
                ),
                category="account",
                urgency="high" if margin < 0.12 else "medium",
                impact=uplift,
                entity_type="account",
                entity_id=account_id,
                owner_id=row["owner"],
                owner_name=ctx.owner_names.get(row["owner"], ""),
                sources=["erp"],
            )
        )
    return out


def rule_churn_risk(ctx: ActionContext) -> list[Action]:
    """Datalake-scored accounts going quiet."""
    accounts = {a.id: a for a in ctx.ds.accounts}
    rev_12m: dict[str, float] = {}
    cutoff = ctx.today - timedelta(days=365)
    for o in ctx.ds.orders:
        if o.order_date >= cutoff and o.status != "credited":
            rev_12m[o.account_id] = rev_12m.get(o.account_id, 0.0) + o.amount

    out = []
    for h in ctx.ds.account_health:
        acc = accounts.get(h.account_id)
        if acc is None or not ctx.scope.owns(acc.owner_id):
            continue
        if h.churn_risk < THRESHOLDS["churn_risk"]:
            continue
        annual = rev_12m.get(acc.id, 0.0)
        if annual < 20_000:
            continue
        out.append(
            Action(
                id=f"churn:{acc.id}",
                title=f"Reactivate {acc.name}",
                detail=(
                    f"Churn risk {h.churn_risk*100:.0f}%, no order in {h.days_since_last_order} days, "
                    f"{_money(annual)} billed over the last year."
                ),
                category="account",
                urgency="high" if h.churn_risk > 0.8 else "medium",
                impact=annual * 0.5,
                entity_type="account",
                entity_id=acc.id,
                owner_id=acc.owner_id,
                owner_name=ctx.owner_names.get(acc.owner_id, ""),
                sources=["datalake", "erp"],
            )
        )
    return out


def rule_coaching(ctx: ActionContext) -> list[Action]:
    """Manager-only: reps who will miss without an intervention."""
    if ctx.scope.viewer.role == Role.REP or not ctx.scope.is_team_view:
        return []
    board = ctx.kpi.leaderboard(ctx.scope)["rows"]
    m_start, m_end = month_bounds(ctx.today)
    days_left = (m_end - ctx.today).days
    days_in_month = (m_end - m_start).days + 1
    day_of_month = (ctx.today - m_start).days + 1
    # Compare against the pace line, not the full-month target. On day 5 of the
    # month every rep is "below target" and an alert that fires for everyone is
    # an alert nobody reads.
    pace_fraction = day_of_month / days_in_month
    out = []
    for row in board:
        if row["user_id"] == ctx.scope.viewer.id:
            continue
        pace_target = row["target"] * pace_fraction
        if pace_target <= 0:
            continue
        vs_pace = row["revenue"] / pace_target
        if vs_pace >= THRESHOLDS["coaching_attainment"]:
            continue
        gap = row["target"] - row["revenue"]
        if gap <= 0:
            continue
        out.append(
            Action(
                id=f"coach:{row['user_id']}",
                title=f"Coach {row['name']}",
                detail=(
                    f"At {vs_pace*100:.0f}% of the day-{day_of_month} pace line with {days_left} days left - "
                    f"{_money(gap)} still to find. Review their top open deals together."
                ),
                category="coaching",
                urgency="critical" if vs_pace < 0.4 and days_left < 10 else "high",
                impact=gap,
                entity_type="user",
                entity_id=row["user_id"],
                owner_id=row["user_id"],
                owner_name=row["name"],
                due_in_days=days_left,
                sources=["erp", "datalake"],
            )
        )
    return out


def rule_activity_drop(ctx: ActionContext) -> list[Action]:
    """Datalake activity rollups: leading indicator, before revenue moves."""
    if not ctx.scope.user_ids:
        return []
    recent_start = ctx.today - timedelta(days=14)
    base_start = ctx.today - timedelta(days=56)
    out = []
    for uid in sorted(ctx.scope.user_ids):
        rows = [a for a in ctx.ds.activity if a.user_id == uid]
        if not rows:
            continue
        recent = [a for a in rows if a.week_start >= recent_start]
        base = [a for a in rows if base_start <= a.week_start < recent_start]
        if len(recent) < 1 or len(base) < 2:
            continue
        r = sum(a.meetings + a.demos for a in recent) / len(recent)
        b = sum(a.meetings + a.demos for a in base) / len(base)
        if b == 0 or r >= b * 0.7:
            continue
        name = ctx.owner_names.get(uid, "You")
        subject = "Your" if uid == ctx.scope.viewer.id else f"{name}'s"
        out.append(
            Action(
                id=f"activity:{uid}",
                title=f"{subject} customer meetings are down {(1-r/b)*100:.0f}%",
                detail=f"{r:.1f} meetings+demos per week vs {b:.1f} over the prior six weeks.",
                category="coaching" if uid != ctx.scope.viewer.id else "pipeline",
                urgency="medium",
                impact=ctx.kpi.target_for(ctx.scope, month_key(ctx.today)) * 0.1,
                entity_type="user",
                entity_id=uid,
                owner_id=uid,
                owner_name=name,
                sources=["datalake"],
            )
        )
    return out


RULES: list[Rule] = [
    rule_stalled_deals,
    rule_closing_this_period,
    rule_slipped_deals,
    rule_coverage_gap,
    rule_overdue_receivables,
    rule_margin_erosion,
    rule_churn_risk,
    rule_coaching,
    rule_activity_drop,
]


# ------------------------------------------------------------------- engine


class ActionEngine:
    def __init__(self, ds: Dataset, today: date) -> None:
        self.ds = ds
        self.today = today
        self.kpi = KpiEngine(ds, today)
        self.owner_names = {u.id: u.name for u in ds.users}

    def build(self, scope: Scope, limit: int = 12) -> list[Action]:
        ctx = ActionContext(
            ds=self.ds, scope=scope, today=self.today, kpi=self.kpi, owner_names=self.owner_names
        )
        actions: list[Action] = []
        for rule in RULES:
            try:
                actions.extend(rule(ctx))
            except Exception as exc:  # a broken rule must not blank the whole list
                actions.append(
                    Action(
                        id=f"error:{rule.__name__}",
                        title=f"Rule '{rule.__name__}' failed",
                        detail=str(exc),
                        category="deal",
                        urgency="medium",
                        impact=0.0,
                    )
                )

        if not actions:
            return []

        max_impact = max(a.impact for a in actions) or 1.0
        for a in actions:
            recency = 1.0
            if a.due_in_days is not None:
                if a.due_in_days < 0:
                    recency = 1.4
                elif a.due_in_days <= 7:
                    recency = 1.3
                elif a.due_in_days <= 21:
                    recency = 1.1
            a.score = round((a.impact / max_impact) * URGENCY_WEIGHT[a.urgency] * recency, 4)

        # One action per entity: the highest-scoring reason wins, so a rep is
        # never told three things about the same deal.
        best: dict[str, Action] = {}
        for a in sorted(actions, key=lambda x: x.score, reverse=True):
            key = f"{a.entity_type}:{a.entity_id}" if a.entity_id else a.id
            if key not in best:
                best[key] = a

        ranked = sorted(best.values(), key=lambda x: x.score, reverse=True)

        # Cap how much of the list any one category can take. Without this a
        # bad receivables month buries every deal action, and the list stops
        # being a to-do list and becomes a debtors report.
        per_category = max(3, limit // 3)
        counts: dict[str, int] = {}
        picked: list[Action] = []
        overflow: list[Action] = []
        for a in ranked:
            if counts.get(a.category, 0) < per_category:
                counts[a.category] = counts.get(a.category, 0) + 1
                picked.append(a)
            else:
                overflow.append(a)
        picked.extend(overflow)  # backfill if the caps left the list short
        return picked[:limit]


def _money(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"EUR {v/1_000_000:.2f}M"
    if a >= 1_000:
        return f"EUR {v/1_000:.0f}k"
    return f"EUR {v:.0f}"
