from fastapi import APIRouter, Body, Request
from . import service as m3_service
from .state import EventStore

router = APIRouter(tags=["M3-事件去重"])


def _store(request: Request) -> EventStore:
    return request.app.state.event_store


@router.get("/events")
def page(request: Request, service: str | None = None):
    data = m3_service.overview(_store(request), service)
    return request.app.state.templates.TemplateResponse(
        request, "m3_events.html", {"title": "事件去重", "data": data}
    )


@router.post("/api/events/dedupe")
def api(request: Request, payload: dict = Body(default={})):
    # 无 payload 的样例运行保持旧行为；带 service/batch 的请求进入有状态批次流程
    if not payload or not (payload.get("service") or payload.get("batch_id")):
        return m3_service.execute(payload or None)
    return m3_service.import_batch(payload, _store(request))


@router.get("/api/events/contracts")
def contracts(request: Request, service: str | None = None):
    return {"contracts": m3_service.overview(_store(request), service)["contracts"]}


@router.post("/api/events/contracts/switch")
def switch_contract(request: Request, payload: dict = Body(...)):
    return m3_service.switch_contract(payload, _store(request))


@router.post("/api/events/contracts/rollback")
def rollback_contract(request: Request, payload: dict = Body(...)):
    return m3_service.rollback_contract(payload, _store(request))


@router.get("/api/events/batches")
def batches(request: Request, service: str | None = None):
    return {"batches": m3_service.overview(_store(request), service)["batches"]}


@router.get("/api/events/batches/{batch_id}")
def batch_detail(request: Request, batch_id: str):
    return m3_service.batch_detail(batch_id, _store(request))
