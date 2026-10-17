"""REST routes under /api (contracts/openapi.yaml)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from sse_starlette.sse import EventSourceResponse

from mulligan.api.auth import require_dashboard
from mulligan.interfaces import HoldNotFound, NotHeld, WrongCode
from mulligan.models import (
    Action,
    ActionStatus,
    AgentKind,
    ApprovalDecisionRequest,
    DemoResetResult,
    RewindJob,
    RewindRequest,
    Session,
    SessionStatus,
    System,
    SystemChecksum,
)
from mulligan.orchestrator.rewind import EmptyScope, RewindBusy
from mulligan.services import Services

router = APIRouter(prefix="/api")

Limit = Annotated[int, Query(ge=1, le=1000)]
SES_ID = r"^ses_[0-9A-HJKMNP-TV-Z]{26}$"
ACT_ID = r"^act_[0-9A-HJKMNP-TV-Z]{26}$"
RWD_ID = r"^rwd_[0-9A-HJKMNP-TV-Z]{26}$"
PING_SECONDS = 15


def services(request: Request) -> Services:
    svc: Services = request.app.state.services
    return svc


Svc = Annotated[Services, Depends(services)]


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")


@router.get("/sessions")
async def list_sessions(
    svc: Svc,
    status_: Annotated[SessionStatus | None, Query(alias="status")] = None,
    agent_kind: AgentKind | None = None,
    limit: Limit = 200,
) -> list[Session]:
    return await svc.store.list_sessions(
        status=status_.value if status_ else None,
        agent_kind=agent_kind.value if agent_kind else None,
        limit=limit,
    )


@router.get("/sessions/{session_id}")
async def get_session(svc: Svc, session_id: Annotated[str, Path(pattern=SES_ID)]) -> Session:
    session = await svc.store.get_session(session_id)
    if session is None:
        raise _not_found()
    return session


@router.get("/actions")
async def list_actions(
    svc: Svc,
    session_id: Annotated[str | None, Query(pattern=SES_ID)] = None,
    since: datetime | None = None,
    system: System | None = None,
    status_: Annotated[ActionStatus | None, Query(alias="status")] = None,
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "asc",
    limit: Limit = 200,
) -> list[Action]:
    return await svc.store.list_actions(
        session_id=session_id,
        since=since,
        system=system,
        status=status_,
        newest_first=order == "desc",
        limit=limit,
    )


@router.get("/actions/{action_id}")
async def get_action(svc: Svc, action_id: Annotated[str, Path(pattern=ACT_ID)]) -> Action:
    action = await svc.store.get(action_id)
    if action is None:
        raise _not_found()
    return action


@router.post("/rewind", responses={202: {"model": RewindJob}})
async def create_rewind(
    svc: Svc, body: RewindRequest, request: Request, response: Response
) -> RewindJob:
    if body.requested_by is None:
        body = body.model_copy(update={"requested_by": getattr(request.state, "caller", None)})
    try:
        job = await svc.rewinder.create(body)
    except RewindBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except EmptyScope as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    response.status_code = status.HTTP_200_OK if job.dry_run else status.HTTP_202_ACCEPTED
    return job


@router.get("/rewind")
async def list_rewinds(
    svc: Svc, session_id: Annotated[str | None, Query(pattern=SES_ID)] = None, limit: Limit = 200
) -> list[RewindJob]:
    return await svc.store.list_jobs(session_id=session_id, limit=limit)


@router.get("/rewind/{rewind_job_id}")
async def get_rewind(svc: Svc, rewind_job_id: Annotated[str, Path(pattern=RWD_ID)]) -> RewindJob:
    job = await svc.store.get_job(rewind_job_id)
    if job is None:
        raise _not_found()
    return job


@router.post("/approvals/{action_id}/decision")
async def decide(
    svc: Svc, action_id: Annotated[str, Path(pattern=ACT_ID)], body: ApprovalDecisionRequest
) -> Action:
    try:
        return await svc.hold_manager.on_decision(action_id, body)
    except HoldNotFound as exc:
        raise _not_found() from exc
    except NotHeld as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except WrongCode as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/checksums")
async def checksums(svc: Svc, recompute: bool = False) -> dict[str, SystemChecksum]:
    current = await svc.current_checksums(recompute)
    return {k: SystemChecksum(hash=h, computed_at=t) for k, (h, t) in current.items()}


@router.get("/events")
async def events(
    svc: Svc, last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None
) -> EventSourceResponse:
    async def stream() -> AsyncIterator[dict[str, Any]]:
        async for envelope in svc.bus.subscribe(last_event_id):
            event = envelope.root
            yield {
                "id": str(event.seq),
                "event": event.event.value,
                "data": event.model_dump_json(),
            }

    return EventSourceResponse(stream(), ping=PING_SECONDS)


@router.post("/demo/reset", dependencies=[Depends(require_dashboard)])
async def demo_reset(svc: Svc) -> DemoResetResult:
    if not svc.settings.demo:
        raise _not_found()
    if svc.rewinder.running:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="a rewind is running")
    for reset in svc.demo_resets:
        await reset()
    await svc.store.clear()
    current = await svc.current_checksums(recompute=True)
    return DemoResetResult(
        ok=True,
        checksums={k: SystemChecksum(hash=h, computed_at=t) for k, (h, t) in current.items()},
    )
