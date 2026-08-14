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


async def require_admin(authorization: str = Header(None)) -> str:
    if not SUPABASE_JWT_SECRET:
        # Falha explícita em vez de aceitar qualquer coisa — ao contrário do
        # captcha (que tem um modo dev proposital), autenticação de admin
        # nunca deve "abrir" silenciosamente por falta de configuração.
        raise HTTPException(status_code=500, detail="SUPABASE_JWT_SECRET não configurado no servidor")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token ausente")

    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")

    user_id = payload.get("sub")
    if not user_id or not await db.is_admin(user_id):
        raise HTTPException(status_code=403, detail="Sem permissão de admin")
    return user_id
