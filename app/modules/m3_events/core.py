"""M3 事件去重核心规则。

采信契约按服务切换：
- legacy（旧批次）：请求号采用实例内编号，身份仅由 ``request_id`` 构成，
  重试沿用原去重身份，避免全量换规则导致重复入账。
- instance（新批次）：身份由 ``service``、``instance_id``、``request_id``
  共同构成；缺少 ``instance_id`` 的事件隔离，绝不与其它实例合并。

批次在导入瞬间绑定当时的契约版本；之后切换或回退契约都不改变已接收
批次的身份。
"""
from __future__ import annotations

import json

from app.errors import DomainError
from app.manifest import canonical_digest
from app.modules.m3_events.state import (
    DONE,
    INSTANCE,
    INTERRUPTED,
    LEGACY,
    EventStore,
)
from app.reasons import reason

SAMPLE = {
    "events": [
        {"request_id": "r1", "status": 200, "timestamp": "2026-11-15T01:00:00Z"},
        {"request_id": "r1", "status": 200, "timestamp": "2026-11-15T01:00:00Z"},
        {"request_id": "r2", "status": 500, "timestamp": "2026-11-15T01:01:00Z"},
        {"request_id": "r2", "status": 200, "timestamp": "2026-11-15T01:02:00Z"},
    ]
}


def identity_key(row: dict, kind: str, service: str) -> str:
    """按契约生成去重身份。身份前缀隔离两种契约，避免新旧键互相覆盖。"""
    request_id = str(row.get("request_id", ""))
    if kind == INSTANCE:
        instance_id = str(row.get("instance_id", "")).strip()
        if not instance_id:
            raise DomainError("MISSING_INSTANCE", reason("MISSING_INSTANCE"))
        return f"{INSTANCE}|{service}|{instance_id}|{request_id}"
    return f"{LEGACY}|{request_id}"


def _same_payload(left: dict, right: dict) -> bool:
    # source_line 等受理时附加的证据字段不参与业务内容比较
    ignored = {"source_line"}
    left_biz = {k: v for k, v in left.items() if k not in ignored}
    right_biz = {k: v for k, v in right.items() if k not in ignored}
    return canonical_digest(left_biz) == canonical_digest(right_biz)


def dedupe(events, contract: dict | None = None):
    """无状态去重，保留原始返回结构（accepted/duplicate/conflict）。

    ``contract`` 形如 ``{"kind": "legacy"|"instance", "version": 1}``，
    缺省沿用实例内编号的 legacy 契约，保证旧调用方行为不变。
    """
    kind = (contract or {}).get("kind", LEGACY)
    service = (contract or {}).get("service", "")
    seen: dict[str, dict] = {}
    duplicate: list[dict] = []
    conflict: list[dict] = []
    quarantined: list[dict] = []
    for row in events:
        try:
            key = identity_key(row, kind, service)
        except DomainError as exc:
            quarantined.append(
                {
                    "source_line": row.get("source_line"),
                    "reason": exc.code,
                    "reason_text": exc.message,
                    "row": row,
                }
            )
            continue
        if key not in seen:
            seen[key] = row
            continue
        if _same_payload(seen[key], row):
            duplicate.append(row)
        else:
            conflict.append(
                {
                    "request_id": str(row.get("request_id", "")),
                    "identity_key": key,
                    "first": seen[key],
                    "other": row,
                    "reason": "DUPLICATE_CONFLICT",
                }
            )
    return {
        "accepted": list(seen.values()),
        "duplicate": duplicate,
        "conflict": conflict,
        "quarantined": quarantined,
    }


def run(payload):
    """无状态样例入口，字段与旧版保持一致。"""
    payload = payload or SAMPLE
    events = payload.get("events", SAMPLE["events"])
    contract = payload.get("contract")
    out = dedupe(events, contract)
    return {
        "status": "READY",
        "input_count": len(events),
        "valid_count": len(out["accepted"]),
        "quarantine_count": len(out["conflict"]) + len(out["quarantined"]),
        "duplicates": len(out["duplicate"]),
        "reason": "CONFLICT" if out["conflict"] else "OK",
    }


# ----------------------------- 有状态批次 -----------------------------


def parse_events(payload: dict) -> tuple[str, list[dict]]:
    """支持 events 数组或 JSONL 文本两种真实批次输入。"""
    service = str(payload.get("service", "")).strip()
    if not service:
        raise DomainError("EMPTY_VALUE", reason("EMPTY_VALUE") + ": service")
    events = payload.get("events")
    text = payload.get("text")
    rows: list[dict] = []
    if events is not None:
        if not isinstance(events, list):
            raise DomainError("INVALID_VALUE", "events 必须是数组")
        rows = list(events)
    elif text is not None:
        for line_no, line in enumerate(str(text).splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                rows.append({"__parse_error__": "TRUNCATED", "source_line": line_no, "raw": line})
                continue
            if not isinstance(row, dict):
                rows.append({"__parse_error__": "INVALID_VALUE", "source_line": line_no, "raw": line})
                continue
            row["source_line"] = line_no
            rows.append(row)
    else:
        rows = list(SAMPLE["events"])
    return service, rows


def _add_finding(db, batch_id: str, source_line, code: str, detail: dict) -> None:
    db.execute(
        "INSERT INTO m3_findings(batch_id, source_line, reason_code, detail) VALUES(?,?,?,?)",
        (batch_id, source_line, code, json.dumps(detail, ensure_ascii=False, sort_keys=True)),
    )


def import_batch(store: EventStore, payload: dict) -> dict:
    """导入（或断点续传）一个真实批次。

    重试同一 ``batch_id`` 不新增已入账事件：已入账身份命中唯一索引，
    完全相同的载荷计为 DUPLICATE_RETRY/DUPLICATE_SKIP，不同载荷计
    DUPLICATE_CONFLICT。所有计数随检查点落库，中断后按原进度恢复。
    """
    service, rows = parse_events(payload)
    stop_after = payload.get("stop_after")
    with store._lock, store.connect() as db:
        batch_id = payload.get("batch_id")
        batch = store.get_batch(db, batch_id) if batch_id else None
        if batch is None:
            contract = store.get_contract(db, service)
            expected = payload.get("expected_version")
            if expected is not None and int(expected) != contract["version"]:
                raise DomainError(
                    "STALE_VERSION",
                    f"{reason('STALE_VERSION')}: 期望版本 {expected}，当前版本 {contract['version']}",
                )
            digest = canonical_digest({"service": service, "events": rows})
            batch_id = batch_id or f"batch-{digest[:12]}"
            if store.get_batch(db, batch_id) is not None:
                raise DomainError("INVALID_VALUE", f"批次编号已存在: {batch_id}")
            batch = store.create_batch(db, batch_id, service, contract, len(rows))
        else:
            if batch["service"] != service:
                raise DomainError(
                    "INVALID_VALUE",
                    f"批次 {batch_id} 绑定服务 {batch['service']}，与本次服务 {service} 不一致",
                )
            expected = payload.get("expected_version")
            if expected is not None and int(expected) != batch["contract_version"]:
                raise DomainError(
                    "STALE_VERSION",
                    f"{reason('STALE_VERSION')}: 批次绑定版本 {batch['contract_version']}，"
                    f"提交版本 {expected}",
                )
            if batch["total"] != len(rows):
                raise DomainError(
                    "INVALID_VALUE",
                    f"重试批次必须提交相同输入（原 {batch['total']} 条，本次 {len(rows)} 条）",
                )
        attempt_no = store.start_attempt(db, batch_id)
        kind = batch["contract_kind"]
        # 仅中断批次从检查点续传；已完成批次的重试整批重放，逐行复核且不重复入账
        resume_from = batch["position"] if batch["status"] == INTERRUPTED else 0
        position = resume_from
        attempt_counters = {"accepted": 0, "duplicate": 0, "conflict": 0, "quarantined": 0}
        totals = {
            "accepted": batch["accepted"],
            "duplicate": batch["duplicate"],
            "conflict": batch["conflict"],
            "quarantined": batch["quarantined"],
        }
        interrupted = False
        for index in range(resume_from, len(rows)):
            row = rows[index]
            line_no = row.get("source_line", index + 1)
            if "__parse_error__" in row:
                _add_finding(
                    db, batch_id, line_no, row["__parse_error__"],
                    {"attempt_no": attempt_no, "raw": row.get("raw", "")},
                )
                attempt_counters["quarantined"] += 1
                totals["quarantined"] += 1
            else:
                request_id = str(row.get("request_id", "")).strip()
                if not request_id:
                    _add_finding(
                        db, batch_id, line_no, "EMPTY_VALUE",
                        {"attempt_no": attempt_no, "row": row},
                    )
                    attempt_counters["quarantined"] += 1
                    totals["quarantined"] += 1
                else:
                    try:
                        key = identity_key(row, kind, service)
                    except DomainError as exc:
                        _add_finding(
                            db, batch_id, line_no, exc.code,
                            {"attempt_no": attempt_no, "row": row},
                        )
                        attempt_counters["quarantined"] += 1
                        totals["quarantined"] += 1
                    else:
                        existing = db.execute(
                            "SELECT batch_id, payload, source_line FROM m3_ledger "
                            "WHERE service=? AND identity_key=?",
                            (service, key),
                        ).fetchone()
                        if existing is None:
                            db.execute(
                                "INSERT INTO m3_ledger(service, identity_key, batch_id, payload, source_line) "
                                "VALUES(?,?,?,?,?)",
                                (service, key, batch_id,
                                 json.dumps(row, ensure_ascii=False, sort_keys=True), line_no),
                            )
                            attempt_counters["accepted"] += 1
                            totals["accepted"] += 1
                        else:
                            prior_payload = json.loads(existing["payload"])
                            if _same_payload(prior_payload, row):
                                code = (
                                    "DUPLICATE_RETRY"
                                    if existing["batch_id"] == batch_id
                                    else "DUPLICATE_SKIP"
                                )
                                _add_finding(
                                    db, batch_id, line_no, code,
                                    {
                                        "attempt_no": attempt_no,
                                        "identity_key": key,
                                        "first_batch": existing["batch_id"],
                                    },
                                )
                                attempt_counters["duplicate"] += 1
                                totals["duplicate"] += 1
                            else:
                                _add_finding(
                                    db, batch_id, line_no, "DUPLICATE_CONFLICT",
                                    {
                                        "attempt_no": attempt_no,
                                        "identity_key": key,
                                        "first": prior_payload,
                                        "other": row,
                                        "first_batch": existing["batch_id"],
                                    },
                                )
                                attempt_counters["conflict"] += 1
                                totals["conflict"] += 1
            position = index + 1
            # 中断演练：无论该行裁决结果如何，处理到第 stop_after 行即落检查点
            if stop_after is not None and (index + 1) >= int(stop_after) and index + 1 < len(rows):
                interrupted = True
                break
        status = INTERRUPTED if interrupted else DONE
        store.finish_attempt(db, batch_id, attempt_no, attempt_counters)
        store.touch_batch(db, batch_id, position, status, totals)
        fresh = store.get_batch(db, batch_id)
        findings = store.findings(batch_id, db)
        return _batch_summary(
            store, db, fresh, findings, attempt_no, attempt_counters, resume_from > 0
        )


def _batch_summary(
    store: EventStore,
    db,
    batch: dict,
    findings: list[dict],
    attempt_no: int | None = None,
    attempt_counters: dict | None = None,
    resumed: bool = False,
) -> dict:
    ledger_total = db.execute(
        "SELECT COUNT(*) FROM m3_ledger WHERE service=?", (batch["service"],)
    ).fetchone()[0]
    ledger_in_batch = db.execute(
        "SELECT COUNT(*) FROM m3_ledger WHERE batch_id=?", (batch["batch_id"],)
    ).fetchone()[0]
    attempts = store.attempt_count(db, batch["batch_id"])
    if attempt_no is None:
        # 批次详情视角：展示累计口径
        attempt_no = attempts
        attempt_counters = {
            "accepted": batch["accepted"],
            "duplicate": batch["duplicate"],
            "conflict": batch["conflict"],
            "quarantined": batch["quarantined"],
        }
    by_reason: dict[str, int] = {}
    for item in findings:
        if item["detail"].get("attempt_no") in (None, attempt_no):
            by_reason[item["reason"]] = by_reason.get(item["reason"], 0) + 1
    return {
        "status": "READY",
        "batch_id": batch["batch_id"],
        "service": batch["service"],
        "contract_version": batch["contract_version"],
        "contract_kind": batch["contract_kind"],
        "batch_status": batch["status"],
        "attempt_no": attempt_no,
        "attempts": attempts,
        "resumed": resumed or attempts > 1,
        "input_count": batch["total"],
        "position": batch["position"],
        # 本次尝试口径：有效=本次新入账，其余为本次裁决
        "valid_count": attempt_counters["accepted"],
        "duplicates": attempt_counters["duplicate"],
        "conflict_count": attempt_counters["conflict"],
        "quarantine_count": attempt_counters["quarantined"],
        # 批次累计口径（跨所有尝试）
        "totals": {
            "accepted": batch["accepted"],
            "duplicate": batch["duplicate"],
            "conflict": batch["conflict"],
            "quarantined": batch["quarantined"],
        },
        "reasons": by_reason,
        "reason_text": {code: reason(code) for code in by_reason},
        "findings": [f for f in findings if f["detail"].get("attempt_no") in (None, attempt_no)],
        "ledger_total": ledger_total,
        "ledger_in_batch": ledger_in_batch,
        "reason": "CONFLICT" if attempt_counters["conflict"] else (
            "INTERRUPTED" if batch["status"] == INTERRUPTED else "OK"
        ),
    }


def get_batch_detail(store: EventStore, batch_id: str) -> dict:
    with store._lock, store.connect() as db:
        batch = store.get_batch(db, batch_id)
        if batch is None:
            raise DomainError("INVALID_VALUE", f"批次不存在: {batch_id}")
        latest = store.attempt_count(db, batch_id)
        attempt = store.get_attempt(db, batch_id, latest)
        attempt_counters = {
            "accepted": attempt.get("accepted", batch["accepted"]),
            "duplicate": attempt.get("duplicate", batch["duplicate"]),
            "conflict": attempt.get("conflict", batch["conflict"]),
            "quarantined": attempt.get("quarantined", batch["quarantined"]),
        }
        return _batch_summary(
            store, db, batch, store.findings(batch_id, db), latest, attempt_counters
        )


def overview(store: EventStore, service: str | None = None) -> dict:
    with store._lock, store.connect() as db:
        if service:
            contracts = [store.get_contract(db, service)]
        else:
            contracts = store.list_contracts(db)
        if service:
            batch_rows = db.execute(
                "SELECT * FROM m3_batches WHERE service=? ORDER BY created_at", (service,)
            ).fetchall()
        else:
            batch_rows = db.execute("SELECT * FROM m3_batches ORDER BY created_at").fetchall()
        batches = [dict(r) for r in batch_rows]
        if service:
            ledger_total = db.execute(
                "SELECT COUNT(*) FROM m3_ledger WHERE service=?", (service,)
            ).fetchone()[0]
        else:
            ledger_total = db.execute("SELECT COUNT(*) FROM m3_ledger").fetchone()[0]
        agg = db.execute(
            "SELECT COALESCE(SUM(total),0) AS inputs, "
            "COALESCE(SUM(quarantined),0) AS quarantined, "
            "COALESCE(SUM(CASE WHEN status=? THEN 1 ELSE 0 END),0) AS interrupted "
            "FROM m3_batches" + (" WHERE service=?" if service else ""),
            ([INTERRUPTED, service] if service else [INTERRUPTED]),
        ).fetchone()
    return {
        "status": "READY",
        "input_count": agg["inputs"],
        "valid_count": ledger_total,
        "quarantine_count": agg["quarantined"],
        "interrupted_batches": agg["interrupted"],
        "contracts": contracts,
        "batches": [
            {
                "batch_id": b["batch_id"],
                "service": b["service"],
                "contract_version": b["contract_version"],
                "contract_kind": b["contract_kind"],
                "batch_status": b["status"],
                "total": b["total"],
                "position": b["position"],
            }
            for b in batches
        ],
        "ledger_total": ledger_total,
        "reason": "OK",
    }
