"""
Rotas administrativas — todas protegidas por `Depends(require_admin)`.

Registrado em app.py com:
    app.include_router(admin_router, prefix="/admin")
"""
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import database as db
from auth import require_admin
from payouts import send_payout

router = APIRouter(tags=["admin"])


class BlocklistIn(BaseModel):
    identifier: str = Field(..., max_length=120)
    reason: str = Field("", max_length=280)


class RejectIn(BaseModel):
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
        "min_withdrawal_amount": float(os.getenv("MIN_WITHDRAWAL_AMOUNT", "0.00001")),
    }


# ---------------- Fase 3 — gestão de saques ----------------
# Resgate (claim) só credita saldo agora; o dinheiro só sai daqui, quando o
# admin aprova um pedido de saque (o que dispara o payout real via
# FaucetPay). Ver user.py para o lado do usuário (pedir saque).

@router.get("/withdrawals")
async def admin_list_withdrawals(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, description="Filtra por status: pending, paid ou rejected"),
    _admin: str = Depends(require_admin),
):
    return await db.list_withdrawals(limit=limit, offset=offset, status=status)


@router.post("/withdrawals/{withdrawal_id}/approve")
async def admin_approve_withdrawal(withdrawal_id: int, _admin: str = Depends(require_admin)):
    """Aprova e paga o saque: dispara o payout real via FaucetPay (ou
    simulado, se DRY_RUN) e só marca como 'paid' se o payout confirmar —
    mesma lógica fail-safe que o /claim antigo já usava, adaptada aqui pro
    saque (outbox: o pedido já existe como 'pending' antes da chamada
    externa, então nada se perde se o processo cair no meio)."""
    withdrawal = await db.get_withdrawal(withdrawal_id)
    if not withdrawal:
        raise HTTPException(status_code=404, detail="Saque não encontrado")
    if withdrawal["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Saque já está '{withdrawal['status']}'")

    payout_result = await send_payout(withdrawal["wallet"], withdrawal["amount"])  # levanta HTTPException se falhar
    payout_id = payout_result.get("payout_id") if isinstance(payout_result, dict) else None
    payout_ref = str(payout_id) if payout_id is not None else None

    updated = await db.mark_withdrawal_paid(withdrawal_id, payout_ref)
    if not updated:
        # corrida rara: outro admin/aba já processou esse saque entre o
        # get_withdrawal acima e agora. O payout já foi disparado — registra
        # isso claramente pro time reconciliar manualmente contra a FaucetPay.
        raise HTTPException(
            status_code=409,
            detail="Payout enviado, mas o saque já não estava mais 'pending' — verifique manualmente para evitar pagamento duplicado",
        )
    return {"status": "ok", "payout_ref": payout_ref}


@router.post("/withdrawals/{withdrawal_id}/reject")
async def admin_reject_withdrawal(withdrawal_id: int, payload: RejectIn, _admin: str = Depends(require_admin)):
    """Rejeita o pedido sem pagar nada — o valor volta a contar como saldo
    disponível automaticamente (db.get_balance ignora saques 'rejected')."""
    updated = await db.mark_withdrawal_rejected(withdrawal_id, payload.reason)
    if not updated:
        raise HTTPException(status_code=409, detail="Saque não encontrado ou não está mais 'pending'")
    return {"status": "ok"}
