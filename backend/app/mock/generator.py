"""
Deterministic fake source data.

IMPORTANT: this module deliberately emits records in the *native shape of each
source system* — Salesforce-ish CamelCase for the CRM, SAP-ish uppercase column
names for the ERP, snake_case warehouse rows for the Datalake. The connectors
then map those onto `models.py`. That way the mapping layer is exercised for
real in mock mode, and switching a connector to live credentials does not
change a single line downstream.

Seeded so every run produces the same numbers — demos stay reproducible and
screenshots stay comparable.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

SEED = 20260812

REGIONS = ["EMEA", "NAM", "APAC"]
SEGMENTS = ["Enterprise", "Mid-Market", "SMB"]
INDUSTRIES = ["Manufacturing", "Retail", "Healthcare", "Logistics", "Energy", "Financial Services"]
PRODUCT_FAMILIES = ["Core Platform", "Analytics Add-on", "Services", "Support Plan", "Hardware"]

SF_STAGES = [
    "Qualification",
    "Needs Analysis",
    "Proposal/Price Quote",
    "Negotiation/Review",
    "Closed Won",
    "Closed Lost",
]

ACCOUNT_PREFIXES = [
    "Nordwind", "Ardenne", "Vitalis", "Kernstadt", "Baltica", "Solvent", "Praxis",
    "Larimar", "Halcyon", "Ferrovia", "Cobalt", "Meridian", "Alkmaar", "Tessera",
    "Ostara", "Vulcanis", "Cardinal", "Brightwell", "Lumen", "Steinmark", "Aurelia",
    "Novena", "Hartwig", "Calder", "Perrin", "Wexford", "Sable", "Ridgeway",
    "Kastellan", "Torvald", "Mirandola", "Quarto", "Vantage", "Brechin", "Oakhill",
]
ACCOUNT_SUFFIXES = ["Group", "Industries", "Holdings", "AG", "S.A.", "Logistics", "Retail NV", "Systems"]

FIRST_NAMES = [
    "Marta", "Tobias", "Ines", "Jonas", "Camille", "Rafael", "Anouk", "Diego",
    "Freya", "Malik", "Sofia", "Henrik", "Nadia", "Lucas", "Elena", "Bastien",
]
LAST_NAMES = [
    "Lindqvist", "Okonkwo", "Ferrer", "Haugen", "Dubois", "Moreno", "Visser",
    "Alvarez", "Nyberg", "Haddad", "Rossi", "Sorensen", "Benali", "Peeters",
    "Kovacs", "Marchand",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


class MockWorld:
    """Builds one coherent synthetic company and serves it per-source."""

    def __init__(self, seed: int = SEED, today: date | None = None) -> None:
        self.rng = random.Random(seed)
        self.today = today or _now().date()
        self._build()

    # ------------------------------------------------------------------ build

    def _build(self) -> None:
        rng = self.rng

        # --- people -------------------------------------------------------
        # 2 managers, 8 reps. Manager 1 owns EMEA, manager 2 owns NAM+APAC.
        self.users: list[dict] = []
        managers = [
            ("u-mgr-1", "Marta Lindqvist", "EMEA", "Team Europe"),
            ("u-mgr-2", "Rafael Moreno", "NAM", "Team Americas"),
        ]
        for uid, name, region, team in managers:
            self.users.append(
                {
                    "Id": uid,
                    "Name": name,
                    "Email": self._email(name),
                    "UserRole": "Sales Manager",
                    "Region__c": region,
                    "Team__c": team,
                    "ManagerId": None,
                    "AnnualQuota__c": 0.0,
                }
            )

        rep_defs = [
            ("u-rep-1", "u-mgr-1", "EMEA", "Team Europe"),
            ("u-rep-2", "u-mgr-1", "EMEA", "Team Europe"),
            ("u-rep-3", "u-mgr-1", "EMEA", "Team Europe"),
            ("u-rep-4", "u-mgr-1", "EMEA", "Team Europe"),
            ("u-rep-5", "u-mgr-2", "NAM", "Team Americas"),
            ("u-rep-6", "u-mgr-2", "NAM", "Team Americas"),
            ("u-rep-7", "u-mgr-2", "APAC", "Team Americas"),
            ("u-rep-8", "u-mgr-2", "APAC", "Team Americas"),
        ]
        used_names: set[str] = set()
        for uid, mgr, region, team in rep_defs:
            name = self._person(used_names)
            self.users.append(
                {
                    "Id": uid,
                    "Name": name,
                    "Email": self._email(name),
                    "UserRole": "Sales Rep",
                    "Region__c": region,
                    "Team__c": team,
                    "ManagerId": mgr,
                    "AnnualQuota__c": float(rng.choice([1_200_000, 1_400_000, 1_600_000, 1_800_000])),
                }
            )

        self.rep_ids = [u[0] for u in rep_defs]
        self.manager_ids = [m[0] for m in managers]

        # --- accounts -----------------------------------------------------
        self.accounts: list[dict] = []
        used_accounts: set[str] = set()
        for i in range(64):
            owner = self.rep_ids[i % len(self.rep_ids)]
            owner_region = next(u["Region__c"] for u in self.users if u["Id"] == owner)
            name = self._account_name(used_accounts)
            self.accounts.append(
                {
                    "Id": f"a-{i+1:03d}",
                    "Name": name,
                    "Type": rng.choice(SEGMENTS),
                    "BillingCountryRegion": owner_region,
                    "Industry": rng.choice(INDUSTRIES),
                    "OwnerId": owner,
                    "ERP_Customer_No__c": f"CUST{10_000 + i}",
                }
            )

        # --- opportunities -------------------------------------------------
        self.opportunities: list[dict] = []
        oid = 0
        for acc in self.accounts:
            for _ in range(rng.randint(1, 4)):
                oid += 1
                created = self.today - timedelta(days=rng.randint(5, 260))
                stage = rng.choices(
                    SF_STAGES,
                    weights=[18, 16, 14, 10, 26, 16],
                    k=1,
                )[0]
                base = {"Enterprise": 95_000, "Mid-Market": 42_000, "SMB": 16_000}[acc["Type"]]
                amount = round(base * rng.uniform(0.45, 1.9), -2)

                if stage in ("Closed Won", "Closed Lost"):
                    close = created + timedelta(days=rng.randint(20, 150))
                    if close > self.today:
                        close = self.today - timedelta(days=rng.randint(0, 20))
                else:
                    close = self.today + timedelta(days=rng.randint(-12, 95))

                # Activity recency: some deals are deliberately stale so the
                # priority-action engine has something real to find.
                stale_roll = rng.random()
                if stale_roll < 0.22:
                    last_act = self.today - timedelta(days=rng.randint(21, 70))
                elif stale_roll < 0.4:
                    last_act = self.today - timedelta(days=rng.randint(8, 20))
                else:
                    last_act = self.today - timedelta(days=rng.randint(0, 7))

                self.opportunities.append(
                    {
                        "Id": f"o-{oid:04d}",
                        "AccountId": acc["Id"],
                        "AccountName": acc["Name"],
                        "OwnerId": acc["OwnerId"],
                        "Name": f"{acc['Name']} - {rng.choice(PRODUCT_FAMILIES)}",
                        "StageName": stage,
                        "Amount": amount,
                        "CurrencyIsoCode": "EUR",
                        "Probability": self._probability(stage, rng),
                        "CreatedDate": datetime.combine(created, datetime.min.time(), timezone.utc).isoformat(),
                        "CloseDate": close.isoformat(),
                        "LastActivityDate": last_act.isoformat(),
                    }
                )

        # --- ERP order lines ------------------------------------------------
        self.orders: list[dict] = []
        lid = 0
        for acc in self.accounts:
            # Roughly a year of order history, denser for Enterprise.
            n = {"Enterprise": rng.randint(26, 42), "Mid-Market": rng.randint(14, 26), "SMB": rng.randint(5, 14)}[acc["Type"]]
            for _ in range(n):
                lid += 1
                # Flat across the window: month-to-date then lands at roughly
                # day-of-month/days-in-month of the monthly run rate, which is
                # what "on pace" should look like on day one of a demo.
                od = self.today - timedelta(days=rng.randint(0, 420))
                base = {"Enterprise": 12_000, "Mid-Market": 7_500, "SMB": 5_500}[acc["Type"]]
                net = round(base * rng.uniform(0.35, 1.6), -2)
                margin_pct = rng.uniform(0.09, 0.52)
                status = rng.choices(["invoiced", "open", "credited"], weights=[80, 18, 2], k=1)[0]
                if status == "credited":
                    # Credit notes are partial returns, not full reversals.
                    net = round(net * rng.uniform(0.1, 0.35), -2)
                overdue = 0
                if status == "open" and rng.random() < 0.45:
                    overdue = rng.randint(1, 120)
                self.orders.append(
                    {
                        "VBELN": f"{5_000_000 + lid}",
                        "KUNNR": acc["ERP_Customer_No__c"],
                        "ERDAT": od.strftime("%Y%m%d"),
                        "NETWR": net,
                        "COST": round(net * (1 - margin_pct), 2),
                        "WAERK": "EUR",
                        "SPART": rng.choice(PRODUCT_FAMILIES),
                        "GBSTK": {"invoiced": "C", "open": "A", "credited": "R"}[status],
                        "OVERDUE_DAYS": overdue,
                    }
                )

        # --- Datalake: targets, activity, health -----------------------------
        # Targets are derived from the generated actuals rather than invented
        # independently — otherwise attainment lands nowhere near 100% and the
        # whole dashboard reads as noise. Past months get a target that puts
        # attainment in a believable 80-125% band; the current (partial) month
        # gets a full-month target sized off the recent run rate.
        owner_by_erp = {a["ERP_Customer_No__c"]: a["OwnerId"] for a in self.accounts}
        rev: dict[tuple[str, str], float] = {}
        for o in self.orders:
            if o["GBSTK"] == "R":
                continue
            owner = owner_by_erp.get(o["KUNNR"])
            if not owner:
                continue
            m = f"{o['ERDAT'][:4]}-{o['ERDAT'][4:6]}"
            rev[(owner, m)] = rev.get((owner, m), 0.0) + o["NETWR"]

        self.targets: list[dict] = []
        # Forward months matter: quarter-to-go and coverage maths need targets
        # for months that have not happened yet.
        months = self._recent_months(14) + self._future_months(5)
        current_month = _month_key(self.today)
        for u in self.users:
            if u["UserRole"] != "Sales Rep":
                continue
            past = [m for m in months if m < current_month]
            history = [rev.get((u["Id"], m), 0.0) for m in past]
            run_rate = sum(history[-4:]) / max(len(history[-4:]), 1) or 100_000.0
            annual = 0.0
            for m in months:
                if m >= current_month:
                    amount = run_rate * rng.uniform(0.88, 1.12)
                else:
                    actual = rev.get((u["Id"], m), 0.0) or run_rate
                    amount = actual / rng.uniform(0.8, 1.25)
                amount = round(amount, -2)
                annual += amount
                self.targets.append(
                    {
                        "user_id": u["Id"],
                        "period_month": m,
                        "target_amount": amount,
                        "currency": "EUR",
                    }
                )
            # Keep the CRM quota field consistent with the Datalake targets.
            u["AnnualQuota__c"] = round(annual * 12 / len(months), -3)

        self.activity: list[dict] = []
        for u in self.users:
            if u["UserRole"] != "Sales Rep":
                continue
            for w in range(16):
                ws = self.today - timedelta(days=self.today.weekday() + 7 * w)
                self.activity.append(
                    {
                        "user_id": u["Id"],
                        "week_start_date": ws.isoformat(),
                        "call_count": rng.randint(4, 38),
                        "meeting_count": rng.randint(1, 12),
                        "email_count": rng.randint(15, 90),
                        "demo_count": rng.randint(0, 5),
                    }
                )

        self.account_health: list[dict] = []
        for acc in self.accounts:
            last_order = max(
                (o["ERDAT"] for o in self.orders if o["KUNNR"] == acc["ERP_Customer_No__c"]),
                default=None,
            )
            if last_order:
                d = datetime.strptime(last_order, "%Y%m%d").date()
                days_since = (self.today - d).days
            else:
                days_since = 999
            churn = min(1.0, max(0.02, days_since / 240 + rng.uniform(-0.12, 0.12)))
            self.account_health.append(
                {
                    "account_id": acc["Id"],
                    "churn_risk_score": round(churn, 3),
                    "expansion_score": round(rng.uniform(0.05, 0.95), 3),
                    "days_since_last_order": days_since,
                    "scored_at": _now().isoformat(),
                }
            )

    # ------------------------------------------------------------- helpers

    def _person(self, used: set[str]) -> str:
        while True:
            n = f"{self.rng.choice(FIRST_NAMES)} {self.rng.choice(LAST_NAMES)}"
            if n not in used:
                used.add(n)
                return n

    def _account_name(self, used: set[str]) -> str:
        while True:
            n = f"{self.rng.choice(ACCOUNT_PREFIXES)} {self.rng.choice(ACCOUNT_SUFFIXES)}"
            if n not in used:
                used.add(n)
                return n

    @staticmethod
    def _email(name: str) -> str:
        return name.lower().replace(" ", ".").replace("'", "") + "@northwind-commercial.example"

    @staticmethod
    def _probability(stage: str, rng: random.Random) -> float:
        return {
            "Qualification": rng.uniform(0.05, 0.15),
            "Needs Analysis": rng.uniform(0.15, 0.3),
            "Proposal/Price Quote": rng.uniform(0.35, 0.55),
            "Negotiation/Review": rng.uniform(0.6, 0.85),
            "Closed Won": 1.0,
            "Closed Lost": 0.0,
        }[stage]

    def _recent_months(self, n: int) -> list[str]:
        out = []
        y, m = self.today.year, self.today.month
        for _ in range(n):
            out.append(f"{y:04d}-{m:02d}")
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        return list(reversed(out))

    def _future_months(self, n: int) -> list[str]:
        out = []
        y, m = self.today.year, self.today.month
        for _ in range(n):
            m += 1
            if m == 13:
                m = 1
                y += 1
            out.append(f"{y:04d}-{m:02d}")
        return out

    # --------------------------------------------------------- source views

    def crm_payload(self) -> dict:
        return {
            "User": self.users,
            "Account": self.accounts,
            "Opportunity": self.opportunities,
        }

    def erp_payload(self) -> dict:
        return {
            "KNA1": [
                {"KUNNR": a["ERP_Customer_No__c"], "NAME1": a["Name"], "CRM_REF": a["Id"], "CREDIT_HOLD": False}
                for a in self.accounts
            ],
            "VBAK": self.orders,
        }

    def datalake_payload(self) -> dict:
        return {
            "fct_sales_target": self.targets,
            "fct_activity_weekly": self.activity,
            "dim_account_health": self.account_health,
        }


_WORLD: MockWorld | None = None


def world() -> MockWorld:
    global _WORLD
    if _WORLD is None:
        _WORLD = MockWorld()
    return _WORLD
