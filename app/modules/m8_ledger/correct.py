"""更正重算：合并读数集 → 与已入账账对比 → 调账分项 → 新版本内容。

三条铁律（违反任何一条，复核人就看不懂账）：
1. 已入账的历史月账一行不改，差额以调账分项进入开放账期；
2. 调账分项只动金额、不进用量，开放账期用量仍与原始读数对齐；
3. 重算输入 = 基准批次读数 + 血统内全部已入账更正，同一更正必然同一结果。
"""
from __future__ import annotations

import json
from decimal import Decimal

from ... import config, manifest

D0 = Decimal("0")


# ---------- 更正身份与读数行 ----------

def canonical_item(item: dict) -> str:
    return "|".join(str(item.get(key) or "").strip() for key in
                    ("meter_id", "register", "read_at", "reading", "unit",
                     "read_type", "note"))


def correction_id_for(base_batch_id: str, posted_month: str, reason: str,
                      ticket: str, items: list[dict]) -> str:
    """同一基准 + 同一入账月 + 同一原因 + 同一批更正行 → 同一个更正号。

    重跑同一更正命中这个号，不新增版本、不重复调账。
    """
    canonical = sorted(canonical_item(item) for item in items)
    return "cor-" + manifest.digest_parts(
        [base_batch_id, posted_month, reason, ticket] + canonical)[:12]


def item_rows(items: list[dict]) -> list[dict]:
    """把接口里的更正行包成 M2 契约解释能吃的行结构。"""
    rows = []
    for index, item in enumerate(items, start=1):
        rows.append({
            "lineno": 10000 + index,
            "segment": 1,
            "cells": {key: str(item.get(key) or "") for key in
                      ("meter_id", "register", "read_at", "reading", "unit",
                       "read_type", "note")},
            "raw": json.dumps(item, ensure_ascii=False, sort_keys=True,
                              default=str),
        })
    return rows


def base_record(row: dict) -> dict:
    return {"lineno": row["lineno"], "segment": row["segment"],
            "meter_id": row["meter_id"], "register": row["register"],
            "read_at": row["read_at"], "reading": row["reading_norm"],
            "reading_text": row["reading_text"], "unit_raw": row["unit_raw"],
            "unit_norm": row["unit_norm"], "read_type": row["read_type"],
            "flags": list(row.get("flags") or []), "note": row.get("note", "")}


def correction_record(row: dict) -> dict:
    return {"lineno": row["_lineno"], "segment": 1,
            "meter_id": row["meter_id"], "register": row["register"],
            "read_at": row["read_at"], "reading": Decimal(str(row["reading_norm"])),
            "reading_text": row["reading_text"], "unit_raw": row.get("unit_raw", ""),
            "unit_norm": row.get("unit_norm"), "read_type": row.get("read_type", "N"),
            "flags": [], "note": row.get("note", "")}


def effective_records(base_readings: list[dict], correction_rows: list[dict]):
    """有效读数集：同业务键时后到更正取代先到行（原行留在库里，只是不参与还原）。

    返回 (记录列表, 取代说明, 被取代的批次数, 被取代的更正行数)：
    取代说明以胜出的更正行 (correction_id, seq) 为键，给复核人看每一行顶替了谁；
    两个计数进读数账的「被取代」桶，账才能平。
    """
    base_by_key: dict[tuple, dict] = {}
    for row in base_readings:
        base_by_key.setdefault(
            (row["meter_id"], row["register"], row["read_at"]), row)
    winners: dict[tuple, dict] = {}
    supersedes: dict[tuple, str] = {}
    for row in correction_rows:
        key = (row["meter_id"], row["register"], row["read_at"])
        note = ""
        if key in winners:
            prev = winners[key]
            note = f"{prev['correction_id']}#{prev['seq']}"
        elif key in base_by_key:
            note = f"批次行 {base_by_key[key]['lineno']}"
        winners[key] = row
        if note:
            supersedes[(row["correction_id"], row["seq"])] = note
    displaced_base = sum(1 for key in winners if key in base_by_key)
    displaced_cor = len(correction_rows) - len(winners)
    records = [base_record(row) for key, row in base_by_key.items()
               if key not in winners]
    records.extend(correction_record(row) for row in winners.values())
    return records, supersedes, displaced_base, displaced_cor


# ---------- 账单差与调账分项 ----------

def usage_lines(month: str, meter_id: str, bill: dict) -> list[dict]:
    return [{"month": month, "meter_id": meter_id, "line_kind": "usage",
             "register": comp["register"], "usage_kwh": Decimal(str(comp["usage"])),
             "amount_yuan": Decimal(str(comp["amount"])), "price": str(comp["price"]),
             "origin_month": None, "correction_id": None}
            for comp in bill["components"]]


def _effective_map(issued_lines: list[dict], booked_lines: list[dict]) -> dict:
    """已入账基数：当前版本的用量分项 + 血统内全部调账分项（按起源月归集）。

    历史月的差额 = 应算 − 已入账；已入账必须含此前各版本已调的差额，
    否则第二次更正会把第一次的调账冲掉。
    """
    eff: dict[tuple, dict] = {}

    def add(month: str, meter_id: str, register: str, usage: Decimal,
            amount: Decimal) -> None:
        slot = eff.setdefault((month, meter_id, register),
                              {"usage": D0, "amount": D0})
        slot["usage"] += usage
        slot["amount"] += amount

    for line in issued_lines:
        if line["line_kind"] != "usage":
            continue
        add(line["month"], line["meter_id"], line["register"],
            Decimal(str(line["usage_kwh"])), Decimal(str(line["amount_yuan"])))
    for line in booked_lines:
        add(line["origin_month"], line["meter_id"], line["register"],
            Decimal(str(line["usage_kwh"])), Decimal(str(line["amount_yuan"])))
    return eff


def build_bill_changes(should_bills: list[dict], issued_bills: list[dict],
                       issued_lines: list[dict], booked_lines: list[dict],
                       closed_months: set[str], prices: dict) -> tuple[list, list]:
    """逐 (关账月, 表号) 对比应算与已入账，差额拆成寄存器级调账分项。

    两边金额都是闭合到 0.01 的 Decimal，差额逐分精确；
    分量闭合保证 Σ寄存器差额 = 账单差额，调账账因此必然平衡。
    """
    from ..m5_months import tou

    should_by_key = {(b["month"], b["meter_id"]): b for b in should_bills}
    eff = _effective_map(issued_lines, booked_lines)
    eff_bill: dict[tuple, dict] = {}
    for (month, meter_id, _register), slot in eff.items():
        bill_slot = eff_bill.setdefault((month, meter_id), {"usage": D0, "amount": D0})
        bill_slot["usage"] += slot["usage"]
        bill_slot["amount"] += slot["amount"]

    keys = sorted(should_by_key.keys() | eff_bill.keys())
    changes: list[dict] = []
    adjustments: list[dict] = []
    for month, meter_id in keys:
        if month not in closed_months:
            continue
        should = should_by_key.get((month, meter_id))
        comps = {c["register"]: c for c in (should["components"] if should else [])}
        booked = eff_bill.get((month, meter_id), {"usage": D0, "amount": D0})
        should_usage = should["usage_kwh"] if should else D0
        should_amount = should["amount_yuan"] if should else D0
        delta_usage = should_usage - booked["usage"]
        delta_amount = should_amount - booked["amount"]
        if delta_usage == 0 and delta_amount == 0:
            continue
        changes.append({
            "month": month, "meter_id": meter_id,
            "booked_usage": booked["usage"], "booked_amount": booked["amount"],
            "should_usage": should_usage, "should_amount": should_amount,
            "delta_usage": delta_usage, "delta_amount": delta_amount,
        })
        registers = sorted(
            set(comps) | {r for (m, mt, r) in eff if (m, mt) == (month, meter_id)},
            key=lambda r: config.REGISTERS.index(r) if r in config.REGISTERS else 9)
        for register in registers:
            comp = comps.get(register)
            slot = eff.get((month, meter_id, register), {"usage": D0, "amount": D0})
            line_usage = (Decimal(str(comp["usage"])) if comp else D0) - slot["usage"]
            line_amount = (Decimal(str(comp["amount"])) if comp else D0) - slot["amount"]
            if line_usage == 0 and line_amount == 0:
                continue
            adjustments.append({
                "origin_month": month, "meter_id": meter_id, "register": register,
                "usage_kwh": line_usage, "amount_yuan": line_amount,
                "price": str(comp["price"]) if comp
                else str(tou.price_of(register, prices)),
            })
    return changes, adjustments


def compose_version(issued_bills: list[dict], issued_lines: list[dict],
                    should_bills: list[dict], adjustments: list[dict],
                    posted_month: str, closed_months: set[str]) -> tuple[list, list]:
    """新版本内容 = 历史月原样结转 + 开放月自然账 + 入账月调账分项。"""
    bills: list[dict] = []
    lines: list[dict] = []
    lines_by_key: dict[tuple, list] = {}
    for line in issued_lines:
        lines_by_key.setdefault((line["month"], line["meter_id"]), []).append(line)
    for bill in sorted(issued_bills, key=lambda b: (b["month"], b["meter_id"])):
        key = (bill["month"], bill["meter_id"])
        bills.append({"month": bill["month"], "meter_id": bill["meter_id"],
                      "usage_kwh": bill["usage_kwh"], "amount_yuan": bill["amount_yuan"],
                      "kind": "carried", "adjust_cents": bill["adjust_cents"]})
        lines.extend(dict(line) for line in lines_by_key.get(key, []))

    should_by_key = {(b["month"], b["meter_id"]): b for b in should_bills}
    for (month, meter_id), should in sorted(should_by_key.items()):
        if month in closed_months or month == posted_month:
            continue
        bills.append({"month": month, "meter_id": meter_id,
                      "usage_kwh": should["usage_kwh"],
                      "amount_yuan": should["amount_yuan"], "kind": "issued",
                      "adjust_cents": should["adjust_cents"]})
        lines.extend(usage_lines(month, meter_id, should))

    posted_meters = sorted(
        {meter for (month, meter) in should_by_key if month == posted_month}
        | {adj["meter_id"] for adj in adjustments})
    for meter_id in posted_meters:
        should = should_by_key.get((posted_month, meter_id))
        adj = [item for item in adjustments if item["meter_id"] == meter_id]
        nat_lines = usage_lines(posted_month, meter_id, should) if should else []
        adj_amount = sum((item["amount_yuan"] for item in adj), D0)
        bills.append({
            "month": posted_month, "meter_id": meter_id,
            "usage_kwh": should["usage_kwh"] if should else D0,
            "amount_yuan": (should["amount_yuan"] if should else D0) + adj_amount,
            "kind": "adjusted" if adj else "issued",
            "adjust_cents": should["adjust_cents"] if should else 0,
        })
        lines.extend(nat_lines)
        lines.extend({
            "month": posted_month, "meter_id": meter_id, "line_kind": "adjustment",
            "register": item["register"], "usage_kwh": item["usage_kwh"],
            "amount_yuan": item["amount_yuan"], "price": item["price"],
            "origin_month": item["origin_month"],
            "correction_id": item.get("correction_id"),
        } for item in adj)
    return bills, lines


# ---------- 版本四账与摘要 ----------

def _account(key: str, label: str, left, right, unit: str, note: str = "") -> dict:
    diff = left - right
    return {"key": key, "label": label, "left": left, "right": right,
            "diff": diff, "unit": unit, "balanced": diff == 0, "note": note}


def build_version_reconcile(*, raw_lines: int, valid: int, estimate: int,
                            quarantine: int, blank: int, superseded: int,
                            interval_usage: Decimal, month_usage: Decimal,
                            tou_before: Decimal, tou_after: Decimal,
                            tou_adjustments: int, bills: list[dict],
                            lines: list[dict], adjustments: list[dict],
                            bill_changes: list[dict]) -> dict:
    """版本级五账：原四账 + 调账账（调账分项合计 = 历史月应补退差额合计）。"""
    bill_total = sum((bill["amount_yuan"] for bill in bills), D0)
    line_total = sum((line["amount_yuan"] for line in lines), D0)
    adjust_total = sum((item["amount_yuan"] for item in adjustments), D0)
    delta_total = sum((item["delta_amount"] for item in bill_changes), D0)
    accounts = [
        _account("readings", "读数账", raw_lines,
                 valid + estimate + quarantine + blank + superseded, "行",
                 f"有效 {valid} + 估抄 {estimate} + 隔离 {quarantine} + 空行 {blank}"
                 f" + 被取代 {superseded}"),
        _account("intervals", "区间账",
                 interval_usage.quantize(config.USAGE_QUANT),
                 month_usage.quantize(config.USAGE_QUANT), "kWh",
                 "主寄存器区间用量 = 月度分摊用量"),
        _account("months", "月度账",
                 tou_after.quantize(config.USAGE_QUANT), D0, "kWh",
                 f"闭合前尾差 {tou_before.quantize(config.USAGE_QUANT)}，"
                 f"并入最大分量 {tou_adjustments} 组"),
        _account("bills", "账单账",
                 bill_total.quantize(config.MONEY_QUANT),
                 line_total.quantize(config.MONEY_QUANT), "元",
                 f"{len(bills)} 张账单 = 分项金额之和"),
        _account("adjustments", "调账账",
                 adjust_total.quantize(config.MONEY_QUANT),
                 delta_total.quantize(config.MONEY_QUANT), "元",
                 f"调账 {len(adjustments)} 条 = 历史月应补退差额"),
    ]
    diffs = [item for item in accounts if not item["balanced"]]
    return {"accounts": accounts, "all_balanced": not diffs,
            "diff_count": len(diffs), "checked_at": None}


def version_digest(bills: list[dict], lines: list[dict]) -> str:
    """版本内容摘要：同内容必然同摘要，用于「结果相同不新增版本」。

    只覆盖账单与分项的数值内容：四账由同一次重算推出，账单相同则账必然相同；
    kind 只是结转/发行的展示标记，不参与「内容是否变化」的判定。
    """
    canonical = json.dumps({
        "bills": [{key: str(bill[key]) for key in
                   ("month", "meter_id", "usage_kwh", "amount_yuan",
                    "adjust_cents")} for bill in bills],
        "lines": [{key: str(line[key]) for key in
                   ("month", "meter_id", "line_kind", "register", "usage_kwh",
                    "amount_yuan", "price", "origin_month", "correction_id")}
                  for line in lines],
    }, ensure_ascii=False, sort_keys=True)
    return manifest.digest_bytes(canonical.encode("utf-8"))
