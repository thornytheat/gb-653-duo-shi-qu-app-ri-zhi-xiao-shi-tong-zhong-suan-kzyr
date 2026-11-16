from . import core
from .state import EventStore


def execute(payload: dict | None = None, store: EventStore | None = None) -> dict:
    """无状态样例入口（向后兼容）。"""
    return core.run(payload or core.SAMPLE)


def import_batch(payload: dict, store: EventStore) -> dict:
    return core.import_batch(store, payload or {})


def switch_contract(payload: dict, store: EventStore) -> dict:
    return store.switch_contract(
        service=str(payload["service"]),
        kind=str(payload.get("kind", "instance")),
        expected_version=payload.get("expected_version"),
        note=str(payload.get("note", "")),
    )


def rollback_contract(payload: dict, store: EventStore) -> dict:
    return store.rollback_contract(
        service=str(payload["service"]),
        target_version=payload.get("target_version"),
        expected_version=payload.get("expected_version"),
    )


def batch_detail(batch_id: str, store: EventStore) -> dict:
    return core.get_batch_detail(store, batch_id)


def overview(store: EventStore, service: str | None = None) -> dict:
    return core.overview(store, service)
