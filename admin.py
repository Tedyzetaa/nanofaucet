"""
Rotas administrativas — todas protegidas por `Depends(require_admin)`.

Registrado em app.py com:
    app.include_router(admin_router, prefix="/admin")
"""
import os

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

import database as db
from auth import require_admin

router = APIRouter(tags=["admin"])


class BlocklistIn(BaseModel):
    identifier: str = Field(..., max_length=120)
    reason: str = Field("", max_length=280)


@router.get("/claims")
async def admin_list_claims(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, description="Filtra por status: pending, paid ou failed"),
    _admin: str = Depends(require_admin),
):
    # 'status' expõe claims 'pending' presos (ex: processo caiu no meio do
    # payout) — útil para reconciliação manual contra o extrato da FaucetPay.
    return await db.list_claims(limit=limit, offset=offset, status=status)


@router.get("/blocklist")
async def admin_list_blocklist(_admin: str = Depends(require_admin)):
    return await db.list_blocklist()


@router.post("/blocklist")
async def admin_add_to_blocklist(payload: BlocklistIn, _admin: str = Depends(require_admin)):
    await db.add_to_blocklist(payload.identifier.strip().lower(), payload.reason)
    return {"status": "ok"}


@router.delete("/blocklist/{identifier}")
async def admin_remove_from_blocklist(identifier: str, _admin: str = Depends(require_admin)):
    await db.remove_from_blocklist(identifier.strip().lower())
    return {"status": "ok"}


@router.get("/config")
async def admin_get_config(_admin: str = Depends(require_admin)):
    """Leitura apenas — alterar em runtime sem redeploy fica de backlog
    (exigiria mover essas configs pro banco em vez de env vars)."""
    return {
        "claim_amount": float(os.getenv("CLAIM_AMOUNT", "0.00000010")),
        "cooldown_seconds": int(os.getenv("COOLDOWN_SECONDS", "60")),
        "ip_cooldown_seconds": int(os.getenv("IP_COOLDOWN_SECONDS", "5")),
        "max_claims_per_ip_per_day": int(os.getenv("MAX_CLAIMS_PER_IP_PER_DAY", "50")),
        "dry_run": os.getenv("DRY_RUN", "true").lower() == "true",
        "faucetpay_currency": os.getenv("FAUCETPAY_CURRENCY", "MATIC"),
    }
