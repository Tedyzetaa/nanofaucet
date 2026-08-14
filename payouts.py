"""
Integração com a API de payout da FaucetPay.

Antes, essa chamada acontecia dentro de POST /claim (pagamento automático a
cada resgate). Com o sistema de saldo + saque, o claim só credita saldo — o
dinheiro só sai daqui, quando o admin aprova um saque (`admin.py`,
POST /admin/withdrawals/{id}/approve). Centralizado neste módulo pra não
criar import circular entre app.py e admin.py.
"""
import logging
import os

import httpx
from fastapi import HTTPException

logger = logging.getLogger("nanofaucet")

FAUCETPAY_API_KEY = os.getenv("FAUCETPAY_API_KEY", "")
FAUCETPAY_CURRENCY = os.getenv("FAUCETPAY_CURRENCY", "MATIC")
FAUCETPAY_URL = "https://faucetpay.io/api/v1/send"

# true = não paga de verdade, só simula (mesma flag que já existia pro /claim)
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"


async def send_payout(wallet: str, amount: float) -> dict:
    """Envia o pagamento via API da FaucetPay. Em DRY_RUN, apenas simula."""
    if DRY_RUN or not FAUCETPAY_API_KEY:
        return {"status": 200, "message": "dry_run", "payout_id": None}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            FAUCETPAY_URL,
            data={
                "api_key": FAUCETPAY_API_KEY,
                "amount": amount,
                "to": wallet,
                "currency": FAUCETPAY_CURRENCY,
            },
        )
        data = resp.json()
        if data.get("status") != 200:
            raise HTTPException(
                status_code=402,
                detail=f"Falha no pagamento FaucetPay: {data.get('message', 'erro desconhecido')}",
            )
        return data
