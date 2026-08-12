# Hand-off guide

For the internal developer who picks this up next. Read `README.md` first for what the
thing does; this file is about where to put your hands.

---

## 1. Get it running in five minutes

```bash
pip install -r backend/requirements.txt
sh tools/run.sh 8077
open http://localhost:8077/
```

Everything runs in mock mode by default, so there are no credentials to chase before you
can see the product. The mock world is seeded (`SEED = 20260812` in
`backend/app/mock/generator.py`), so the same numbers come back every run and your
screenshots stay comparable.

---

## 2. The map

Work outward from `backend/app/models.py`. That file is the contract between the
integration layer and everything else. If you understand it, the rest follows.

```
source system  →  connector.extract()   raw, vendor-shaped
               →  connector.normalize() → models.py            ← the contract
               →  services/scope.py     who is allowed to see what
               →  services/kpi.py       every number
               →  services/actions.py   every recommendation
               →  main.py               one JSON payload
               →  frontend/js/app.js    render
```

Nothing skips a step. There is no place where the UI reaches into a source system, and no
place where a connector computes a KPI.

---

## 3. The five jobs you are most likely to be given

### "Point it at our real Salesforce"
1. Create a connected app with the client-credentials flow.
2. Set `CRM_MODE=live` plus the `SFDC_*` env vars (see README).
3. Check `SOQL` in `connectors/crm.py` — field API names are the only thing that differs
   between orgs. `Region__c`, `Team__c`, `AnnualQuota__c` and `ERP_Customer_No__c` are
   custom fields and almost certainly named differently in yours.
4. Check `STAGE_MAP`. Any stage name you do not map is parked in `qualify` and reported on
   the status strip — look there after the first live sync.

### "Add a KPI"
`services/kpi.py` → `KpiEngine.headline()` → append a `Kpi(...)`. Set `sources=[...]` so
the tile can flag itself when one of its sources is degraded. The UI needs no change; it
renders whatever the list contains. Units supported: `currency`, `percent`, `number`, `days`.

### "Add a priority-action rule"
`services/actions.py` → write a function `rule_x(ctx) -> list[Action]` → add it to `RULES`.
The context gives you the whole normalized dataset, the viewer's scope, today's date, and a
`KpiEngine`. Set `impact` in euros — that is what the ranking is built on — plus an
`urgency` of `critical` / `high` / `medium`. Set `entity_type` + `entity_id` so the deduper
can collapse multiple reasons about the same deal into the single best one.

### "Change who sees what"
`services/scope.py`, and only there. `build_scope()` returns the set of user ids a request
is allowed to touch; every engine filters on it. Do not add a second filter anywhere else —
that is how permission bugs get born.

### "Make it faster / bigger"
Today `SyncEngine.sync()` pulls everything into memory on a 30-second timer. The seams for
scaling, in the order you will probably need them:
1. Incremental extraction — the SOQL/OData queries already have date predicates to narrow.
2. Persist `Dataset` to Postgres and have the engines query it instead of scanning lists.
3. Cache `/api/dashboard` per (user, focus) between syncs — the payload is fully derived,
   so it is safe to cache and drop on every sync event.

---

## 4. Things that will bite you if nobody tells you

- **Connector order is join order.** `registry.py` runs CRM → ERP → Datalake because ERP
  joins onto the accounts CRM produced. If you add a source, put it where its joins are
  satisfied.
- **Connectors are rebuilt on every sync** so per-run state (unjoined rows, unmapped stages)
  does not accumulate across syncs and give you a slowly rising false alarm.
- **Coaching and leaderboard colour compare against the *pace line*, not the month target.**
  On day 3 of the month everyone is below target; an alert that fires for everyone is an
  alert nobody reads.
- **Coverage is measured against the remaining quarter**, not the remaining month. Against a
  month it is a meaninglessly large number.
- **Credit notes are subtracted, not dropped** (`status == "credited"` → negative). If you
  ever see revenue that looks too high, check whether a new ERP status code is being mapped
  to `invoiced` by mistake in `STATUS_MAP`.
- **The frontend has no build step on purpose.** Plain ES modules, no bundler, no npm. If
  you introduce a framework later, the API surface it needs is already one endpoint.
- **Charts are hand-rolled SVG** in `frontend/js/charts.js` — about 200 lines, no library.
  Each takes a container and data, redraws on resize, and owns its own tooltip.

---

## 5. Before this touches real data

In priority order:

1. **Replace `auth.py`.** Swap `verify_credentials` for your OIDC code exchange and keep
   `current_user` as the single "who is this request?" seam. Set `APP_SECRET` from a secret
   store, and set `EXPOSE_DEMO_USERS=0`.
2. **Set `CORS_ORIGINS`** to the real front-end origin — it defaults to `*`.
3. **Serve over TLS**, behind nginx or your ingress. The SSE endpoint needs
   proxy buffering off (`X-Accel-Buffering: no` is already sent).
4. **Review field-level exposure.** `/api/dashboard` returns account names and deal values
   for everyone in scope; confirm that matches your data-access policy.
5. **Rate-limit `/api/auth/login`.** There is no lockout today.

---

## 6. Visual QA

```bash
python3 tools/shoot.py <output-dir> [email]
```
Logs in, walks the page, and writes viewport screenshots plus a dark-mode shot. It fails
loudly on console errors, so it doubles as a smoke test after a change.
