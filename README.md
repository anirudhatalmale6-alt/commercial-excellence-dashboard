# Commercial Excellence — Sales Performance Dashboard (MVP)

One web dashboard that consolidates **CRM + ERP + Datalake** into a single view for
managers and sales reps: live performance against target, an automatically refreshed
priority-action list per user, and an integration-status strip so nobody has to guess
whether the numbers can be trusted.

This is the MVP build: it runs end to end today against a realistic mock of all three
source systems, and the connectors are written so that pointing them at the real
Salesforce / SAP / Snowflake instances is a credentials-and-config change, not a rewrite.

---

## Quick start

```bash
cd backend
pip install -r requirements.txt
cd ..
sh tools/run.sh 8077          # starts uvicorn, writes logs/server.log + logs/server.pid
```

Open <http://localhost:8077/> → you are redirected to `/login`.
Pick any demo account from the dropdown. **Password for every demo account: `demo1234`.**

- Sign in as a **manager** (Marta Lindqvist / Rafael Moreno) to see the team view,
  the leaderboard, coaching actions, and the drill-down into any rep.
- Sign in as a **rep** to see the same layout scoped to their own book, with the
  team-only panels and coaching actions removed.

Stop the server with `kill $(cat logs/server.pid)`.

---

## What it does

**Real-time performance metrics.** Eight headline KPIs — revenue MTD, target attainment,
open pipeline, pipeline coverage, win rate, average won deal, gross margin, overdue
receivables. Each tile shows the value, progress against target, the change against the
comparable prior period, and *which source systems the number depends on*. If a source is
degraded, the chip for that source turns amber on every tile that uses it.

**Priority actions.** A rules engine scans the normalized data and emits ranked actions:
stalled deals, deals due inside the window that are still in an early stage, close dates
that have already slipped, pipeline-coverage gaps, overdue receivables, margin erosion,
churn-risk accounts, activity drop-off, and (managers only) reps who need coaching.
Every action carries the euros at stake, and the list is scored, deduped per entity, and
capped per category so a bad receivables month cannot bury every deal action.

**Integration status.** Per connector: healthy / degraded / down, record count, round-trip
latency, how far behind the data is, and a plain-English message (e.g. "3 customers not
matched to a CRM account"). Status is derived from the actual sync, not hard-coded.

**Role-based scope.** One interface, two audiences. A rep sees only their own records; a
manager sees themselves plus everyone reporting to them, recursively, and can drill into a
single rep. Scope is enforced server-side in one place (`services/scope.py`) — passing
another team's user id in the query string returns 403.

**Live refresh.** The backend syncs every 30 seconds and pushes a server-sent event; the
page refetches without a reload. There is also a manual **Refresh** button.

---

## Architecture

```
frontend/                 no build step — plain ES modules, hand-rolled SVG charts
  index.html  login.html
  css/app.css             design tokens, light + dark
  js/api.js               fetch wrapper + token storage + SSE subscription
  js/charts.js            revenue-vs-target, pipeline funnel, activity trend
  js/app.js               dashboard controller

backend/app/
  main.py                 FastAPI routes, background sync loop, static hosting
  auth.py                 HMAC-signed stateless tokens (swap for the IdP later)
  models.py               THE CANONICAL DATA MODEL — everything normalizes to this
  connectors/
    base.py               Connector contract: authenticate / extract / normalize
    crm.py                Salesforce (mock + live REST/SOQL)
    erp.py                SAP S/4HANA (mock + live OData)
    datalake.py           Snowflake (mock + live key-pair auth)
    registry.py           sync orchestrator; connector order = join order
  services/
    scope.py              role-based scoping, one rule in one place
    kpi.py                every number on the screen
    actions.py            the priority-action rules engine
  mock/generator.py       deterministic synthetic company, in each source's native shape

tools/run.sh              start / restart
tools/shoot.py            Playwright screenshots for visual QA
docs/HANDOFF.md           hand-off guide for your internal developers
```

### The one design decision that matters

Every connector maps its own payload onto `models.py` and **nothing downstream ever sees a
source-specific field name.** The KPI engine does not know that `Amount` is Salesforce and
`NETWR` is SAP; it only knows `Opportunity.amount` and `OrderLine.amount`. Swapping
Salesforce for Dynamics means rewriting one `extract()` and one stage map — the KPI engine,
the action rules, the API, and the UI are untouched.

Two rules keep the numbers defensible and are enforced in `services/kpi.py`:

1. **Revenue actuals come from the ERP only.** Credit notes are subtracted, never dropped.
2. **Pipeline comes from the CRM only**, open stages only.

Mixing the two is the most common reason a sales dashboard disagrees with finance.

### Join keys

| From | To | Key |
|---|---|---|
| CRM Account | ERP customer | `Account.ERP_Customer_No__c` ↔ `KNA1-KUNNR` |
| CRM User | Datalake targets/activity | `User.Id` ↔ `fct_sales_target.user_id` |
| CRM Account | Datalake health scores | `Account.Id` ↔ `dim_account_health.account_id` |

ERP customers that fail to join are counted and surfaced on the status strip. A silent join
failure is a silent wrong number, so it is never silent.

---

## Going live against the real systems

Every connector runs in `mock` or `live` mode, set per source. Nothing else changes.

```bash
export CRM_MODE=live
export SFDC_DOMAIN=yourorg.my.salesforce.com
export SFDC_CLIENT_ID=...            # connected app, client-credentials flow
export SFDC_CLIENT_SECRET=...

export ERP_MODE=live
export SAP_ODATA_BASE=https://sap.internal/sap/opu/odata/sap
export SAP_USER=...
export SAP_PASSWORD=...

export DATALAKE_MODE=live
export SNOWFLAKE_ACCOUNT=xy12345.eu-central-1
export SNOWFLAKE_USER=CE_DASHBOARD
export SNOWFLAKE_PRIVATE_KEY_PATH=/etc/ce/snowflake.p8
export SNOWFLAKE_WAREHOUSE=COMMERCIAL_WH
export SNOWFLAKE_DATABASE=ANALYTICS

export APP_SECRET="$(openssl rand -hex 32)"   # MUST be set outside the test environment
export SYNC_INTERVAL_SECONDS=60
export EXPOSE_DEMO_USERS=0                    # hides the demo-account picker
```

Datalake queries expect three curated marts; the exact SQL is in
`connectors/datalake.py` and is the shortest thing to adjust to your warehouse:

- `analytics.commercial.fct_sales_target` — `user_id, period_month, target_amount, currency`
- `analytics.commercial.fct_activity_weekly` — `user_id, week_start_date, call_count, meeting_count, email_count, demo_count`
- `analytics.commercial.dim_account_health` — `account_id, churn_risk_score, expansion_score, days_since_last_order, scored_at`

`connectors/crm.py` `STAGE_MAP` maps your CRM's stage names onto the canonical funnel.
Unmapped stages are parked in the first stage and reported on the status strip rather than
being dropped.

---

## Tuning the action engine

All thresholds live in one dict at the top of `services/actions.py`:

```python
THRESHOLDS = {
    "stale_days_open_deal": 21,     # no activity before a deal counts as stalled
    "stale_days_late_stage": 10,    # tighter for propose/negotiate
    "closing_soon_days": 14,
    "coverage_floor": 3.0,          # pipeline cover on the remaining quarter
    "overdue_days": 45,
    "low_margin_pct": 0.20,
    "churn_risk": 0.65,
    "coaching_attainment": 0.70,    # measured against the pace line, not the month target
    "min_deal_amount": 5_000,
}
```

Adding a rule is one function plus one line in `RULES`. The UI needs no change — it renders
whatever the engine emits. A rule that throws is caught and reported in the list instead of
blanking it.

---

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/login` | email + password → bearer token |
| GET | `/api/auth/demo-users` | test-environment convenience; disable with `EXPOSE_DEMO_USERS=0` |
| GET | `/api/me` | viewer identity + who they may drill into |
| GET | `/api/dashboard?focus=<user_id>` | everything one screen needs, in one call |
| GET | `/api/actions?focus=&limit=` | priority actions only |
| GET | `/api/integrations` | connector health |
| POST | `/api/sync` | force a refresh |
| GET | `/api/stream` | SSE, one message per completed sync |
| GET | `/api/healthz` | liveness |

---

## Known MVP limits

Stated plainly so nothing is a surprise:

- Auth is HMAC-signed demo tokens with a shared demo password. It is the seam to replace
  with your IdP (`auth.py`), and it must be replaced before this holds real data.
- Data is held in memory and rebuilt on every sync. That is fine at this size and needs a
  cache/warehouse layer before you point it at a full production dataset.
- Currency is assumed to be EUR throughout; multi-currency FX conversion is a stub in the
  normalizer.
- Actions are read-only — there is no "mark done" / write-back to the CRM yet.
- No automated test suite yet; the engines were verified against the mock world and the UI
  against Playwright screenshots.
