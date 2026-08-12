"""
KPI engine.

Every number the dashboard shows is computed here, from the normalized Dataset,
scoped to a set of user ids. Two rules that keep the numbers defensible:

  1. Revenue actuals come from the ERP only. Credited lines are subtracted;
     they are never silently dropped.
  2. Pipeline comes from the CRM only, open stages only.

Each KPI carries its own `sources` list so the UI can tell a user exactly which
systems a number depends on — that is what makes the integration-status strip
actionable rather than decorative.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..models import (
    FUNNEL_ORDER,
    Dataset,
    OpportunityStage,
    OrderLine,
)
from .scope import Scope


@dataclass
class Kpi:
    key: str
    label: str
    value: float
    unit: str  # currency | percent | number | days
    target: float | None = None
    delta_pct: float | None = None  # vs previous comparable period
    hint: str = ""
    sources: list[str] = field(default_factory=list)
    status: str = "neutral"  # good | warning | critical | neutral


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_bounds(d: date) -> tuple[date, date]:
    last = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, 1), date(d.year, d.month, last)


def prev_month(d: date) -> date:
    first = date(d.year, d.month, 1)
    return first - timedelta(days=1)


def quarter_months(d: date) -> list[str]:
    """The three month keys of the calendar quarter containing `d`."""
    q_start_month = 3 * ((d.month - 1) // 3) + 1
    return [f"{d.year:04d}-{q_start_month + i:02d}" for i in range(3)]


def quarter_bounds(d: date) -> tuple[date, date]:
    q_start_month = 3 * ((d.month - 1) // 3) + 1
    start = date(d.year, q_start_month, 1)
    end_month = q_start_month + 2
    last = calendar.monthrange(d.year, end_month)[1]
    return start, date(d.year, end_month, last)


def _net(o: OrderLine) -> float:
    return -o.amount if o.status == "credited" else o.amount


def _net_cost(o: OrderLine) -> float:
    return -o.cost if o.status == "credited" else o.cost


class KpiEngine:
    def __init__(self, ds: Dataset, today: date) -> None:
        self.ds = ds
        self.today = today

    # --------------------------------------------------------------- slices

    def orders(self, scope: Scope, start: date, end: date) -> list[OrderLine]:
        return [
            o
            for o in self.ds.orders
            if scope.owns(o.owner_id) and start <= o.order_date <= end
        ]

    def open_opps(self, scope: Scope):
        return [o for o in self.ds.opportunities if scope.owns(o.owner_id) and o.is_open]

    def target_for(self, scope: Scope, period: str) -> float:
        return sum(t.amount for t in self.ds.targets if t.user_id in scope.user_ids and t.period == period)

    def quarter_progress(self, scope: Scope) -> tuple[float, float]:
        """(quarter target, quarter-to-date actual) for this scope."""
        months = set(quarter_months(self.today))
        target = sum(t.amount for t in self.ds.targets if t.user_id in scope.user_ids and t.period in months)
        q_start, _ = quarter_bounds(self.today)
        revenue = sum(_net(o) for o in self.orders(scope, q_start, self.today))
        return target, revenue

    # ------------------------------------------------------------- headline

    def headline(self, scope: Scope) -> list[Kpi]:
        m_start, m_end = month_bounds(self.today)
        p_ref = prev_month(self.today)
        p_start, p_end = month_bounds(p_ref)

        mtd = self.orders(scope, m_start, self.today)
        prev_same = self.orders(scope, p_start, min(p_end, p_start + (self.today - m_start)))
        prev_full = self.orders(scope, p_start, p_end)

        revenue = sum(_net(o) for o in mtd)
        revenue_prev = sum(_net(o) for o in prev_same)
        target = self.target_for(scope, month_key(self.today))

        days_in_month = (m_end - m_start).days + 1
        day_of_month = (self.today - m_start).days + 1
        pace_target = target * day_of_month / days_in_month if target else 0.0

        attainment = revenue / target if target else 0.0
        pace_ratio = revenue / pace_target if pace_target else 0.0

        open_opps = self.open_opps(scope)
        pipeline = sum(o.amount for o in open_opps)
        weighted = sum(o.amount * o.probability for o in open_opps)
        # Coverage is measured against the remaining QUARTER, which is how sales
        # ops reads it. Against a single month it is meaninglessly large.
        q_target, q_revenue = self.quarter_progress(scope)
        remaining = max(q_target - q_revenue, 0.0)
        coverage = pipeline / remaining if remaining > 0 else float("inf") if pipeline else 0.0

        cost = sum(_net_cost(o) for o in mtd)
        margin_pct = (revenue - cost) / revenue if revenue > 0 else 0.0
        margin_prev = (
            (sum(_net(o) for o in prev_same) - sum(_net_cost(o) for o in prev_same))
            / sum(_net(o) for o in prev_same)
            if sum(_net(o) for o in prev_same)
            else 0.0
        )

        won, lost = self._won_lost(scope, days=90)
        win_rate = len(won) / (len(won) + len(lost)) if (won or lost) else 0.0
        avg_deal = (sum(o.amount for o in won) / len(won)) if won else 0.0
        cycle = (
            sum((o.close_date - o.created_at.date()).days for o in won) / len(won) if won else 0.0
        )

        overdue = sum(
            o.amount
            for o in self.ds.orders
            if scope.owns(o.owner_id) and o.status == "open" and o.days_overdue > 0
        )
        overdue_90 = sum(
            o.amount
            for o in self.ds.orders
            if scope.owns(o.owner_id) and o.status == "open" and o.days_overdue > 90
        )

        return [
            Kpi(
                key="revenue_mtd",
                label="Revenue MTD",
                value=revenue,
                unit="currency",
                target=target,
                delta_pct=_pct_change(revenue, revenue_prev),
                hint=f"vs same point last month ({_short_money(revenue_prev)})",
                sources=["erp"],
                status=_status_from(pace_ratio, 0.95, 0.8),
            ),
            Kpi(
                key="attainment",
                label="Target attainment",
                value=attainment,
                unit="percent",
                target=1.0,
                hint=f"On-pace would be {pace_ratio*100:.0f}% of the day-{day_of_month} line",
                sources=["erp", "datalake"],
                status=_status_from(pace_ratio, 0.95, 0.8),
            ),
            Kpi(
                key="pipeline",
                label="Open pipeline",
                value=pipeline,
                unit="currency",
                hint=f"Weighted {_short_money(weighted)} · {len(open_opps)} open deals",
                sources=["crm"],
                status="neutral",
            ),
            Kpi(
                key="coverage",
                label="Pipeline coverage",
                value=coverage if coverage != float("inf") else 99.0,
                unit="number",
                target=3.0,
                hint=f"Against {_short_money(remaining)} still to close this quarter",
                sources=["crm", "erp", "datalake"],
                status=_status_from(coverage if coverage != float("inf") else 99, 3.0, 2.0),
            ),
            Kpi(
                key="win_rate",
                label="Win rate (90d)",
                value=win_rate,
                unit="percent",
                hint=f"{len(won)} won / {len(lost)} lost",
                sources=["crm"],
                status=_status_from(win_rate, 0.35, 0.25),
            ),
            Kpi(
                key="avg_deal",
                label="Avg won deal (90d)",
                value=avg_deal,
                unit="currency",
                hint=f"Avg cycle {cycle:.0f} days",
                sources=["crm"],
                status="neutral",
            ),
            Kpi(
                key="margin",
                label="Gross margin MTD",
                value=margin_pct,
                unit="percent",
                delta_pct=_pct_change(margin_pct, margin_prev),
                hint="ERP net revenue less cost of goods",
                sources=["erp"],
                status=_status_from(margin_pct, 0.32, 0.25) if revenue > 0 else "neutral",
            ),
            Kpi(
                key="overdue_ar",
                label="Overdue receivables",
                value=overdue,
                unit="currency",
                hint=f"{_short_money(overdue_90)} over 90 days",
                sources=["erp"],
                status="critical" if overdue_90 > 0 else ("warning" if overdue > 0 else "good"),
            ),
        ]

    def _won_lost(self, scope: Scope, days: int):
        cutoff = self.today - timedelta(days=days)
        won = [
            o
            for o in self.ds.opportunities
            if scope.owns(o.owner_id) and o.stage == OpportunityStage.WON and o.close_date >= cutoff
        ]
        lost = [
            o
            for o in self.ds.opportunities
            if scope.owns(o.owner_id) and o.stage == OpportunityStage.LOST and o.close_date >= cutoff
        ]
        return won, lost

    # --------------------------------------------------------------- charts

    def revenue_trend(self, scope: Scope, months: int = 6) -> dict:
        points = []
        ref = self.today
        keys: list[date] = []
        for _ in range(months):
            keys.append(ref)
            ref = prev_month(ref)
        for d in reversed(keys):
            start, end = month_bounds(d)
            end = min(end, self.today)
            actual = sum(_net(o) for o in self.orders(scope, start, end))
            target = self.target_for(scope, month_key(d))
            points.append(
                {
                    "period": month_key(d),
                    "label": calendar.month_abbr[d.month],
                    "actual": round(actual, 2),
                    "target": round(target, 2),
                    "partial": d.month == self.today.month and d.year == self.today.year,
                }
            )
        return {"points": points, "unit": "currency"}

    def funnel(self, scope: Scope) -> dict:
        opps = [o for o in self.ds.opportunities if scope.owns(o.owner_id)]
        out = []
        for stage in FUNNEL_ORDER:
            if stage == OpportunityStage.WON:
                sel = [
                    o
                    for o in opps
                    if o.stage == stage and o.close_date >= self.today - timedelta(days=90)
                ]
                label = "Won (90d)"
            else:
                sel = [o for o in opps if o.stage == stage]
                label = stage.value.capitalize()
            out.append(
                {
                    "stage": stage.value,
                    "label": label,
                    "count": len(sel),
                    "amount": round(sum(o.amount for o in sel), 2),
                }
            )
        return {"stages": out}

    def leaderboard(self, scope: Scope) -> dict:
        """Per-rep attainment. Only meaningful in a team view."""
        rows = []
        period = month_key(self.today)
        m_start, m_end = month_bounds(self.today)
        # Mid-month, everyone is "below target". What matters is whether they
        # are ahead of or behind the pace line, so the row carries both.
        pace_fraction = ((self.today - m_start).days + 1) / ((m_end - m_start).days + 1)
        for u in self.ds.users:
            if u.id not in scope.user_ids or u.quota_annual <= 0:
                continue
            rev = sum(
                _net(o)
                for o in self.ds.orders
                if o.owner_id == u.id and m_start <= o.order_date <= self.today
            )
            tgt = sum(t.amount for t in self.ds.targets if t.user_id == u.id and t.period == period)
            rows.append(
                {
                    "user_id": u.id,
                    "name": u.name,
                    "region": u.region,
                    "revenue": round(rev, 2),
                    "target": round(tgt, 2),
                    "attainment": round(rev / tgt, 4) if tgt else 0.0,
                    "vsPace": round(rev / (tgt * pace_fraction), 4) if tgt else 0.0,
                }
            )
        rows.sort(key=lambda r: r["attainment"], reverse=True)
        return {"rows": rows, "paceFraction": round(pace_fraction, 4)}

    def top_accounts(self, scope: Scope, limit: int = 6) -> dict:
        m_start = self.today - timedelta(days=90)
        agg: dict[str, dict] = {}
        for o in self.ds.orders:
            if not scope.owns(o.owner_id) or o.order_date < m_start:
                continue
            row = agg.setdefault(
                o.account_id, {"account_id": o.account_id, "name": o.account_name, "revenue": 0.0}
            )
            row["revenue"] += _net(o)
        rows = sorted(agg.values(), key=lambda r: r["revenue"], reverse=True)[:limit]
        for r in rows:
            r["revenue"] = round(r["revenue"], 2)
        return {"rows": rows}

    def activity(self, scope: Scope, weeks: int = 8) -> dict:
        cutoff = self.today - timedelta(weeks=weeks)
        buckets: dict[str, dict] = {}
        for a in self.ds.activity:
            if a.user_id not in scope.user_ids or a.week_start < cutoff:
                continue
            b = buckets.setdefault(
                a.week_start.isoformat(),
                {"week": a.week_start.isoformat(), "meetings": 0, "demos": 0, "calls": 0},
            )
            b["meetings"] += a.meetings
            b["demos"] += a.demos
            b["calls"] += a.calls
        return {"weeks": sorted(buckets.values(), key=lambda b: b["week"])}


def _pct_change(new: float, old: float) -> float | None:
    if not old:
        return None
    return (new - old) / abs(old)


def _status_from(value: float, good_at: float, warn_at: float) -> str:
    if value >= good_at:
        return "good"
    if value >= warn_at:
        return "warning"
    return "critical"


def _short_money(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"€{v/1_000_000:.1f}M"
    if a >= 1_000:
        return f"€{v/1_000:.0f}k"
    return f"€{v:.0f}"
