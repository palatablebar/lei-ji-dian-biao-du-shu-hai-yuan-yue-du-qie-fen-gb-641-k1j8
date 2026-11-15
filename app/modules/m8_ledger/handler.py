"""M8 入口：发布正式账、提交与查看更正、版本化导出、正式账页面。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ...web import Ctx, get_ctx, render
from ..m2_contract import contract as contract_mod
from ..m5_months import weights as weights_mod
from . import service

router = APIRouter(tags=["M8 正式账与更正"])


@router.post("/api/ledger/publish")
def api_ledger_publish(payload: dict | None = None,
                       ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    return service.publish_genesis(ctx.settings, ctx.store,
                                   batch_id=body.get("batch_id"))


@router.get("/api/ledger")
def api_ledger(version_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.ledger_payload(ctx.store, version_id)


@router.get("/api/ledger/versions")
def api_ledger_versions(ctx: Ctx = Depends(get_ctx)) -> dict:
    return {"versions": service.version_list(ctx.store)}


@router.post("/api/corrections")
def api_correction_apply(payload: dict, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.apply_correction(ctx.settings, ctx.store, payload or {})


@router.get("/api/corrections")
def api_corrections(ctx: Ctx = Depends(get_ctx)) -> dict:
    return {"corrections": service.correction_list(ctx.store)}


@router.get("/api/corrections/{correction_id}")
def api_correction_detail(correction_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.correction_detail(ctx.store, correction_id)


def _download(body: str, filename: str, media_type: str) -> Response:
    return Response(content=body, media_type=media_type,
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


@router.get("/api/export/ledger/monthly.csv")
def export_ledger_monthly(version_id: str | None = None,
                          ctx: Ctx = Depends(get_ctx)) -> Response:
    body, filename = service.monthly_csv(ctx.store, version_id)
    return _download(body, filename, "text/csv; charset=utf-8")


@router.get("/api/export/ledger/adjustments.csv")
def export_ledger_adjustments(version_id: str | None = None,
                              ctx: Ctx = Depends(get_ctx)) -> Response:
    body, filename = service.adjustments_csv(ctx.store, version_id)
    return _download(body, filename, "text/csv; charset=utf-8")


@router.get("/api/export/ledger/reconcile.json")
def export_ledger_reconcile(version_id: str | None = None,
                            ctx: Ctx = Depends(get_ctx)) -> Response:
    body, filename = service.reconcile_json(ctx.store, version_id)
    return _download(body, filename, "application/json; charset=utf-8")


@router.get("/ledger")
def page_ledger(request: Request, version_id: str | None = None,
                correction: str | None = None, ctx: Ctx = Depends(get_ctx)):
    payload = service.ledger_payload(ctx.store, version_id or None)
    versions = service.version_list(ctx.store)
    corrections = service.correction_list(ctx.store)
    review = None
    if correction:
        review = service.correction_detail(ctx.store, correction)
    elif corrections:
        review = service.correction_detail(ctx.store,
                                           corrections[0]["correction_id"])
    contracts = contract_mod.load(ctx.settings)
    default_posted = ""
    current = service.ledger_payload(ctx.store)
    if current["version"] is not None and current["bills"]:
        max_month = max(bill["month"] for bill in current["bills"])
        default_posted = weights_mod.shift_month(max_month, 1)
    return render(request, "ledger.html", page="ledger", page_title="正式账与更正",
                  batch=None, ledger=payload, versions=versions,
                  corrections=corrections, review=review,
                  contracts=contract_mod.payload(contracts),
                  default_posted=default_posted)
