"""
NANOFAUCET backend — FastAPI

Endpoints:
  POST /claim   -> valida wallet, captcha, cooldown (wallet+IP) e anti-fraude, paga via FaucetPay, registra
  GET  /stats   -> estatísticas públicas (total pago 24h, resgates hoje, carteiras únicas)
  GET  /health  -> healthcheck para o Render

Rode local:
  pip install -r requirements.txt
  uvicorn app:app --reload --port 8000
"""
import logging
import os
import re
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, Field

import database as db
from admin import router as admin_router

# ---------------- logging ----------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("nanofaucet")

# ---------------- configuração ----------------
CLAIM_AMOUNT = float(os.getenv("CLAIM_AMOUNT", "0.00000010"))   # em cripto (ex: MATIC)
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "60"))     # tempo entre resgates por wallet
IP_COOLDOWN_SECONDS = int(os.getenv("IP_COOLDOWN_SECONDS", "5"))  # tempo mínimo entre requisições do mesmo IP
MAX_CLAIMS_PER_IP_PER_DAY = int(os.getenv("MAX_CLAIMS_PER_IP_PER_DAY", "50"))

FAUCETPAY_API_KEY = os.getenv("FAUCETPAY_API_KEY", "")
FAUCETPAY_CURRENCY = os.getenv("FAUCETPAY_CURRENCY", "MATIC")
FAUCETPAY_URL = "https://faucetpay.io/api/v1/send"

HCAPTCHA_SECRET = os.getenv("HCAPTCHA_SECRET", "")
HCAPTCHA_VERIFY_URL = "https://hcaptcha.com/siteverify"

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"  # true = não paga de verdade, só simula

# "production" trava comportamentos fail-open que fazem sentido em dev mas
# são perigosos em produção (captcha aceito sem verificação, docs expostos).
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
IS_PRODUCTION = ENVIRONMENT == "production"

SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=SENTRY_DSN, environment=ENVIRONMENT, traces_sample_rate=0.1)
    except ImportError:
        logger.warning("SENTRY_DSN configurado mas sentry-sdk não instalado")

# origens permitidas configuráveis via env (fallback pros valores atuais de produção/dev)
_default_origins = "https://nanofaucet-green.vercel.app,http://localhost:5500,http://127.0.0.1:5500"
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()]

# validação de wallet: e-mail OU endereço EVM (0x + 40 hex). Ajuste se a FaucetPay
# aceitar outros formatos de identificador para a moeda configurada.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

@asynccontextmanager
async def lifespan(app: FastAPI):
    if IS_PRODUCTION and not HCAPTCHA_SECRET:
        # mesma filosofia fail-closed já aplicada ao admin (auth.py): em
        # produção, a ausência do secret do captcha NUNCA deve resultar em
        # aceitar qualquer token silenciosamente — melhor não subir.
        raise RuntimeError(
            "ENVIRONMENT=production mas HCAPTCHA_SECRET não está configurado. "
            "Defina HCAPTCHA_SECRET ou rode com ENVIRONMENT=development."
        )
    await db.init_db()
    yield


app = FastAPI(
    title="Nanofaucet API",
    lifespan=lifespan,
    # em produção não expomos o schema completo da API (inclui rotas /admin/*)
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(admin_router, prefix="/admin")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Normaliza erros de validação do Pydantic pro mesmo formato {"detail": "<mensagem>"}
    que o frontend já espera (em vez do array padrão do FastAPI)."""
    first_error = exc.errors()[0]
    message = first_error.get("msg", "Dados inválidos").replace("Value error, ", "")
    return JSONResponse(status_code=400, content={"detail": message})


class ClaimRequest(BaseModel):
    wallet: str = Field(..., max_length=120)
    captcha_token: str = Field(..., max_length=4000)

    @field_validator("wallet")
    @classmethod
    def validate_wallet(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("Endereço/e-mail obrigatório")
        if not (_EMAIL_RE.match(v) or _EVM_ADDRESS_RE.match(v)):
            raise ValueError("Formato de endereço/e-mail inválido")
        return v


def get_client_ip(request: Request) -> str:
    """
    Em produção (Render), o proxy da plataforma injeta X-Forwarded-For de forma
    confiável e o app não é acessível diretamente pulando o proxy — por isso
    confiamos no primeiro IP do header. Se este backend for hospedado atrás de
    outro proxy/CDN no futuro, revalidar essa suposição (o header pode ser
    forjado por quem conectar direto ao processo).
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def verify_captcha(token: str, remote_ip: str) -> bool:
    """Verifica o token do hCaptcha. Se HCAPTCHA_SECRET não estiver configurado,
    aceita qualquer token — mas isso só é alcançável em desenvolvimento, pois
    o lifespan acima recusa subir em produção sem o secret (fail-closed)."""
    if not HCAPTCHA_SECRET:
        logger.warning("HCAPTCHA_SECRET não configurado — aceitando captcha sem verificar (modo dev)")
        return True
    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.post(
            HCAPTCHA_VERIFY_URL,
            data={"secret": HCAPTCHA_SECRET, "response": token, "remoteip": remote_ip},
        )
        data = resp.json()
        return bool(data.get("success"))


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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/stats")
async def stats():
    return await db.get_stats()


@app.post("/claim")
async def claim(payload: ClaimRequest, request: Request):
    wallet = payload.wallet  # já normalizada (trim + lowercase) pelo validator
    ip = get_client_ip(request)

    # dados mascarados só para uso em log — nunca gravados assim no banco
    m_wallet, m_ip = db.mask_wallet(wallet), db.mask_ip(ip)

    # anti-fraude: bloqueio manual (VPN/proxy conhecidos, contas banidas, etc.)
    if await db.is_blocked(wallet) or await db.is_blocked(ip):
        logger.warning("claim bloqueado: wallet=%s ip=%s (blocklist)", m_wallet, m_ip)
        raise HTTPException(status_code=403, detail="Conta ou IP bloqueado por atividade suspeita")

    # limite diário por IP (checagem best-effort; a proteção "dura" contra
    # burst é o cooldown por IP logo abaixo, que é atômico)
    now = int(time.time())
    day_ago = now - 86400
    if await db.count_ip_claims_since(ip, day_ago) >= MAX_CLAIMS_PER_IP_PER_DAY:
        logger.warning("claim bloqueado: ip=%s (limite diário atingido)", m_ip)
        raise HTTPException(status_code=429, detail="Limite diário de resgates deste IP atingido")

    # captcha
    if not await verify_captcha(payload.captcha_token, ip):
        logger.warning("claim bloqueado: wallet=%s ip=%s (captcha falhou)", m_wallet, m_ip)
        raise HTTPException(status_code=400, detail="Verificação anti-bot falhou")

    # cooldown atômico por IP (evita burst de wallets diferentes no mesmo IP)
    ip_allowed, ip_wait = await db.try_acquire_slot("ip", ip, IP_COOLDOWN_SECONDS, now)
    if not ip_allowed:
        raise HTTPException(status_code=429, detail=f"Aguarde {ip_wait}s antes de tentar novamente")

    # cooldown atômico por wallet (substitui o SELECT+INSERT antigo, que tinha
    # race condition entre a checagem e o registro do claim)
    wallet_allowed, wallet_wait = await db.try_acquire_slot("wallet", wallet, COOLDOWN_SECONDS, now)
    if not wallet_allowed:
        await db.release_slot("ip", ip)  # não penaliza o IP por uma tentativa negada no nível de wallet
        raise HTTPException(status_code=429, detail=f"Aguarde {wallet_wait}s para resgatar novamente")

    # PADRÃO OUTBOX: grava o claim como 'pending' ANTES do payout. Se o
    # processo morrer entre a chamada à FaucetPay e a atualização de status,
    # sobra um registro 'pending' rastreável (e reconciliável manualmente)
    # em vez de um pagamento sem nenhum vestígio no banco.
    claim_id = await db.create_pending_claim(wallet, ip, CLAIM_AMOUNT)

    try:
        payout_result = await send_payout(wallet, CLAIM_AMOUNT)
    except HTTPException:
        # pagamento falhou — libera os locks pra não penalizar o usuário por
        # um erro nosso, e marca o claim como failed (mantém o rastro).
        await db.release_slot("wallet", wallet)
        await db.release_slot("ip", ip)
        await db.mark_claim_failed(claim_id, reason="faucetpay_rejected")
        raise
    except Exception:
        await db.release_slot("wallet", wallet)
        await db.release_slot("ip", ip)
        await db.mark_claim_failed(claim_id, reason="unexpected_error")
        logger.exception("erro inesperado no payout: wallet=%s claim_id=%s", m_wallet, claim_id)
        raise HTTPException(status_code=502, detail="Erro ao processar pagamento, tente novamente")

    # confirma
    payout_ref = str(payout_result.get("payout_id")) if isinstance(payout_result, dict) else None
    await db.mark_claim_paid(claim_id, payout_ref)
    logger.info("claim ok: wallet=%s ip=%s amount=%s claim_id=%s", m_wallet, m_ip, CLAIM_AMOUNT, claim_id)

    return {
        "status": "ok",
        "amount": CLAIM_AMOUNT,
        "currency": FAUCETPAY_CURRENCY,
        "cooldown_seconds": COOLDOWN_SECONDS,
    }
