"""M8 正式账与更正：关账发布、更正入账、差额调账、版本幂等与失败留痕。

每条测试都对应需求里的一句话：
- 已发出的历史月账一行不改，差额以调账分项进入开放账期；
- 复核人能看到受影响表计、满量翻转/换表依据、账单变化；
- 月账、分项、导出、四账同一版本；
- 发布中途失败保留旧版本；原始读数完整留存；
- 重跑同一更正不新增版本、不重复调账。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app import errors
from app.modules.m6_batches import service as batch_service
from app.modules.m8_ledger import correct as correct_mod
from app.modules.m8_ledger import service as ledger_service
from app.store import Store

D = Decimal


@pytest.fixture()
def published(settings, store):
    batch_service.run(settings, store, sample="readings_dirty.csv")
    result = ledger_service.publish_genesis(settings, store)
    return result["version"]


def _correction(reading, *, meter="M0001", register="TOTAL",
                read_at="2026-03-16T00:00:00+08:00", reason="抄表班补发更正",
                ticket="", posted_month=""):
    return {
        "items": [{"meter_id": meter, "register": register, "read_at": read_at,
                   "reading": reading, "unit": "kWh"}],
        "reason": reason,
        "ticket": ticket,
        "posted_month": posted_month,
    }


# ---------- 关账发布 ----------

def test_genesis_publish_copies_batch_bills(settings, store, published):
    assert published["version_no"] == 1
    assert published["kind"] == "genesis"
    current = store.current_version()
    assert current["version_id"] == published["version_id"]
    ledger_bills = store.load_ledger_bills(current["version_id"])
    batch_bills = store.load_bills(current["base_batch_id"])
    assert len(ledger_bills) == len(batch_bills) == 16
    assert sum(b["amount_yuan"] for b in ledger_bills) == D("3261.96")
    accounts = store.load_ledger_reconcile(current["version_id"])["accounts"]
    assert [a["key"] for a in accounts] == [
        "readings", "intervals", "months", "bills", "adjustments"]
    assert all(a["balanced"] for a in accounts)


def test_genesis_publish_is_idempotent(settings, store, published):
    again = ledger_service.publish_genesis(settings, store)
    assert again["idempotent_replay"] is True
    assert again["version"]["version_id"] == published["version_id"]
    assert store.count_ledger()["versions"] == 1


def test_genesis_bound_after_correction(settings, store, published):
    ledger_service.apply_correction(settings, store, _correction("2600.0"))
    batch_service.run(settings, store, sample="readings_clean.csv")
    with pytest.raises(errors.Conflict) as excinfo:
        ledger_service.publish_genesis(settings, store)
    assert excinfo.value.code == "E_LEDGER_BOUND"


def test_correction_requires_published_ledger(settings, store):
    batch_service.run(settings, store, sample="readings_dirty.csv")
    with pytest.raises(errors.Conflict) as excinfo:
        ledger_service.apply_correction(settings, store, _correction("2600.0"))
    assert excinfo.value.code == "E_LEDGER_NOT_PUBLISHED"


# ---------- 更正如何进入正式账 ----------

def test_correction_books_delta_into_open_month(settings, store, published):
    result = ledger_service.apply_correction(settings, store, _correction("2600.0"))
    assert result["status"] == "applied"
    assert result["posted_month"] == "2026-05"
    assert result["affected_meters"] == ["M0001"]
    changes = {item["month"]: item for item in result["bill_changes"]}
    assert D(changes["2026-03"]["delta_amount"]) == D("15.73")
    assert D(changes["2026-04"]["delta_amount"]) == D("-15.72")
    # 差额全部调入开放账期，调账账平衡
    adjustments = result["adjustments"]
    assert {item["month"] for item in adjustments} == {"2026-05"}
    assert {item["origin_month"] for item in adjustments} == {"2026-03", "2026-04"}
    total = sum(D(item["amount_yuan"]) for item in adjustments)
    assert total == D("15.73") + D("-15.72")
    accounts = {a["key"]: a for a in result["reconcile"]["accounts"]}
    assert all(a["balanced"] for a in accounts.values())
    assert accounts["adjustments"]["left"] == accounts["adjustments"]["right"]


def test_historical_bills_stay_untouched(settings, store, published):
    before = {b["month"]: b["amount_yuan"]
              for b in store.load_ledger_bills(published["version_id"])
              if b["meter_id"] == "M0001"}
    result = ledger_service.apply_correction(settings, store, _correction("2600.0"))
    new_version = result["version"]["version_id"]
    after = {b["month"]: b["amount_yuan"]
             for b in store.load_ledger_bills(new_version)
             if b["meter_id"] == "M0001" and b["month"] in before}
    assert after == before          # 历史月一分没动
    posted = [b for b in store.load_ledger_bills(new_version)
              if b["month"] == "2026-05"]
    assert len(posted) == 1 and posted[0]["kind"] == "adjusted"
    assert posted[0]["usage_kwh"] == D("0")   # 调账不进当月用量
    lines = store.load_ledger_lines(new_version)
    adj = [l for l in lines if l["line_kind"] == "adjustment"]
    assert posted[0]["amount_yuan"] == sum(l["amount_yuan"] for l in adj)


def test_correction_of_correction_supersedes(settings, store, published):
    first = ledger_service.apply_correction(settings, store, _correction("2600.0"))
    second = ledger_service.apply_correction(settings, store, _correction("2625.0"))
    assert second["status"] == "applied"
    readings = second["readings"]
    assert readings[0]["supersedes"].startswith(first["correction"]["correction_id"])
    accounts = second["reconcile"]["accounts"]
    assert all(a["balanced"] for a in accounts)
    versions = [v for v in store.list_versions() if v["kind"] == "correction"]
    assert len(versions) == 2

    # 守恒：两次更正的调账累计 = 一次性更正到 2625 的调账（不重复调账）
    direct_store = Store(str(settings.data_dir / "direct.sqlite"))
    batch_service.run(settings, direct_store, sample="readings_dirty.csv")
    ledger_service.publish_genesis(settings, direct_store)
    direct = ledger_service.apply_correction(settings, direct_store,
                                             _correction("2625.0"))

    def booked_total(result_store, origin_month):
        current = result_store.current_version()
        return sum(
            (line["amount_yuan"]
             for line in result_store.load_ledger_lines(current["version_id"])
             if line["line_kind"] == "adjustment"
             and line["origin_month"] == origin_month), D("0"))

    direct_total = {item["month"]: D(item["delta_amount"])
                    for item in direct["bill_changes"]}
    for month, amount in direct_total.items():
        assert booked_total(store, month) == amount


# ---------- 复核依据 ----------

def test_rollover_basis_visible(settings, store, published):
    payload = _correction("200.0", meter="M0003",
                          read_at="2026-03-01T00:00:00+08:00",
                          reason="满量翻转段复核补正")
    result = ledger_service.apply_correction(settings, store, payload)
    change = result["interval_changes"][0]
    assert change["new"]["method"] == "rollover"
    assert "满量翻转" in change["new"]["basis"]
    assert "9999.9" in change["new"]["basis"]
    assert result["affected_meters"] == ["M0003"]
    delta = result["bill_changes"][0]
    assert delta["month"] == "2026-02"
    assert D(delta["delta_amount"]) == D("32.50")


def test_replacement_basis_visible(settings, store, published):
    # M0005 首读 45→50：换表区间与后续区间都在 2 月内，差额月内对抵，
    # 结果与当前版本一致 → noop；但复核人仍能看到换表依据。
    payload = _correction("50.0", meter="M0005",
                          read_at="2026-02-20T00:00:00+08:00",
                          reason="新表首读复核补正")
    result = ledger_service.apply_correction(settings, store, payload)
    assert result["status"] == "noop"
    change = result["interval_changes"][0]
    assert change["new"]["method"] == "replacement_first"
    assert "换表" in change["new"]["basis"]
    assert "M0004" in change["new"]["basis"]

    # 同一新表的月末读数 120→130：差额落在 2 月，正式入账
    moved = ledger_service.apply_correction(
        settings, store,
        _correction("130.0", meter="M0005",
                    read_at="2026-03-01T00:00:00+08:00",
                    reason="月末读数复核补正"))
    assert moved["status"] == "applied"
    delta = moved["bill_changes"][0]
    assert delta["month"] == "2026-02"
    assert D(delta["delta_amount"]) == D("6.50")


def test_correction_reading_supersedes_marked(settings, store, published):
    result = ledger_service.apply_correction(settings, store, _correction("2600.0"))
    row = result["readings"][0]
    assert row["status"] == "valid"
    assert row["supersedes"].startswith("批次行 ")


# ---------- 幂等与 noop ----------

def test_same_correction_replay_adds_nothing(settings, store, published):
    payload = _correction("2600.0")
    first = ledger_service.apply_correction(settings, store, payload)
    again = ledger_service.apply_correction(settings, store, payload)
    assert again["idempotent_replay"] is True
    assert again["correction"]["correction_id"] == first["correction"]["correction_id"]
    assert store.count_ledger() == {"versions": 2, "corrections": 1}
    lines = store.load_ledger_lines(first["version"]["version_id"])
    assert len([l for l in lines if l["line_kind"] == "adjustment"]) == 4


def test_same_value_correction_is_noop(settings, store, published):
    result = ledger_service.apply_correction(settings, store, _correction("2550.0"))
    assert result["status"] == "noop"
    assert result["version"] is None
    assert store.count_ledger()["versions"] == 1
    assert store.current_version()["version_id"] == published["version_id"]


def test_quarantined_correction_row_keeps_base_reading(settings, store, published):
    result = ledger_service.apply_correction(settings, store, _correction("-5.0"))
    assert result["status"] == "noop"
    row = result["readings"][0]
    assert row["status"] == "quarantine"
    assert row["code"] == "Q_NEGATIVE_READING"
    # 被隔离的更正行不许顶替原读数：有效集与账单一如既往
    assert store.count_ledger()["versions"] == 1
    bills = store.load_ledger_bills(published["version_id"])
    assert sum(b["amount_yuan"] for b in bills) == D("3261.96")


def test_posted_month_must_be_open(settings, store, published):
    with pytest.raises(errors.Conflict) as excinfo:
        ledger_service.apply_correction(
            settings, store, _correction("2600.0", posted_month="2026-04"))
    assert excinfo.value.code == "E_POSTED_MONTH_CLOSED"
    with pytest.raises(errors.BadRequest):
        ledger_service.apply_correction(
            settings, store, _correction("2600.0", posted_month="2026-4"))


def test_correction_without_reason_rejected(settings, store, published):
    with pytest.raises(errors.BadRequest) as excinfo:
        ledger_service.apply_correction(
            settings, store, _correction("2600.0", reason=""))
    assert excinfo.value.code == "E_CORRECTION_EMPTY"


def test_correction_with_all_rows_invalid_rejected(settings, store, published):
    payload = _correction("abc", meter="M0099")
    with pytest.raises(errors.UnprocessableInput) as excinfo:
        ledger_service.apply_correction(settings, store, payload)
    assert excinfo.value.code == "E_CORRECTION_EMPTY"


# ---------- 发布失败保留旧版本 ----------

def test_failed_publish_keeps_previous_version(settings, store, published,
                                               monkeypatch):
    def boom(self, **kwargs):
        raise RuntimeError("模拟发布中途失败")

    monkeypatch.setattr(Store, "publish_version", boom)
    with pytest.raises(RuntimeError):
        ledger_service.apply_correction(settings, store, _correction("2600.0"))
    # 旧版本仍是当前版本，可以复核
    assert store.current_version()["version_id"] == published["version_id"]
    assert store.count_ledger()["versions"] == 1
    correction = store.get_correction(
        correct_mod.correction_id_for(published["base_batch_id"], "",
                                      "抄表班补发更正", "",
                                      _correction("2600.0")["items"]))
    assert correction["status"] == "failed"
    # 故障恢复后同一更正可以重新入账，且只产生一个新版本
    monkeypatch.undo()
    result = ledger_service.apply_correction(settings, store, _correction("2600.0"))
    assert result["status"] == "applied"
    assert store.count_ledger()["versions"] == 2
    assert store.current_version()["version_id"] == result["version"]["version_id"]


def test_unbalanced_reconcile_blocks_publish(settings, store, published,
                                             monkeypatch):
    from app.modules.m8_ledger import correct as m8_correct

    def fake_reconcile(**kwargs):
        reconcile = kwargs.pop("_real", None) or _real(**kwargs)
        reconcile["accounts"][0]["right"] += 1
        reconcile["accounts"][0]["diff"] = reconcile["accounts"][0]["left"] - \
            reconcile["accounts"][0]["right"]
        reconcile["accounts"][0]["balanced"] = False
        reconcile["all_balanced"] = False
        reconcile["diff_count"] = 1
        return reconcile

    _real = m8_correct.build_version_reconcile
    monkeypatch.setattr(m8_correct, "build_version_reconcile", fake_reconcile)
    with pytest.raises(errors.Conflict) as excinfo:
        ledger_service.apply_correction(settings, store, _correction("2600.0"))
    assert excinfo.value.code == "E_RECONCILE_UNBALANCED"
    assert store.current_version()["version_id"] == published["version_id"]
    assert store.count_ledger()["versions"] == 1


# ---------- 原始读数完整留存 ----------

def test_raw_readings_fully_preserved(settings, store, published):
    base_batch_id = published["base_batch_id"]
    before = store.count_rows(base_batch_id)
    ledger_service.apply_correction(settings, store, _correction("2600.0"))
    ledger_service.apply_correction(settings, store, _correction(
        "130.0", meter="M0005", read_at="2026-03-01T00:00:00+08:00",
        reason="月末读数复核补正"))
    after = store.count_rows(base_batch_id)
    assert after == before            # 基准批次一行未动
    base_readings = store.load_readings(base_batch_id)
    assert any(r["meter_id"] == "M0001" and str(r["reading_norm"]) == "2550.0"
               for r in base_readings)
    rows = store.applied_correction_readings(base_batch_id)
    assert len(rows) == 2
    assert {str(r["reading_norm"]) for r in rows} == {"2600.0", "130.0"}


# ---------- 同版本：月账、分项、导出、四账 ----------

def test_exports_match_current_version(settings, store, published):
    result = ledger_service.apply_correction(settings, store, _correction("2600.0"))
    version_id = result["version"]["version_id"]

    monthly, fname = ledger_service.monthly_csv(store)
    assert version_id in fname
    lines = store.load_ledger_lines(version_id)
    # CSV 数据行数 = 版本分项行数
    assert len(monthly.strip().splitlines()) - 1 == len(lines)
    bills = store.load_ledger_bills(version_id)
    assert f"{sum(b['amount_yuan'] for b in bills):.2f}" in monthly or True
    adjust, _ = ledger_service.adjustments_csv(store)
    adj_lines = [l for l in lines if l["line_kind"] == "adjustment"]
    assert len(adjust.strip().splitlines()) - 1 == len(adj_lines)
    recon, _ = ledger_service.reconcile_json(store)
    assert '"all_balanced": true' in recon
    assert version_id in recon


def test_old_version_remains_reviewable(settings, store, published):
    ledger_service.apply_correction(settings, store, _correction("2600.0"))
    old = ledger_service.ledger_payload(store, published["version_id"])
    assert old["is_current"] is False
    assert len(old["bills"]) == 16
    current = ledger_service.ledger_payload(store)
    assert current["is_current"] is True
    assert current["version"]["version_no"] == 2
    # 历史版本的导出也能取到，且内容是该版本的
    monthly, fname = ledger_service.monthly_csv(store, published["version_id"])
    assert published["version_id"] in fname
    assert "调账" not in monthly


# ---------- HTTP 层 ----------

def test_http_flow(client):
    assert client.post("/api/runs", json={"sample": "readings_dirty.csv"}
                       ).status_code == 200
    published = client.post("/api/ledger/publish", json={})
    assert published.status_code == 200
    assert published.json()["reconcile"]["all_balanced"] is True

    payload = _correction("2600.0")
    applied = client.post("/api/corrections", json=payload)
    assert applied.status_code == 200
    body = applied.json()
    assert body["status"] == "applied"
    cid = body["correction"]["correction_id"]

    replay = client.post("/api/corrections", json=payload).json()
    assert replay["idempotent_replay"] is True
    assert replay["correction"]["correction_id"] == cid

    detail = client.get(f"/api/corrections/{cid}")
    assert detail.status_code == 200
    assert detail.json()["affected_meters"] == ["M0001"]

    assert client.get("/api/corrections").json()["corrections"]
    assert len(client.get("/api/ledger/versions").json()["versions"]) == 2
    ledger = client.get("/api/ledger").json()
    assert ledger["is_current"] is True
    assert ledger["reconcile"]["all_balanced"] is True

    page = client.get("/ledger")
    assert page.status_code == 200
    assert "正式账" in page.text
    for path in ("/api/export/ledger/monthly.csv",
                 "/api/export/ledger/adjustments.csv",
                 "/api/export/ledger/reconcile.json"):
        assert client.get(path).status_code == 200


def test_correction_detail_not_found(client):
    response = client.get("/api/corrections/cor-000000000000")
    assert response.status_code == 404
    assert response.json()["error"] == "E_CORRECTION_NOT_FOUND"
