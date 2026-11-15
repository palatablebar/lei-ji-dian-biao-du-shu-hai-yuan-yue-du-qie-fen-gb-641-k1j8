"""复核证据：区间为什么这样判（满量翻转 / 换表依据）、账单变了多少。

复核人打开一条更正，必须能看到：受影响表计、每段区间的判定依据、
每个历史月的已入账 → 应算 → 差额，以及差额调进了哪个月。
"""
from __future__ import annotations

from decimal import Decimal

from ..m2_contract import contract as contract_mod
from ..m4_intervals import swaps

D0 = Decimal("0")


def interval_basis(interval: dict,
                   contracts: dict[str, contract_mod.MeterContract]) -> str:
    """单段区间的判定依据：翻转看满量线，换表看承接关系。"""
    meter = contracts[interval["meter_id"]]
    method = interval["method"]
    if method == "rollover":
        return (f"满量翻转：上次 {interval['prev_reading']} ≥ 满量 {meter.capacity}"
                f" × 0.9（{meter.rollover_floor}），用量 = {meter.capacity}"
                f" − {interval['prev_reading']} + {interval['cur_reading']}")
    if method == "replacement_first":
        predecessor = swaps.predecessor_map(contracts).get(interval["meter_id"])
        if predecessor is not None and predecessor.replaced_at is not None:
            return (f"换表：承接 {predecessor.meter_id}，换表时点 "
                    f"{predecessor.replaced_at.isoformat()}，新表首读从 0 起算")
        return "换表：新表首读从 0 起算"
    return f"常规：{interval['cur_reading']} − {interval['prev_reading']}"


def _side(interval: dict | None,
          contracts: dict[str, contract_mod.MeterContract]) -> dict | None:
    if interval is None:
        return None
    return {"usage_kwh": str(interval["usage_kwh"]), "method": interval["method"],
            "basis": interval_basis(interval, contracts)}


def diff_intervals(prev: list[dict], nxt: list[dict],
                   contracts: dict[str, contract_mod.MeterContract]) -> list[dict]:
    """以 (表号, 寄存器, 起, 止) 为键对比两次还原，列出变了的区间。"""
    def key(item: dict) -> tuple:
        return (item["meter_id"], item["register"], item["start_at"], item["end_at"])

    pmap = {key(item): item for item in prev}
    nmap = {key(item): item for item in nxt}
    changes = []
    for item_key in sorted(set(pmap) | set(nmap)):
        old, new = pmap.get(item_key), nmap.get(item_key)
        if (old and new and old["usage_kwh"] == new["usage_kwh"]
                and old["method"] == new["method"]):
            continue
        changes.append({
            "meter_id": item_key[0], "register": item_key[1],
            "start_at": item_key[2], "end_at": item_key[3],
            "old": _side(old, contracts), "new": _side(new, contracts),
        })
    return changes


def bill_change_row(change: dict) -> dict:
    return {
        "month": change["month"], "meter_id": change["meter_id"],
        "booked_usage": str(change["booked_usage"]),
        "booked_amount": str(change["booked_amount"]),
        "should_usage": str(change["should_usage"]),
        "should_amount": str(change["should_amount"]),
        "delta_usage": str(change["delta_usage"]),
        "delta_amount": str(change["delta_amount"]),
    }


def adjustment_row(item: dict) -> dict:
    return {
        "month": item["month"], "origin_month": item["origin_month"],
        "meter_id": item["meter_id"], "register": item["register"],
        "usage_kwh": str(item["usage_kwh"]), "amount_yuan": str(item["amount_yuan"]),
        "price": str(item["price"]),
    }


def reading_row(row: dict) -> dict:
    return {
        "seq": row["seq"], "meter_id": row["meter_id"], "register": row["register"],
        "read_at": row["read_at"], "reading_text": row["reading_text"],
        "reading_norm": (None if row.get("reading_norm") is None
                         else str(row["reading_norm"])),
        "unit_norm": row.get("unit_norm"), "read_type": row.get("read_type", "N"),
        "status": row["status"], "code": row.get("code"),
        "supersedes": row.get("supersedes", ""), "note": row.get("note", ""),
    }


def reconcile_payload(reconcile: dict | None) -> dict:
    if not reconcile:
        return {"accounts": [], "all_balanced": None, "diff_count": 0}
    return {
        "all_balanced": reconcile["all_balanced"],
        "diff_count": reconcile["diff_count"],
        "accounts": [{
            "key": item["key"], "label": item["label"],
            "left": str(item["left"]), "right": str(item["right"]),
            "diff": str(item["diff"]), "unit": item["unit"],
            "balanced": item["balanced"], "note": item["note"],
        } for item in reconcile["accounts"]],
    }
