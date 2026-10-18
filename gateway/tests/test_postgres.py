"""The Postgres adapter end to end: typed tools through the gateway, then a rewind, against a fresh
copy of the demo company on prod-db (needs `make db`)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from mulligan.adapters.postgres.adapter import PostgresAdapter
from mulligan.log.events import EventBus
from mulligan.log.store import LogStore
from mulligan.models import (
    ActionStatus,
    Agent,
    AgentKind,
    RewindJob,
    RewindJobStatus,
    RewindRequest,
    RewindScope,
    Session,
    SessionMode,
    StepResult,
)
from mulligan.services import Services, assemble
from mulligan.settings import Settings

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
PROD_ADMIN = os.environ.get("MULLIGAN_TEST_PROD_URL", "postgresql://acme:acme@127.0.0.1:5434/acme")
AGENT = Agent(id="claude_code", kind=AgentKind.claude_code, display_name="Claude Code")


def _url(user: str, password: str, db: str) -> str:
    return f"postgresql://{user}:{password}@127.0.0.1:5434/{db}"


@pytest.fixture
async def prod_db() -> AsyncIterator[str]:
    name = f"acme_test_{uuid.uuid4().hex[:8]}"
    try:
        admin = await psycopg.AsyncConnection.connect(PROD_ADMIN, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"prod-db not reachable ({exc}); run `make db`")
    async with admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    async with await psycopg.AsyncConnection.connect(
        _url("acme", "acme", name), autocommit=True
    ) as conn:
        for path in [
            REPO / "infra/db/prod-db/00_mulligan.sql",
            REPO / "demo/seed/prod.sql",
            REPO / "infra/db/prod-db/20_enable.sql",
        ]:
            await conn.execute(path.read_text())  # type: ignore[arg-type]
    try:
        yield name
    finally:
        async with await psycopg.AsyncConnection.connect(PROD_ADMIN, autocommit=True) as admin:
            await admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
async def svc(log_pool: Any, prod_db: str) -> AsyncIterator[Services]:
    settings = Settings(
        prod_db_url=_url("mulligan_gw", "mulligan_gw", prod_db),
        prod_db_readonly_url=_url("mulligan_ro", "mulligan_ro", prod_db),
        prod_db_admin_url=_url("acme", "acme", prod_db),
        pg_query_timeout_ms=1000,
    )
    adapter = await PostgresAdapter.open(settings)
    try:
        yield assemble(settings, LogStore(log_pool), EventBus(log_pool), [adapter])
    finally:
        await adapter.close()


@pytest.fixture
async def human(prod_db: str) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    """A teammate writing straight to the database, outside Mulligan."""
    async with await psycopg.AsyncConnection.connect(
        _url("acme", "acme", prod_db), autocommit=True
    ) as conn:
        yield conn


async def scalar(conn: psycopg.AsyncConnection[Any], query: str) -> Any:
    cur = await conn.execute(query)  # type: ignore[arg-type]
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def rewind(svc: Services, session: Session) -> RewindJob:
    job = await svc.rewinder.create(
        RewindRequest(scope=RewindScope(session_id=session.id), dry_run=False)
    )
    await svc.rewinder.wait()
    done = await svc.store.get_job(job.id)
    assert done is not None
    return done


INACTIVE = [{"column": "last_order_at", "op": "lt", "value": "2025-10-17T00:00:00Z"}]


async def test_cascaded_delete_drop_and_update_rewind_to_the_same_checksum(
    svc: Services, human: Any
) -> None:
    adapter = svc.gateway.by_system[PostgresAdapter.system]
    before = await adapter.checksum()
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    call = svc.gateway.call_tool

    deleted = await call(session, "pg_delete_rows", {"table": "customers", "filter": INACTIVE})
    assert deleted.structured_content["deleted"] == 30
    assert deleted.structured_content["cascaded"]["orders"] > 0
    assert "cascaded" in deleted.summary
    await call(
        session,
        "pg_update_rows",
        {
            "table": "products",
            "filter": [{"column": "sku", "op": "like", "value": "WID-%"}],
            "values": {"price": 0},
        },
    )
    dropped = await call(session, "pg_drop_table", {"table": "legacy_pricing"})
    assert dropped.structured_content["referencing_foreign_keys"][0]["table"] == "price_overrides"
    inserted = await call(
        session,
        "pg_insert_rows",
        {"table": "products", "rows": [{"sku": "NEW-1", "name": "New thing", "price": 5}]},
    )
    assert inserted.structured_content["inserted"] == 1

    assert await scalar(human, "select count(*) from customers") == 90
    assert await scalar(human, "select to_regclass('public.legacy_pricing') is null")
    assert await adapter.checksum() != before

    job = await rewind(svc, session)
    assert job.status is RewindJobStatus.completed, job
    assert [s.tool for s in job.steps] == [
        "pg_insert_rows",
        "pg_drop_table",
        "pg_update_rows",
        "pg_delete_rows",
    ]
    assert all(s.result is StepResult.restored for s in job.steps)
    assert job.verification["postgres"].ok is True
    assert await adapter.checksum() == before
    assert await scalar(human, "select count(*) from customers") == 120
    assert (
        await scalar(
            human,
            "select count(*) from price_overrides po"
            " join legacy_pricing lp on lp.id = po.legacy_pricing_id",
        )
        == 12
    )


async def test_teammate_edit_is_kept_and_verification_still_passes(
    svc: Services, human: Any
) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    sku1 = [{"column": "sku", "op": "eq", "value": "WID-001"}]
    await svc.gateway.call_tool(
        session,
        "pg_update_rows",
        {"table": "products", "filter": sku1, "values": {"price": 0, "name": "Free Widget"}},
    )
    await human.execute("update products set price = 45 where sku = 'WID-001'")

    plan = await svc.rewinder.create(RewindRequest(scope=RewindScope(session_id=session.id)))
    assert plan.steps[0].kept_fields == ["postgres:products/1/price"]

    job = await rewind(svc, session)
    step = job.steps[0]
    assert step.result is StepResult.kept_human_edit
    assert step.kept_fields == ["postgres:products/1/price"]
    kept = step.changes[0]
    assert (kept.before, kept.agent_value, kept.current) == (49, 0, 45)
    assert await scalar(human, "select price from products where sku = 'WID-001'") == 45
    assert (
        await scalar(human, "select name from products where sku = 'WID-001'") == "Classic Widget"
    )
    assert job.verification["postgres"].ok is True
    action = await svc.store.get(step.action_id)
    assert action is not None and action.status is ActionStatus.conflict


async def test_reused_primary_key_is_a_conflict(svc: Services, human: Any) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    await svc.gateway.call_tool(
        session,
        "pg_delete_rows",
        {"table": "support_tickets", "filter": [{"column": "id", "op": "eq", "value": 1}]},
    )
    await human.execute(
        "insert into support_tickets (id, customer_id, subject, opened_on) overriding system value"
        " values (1, 2, 'New ticket', '2026-10-17')"
    )
    job = await rewind(svc, session)
    assert job.steps[0].result is StepResult.kept_human_edit
    assert job.steps[0].kept_fields == ["postgres:support_tickets/1"]
    assert await scalar(human, "select subject from support_tickets where id = 1") == "New ticket"
    assert job.verification["postgres"].ok is True


async def test_bad_writes_are_refused(svc: Services) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    call = svc.gateway.call_tool
    empty = await call(session, "pg_delete_rows", {"table": "customers", "filter": []})
    assert empty.is_error and "all=true" in empty.summary
    hidden = await call(
        session, "pg_delete_rows", {"table": "mulligan_audit.audit_log", "all": True}
    )
    assert hidden.is_error
    ident = await call(session, "pg_insert_rows", {"table": "customers", "rows": [{"id": 1}]})
    assert ident.is_error and "generated" in ident.summary
    sneaky = await call(
        session,
        "pg_update_rows",
        {
            "table": "products",
            "filter": [{"column": "id); drop table products; --", "op": "eq", "value": 1}],
            "values": {"price": 1},
        },
    )
    assert sneaky.is_error and "unknown column" in sneaky.summary
    failed = await svc.store.list_actions(session_id=session.id, status=ActionStatus.failed)
    assert len(failed) == 4


async def test_pg_query_is_read_only_capped_and_times_out(svc: Services) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    call = svc.gateway.call_tool
    ok = await call(
        session, "pg_query", {"sql": "select id, price from products order by id", "max_rows": 5}
    )
    assert ok.structured_content["row_count"] == 5 and ok.structured_content["truncated"] is True
    assert ok.structured_content["rows"][0] == [1, 49]
    for bad in [
        "delete from customers",
        "select 1; delete from customers",
        "select * from mulligan_audit.audit_log",
        "select pg_sleep(3)",
    ]:
        result = await call(session, "pg_query", {"sql": bad})
        assert result.is_error, bad
    dropped = await call(session, "pg_drop_table", {"table": "legacy_pricing"})
    assert not dropped.is_error
    action = await svc.store.get(dropped.structured_content["action_id"])
    assert action is not None and action.inverse is not None
    trash = action.inverse.payload["trash_name"]
    assert trash.startswith(action.id.lower() + "__")
    peek = await call(
        session, "pg_query", {"sql": f'select count(*) from mulligan_trash."{trash}"'}
    )
    assert peek.is_error and "permission denied" in peek.summary


async def test_demo_reset_reseeds_and_clears_the_trash(svc: Services, human: Any) -> None:
    adapter = svc.gateway.by_system[PostgresAdapter.system]
    assert isinstance(adapter, PostgresAdapter)
    seeded = await adapter.checksum()
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    await svc.gateway.call_tool(
        session, "pg_delete_rows", {"table": "customers", "filter": INACTIVE}
    )
    await svc.gateway.call_tool(session, "pg_drop_table", {"table": "legacy_pricing"})
    await adapter.reset()
    assert await adapter.checksum() == seeded
    assert (
        await scalar(human, "select count(*) from pg_tables where schemaname = 'mulligan_trash'")
        == 0
    )
    assert await scalar(human, "select count(*) from mulligan_audit.audit_log") == 0
    assert (
        await scalar(human, "select count(*) from pg_trigger where tgname = 'mulligan_audit_row'")
        == 7
    )
    again = await svc.gateway.call_tool(
        session, "pg_delete_rows", {"table": "customers", "filter": INACTIVE}
    )
    assert again.structured_content["deleted"] == 30


async def test_edits_elsewhere_dont_fail_verification(svc: Services, human: Any) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    call = svc.gateway.call_tool
    wid = [{"column": "sku", "op": "eq", "value": "WID-001"}]
    await call(
        session, "pg_update_rows", {"table": "products", "filter": wid, "values": {"price": 0}}
    )
    await call(session, "pg_delete_rows", {"table": "customers", "filter": INACTIVE})
    # A teammate renames the same product (a column the agent never touched), edits a ticket
    # the agent never touched, and adds a product.
    await human.execute("update products set name = 'Classic Widget v2' where sku = 'WID-001'")
    await human.execute("update support_tickets set subject = 'Called back' where id = 1")
    await human.execute("insert into products (sku, name, price) values ('NEW-9', 'Gizmo', 3)")
    job = await rewind(svc, session)
    assert job.status is RewindJobStatus.completed
    assert all(s.result is StepResult.restored for s in job.steps)
    assert job.verification["postgres"].ok is True
    assert await scalar(
        human, "select name || ' ' || price from products where sku = 'WID-001'"
    ) == ("Classic Widget v2 49.00")


async def test_rows_changed_then_table_dropped(svc: Services, human: Any) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    call = svc.gateway.call_tool
    us = [{"column": "region", "op": "eq", "value": "us"}]
    apac = [{"column": "region", "op": "eq", "value": "apac"}]
    before = await scalar(human, "select sum(price) from legacy_pricing")
    await call(
        session, "pg_update_rows", {"table": "legacy_pricing", "filter": us, "values": {"price": 1}}
    )
    await call(session, "pg_delete_rows", {"table": "price_overrides", "all": True})
    await call(session, "pg_delete_rows", {"table": "legacy_pricing", "filter": apac})
    await call(session, "pg_drop_table", {"table": "legacy_pricing"})
    job = await rewind(svc, session)
    assert job.status is RewindJobStatus.completed, [s.detail for s in job.steps]
    assert job.verification["postgres"].ok is True
    assert await scalar(human, "select sum(price) from legacy_pricing") == before
    assert await scalar(human, "select count(*) from price_overrides") == 12


async def test_cascade_into_a_dropped_table_verifies_and_keeps_old_names(
    svc: Services, human: Any
) -> None:
    adapter = svc.gateway.by_system[PostgresAdapter.system]
    before = await adapter.checksum()
    await human.execute("comment on table orders is 'Every order'")
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    call = svc.gateway.call_tool
    await call(session, "pg_drop_table", {"table": "orders"})
    one = [{"column": "id", "op": "eq", "value": 2}]
    deleted = await call(session, "pg_delete_rows", {"table": "customers", "filter": one})
    assert deleted.structured_content["cascaded"] == {"orders": 1, "order_items": 2}
    assert "act_" not in deleted.summary
    bad = await call(
        session,
        "pg_insert_rows",
        {
            "table": "order_items",
            "rows": [{"order_id": 9999, "line_no": 1, "product_id": 1, "qty": 1, "unit_price": 1}],
        },
    )
    assert bad.is_error and '"orders"' in bad.summary and "act_" not in bad.summary
    job = await rewind(svc, session)
    assert job.status is RewindJobStatus.completed, job.verification
    assert all("act_" not in (s.detail or "") for s in job.steps)
    assert await adapter.checksum() == before
    assert await scalar(human, "select obj_description('orders'::regclass, 'pg_class')") == (
        "Every order"
    )


async def test_kept_edit_under_a_generated_column_verifies(svc: Services, human: Any) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    item = [
        {"column": "order_id", "op": "eq", "value": 2},
        {"column": "line_no", "op": "eq", "value": 1},
    ]
    await svc.gateway.call_tool(
        session, "pg_update_rows", {"table": "order_items", "filter": item, "values": {"qty": 7}}
    )
    await human.execute("update order_items set qty = 42 where order_id = 2 and line_no = 1")
    job = await rewind(svc, session)
    assert job.steps[0].kept_fields == ["postgres:order_items/2,1/qty"]
    assert job.status is RewindJobStatus.completed and job.verification["postgres"].ok is True


async def test_primary_key_change_then_drop_verifies(svc: Services) -> None:
    adapter = svc.gateway.by_system[PostgresAdapter.system]
    before = await adapter.checksum()
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    item = [
        {"column": "order_id", "op": "eq", "value": 2},
        {"column": "line_no", "op": "eq", "value": 1},
    ]
    await svc.gateway.call_tool(
        session,
        "pg_update_rows",
        {"table": "order_items", "filter": item, "values": {"line_no": 127}},
    )
    await svc.gateway.call_tool(session, "pg_drop_table", {"table": "order_items"})
    job = await rewind(svc, session)
    assert job.status is RewindJobStatus.completed and job.verification["postgres"].ok is True
    assert await adapter.checksum() == before


async def test_verification_catches_a_bad_restore(svc: Services, human: Any) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    wid = [{"column": "sku", "op": "eq", "value": "WID-002"}]
    await svc.gateway.call_tool(
        session, "pg_update_rows", {"table": "products", "filter": wid, "values": {"price": 1}}
    )
    adapter = svc.gateway.by_system[PostgresAdapter.system]
    real_revert = adapter.revert

    async def sloppy_revert(action: Any) -> Any:
        step = await real_revert(action)
        await human.execute("update products set price = 98 where sku = 'WID-002'")
        return step

    adapter.revert = sloppy_revert  # type: ignore[method-assign]
    job = await rewind(svc, session)
    assert job.verification["postgres"].ok is False
    assert job.status is RewindJobStatus.partial
