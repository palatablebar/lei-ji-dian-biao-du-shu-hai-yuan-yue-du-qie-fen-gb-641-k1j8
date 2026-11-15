"""正式账与更正编排：关账发布 → 受理更正 → 差额调入开放账期 → 原子发布。

- 发布是原子动作：版本、账单、分项、四账、指针在同一事务里落库；
  中途失败整体回滚，当前版本不动，失败尝试单独留痕（corrections.status='failed'）。
- 更正是幂等的：同一更正内容算出同一个 correction_id，重跑直接返回首次结果，
  不新增版本、不重复调账；结果与当前版本一致的更正记 noop，也不新增版本。
"""
from __future__ import annotations

import json
from decimal import Decimal

from ... import errors
from ..m2_contract import contract as contract_mod
from ..m2_contract import service as m2_service
from ..m3_quality import service as m3_service
from ..m4_intervals import restore as restore_mod
from ..m5_months import allocate as allocate_mod
from ..m5_months import weights as weights_mod
from ..m6_batches import history
from ..m7_export import csv_out
from . import correct, evidence

D0 = Decimal("0")


# ---------- 关账发布（创世版本） ----------

def publish_genesis(settings, store, batch_id: str | None = None) -> dict:
    batch = store.get_batch(batch_id) if batch_id else store.latest_batch("done")
    if batch is None or batch["status"] != "done":
        raise errors.NotFound("还没有可发布的完成批次", code="E_BATCH_NOT_FOUND")
    existing = store.find_genesis(batch["batch_id"])
    if existing is not None:
        return _genesis_payload(store, existing, idempotent=True)
    current = store.current_version()
    if current is not None and store.lineage_has_corrections(current["base_batch_id"]):
        raise errors.Conflict(
            f"正式账已绑定批次 {current['base_batch_id']} 且已有更正入账，不能更换基准批次",
            code="E_LEDGER_BOUND")

    bills: list[dict] = []
    lines: list[dict] = []
    for bill in store.load_bills(batch["batch_id"]):
        bills.append({"month": bill["month"], "meter_id": bill["meter_id"],
                      "usage_kwh": bill["usage_kwh"], "amount_yuan": bill["amount_yuan"],
                      "kind": "issued", "adjust_cents": bill["adjust_cents"]})
        lines.extend(correct.usage_lines(bill["month"], bill["meter_id"], bill))
    if not bills:
        raise errors.UnprocessableInput(
            f"批次 {batch['batch_id']} 没有账单，不能发布为正式账",
            code="E_LEDGER_EMPTY")
    totals = store.load_totals(batch["batch_id"])
    base_reconcile = json.loads(totals.get("reconcile_json") or "{}")
    accounts = [
        {**acc, "left": Decimal(str(acc["left"])), "right": Decimal(str(acc["right"])),
         "diff": Decimal(str(acc["diff"]))}
        for acc in base_reconcile.get("accounts", [])
    ]
    accounts.append({
        "key": "adjustments", "label": "调账账", "left": D0, "right": D0,
        "diff": D0, "unit": "元", "balanced": True,
        "note": "关账版本无调账"})
    reconcile = {"accounts": accounts,
                 "all_balanced": base_reconcile.get("all_balanced"),
                 "diff_count": base_reconcile.get("diff_count", 0),
                 "checked_at": None}
    version_no = store.next_version_no()
    version = {
        "version_id": f"lv-{version_no:04d}",
        "version_no": version_no,
        "base_batch_id": batch["batch_id"],
        "correction_id": None,
        "kind": "genesis",
        "status": "published",
        "digest": correct.version_digest(bills, lines),
        "posted_month": None,
        "note": f"关账发布 {batch['batch_id']}",
        "created_at": history.iso(history.now_utc()),
    }
    store.publish_version(version=version, bills=bills, lines=lines,
                          reconcile=reconcile)
    return _genesis_payload(store, store.get_version(version["version_id"]),
                            idempotent=False)


def _genesis_payload(store, version: dict, idempotent: bool) -> dict:
    return {
        "status": "published",
        "idempotent_replay": idempotent,
        "version": _version_brief(version),
        "reconcile": evidence.reconcile_payload(
            store.load_ledger_reconcile(version["version_id"])),
        "counts": store.count_ledger(),
    }


def _version_brief(version: dict | None) -> dict | None:
    if version is None:
        return None
    return {"version_id": version["version_id"], "version_no": version["version_no"],
            "kind": version["kind"], "base_batch_id": version["base_batch_id"],
            "correction_id": version["correction_id"], "digest": version["digest"],
            "posted_month": version["posted_month"], "note": version["note"],
            "created_at": version["created_at"]}


# ---------- 受理更正 ----------

def _validate_posted_month(posted_month: str, max_issued: str) -> None:
    try:
        year, month = int(posted_month[:4]), int(posted_month[5:7])
        ok = len(posted_month) == 7 and posted_month[4] == "-" and 1 <= month <= 12 \
            and year > 0
    except (ValueError, IndexError):
        ok = False
    if not ok:
        raise errors.BadRequest(f"入账月份格式应为 YYYY-MM：{posted_month!r}")
    if posted_month <= max_issued:
        raise errors.Conflict(
            f"入账月份 {posted_month} 已关账（已发账到 {max_issued}），"
            f"差额只能进开放账期", code="E_POSTED_MONTH_CLOSED")


def _build_quality(base_records: list[dict], correction_rows: list[dict], contracts):
    """有效集 + 质量评估：被隔离的更正行不许顶替原行，逐轮重建直到稳定。"""
    active = list(correction_rows)
    while True:
        records, supersedes, displaced_base, displaced_cor = \
            correct.effective_records(base_records, active)
        quality = m3_service.evaluate(records, contracts)
        dropped = {row["lineno"]: row["code"] for row in quality["quarantine"]}
        displaced_dropped = [
            row for row in active
            if row["_lineno"] in dropped
            and (row["correction_id"], row["seq"]) in supersedes
        ]
        if not displaced_dropped:
            stats = {"displaced_base": displaced_base,
                     "displaced_cor": displaced_cor}
            return quality, supersedes, dropped, stats
        drop_ids = {row["_lineno"] for row in displaced_dropped}
        for row in active:
            if row["_lineno"] in drop_ids:
                row["_dropped_code"] = dropped[row["_lineno"]]
        active = [row for row in active if row["_lineno"] not in drop_ids]


def apply_correction(settings, store, payload: dict) -> dict:
    current = store.current_version()
    if current is None:
        raise errors.Conflict("还没有已发布的正式账，先把批次发布入账",
                              code="E_LEDGER_NOT_PUBLISHED")
    items = payload.get("items") or []
    reason = str(payload.get("reason") or "").strip()
    if not items or not reason:
        raise errors.BadRequest("更正行与更正原因都不能为空", code="E_CORRECTION_EMPTY")
    ticket = str(payload.get("ticket") or "").strip()
    base_batch_id = current["base_batch_id"]
    issued_bills = store.load_ledger_bills(current["version_id"])
    issued_lines = store.load_ledger_lines(current["version_id"])
    max_issued = max(bill["month"] for bill in issued_bills)
    requested_posted = str(payload.get("posted_month") or "").strip()
    posted_month = requested_posted or weights_mod.shift_month(max_issued, 1)
    _validate_posted_month(posted_month, max_issued)

    # 更正身份用「请求里的入账月」（可能为空），不随版本推进漂移：
    # 同一 payload 永远命中同一个 correction_id，重跑必然幂等。
    cid = correct.correction_id_for(base_batch_id, requested_posted, reason,
                                    ticket, items)
    existing = store.get_correction(cid)
    if existing is not None and existing["status"] in ("applied", "noop"):
        return _correction_payload(store, existing, idempotent=True)

    contracts = contract_mod.load(settings)
    prices = contract_mod.load_prices(settings)
    records, q_interpret, _ = m2_service.interpret_rows(
        correct.item_rows(items), contracts)
    if not records:
        raise errors.UnprocessableInput(
            "更正行全部无法解析，未入账", code="E_CORRECTION_EMPTY",
            detail={"codes": [row["code"] for row in q_interpret]})
    record_by_lineno = {row["lineno"]: row for row in records}
    quar_by_lineno = {row["lineno"]: row for row in q_interpret}

    new_rows: list[dict] = []
    for index, item in enumerate(items, start=1):
        lineno = 10000 + index
        record = record_by_lineno.get(lineno)
        quar = quar_by_lineno.get(lineno)
        row = {
            "correction_id": cid, "seq": index, "_lineno": lineno,
            "meter_id": str(item.get("meter_id") or "").strip(),
            "register": (record["register"] if record
                         else str(item.get("register") or "").strip().upper()),
            "read_at": (record["read_at"] if record
                        else str(item.get("read_at") or "").strip()),
            "reading_text": (record["reading_text"] if record
                             else str(item.get("reading") or "").strip()),
            "unit_raw": record["unit_raw"] if record else str(item.get("unit") or ""),
            "reading_norm": record["reading"] if record else None,
            "unit_norm": record["unit_norm"] if record else None,
            "read_type": record["read_type"] if record else "N",
            "status": ("estimate" if record and record["read_type"] == "E"
                       else "valid" if record else "quarantine"),
            "code": quar["code"] if quar else None,
            "note": str(item.get("note") or "").strip(),
        }
        new_rows.append(row)

    base_records = store.load_readings(base_batch_id)
    prior_rows = store.applied_correction_readings(base_batch_id)
    for order, row in enumerate(prior_rows, start=1):
        row["_lineno"] = 20000 + order
    valid_new = [row for row in new_rows if row["status"] in ("valid", "estimate")]

    quality_prev, _, _, _ = _build_quality(base_records, prior_rows, contracts)
    quality_next, supersedes, dropped, stats = _build_quality(
        base_records, prior_rows + valid_new, contracts)
    for row in new_rows:
        if row["status"] == "quarantine":
            continue
        code = row.pop("_dropped_code", None) or dropped.get(row["_lineno"])
        if code:
            row["status"] = "quarantine"
            row["code"] = code
    for row in new_rows:
        key = (row["correction_id"], row["seq"])
        if row["status"] in ("valid", "estimate") and key in supersedes:
            row["supersedes"] = supersedes[key]

    prev_intervals = restore_mod.build_intervals(
        quality_prev["valid"], quality_prev["estimate"], contracts)
    next_intervals = restore_mod.build_intervals(
        quality_next["valid"], quality_next["estimate"], contracts)
    next_alloc = allocate_mod.allocate(next_intervals, contracts, prices)
    interval_changes = evidence.diff_intervals(prev_intervals, next_intervals,
                                               contracts)

    closed_months = {bill["month"] for bill in issued_bills}
    # 已入账基数 = 当前版本的全部分项：历史调账随版本结转，当前版本里每条恰好一份，
    # 跨版本求和会把结转件重复计数。
    booked = [line for line in issued_lines if line["line_kind"] == "adjustment"]
    bill_changes, adjustments = correct.build_bill_changes(
        next_alloc["bills"], issued_bills, issued_lines, booked, closed_months, prices)
    for item in adjustments:
        item["correction_id"] = cid
        item["month"] = posted_month
    bills, lines = correct.compose_version(
        issued_bills, issued_lines, next_alloc["bills"], adjustments,
        posted_month, closed_months)

    base_totals = store.load_totals(base_batch_id)
    n_corr_total = store.lineage_correction_row_count(base_batch_id) + len(new_rows)
    n_corr_eff = sum(1 for row in quality_next["valid"] + quality_next["estimate"]
                     if row["lineno"] >= 10000)
    cq_all = n_corr_total - n_corr_eff - stats["displaced_cor"]
    bq = sum(1 for row in quality_next["quarantine"] if row["lineno"] < 10000)
    interval_stats = restore_mod.totals(next_intervals, contracts)
    reconcile = correct.build_version_reconcile(
        raw_lines=int(base_totals["raw_lines"]) + n_corr_total,
        valid=len(quality_next["valid"]), estimate=len(quality_next["estimate"]),
        quarantine=int(base_totals["quarantine"]) + cq_all + bq,
        blank=int(base_totals["blank"]),
        superseded=stats["displaced_base"] + stats["displaced_cor"],
        interval_usage=interval_stats["primary_usage_kwh"],
        month_usage=next_alloc["stats"]["usage_total"],
        tou_before=next_alloc["stats"]["tou_residual_before"],
        tou_after=next_alloc["stats"]["tou_residual_after"],
        tou_adjustments=next_alloc["stats"]["tou_adjustments"],
        bills=bills, lines=lines, adjustments=adjustments,
        bill_changes=bill_changes)
    digest = correct.version_digest(bills, lines)

    affected = sorted({item["meter_id"] for item in interval_changes}
                      | {item["meter_id"] for item in bill_changes})
    now = history.iso(history.now_utc())
    correction = {"correction_id": cid, "base_batch_id": base_batch_id,
                  "reason": reason, "ticket": ticket, "posted_month": posted_month,
                  "created_at": now}
    review = {
        "posted_month": posted_month,
        "affected_meters": affected,
        "readings": [evidence.reading_row(row) for row in new_rows],
        "interval_changes": interval_changes,
        "bill_changes": [evidence.bill_change_row(item) for item in bill_changes],
        "adjustments": [evidence.adjustment_row(item) for item in adjustments],
        "reconcile": evidence.reconcile_payload(reconcile),
    }
    readings_rows = [{key: value for key, value in row.items() if key != "_lineno"}
                     for row in new_rows]

    if digest == current["digest"]:
        correction.update(status="noop", note="结果与当前版本一致，未产生新版本",
                          payload={**review, "version": None})
        store.save_correction_attempt(correction, readings_rows)
        stored = store.get_correction(cid)
        return _correction_payload(store, stored, idempotent=False)

    if not reconcile["all_balanced"]:
        correction.update(status="failed", note="更正后四账不平，未入账",
                          payload={**review, "version": None})
        store.save_correction_attempt(correction, readings_rows)
        raise errors.Conflict(
            "更正后四账不平，未入账；当前正式账保持上一版本",
            code="E_RECONCILE_UNBALANCED",
            detail={"correction_id": cid,
                    "diffs": [acc["key"] for acc in reconcile["accounts"]
                              if not acc["balanced"]]})

    version_no = store.next_version_no()
    version = {
        "version_id": f"lv-{version_no:04d}", "version_no": version_no,
        "base_batch_id": base_batch_id, "correction_id": cid, "kind": "correction",
        "status": "published", "digest": digest, "posted_month": posted_month,
        "note": reason, "created_at": now,
    }
    try:
        store.publish_version(
            version=version, bills=bills, lines=lines, reconcile=reconcile,
            correction={**correction, "status": "applied",
                        "result_version_id": version["version_id"],
                        "payload": {**review,
                                    "version": _version_brief(version)}},
            correction_readings=readings_rows)
    except Exception as exc:
        store.save_correction_attempt(
            {**correction, "status": "failed",
             "note": f"发布失败已回滚：{exc}",
             "payload": {**review, "version": None}}, readings_rows)
        raise
    stored = store.get_correction(cid)
    return _correction_payload(store, stored, idempotent=False)


def _correction_payload(store, row: dict, idempotent: bool) -> dict:
    review = json.loads(row["payload_json"] or "{}")
    version = (store.get_version(row["result_version_id"])
               if row.get("result_version_id") else None)
    return {
        "status": row["status"],
        "idempotent_replay": idempotent,
        "correction": {
            "correction_id": row["correction_id"], "reason": row["reason"],
            "ticket": row["ticket"], "posted_month": row["posted_month"],
            "created_at": row["created_at"], "note": row["note"],
        },
        "version": _version_brief(version),
        **review,
        "counts": store.count_ledger(),
    }


# ---------- 查询 ----------

def ledger_payload(store, version_id: str | None = None) -> dict:
    current = store.current_version()
    version = store.get_version(version_id) if version_id else current
    if version is None:
        if version_id:
            raise errors.NotFound(f"账版本 {version_id} 不存在",
                                  code="E_VERSION_NOT_FOUND")
        return {"version": None, "is_current": False, "bills": [],
                "reconcile": None, "counts": store.count_ledger()}
    bills = store.load_ledger_bills(version["version_id"])
    lines = store.load_ledger_lines(version["version_id"])
    lines_by_key: dict[tuple, list] = {}
    for line in lines:
        lines_by_key.setdefault((line["month"], line["meter_id"]), []).append(line)
    bill_rows = []
    for bill in bills:
        key = (bill["month"], bill["meter_id"])
        bill_rows.append({
            "month": bill["month"], "meter_id": bill["meter_id"],
            "usage_kwh": str(bill["usage_kwh"]),
            "amount_yuan": str(bill["amount_yuan"]),
            "kind": bill["kind"], "adjust_cents": bill["adjust_cents"],
            "lines": [{
                "line_kind": line["line_kind"], "register": line["register"],
                "usage_kwh": str(line["usage_kwh"]),
                "amount_yuan": str(line["amount_yuan"]), "price": line["price"],
                "origin_month": line["origin_month"],
                "correction_id": line["correction_id"],
            } for line in lines_by_key.get(key, [])],
        })
    return {
        "version": _version_brief(version),
        "is_current": bool(current)
        and current["version_id"] == version["version_id"],
        "current_version_no": current["version_no"] if current else None,
        "bills": bill_rows,
        "reconcile": evidence.reconcile_payload(
            store.load_ledger_reconcile(version["version_id"])),
        "counts": store.count_ledger(),
    }


def version_list(store) -> list[dict]:
    current = store.current_version()
    out = []
    for row in store.list_versions():
        out.append({**_version_brief(row),
                    "is_current": bool(current)
                    and current["version_id"] == row["version_id"]})
    return out


def correction_list(store) -> list[dict]:
    out = []
    for row in store.list_corrections():
        review = json.loads(row["payload_json"] or "{}")
        out.append({
            "correction_id": row["correction_id"], "reason": row["reason"],
            "ticket": row["ticket"], "posted_month": row["posted_month"],
            "status": row["status"], "note": row["note"],
            "created_at": row["created_at"],
            "result_version_id": row["result_version_id"],
            "affected_meters": review.get("affected_meters", []),
            "adjustment_amount": _adjust_total(review),
        })
    return out


def _adjust_total(review: dict) -> str:
    total = sum((Decimal(item["amount_yuan"])
                 for item in review.get("adjustments", [])), D0)
    return str(total)


def correction_detail(store, correction_id: str) -> dict:
    row = store.get_correction(correction_id)
    if row is None:
        raise errors.NotFound(f"更正 {correction_id} 不存在",
                              code="E_CORRECTION_NOT_FOUND")
    return _correction_payload(store, row, idempotent=True)


# ---------- 导出（与页面同版本） ----------

def _resolve_version(store, version_id: str | None) -> dict:
    version = store.get_version(version_id) if version_id else store.current_version()
    if version is None:
        raise errors.NotFound("还没有已发布的正式账版本", code="E_VERSION_NOT_FOUND")
    return version


def monthly_csv(store, version_id: str | None = None) -> tuple[str, str]:
    version = _resolve_version(store, version_id)
    bills = store.load_ledger_bills(version["version_id"])
    lines = store.load_ledger_lines(version["version_id"])
    lines_by_key: dict[tuple, list] = {}
    for line in lines:
        lines_by_key.setdefault((line["month"], line["meter_id"]), []).append(line)
    rows = []
    for bill in bills:
        for line in lines_by_key.get((bill["month"], bill["meter_id"]), []):
            rows.append([
                version["version_id"], bill["month"], bill["meter_id"],
                _kind_label(bill["kind"]), f"{bill['usage_kwh']:.3f}",
                f"{bill['amount_yuan']:.2f}",
                "调账" if line["line_kind"] == "adjustment" else "用量",
                line["register"], f"{line['usage_kwh']:.3f}",
                line["price"] or "", f"{line['amount_yuan']:.2f}",
                line["origin_month"] or "", line["correction_id"] or "",
            ])
    body = csv_out.render(
        ["版本", "月份", "表号", "账单类型", "账单用量kWh", "账单金额元", "行类型",
         "寄存器", "用量kWh", "单价元", "金额元", "起源月份", "更正号"], rows)
    return body, f"ledger-monthly-{version['version_id']}.csv"


def adjustments_csv(store, version_id: str | None = None) -> tuple[str, str]:
    version = _resolve_version(store, version_id)
    lines = [line for line in store.load_ledger_lines(version["version_id"])
             if line["line_kind"] == "adjustment"]
    reasons_by_id = {row["correction_id"]: row
                     for row in store.list_corrections(version["base_batch_id"])}
    rows = []
    for line in lines:
        correction = reasons_by_id.get(line["correction_id"] or "", {})
        rows.append([
            version["version_id"], line["month"], line["origin_month"] or "",
            line["meter_id"], line["register"], f"{line['usage_kwh']:.3f}",
            f"{line['amount_yuan']:.2f}", line["correction_id"] or "",
            correction.get("reason", ""), correction.get("ticket", ""),
        ])
    body = csv_out.render(
        ["版本", "入账月份", "起源月份", "表号", "寄存器", "用量差kWh", "金额差元",
         "更正号", "更正原因", "工单号"], rows)
    return body, f"ledger-adjustments-{version['version_id']}.csv"


def reconcile_json(store, version_id: str | None = None) -> tuple[str, str]:
    version = _resolve_version(store, version_id)
    payload = {
        "version": _version_brief(version),
        "reconcile": evidence.reconcile_payload(
            store.load_ledger_reconcile(version["version_id"])),
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            f"ledger-reconcile-{version['version_id']}.json")


def _kind_label(kind: str) -> str:
    return {"issued": "当期发行", "carried": "历史结转",
            "adjusted": "含调账"}.get(kind, kind)
