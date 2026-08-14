"""
Validação de JWT do Supabase Auth para as rotas /admin/*.

Não faz nenhuma chamada de rede — decodifica localmente com o JWT Secret
do projeto Supabase (Settings → API → JWT Settings). Isso mantém a
validação rápida e sem dependência externa por request.

Fluxo:
  1. O frontend admin (admin.html) faz login direto contra o Supabase via
     supabase-js (signInWithPassword) e recebe um access_token (JWT).
  2. Cada request a /admin/* envia esse token em `Authorization: Bearer <token>`.
  3. Este módulo decodifica o JWT com SUPABASE_JWT_SECRET e confirma que o
     `sub` (user_id) tem uma linha em `admin_profiles` (db.is_admin) — sem
     essa linha, o usuário está autenticado no Supabase mas NÃO autorizado
     a mexer no faucet (ver HANDOFF, Fase 2.2).
"""
import os

import jwt
from fastapi import Header, HTTPException

import database as db

SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")


def _decode_supabase_jwt(authorization: str | None) -> dict:
    """Decodifica e valida o JWT do Supabase Auth (compartilhado por
    require_admin e require_user — a única diferença entre os dois é a
    checagem extra de `admin_profiles`)."""
    if not SUPABASE_JWT_SECRET:
        raise HTTPException(status_code=500, detail="SUPABASE_JWT_SECRET não configurado no servidor")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token ausente")

    token = authorization.removeprefix("Bearer ").strip()
    try:
        return jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")


async def require_user(authorization: str = Header(None)) -> dict:
    """Exige apenas uma sessão válida do Supabase Auth (conta de usuário
    comum, criada com e-mail+senha) — ao contrário de `require_admin`, NÃO
    checa `admin_profiles`. Usado pelas rotas /me/* (dashboard do usuário)."""
    payload = _decode_supabase_jwt(authorization)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token inválido")
    return {"user_id": user_id, "email": payload.get("email")}


async def require_admin(authorization: str = Header(None)) -> str:
    # Falha explícita em vez de aceitar qualquer coisa — ao contrário do
    # captcha (que tem um modo dev proposital), autenticação de admin
    # nunca deve "abrir" silenciosamente por falta de configuração. Essa
    # checagem já acontece dentro de `_decode_supabase_jwt`.
    payload = _decode_supabase_jwt(authorization)
    user_id = payload.get("sub")
    if not user_id or not await db.is_admin(user_id):
        raise HTTPException(status_code=403, detail="Sem permissão de admin")
    return user_id
