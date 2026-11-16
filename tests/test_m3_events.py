import json

import pytest

from app.errors import DomainError
from app.modules.m3_events.core import (
    SAMPLE,
    dedupe,
    identity_key,
    import_batch,
    run,
)
from app.modules.m3_events.state import INSTANCE, LEGACY


# ---------------- 原有无状态行为（保持兼容） ----------------
def test_trap_1():  # TRAP-07, TRAP-16
    assert len(dedupe([{"request_id": "a", "v": 1}, {"request_id": "a", "v": 2}])["conflict"]) == 1


def test_trap_2():
    assert len(dedupe([{"request_id": "a"}, {"request_id": "a"}])["duplicate"]) == 1


def test_first_kept():
    assert dedupe([{"request_id": "a", "v": 1}, {"request_id": "a", "v": 2}])["accepted"][0]["v"] == 1


def test_large_id():
    assert dedupe([{"request_id": "9007199254740993"}])["accepted"][0]["request_id"] == "9007199254740993"


def test_empty():
    assert dedupe([])["accepted"] == []


def test_two_keys():
    assert len(dedupe([{"request_id": "a"}, {"request_id": "b"}])["accepted"]) == 2


def test_sample_conflict():
    assert run({})["reason"] == "CONFLICT"


def test_input_counts_all():
    assert run({})["input_count"] == 4


# ---------------- 契约身份 ----------------
def test_identity_legacy_ignores_instance():
    k1 = identity_key({"request_id": "r1", "instance_id": "i-1"}, LEGACY, "pay")
    k2 = identity_key({"request_id": "r1", "instance_id": "i-2"}, LEGACY, "pay")
    assert k1 == k2 == "legacy|r1"


def test_identity_instance_includes_instance_and_service():
    k1 = identity_key({"request_id": "r1", "instance_id": "i-1"}, INSTANCE, "pay")
    k2 = identity_key({"request_id": "r1", "instance_id": "i-2"}, INSTANCE, "pay")
    k3 = identity_key({"request_id": "r1", "instance_id": "i-1"}, INSTANCE, "order")
    assert len({k1, k2, k3}) == 3


def test_identity_instance_missing_quarantines():
    with pytest.raises(DomainError) as exc:
        identity_key({"request_id": "r1"}, INSTANCE, "pay")
    assert exc.value.code == "MISSING_INSTANCE"


def test_dedupe_instance_contract_separates_instances():
    events = [
        {"request_id": "r1", "instance_id": "i-1", "service": "pay"},
        {"request_id": "r1", "instance_id": "i-2", "service": "pay"},
    ]
    out = dedupe(events, {"kind": INSTANCE, "service": "pay"})
    assert len(out["accepted"]) == 2
    assert out["conflict"] == [] and out["duplicate"] == []


def test_dedupe_instance_contract_missing_isolated():
    out = dedupe([{"request_id": "r1", "service": "pay"}], {"kind": INSTANCE, "service": "pay"})
    assert out["accepted"] == []
    assert out["quarantined"][0]["reason"] == "MISSING_INSTANCE"


# ---------------- 批次导入：旧契约 / 新契约 ----------------
def _jsonl(rows):
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)


def test_legacy_batch_keeps_inprocess_identity(event_store):
    text = _jsonl([
        {"request_id": "r1", "instance_id": "i-1", "service": "pay", "status": 200},
        {"request_id": "r1", "instance_id": "i-2", "service": "pay", "status": 500},
        {"request_id": "r1", "instance_id": "i-1", "service": "pay", "status": 200},
    ])
    out = import_batch(event_store, {"service": "pay", "text": text})
    assert out["contract_kind"] == LEGACY and out["contract_version"] == 0
    assert out["ledger_in_batch"] == 1
    assert out["duplicates"] == 1 and out["conflict_count"] == 1
    assert "DUPLICATE_CONFLICT" in out["reasons"]
    # 守恒：输入 = 入账 + 重复 + 冲突 + 隔离
    assert out["input_count"] == (
        out["valid_count"] + out["duplicates"] + out["conflict_count"] + out["quarantine_count"]
    )


def test_switch_to_instance_then_missing_instance_isolated(event_store):
    event_store.switch_contract("pay", INSTANCE, None, "新批次按实例隔离")
    text = _jsonl([
        {"request_id": "r1", "service": "pay"},  # 缺 instance_id
        {"request_id": "r1", "instance_id": "i-1", "service": "pay"},
        {"request_id": "r1", "instance_id": "i-2", "service": "pay"},
    ])
    out = import_batch(event_store, {"service": "pay", "text": text, "expected_version": 1})
    assert out["contract_version"] == 1 and out["contract_kind"] == INSTANCE
    assert out["ledger_in_batch"] == 2
    assert out["quarantine_count"] == 1
    assert out["reasons"].get("MISSING_INSTANCE") == 1


def test_contracts_are_per_service(event_store):
    event_store.switch_contract("pay", INSTANCE, None, "")
    with event_store.connect() as db:
        order = event_store.get_contract(db, "order")
        pay_now = event_store.get_contract(db, "pay")
    assert order == {"service": "order", "version": 0, "kind": LEGACY, "note": "默认实例内编号契约"}
    assert pay_now["version"] == 1 and pay_now["kind"] == INSTANCE


# ---------------- 并发版本保护 ----------------
def test_stale_switch_rejected(event_store):
    event_store.switch_contract("pay", INSTANCE, None, "")
    with pytest.raises(DomainError) as exc:
        event_store.switch_contract("pay", LEGACY, expected_version=0, note="")
    assert exc.value.code == "STALE_VERSION"
    with event_store.connect() as db:
        assert event_store.get_contract(db, "pay")["version"] == 1


def test_stale_import_rejected_without_side_effect(event_store):
    event_store.switch_contract("pay", INSTANCE, None, "")
    before = event_store.list_batches("pay")
    with pytest.raises(DomainError) as exc:
        import_batch(
            event_store,
            {"service": "pay", "events": [{"request_id": "r1"}], "expected_version": 0},
        )
    assert exc.value.code == "STALE_VERSION"
    assert event_store.list_batches("pay") == before
    assert event_store.ledger_count("pay") == 0


def test_stale_retry_against_batch_binding_rejected(event_store):
    out = import_batch(event_store, {"service": "pay", "events": [{"request_id": "r1"}]})
    event_store.switch_contract("pay", INSTANCE, None, "")
    with pytest.raises(DomainError) as exc:
        import_batch(
            event_store,
            {"service": "pay", "batch_id": out["batch_id"],
             "events": [{"request_id": "r1"}], "expected_version": 1},
        )
    assert exc.value.code == "STALE_VERSION"


# ---------------- 批次身份冻结 ----------------
def test_old_batch_retry_keeps_original_contract(event_store):
    text = _jsonl([
        {"request_id": "r1", "instance_id": "i-1", "service": "pay", "status": 200},
        {"request_id": "r1", "instance_id": "i-2", "service": "pay", "status": 200},
    ])
    first = import_batch(event_store, {"service": "pay", "text": text})
    event_store.switch_contract("pay", INSTANCE, None, "")
    retry = import_batch(
        event_store, {"service": "pay", "batch_id": first["batch_id"], "text": text}
    )
    assert retry["contract_version"] == first["contract_version"] == 0
    assert retry["contract_kind"] == LEGACY
    assert retry["ledger_in_batch"] == 1  # 旧规则下两个实例同号仍是同一身份
    assert retry["totals"]["accepted"] == 1  # 累计入账不增加


def test_rollback_appends_version_but_never_rewrites_batch(event_store):
    text = _jsonl([{"request_id": "r1", "instance_id": "i-1", "service": "pay"}])
    first = import_batch(event_store, {"service": "pay", "text": text})
    event_store.switch_contract("pay", INSTANCE, None, "")
    rolled = event_store.rollback_contract("pay", target_version=0, expected_version=1)
    assert rolled["version"] == 2 and rolled["kind"] == LEGACY
    with event_store.connect() as db:
        history = event_store.history(db, "pay")
    assert [h["version"] for h in history] == [1, 2]
    detail = import_batch(
        event_store, {"service": "pay", "batch_id": first["batch_id"], "text": text}
    )
    assert detail["contract_version"] == 0  # 已接收批次身份不变


# ---------------- 中断恢复 ----------------
def test_interrupted_import_resumes_from_checkpoint(event_store):
    rows = [{"request_id": f"n{i}", "instance_id": "ix", "service": "pay"} for i in range(5)]
    text = _jsonl(rows)
    part = import_batch(event_store, {"service": "pay", "text": text, "stop_after": 2})
    assert part["batch_status"] == "INTERRUPTED" and part["position"] == 2
    assert part["ledger_in_batch"] == 2 and part["attempts"] == 1
    rest = import_batch(
        event_store, {"service": "pay", "batch_id": part["batch_id"], "text": text}
    )
    assert rest["batch_status"] == "DONE" and rest["position"] == 5
    assert rest["valid_count"] == 3  # 仅续传部分计入本次新入账
    assert rest["ledger_in_batch"] == 5 and rest["resumed"] is True


def test_interruption_honored_after_quarantined_row(event_store):
    # 首行缺实例被隔离，stop_after=1 仍须在检查点停住，不得跳过中断点
    text = _jsonl([
        {"request_id": "r1", "service": "pay"},
        {"request_id": "r2", "instance_id": "i-1", "service": "pay"},
        {"request_id": "r3", "instance_id": "i-1", "service": "pay"},
    ])
    event_store.switch_contract("pay", INSTANCE, None, "")
    part = import_batch(event_store, {"service": "pay", "text": text, "stop_after": 1})
    assert part["batch_status"] == "INTERRUPTED" and part["position"] == 1
    assert part["quarantine_count"] == 1 and part["ledger_in_batch"] == 0
    rest = import_batch(
        event_store, {"service": "pay", "batch_id": part["batch_id"], "text": text}
    )
    assert rest["batch_status"] == "DONE" and rest["position"] == 3
    assert rest["valid_count"] == 2 and rest["ledger_in_batch"] == 2


def test_full_retry_replays_without_double_posting(event_store):
    rows = [{"request_id": f"n{i}", "instance_id": "ix", "service": "pay"} for i in range(4)]
    first = import_batch(event_store, {"service": "pay", "events": rows})
    assert first["ledger_in_batch"] == 4
    retry = import_batch(
        event_store, {"service": "pay", "batch_id": first["batch_id"], "events": rows}
    )
    assert retry["valid_count"] == 0
    assert retry["duplicates"] == 4
    assert retry["ledger_in_batch"] == 4  # 重试不新增已入账事件
    assert retry["totals"]["accepted"] == 4


def test_retry_input_mismatch_rejected(event_store):
    first = import_batch(event_store, {"service": "pay", "events": [{"request_id": "r1"}]})
    with pytest.raises(DomainError) as exc:
        import_batch(
            event_store,
            {"service": "pay", "batch_id": first["batch_id"],
             "events": [{"request_id": "r1"}, {"request_id": "r2"}]},
        )
    assert exc.value.code == "INVALID_VALUE"


def test_empty_request_id_quarantined(event_store):
    out = import_batch(
        event_store,
        {"service": "pay", "events": [{"request_id": ""}, {"request_id": "r2"}]},
    )
    assert out["ledger_in_batch"] == 1 and out["quarantine_count"] == 1
    assert out["reasons"].get("EMPTY_VALUE") == 1


def test_truncated_jsonl_line_quarantined(event_store):
    text = '{"request_id":"r1","service":"pay"}\n{"request_id":"r2"'
    out = import_batch(event_store, {"service": "pay", "text": text})
    assert out["quarantine_count"] == 1 and out["reasons"].get("TRUNCATED") == 1
    assert out["ledger_in_batch"] == 1


# ---------------- HTTP 层 ----------------
def test_api_pages_and_routes(client):
    assert client.get("/events").status_code == 200
    assert client.get("/api/events/contracts").status_code == 200
    assert client.get("/api/events/batches").status_code == 200
    # 无 payload 保持旧的无状态样例行为
    body = client.post("/api/events/dedupe", json={}).json()
    assert body["input_count"] == len(SAMPLE["events"])


def test_api_full_lifecycle(client):
    text = _jsonl([
        {"request_id": "r1", "instance_id": "i-1", "service": "pay"},
        {"request_id": "r1", "instance_id": "i-2", "service": "pay"},
    ])
    first = client.post("/api/events/dedupe", json={"service": "pay", "text": text})
    assert first.status_code == 200 and first.json()["ledger_in_batch"] == 1

    switched = client.post(
        "/api/events/contracts/switch",
        json={"service": "pay", "kind": "instance", "expected_version": 0},
    )
    assert switched.json()["version"] == 1
    stale = client.post(
        "/api/events/contracts/switch",
        json={"service": "pay", "kind": "legacy", "expected_version": 0},
    )
    assert stale.status_code == 422 and stale.json()["error"] == "STALE_VERSION"

    detail = client.get(f"/api/events/batches/{first.json()['batch_id']}")
    assert detail.status_code == 200
    assert detail.json()["contract_version"] == 0


def test_api_rollback_endpoint(client):
    client.post(
        "/api/events/contracts/switch", json={"service": "pay", "kind": "instance"}
    )
    rolled = client.post(
        "/api/events/contracts/rollback",
        json={"service": "pay", "target_version": 0, "expected_version": 1},
    )
    assert rolled.status_code == 200
    assert rolled.json()["kind"] == LEGACY and rolled.json()["version"] == 2


# ---------------- 并发与跨批次 ----------------
def test_concurrent_switch_and_import_only_one_wins(event_store):
    import threading

    outcomes: list[Exception] = []

    def switcher():
        try:
            event_store.switch_contract("pay", INSTANCE, expected_version=0, note="并发切换")
        except DomainError as exc:  # pragma: no cover - 取决于线程调度
            outcomes.append(exc)

    def importer():
        try:
            import_batch(
                event_store,
                {"service": "pay", "events": [{"request_id": "r1"}], "expected_version": 0},
            )
        except DomainError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=switcher), threading.Thread(target=importer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with event_store.connect() as db:
        final = event_store.get_contract(db, "pay")
    # 双方都基于 v0：至多一个 STALE_VERSION，最终契约要么 v0 要么 v1，且无脏写
    assert final["version"] in (0, 1)
    assert all(exc.code == "STALE_VERSION" for exc in outcomes)
    # 若导入落败则零入账；若切换落败则导入按 v0 成功，二者只居其一
    if final["version"] == 1:
        assert event_store.ledger_count("pay") == 0
    else:
        assert event_store.ledger_count("pay") == 1


def test_cross_batch_duplicate_skip_and_conflict(event_store):
    first = import_batch(
        event_store,
        {"service": "pay", "events": [
            {"request_id": "r1", "instance_id": "i-1", "status": 200},
        ]},
    )
    # 同身份同载荷跨批次 -> DUPLICATE_SKIP，不新增入账
    same = import_batch(
        event_store,
        {"service": "pay", "batch_id": "b-other-1", "events": [
            {"request_id": "r1", "instance_id": "i-1", "status": 200},
        ]},
    )
    assert same["valid_count"] == 0 and same["reasons"].get("DUPLICATE_SKIP") == 1
    assert same["ledger_total"] == first["ledger_total"] == 1
    # 同身份不同载荷 -> DUPLICATE_CONFLICT，保留首次入账
    clash = import_batch(
        event_store,
        {"service": "pay", "batch_id": "b-other-2", "events": [
            {"request_id": "r1", "instance_id": "i-1", "status": 500},
        ]},
    )
    assert clash["conflict_count"] == 1 and clash["valid_count"] == 0
    with event_store.connect() as db:
        row = db.execute(
            "SELECT payload FROM m3_ledger WHERE identity_key=?", ("legacy|r1",)
        ).fetchone()
    assert json.loads(row["payload"])["status"] == 200


def test_rollback_to_unknown_version_rejected(event_store):
    event_store.switch_contract("pay", INSTANCE, None, "")
    with pytest.raises(DomainError) as exc:
        event_store.rollback_contract("pay", target_version=99, expected_version=1)
    assert exc.value.code == "INVALID_VALUE"


def test_invalid_contract_kind_rejected(event_store):
    with pytest.raises(DomainError) as exc:
        event_store.switch_contract("pay", "bogus", None, "")
    assert exc.value.code == "INVALID_VALUE"


def test_missing_service_rejected(event_store):
    with pytest.raises(DomainError) as exc:
        import_batch(event_store, {"events": [{"request_id": "r1"}]})
    assert exc.value.code == "EMPTY_VALUE"
