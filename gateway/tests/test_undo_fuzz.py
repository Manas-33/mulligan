"""Randomized check of the Postgres undo, against a fresh copy of the demo company on prod-db
(needs `make db`). Each case reseeds the company, runs a random session of 5 to 30 typed writes
(inserts, updates, deletes that cascade, soft drops), rewinds it newest first, and expects the
pre-session checksum back. A second test adds a teammate's edit after the agent's last write to
one field and expects that edit kept and flagged, with everything else restored.

  MULLIGAN_FUZZ_SESSIONS=200   cases per test (default 200)
  MULLIGAN_FUZZ_SEED=17        replay one case

Some generated calls are refused (FK or unique violations, tables already dropped). That's on
purpose: failed actions must stay out of the rewind.
"""

from __future__ import annotations

import json
import os
import random
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from mulligan.adapters.postgres.adapter import PostgresAdapter
from mulligan.adapters.postgres.catalog import load_table, seg
from mulligan.models import (
    ActionStatus,
    RewindJob,
    RewindJobStatus,
    Session,
    SessionMode,
    StepResult,
    System,
)
from mulligan.services import Services
from tests import test_postgres as pg

pytestmark = pytest.mark.integration

# A fresh acme_test_* database per test, the same fixtures test_postgres.py uses.
prod_db = pg.prod_db
svc = pg.svc
human = pg.human

SESSIONS = int(os.environ.get("MULLIGAN_FUZZ_SESSIONS", "200"))
_ONLY = os.environ.get("MULLIGAN_FUZZ_SEED")
SEEDS = [int(_ONLY)] if _ONLY else list(range(SESSIONS))
MAX_REPORTS = 3

PK = {
    "customers": ("id",),
    "products": ("id",),
    "orders": ("id",),
    "order_items": ("order_id", "line_no"),
    "support_tickets": ("id",),
    "legacy_pricing": ("id",),
    "price_overrides": ("id",),
}
FK = {
    "customer_id": "customers",
    "product_id": "products",
    "order_id": "orders",
    "legacy_pricing_id": "legacy_pricing",
}
UPDATABLE = {
    "customers": ["name", "plan", "signed_up_at", "last_order_at", "email"],
    "products": ["name", "price", "active", "sku"],
    "orders": ["customer_id", "placed_at", "status"],
    "order_items": ["product_id", "qty", "unit_price", "line_no"],
    "support_tickets": ["customer_id", "subject", "opened_on"],
    "legacy_pricing": ["product_id", "region", "price"],
    "price_overrides": ["legacy_pricing_id", "reason"],
}
# Set only when the filter targets one row (unique columns, or a primary-key column).
SINGLE_ROW_ONLY = {"email", "sku", "line_no"}
INSERT_COLS = {
    "customers": ["email", "name", "plan", "signed_up_at"],
    "products": ["sku", "name", "price"],
    "orders": ["customer_id", "placed_at", "status"],
    "order_items": ["order_id", "line_no", "product_id", "qty", "unit_price"],
    "support_tickets": ["customer_id", "subject", "opened_on"],
    "legacy_pricing": ["product_id", "region", "price"],
    "price_overrides": ["legacy_pricing_id", "reason"],
}
OPTIONAL_INSERT_COLS = {"customers": ["last_order_at"], "products": ["active"]}
FILTER_COLS = {
    "customers": ["plan", "name", "last_order_at"],
    "products": ["active", "price"],
    "orders": ["status", "customer_id"],
    "order_items": ["product_id", "qty"],
    "support_tickets": ["subject", "customer_id"],
    "legacy_pricing": ["region", "product_id"],
    "price_overrides": ["reason", "legacy_pricing_id"],
}
# What the teammate types in. No generated agent value can equal these.
HUMAN_EDITS: dict[str, dict[str, Any]] = {
    "customers": {"name": "Edited By Hand", "plan": "hand-picked"},
    "products": {"name": "Hand-priced", "price": 4242.42},
    "orders": {"status": "hand-checked"},
    "order_items": {"qty": 4242, "unit_price": 4242.42},
    "support_tickets": {"subject": "Edited by hand"},
    "legacy_pricing": {"price": 4242.42},
    "price_overrides": {"reason": "edited by hand"},
}
GENERATED_FROM = {("order_items", "qty"), ("order_items", "unit_price")}  # -> line_total
TRASH_CASCADE = "cascade into a trashed table"
PK_CHANGE_THEN_DROP = "primary key changed, then table dropped"
EDIT_UNDER_GENERATED = "kept edit under a generated column"
NAMES = ["Ada", "Grace", "Alan", "Edsger", "Barbara", "Ken", "Frances", "Donald"]
DELETE_WEIGHTS = {"customers": 3, "orders": 3}


@dataclass
class Call:
    tool: str
    args: dict[str, Any]
    status: str
    summary: str
    action_id: str | None
    cascaded: bool

    def line(self) -> str:
        args = json.dumps(self.args, default=str)
        return f"{self.tool} {args} -> {self.status}: {self.summary}"


@dataclass
class Case:
    seed: int
    calls: list[Call] = field(default_factory=list)
    job: RewindJob | None = None
    notes: list[str] = field(default_factory=list)
    known: list[str] = field(default_factory=list)  # known verify_scope bugs this case hits
    edit_under_generated: bool = False

    def executed(self) -> list[Call]:
        return [c for c in self.calls if c.status == "executed"]

    def report(self, problems: list[str]) -> str:
        lines = [f"seed {self.seed} (replay: MULLIGAN_FUZZ_SEED={self.seed})", *self.notes]
        lines += [f"  problem: {p}" for p in problems]
        lines += [f"  {i:2} {c.line()}" for i, c in enumerate(self.calls, 1)]
        if self.job is not None:
            lines.append(f"  rewind {self.job.status.value}: {self.job.error or ''}")
            lines += [
                f"    {s.tool} {s.result.value} kept={s.kept_fields} {s.detail}"
                for s in self.job.steps
            ]
            lines += [f"    verify {k}: {v}" for k, v in self.job.verification.items()]
        return "\n".join(lines)


class Fuzzer:
    """Picks the next tool call from the database as it is now. Rows come back in primary-key
    order and every choice comes from one seeded RNG, so a seed always replays the same calls."""

    def __init__(self, seed: int, db: psycopg.AsyncConnection[Any]) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.db = db
        self.n = 0

    def tick(self) -> int:
        self.n += 1
        return self.n

    async def live(self) -> list[str]:
        cur = await self.db.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
        )
        return [r[0] for r in await cur.fetchall() if r[0] in PK]

    async def rows(self, table: str) -> list[dict[str, Any]]:
        if table not in await self.live():
            return []
        order = sql.SQL(", ").join(map(sql.Identifier, PK[table]))
        cur = await self.db.execute(
            sql.SQL("SELECT to_jsonb(t) FROM public.{} AS t ORDER BY {}").format(
                sql.Identifier(table), order
            )
        )
        return [r[0] for r in await cur.fetchall()]

    async def value(self, col: str) -> Any:
        r, n = self.rng, self.tick()
        if col in FK:
            ids = [row["id"] for row in await self.rows(FK[col])]
            return r.choice(ids) if ids else r.randint(1, 400)
        match col:
            case "email":
                return f"fuzz-{self.seed}-{n}@acme-widgets.test"
            case "sku":
                return f"FZ-{self.seed}-{n}"
            case "line_no":
                return 100 + n
            case "name":
                return f"{r.choice(NAMES)} {n}"
            case "plan":
                return r.choice(["starter", "pro", "enterprise", "trial"])
            case "status":
                return r.choice(["paid", "shipped", "delivered", "refunded"])
            case "subject":
                return r.choice(["Late delivery", "Refund request", "Escalated", "Closed"])
            case "reason":
                return r.choice(["reseller promo", "bulk discount", "price match"])
            case "region":
                return f"r{self.seed}-{n}"
            case "price" | "unit_price":
                return round(r.uniform(0, 500), r.choice([0, 1, 2, 3]))
            case "qty":
                return r.randint(1, 20)
            case "active":
                return r.random() < 0.5
            case "signed_up_at" | "placed_at" | "last_order_at":
                if col == "last_order_at" and r.random() < 0.25:
                    return None
                day = f"2026-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}"
                return f"{day}T{r.randint(0, 23):02d}:{r.randint(0, 59):02d}:00Z"
            case "opened_on":
                return f"2026-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}"
        raise KeyError(col)

    async def where(self, table: str) -> tuple[list[dict[str, Any]], bool]:
        """A filter over rows that exist now, and whether it targets exactly one row."""
        r, pk = self.rng, PK[table]
        rows = await self.rows(table)
        lead = pk[0]
        if not rows:
            return [{"column": lead, "op": "eq", "value": 0}], False
        mode = r.choices(["one", "in", "range", "value"], weights=[30, 30, 20, 20])[0]
        if mode == "one":
            row = r.choice(rows)
            return [{"column": c, "op": "eq", "value": row[c]} for c in pk], True
        if mode == "in":
            keys = sorted({row[lead] for row in rows})
            picked = r.sample(keys, min(len(keys), r.randint(2, 8)))
            return [{"column": lead, "op": "in", "value": sorted(picked)}], False
        if mode == "range":
            keys = sorted({row[lead] for row in rows})
            start = r.choice(keys)
            return [
                {"column": lead, "op": "gte", "value": start},
                {"column": lead, "op": "lt", "value": start + r.randint(1, 15)},
            ], False
        row = r.choice(rows)
        col = r.choice(FILTER_COLS[table])
        v = row[col]
        if v is None:
            f: dict[str, Any] = {"column": col, "op": "is_null"}
        elif col == "name":
            f = {"column": col, "op": "like", "value": v.split()[0] + "%"}
        elif isinstance(v, int | float) and not isinstance(v, bool) and r.random() < 0.5:
            f = {"column": col, "op": r.choice(["lt", "gte"]), "value": v}
        else:
            f = {"column": col, "op": "eq", "value": v}
        filters = [f]
        if r.random() < 0.6:
            filters.append({"column": lead, "op": "lte", "value": row[lead]})
        return filters, False

    async def next_call(self) -> tuple[str, dict[str, Any]]:
        r = self.rng
        live = await self.live()
        tool = r.choices(
            ["pg_update_rows", "pg_delete_rows", "pg_insert_rows", "pg_drop_table"],
            weights=[35, 30, 28, 7],
        )[0]
        if not live:
            return "pg_insert_rows", {"table": "customers", "rows": [{"name": "nobody"}]}
        if tool == "pg_drop_table":
            return tool, {"table": r.choice(live)}
        if tool == "pg_delete_rows":
            table = r.choices(live, weights=[DELETE_WEIGHTS.get(t, 1) for t in live])[0]
            if r.random() < 0.03:
                return tool, {"table": table, "all": True}
            filters, _ = await self.where(table)
            return tool, {"table": table, "filter": filters}
        table = r.choice(live)
        if tool == "pg_update_rows":
            filters, single = await self.where(table)
            cols = [c for c in UPDATABLE[table] if single or c not in SINGLE_ROW_ONLY]
            chosen = r.sample(cols, r.randint(1, min(3, len(cols))))
            return tool, {
                "table": table,
                "filter": filters,
                "values": {c: await self.value(c) for c in chosen},
            }
        cols = INSERT_COLS[table] + [
            c for c in OPTIONAL_INSERT_COLS.get(table, []) if r.random() < 0.5
        ]
        explicit_id = table == "products" and r.random() < 0.3  # BY DEFAULT identity
        new_rows = []
        for _ in range(r.randint(1, 4)):
            row = {"id": 10_000 + self.tick()} if explicit_id else {}
            for c in cols:
                if c == "line_no" and r.random() < 0.5:
                    row[c] = r.randint(1, 4)  # an existing line, or one this session deleted
                elif c == "line_no":
                    row[c] = 10 + self.tick()
                else:
                    row[c] = await self.value(c)
            new_rows.append(row)
        return tool, {"table": table, "rows": new_rows}


async def play(services: Services, db: psycopg.AsyncConnection[Any], case: Case) -> Session:
    fz = Fuzzer(case.seed, db)
    session = await services.gateway.start_session(pg.AGENT, SessionMode.live)
    for _ in range(fz.rng.randint(5, 30)):
        tool, args = await fz.next_call()
        await call(services, session, case, tool, args)
    return session


async def call(
    services: Services, session: Session, case: Case, tool: str, args: dict[str, Any]
) -> Call:
    result = await services.gateway.call_tool(session, tool, args)
    content = result.structured_content
    done = Call(
        tool=tool,
        args=args,
        status=str(content.get("status")),
        summary=result.summary,
        action_id=content.get("action_id"),
        cascaded=bool(content.get("cascaded")),
    )
    case.calls.append(done)
    return done


async def known_verify_bugs(db: psycopg.AsyncConnection[Any], case: Case) -> list[str]:
    """Session shapes where PostgresAdapter.verify_scope reports a mismatch after a correct
    restore (each one reproduced by hand; the full-database checksum still matches):
    - a cascade changed rows of a table while it sat in the trash: those rows' drop-time form
      is counted on the expected side twice;
    - an UPDATE changed a primary key, then the table was dropped: the row at the old key is
      in the restored table but not on the expected side;
    - a teammate's kept edit is on a column a generated column depends on
      (order_items.qty / unit_price -> line_total): the expected side puts the generated
      column back to its pre-session value."""
    ids = [c.action_id for c in case.executed()]
    cur = await db.execute(
        "SELECT action_id, table_name, schema_name = 'mulligan_trash',"
        "       op = 'UPDATE' AND changed && ARRAY(SELECT jsonb_object_keys(pk))"
        "  FROM mulligan_audit.audit_log WHERE action_id = ANY(%s)",
        [ids],
    )
    audit = await cur.fetchall()
    known = []
    if any(in_trash for _, _, in_trash, _ in audit):
        known.append(TRASH_CASCADE)
    order = {c.action_id: i for i, c in enumerate(case.executed())}
    drops = [
        (i, c.args["table"]) for i, c in enumerate(case.executed()) if c.tool == "pg_drop_table"
    ]
    if any(
        moved and table == dropped and order[aid] < i
        for aid, table, _, moved in audit
        for i, dropped in drops
    ):
        known.append(PK_CHANGE_THEN_DROP)
    if case.edit_under_generated:
        known.append(EDIT_UNDER_GENERATED)
    return known


async def trash_tables(db: psycopg.AsyncConnection[Any]) -> int:
    return int(
        await pg.scalar(db, "SELECT count(*) FROM pg_tables WHERE schemaname = 'mulligan_trash'")
    )


async def common_problems(case: Case, db: psycopg.AsyncConnection[Any]) -> list[str]:
    job = case.job
    assert job is not None
    problems = []
    if [s.action_id for s in job.steps] != [c.action_id for c in reversed(case.executed())]:
        problems.append("steps are not the executed actions, newest first")
    failed = [s for s in job.steps if s.result is StepResult.failed]
    if failed:
        problems.append(f"{len(failed)} failed steps")
    check = job.verification.get(System.postgres)
    verified = not case.executed() or (check is not None and check.ok is True)
    if not verified and not failed:
        # Tolerated only for the known verify_scope bugs; the caller still compares the
        # full-database checksum.
        case.known = await known_verify_bugs(db, case)
    if not verified and not case.known:
        problems.append(f"verification: {check}")
    if job.status is not RewindJobStatus.completed and not case.known:
        problems.append(f"rewind status {job.status.value}: {job.error}")
    if await trash_tables(db):
        problems.append("tables left in mulligan_trash")
    return problems


def tally(stats: Counter[str], case: Case) -> None:
    stats["sessions"] += 1
    stats.update(f"known verify_scope mismatch ({k})" for k in case.known)
    for c in case.calls:
        stats["actions"] += 1
        stats[f"{c.tool} {c.status}"] += 1
        if c.tool == "pg_delete_rows" and c.status == "executed" and c.cascaded:
            stats["cascading deletes"] += 1


def summarize(name: str, stats: Counter[str], started: float) -> str:
    parts = ", ".join(f"{k}: {v}" for k, v in sorted(stats.items()))
    seeds = f"{SEEDS[0]}..{SEEDS[-1]}" if len(SEEDS) > 1 else str(SEEDS[0])
    return f"\n{name}: seeds {seeds}, {time.monotonic() - started:.1f}s; {parts}"


def check_coverage(stats: Counter[str]) -> None:
    """With enough cases, every kind of write (and a cascade) must actually have run."""
    if len(SEEDS) < 20:
        return
    for tool in ["pg_insert_rows", "pg_update_rows", "pg_delete_rows", "pg_drop_table"]:
        assert stats[f"{tool} executed"] > 0, f"the generator never ran {tool}"
    assert stats["cascading deletes"] > 0, "the generator never cascaded a delete"


def fail_with(failures: list[tuple[int, str]], total: int) -> None:
    if failures:
        seeds = ", ".join(str(seed) for seed, _ in failures)
        shown = "\n\n".join(report for _, report in failures[:MAX_REPORTS])
        more = len(failures) - MAX_REPORTS
        tail = f"\n\n... and {more} more" if more > 0 else ""
        pytest.fail(
            f"{len(failures)} of {total} cases failed (seeds {seeds})\n\n{shown}{tail}",
            pytrace=False,
        )


async def test_random_sessions_rewind_to_the_pre_session_checksum(
    svc: Services, human: Any
) -> None:
    adapter = svc.gateway.by_system[PostgresAdapter.system]
    assert isinstance(adapter, PostgresAdapter)
    seeded = await adapter.checksum()
    stats: Counter[str] = Counter()
    failures: list[tuple[int, str]] = []
    started = time.monotonic()
    for seed in SEEDS:
        case = Case(seed)
        await adapter.reset()
        before = await adapter.checksum()
        assert before == seeded, "the reset didn't reseed the demo company"
        try:
            session = await play(svc, human, case)
            case.job = await pg.rewind(svc, session)
            problems = await common_problems(case, human)
            not_clean = [
                s
                for s in case.job.steps
                if s.result not in (StepResult.restored, StepResult.skipped)
            ]
            if not_clean:
                problems.append(f"{len(not_clean)} steps neither restored nor skipped")
            after = await adapter.checksum()
            if after != before:
                problems.append(f"checksum {after} != pre-session {before}")
        except Exception as exc:
            problems = [f"{type(exc).__name__}: {exc}"]
        if problems:
            failures.append((seed, case.report(problems)))
        tally(stats, case)
    print(summarize("undo fuzz", stats, started))
    fail_with(failures, len(SEEDS))
    check_coverage(stats)


@dataclass
class Target:
    audit_id: int  # the session's first audit row for this row (its pre-session form)
    action_id: str  # the agent's last write to the field
    table: str
    table_oid: int
    pk: dict[str, Any]
    col: str


async def edit_targets(db: psycopg.AsyncConnection[Any], action_ids: Iterable[str]) -> list[Target]:
    """Fields whose current value is the agent's: the row existed before the session, the
    session's last write to it was an UPDATE of the field, and its table is still in public.
    Tables where the session changed a primary key are left out (the row's identity moved)."""
    cur = db.cursor(row_factory=dict_row)
    await cur.execute(
        """
        SELECT a.id, a.action_id, a.table_oid, a.table_name, a.op, a.pk, a.changed,
               n.nspname AS schema_now
          FROM mulligan_audit.audit_log a
          JOIN pg_class c ON c.oid = a.table_oid JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE a.action_id = ANY(%s) ORDER BY a.id
        """,
        [list(action_ids)],
    )
    audit = await cur.fetchall()
    moved = {
        a["table_name"] for a in audit if a["op"] == "UPDATE" and set(a["changed"]) & set(a["pk"])
    }
    by_row: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for a in audit:
        by_row.setdefault((a["table_name"], json.dumps(a["pk"], sort_keys=True)), []).append(a)
    targets = []
    for (table, _), history in by_row.items():
        last = history[-1]
        if (
            table in moved
            or table not in HUMAN_EDITS
            or last["schema_now"] != "public"
            or last["op"] != "UPDATE"
            or any(a["op"] == "INSERT" for a in history)
        ):
            continue
        for col in sorted(set(last["changed"]) & set(HUMAN_EDITS[table])):
            targets.append(
                Target(
                    history[0]["id"], last["action_id"], table, last["table_oid"], last["pk"], col
                )
            )
    return targets


def pk_where(pk: dict[str, Any]) -> sql.Composable:
    return sql.SQL(" AND ").join(
        sql.SQL("{} = {}").format(sql.Identifier(k), sql.Literal(v)) for k, v in pk.items()
    )


async def test_teammate_edit_after_the_agent_is_kept_and_flagged(svc: Services, human: Any) -> None:
    adapter = svc.gateway.by_system[PostgresAdapter.system]
    assert isinstance(adapter, PostgresAdapter)
    stats: Counter[str] = Counter()
    failures: list[tuple[int, str]] = []
    started = time.monotonic()
    for seed in SEEDS:
        case = Case(seed)
        await adapter.reset()
        before = await adapter.checksum()
        try:
            session = await play(svc, human, case)
            fz = Fuzzer(seed + 1_000_000, human)
            targets = await edit_targets(
                human, [c.action_id for c in case.executed() if c.action_id]
            )
            # No field left with the agent's value: have the agent write one more.
            for table in fz.rng.sample(sorted(HUMAN_EDITS), len(HUMAN_EDITS)):
                if targets:
                    break
                rows = await fz.rows(table)
                if not rows:
                    continue
                row = fz.rng.choice(rows)
                col = fz.rng.choice(sorted(HUMAN_EDITS[table]))
                await call(
                    svc,
                    session,
                    case,
                    "pg_update_rows",
                    {
                        "table": table,
                        "filter": [{"column": c, "op": "eq", "value": row[c]} for c in PK[table]],
                        "values": {col: await fz.value(col)},
                    },
                )
                targets = await edit_targets(
                    human, [c.action_id for c in case.executed() if c.action_id]
                )
            if not targets:
                stats["no field to edit"] += 1
                tally(stats, case)
                continue
            t = fz.rng.choice(targets)
            case.edit_under_generated = (t.table, t.col) in GENERATED_FROM
            human_value = HUMAN_EDITS[t.table][t.col]
            ident = sql.Identifier("public", t.table)
            await human.execute(
                sql.SQL("UPDATE {} SET {} = {} WHERE {}").format(
                    ident, sql.Identifier(t.col), sql.Literal(human_value), pk_where(t.pk)
                )
            )
            async with adapter.pool.connection() as conn:
                key = f"{(await load_table(conn, t.table_oid)).row_key(t.pk)}/{seg(t.col)}"
            case.notes.append(f"  teammate sets {key} = {human_value!r}")

            case.job = job = await pg.rewind(svc, session)
            problems = await common_problems(case, human)
            kept = {f for s in job.steps for f in s.kept_fields}
            if kept != {key}:
                problems.append(f"kept fields {sorted(kept)}, expected only {key}")
            step = next((s for s in job.steps if s.action_id == t.action_id), None)
            if step is None or step.result is not StepResult.kept_human_edit:
                problems.append(f"the agent's last write to {key} wasn't flagged: {step}")
            for s in job.steps:
                if not s.kept_fields and s.result not in (StepResult.restored, StepResult.skipped):
                    problems.append(f"step {s.action_id} {s.result.value} with nothing kept")
            action = await svc.store.get(t.action_id)
            if action is None or action.status is not ActionStatus.conflict:
                problems.append(f"action {t.action_id} is {action and action.status}")
            survived = await pg.scalar(
                human,
                sql.SQL("SELECT to_jsonb({}) = {} FROM {} WHERE {}")
                .format(
                    sql.Identifier(t.col),
                    sql.Literal(Jsonb(human_value)),
                    ident,
                    pk_where(t.pk),
                )
                .as_string(human),
            )
            if not survived:
                problems.append(f"the teammate's {key} was overwritten")
            # Take the teammate's edit back out by hand: the rest must be the pre-session state.
            await human.execute(
                sql.SQL(
                    "UPDATE {t} AS tgt SET {c} = r.{c} FROM mulligan_audit.audit_log a"
                    " CROSS JOIN LATERAL jsonb_populate_record(NULL::{t}, a.old_row) AS r"
                    " WHERE a.id = {id} AND {m}"
                ).format(
                    t=ident,
                    c=sql.Identifier(t.col),
                    id=sql.Literal(t.audit_id),
                    m=sql.SQL(" AND ").join(
                        sql.SQL("tgt.{k} = r.{k}").format(k=sql.Identifier(k)) for k in t.pk
                    ),
                )
            )
            after = await adapter.checksum()
            if after != before:
                problems.append(f"checksum without the teammate's edit {after} != {before}")
            stats["teammate edits kept"] += not problems
        except Exception as exc:
            problems = [f"{type(exc).__name__}: {exc}"]
        if problems:
            failures.append((seed, case.report(problems)))
        tally(stats, case)
    print(summarize("undo fuzz, teammate edits", stats, started))
    fail_with(failures, len(SEEDS))
    check_coverage(stats)
    assert stats["teammate edits kept"] > 0
