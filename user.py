"""
Rotas do dashboard do usuário final — todas protegidas por
`Depends(require_user)` (sessão válida do Supabase Auth, criada com
e-mail+senha, sem precisar de admin_profiles).

Registrado em app.py com:
    app.include_router(user_router, prefix="/me")

Fluxo de saque:
  1. Usuário vincula a wallet (a mesma usada em /claim) à própria conta.
  2. GET /me/balance soma os claims 'paid' daquela wallet e subtrai saques
     já pagos/pendentes -> saldo disponível.
  3. POST /me/withdraw cria um pedido 'pending' (não paga nada ainda).
  4. Admin aprova (dispara o payout real via FaucetPay) ou rejeita
     (devolve o valor ao saldo) em /admin/withdrawals/{id}/*.
"""
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

import database as db
from auth import require_user

router = APIRouter(tags=["me"])

MIN_WITHDRAWAL_AMOUNT = float(os.getenv("MIN_WITHDRAWAL_AMOUNT", "0.00001"))
FAUCETPAY_CURRENCY = os.getenv("FAUCETPAY_CURRENCY", "MATIC")

# mesma validação de wallet usada em /claim (app.py) — e-mail OU endereço EVM
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


class WalletIn(BaseModel):
    wallet: str = Field(..., max_length=120)

    @field_validator("wallet")
    @classmethod
    def validate_wallet(cls, v: str) -> str:
        v = v.strip().lower()
        if not (_EMAIL_RE.match(v) or _EVM_ADDRESS_RE.match(v)):
            raise ValueError("Formato de endereço/e-mail inválido")
        return v


class WithdrawIn(BaseModel):
    # se omitido, saca o saldo disponível inteiro
    amount: float | None = Field(None, gt=0)


@router.get("/profile")
async def me_profile(user: dict = Depends(require_user)):
    wallet = await db.get_user_wallet(user["user_id"])
    return {
        "email": user["email"],
        "wallet": wallet,
        "min_withdrawal_amount": MIN_WITHDRAWAL_AMOUNT,
        "currency": FAUCETPAY_CURRENCY,
    }


@router.put("/wallet")
async def me_set_wallet(payload: WalletIn, user: dict = Depends(require_user)):
    try:
        await db.set_user_wallet(user["user_id"], payload.wallet)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "ok", "wallet": payload.wallet}


@router.get("/balance")
async def me_balance(user: dict = Depends(require_user)):
    wallet = await db.get_user_wallet(user["user_id"])
    if not wallet:
        raise HTTPException(status_code=400, detail="Vincule uma wallet à sua conta primeiro")
    balance = await db.get_balance(wallet)
    balance["min_withdrawal_amount"] = MIN_WITHDRAWAL_AMOUNT
    balance["currency"] = FAUCETPAY_CURRENCY
    return balance


@router.get("/claims")
async def me_claims(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_user),
):
    """Histórico de ganhos (ver P/R: resgate agora vira saldo, não pagamento
    direto — cada linha aqui é um claim que foi creditado)."""
    wallet = await db.get_user_wallet(user["user_id"])
    if not wallet:
        return []
    return await db.list_user_claims(wallet, limit=limit, offset=offset)


@router.get("/withdrawals")
async def me_withdrawals(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_user),
):
    return await db.list_user_withdrawals(user["user_id"], limit=limit, offset=offset)


@router.post("/withdraw")
async def me_withdraw(payload: WithdrawIn, user: dict = Depends(require_user)):
    wallet = await db.get_user_wallet(user["user_id"])
    if not wallet:
        raise HTTPException(status_code=400, detail="Vincule uma wallet à sua conta antes de sacar")

    balance = await db.get_balance(wallet)
    amount = payload.amount if payload.amount is not None else balance["available"]

    if amount < MIN_WITHDRAWAL_AMOUNT:
        raise HTTPException(
            status_code=400,
            detail=f"Valor mínimo de saque é {MIN_WITHDRAWAL_AMOUNT} {FAUCETPAY_CURRENCY}",
        )
    if amount > balance["available"]:
        raise HTTPException(status_code=400, detail="Saldo insuficiente")

    withdrawal_id = await db.create_withdrawal(user["user_id"], wallet, amount)
    if withdrawal_id is None:
        # saldo mudou entre a checagem acima e o INSERT atômico (ex: outra
        # aba pedindo saque ao mesmo tempo) — best-effort, ver docstring de
        # db.create_withdrawal.
        raise HTTPException(status_code=409, detail="Saldo insuficiente, tente novamente")

    return {
        "status": "ok",
        "withdrawal_id": withdrawal_id,
        "amount": amount,
        "message": "Pedido de saque registrado. O pagamento é liberado manualmente pelo admin em até 24 horas.",
    }
