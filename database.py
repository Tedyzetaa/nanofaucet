"""
Camada de dados do faucet — SQLAlchemy assíncrono.

DB-agnóstico via `DATABASE_URL`:
  - Sem a variável definida -> SQLite local assíncrono (`aiosqlite`), zero
    setup, bom pra dev (`sqlite+aiosqlite:///faucet.db`).
  - Com `DATABASE_URL=postgresql+asyncpg://...` (connection string do
    connection pooler do Supabase, porta 6543, modo transaction) -> Postgres
    em produção, sem mudar nenhuma query (todo o SQL abaixo é compatível com
    os dois dialetos, incluindo o `ON CONFLICT ... DO NOTHING`).

    IMPORTANTE: usar sempre o pooler (porta 6543), não a conexão direta
    (porta 5432) — o Render free tier + Postgres direto esgota conexões
    rápido. `pool_pre_ping=True` é obrigatório porque o pooler do Supabase
    derruba conexões ociosas; sem isso aparecem `OperationalError`
    esporádicos em produção.

Todas as funções públicas deste módulo são `async def` e abrem sua própria
sessão internamente (`async with async_session() as session:`), mantendo a
mesma assinatura que o `app.py` já usava — a única mudança do lado de fora é
adicionar `await` nas chamadas.

Anti-race-condition: cooldown (por wallet E por IP) é resolvido com uma
tabela `rate_locks` (chave = "wallet:<endereço>" ou "ip:<ip>") usando um
UPDATE condicional atômico, seguido de INSERT ... ON CONFLICT DO NOTHING se
a chave ainda não existir. Isso é seguro sob concorrência tanto no SQLite
(single-writer) quanto no Postgres (linha travada pelo próprio UPDATE), sem
precisar de locks explícitos ou SELECT FOR UPDATE.
"""
import os
import time

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///faucet.db")
_is_sqlite = DATABASE_URL.startswith("sqlite")


def mask_wallet(wallet: str) -> str:
    """Mascara wallet/e-mail para uso em LOGS (nunca no banco ou no painel
    admin — lá o dado completo continua necessário para operação/suporte).
    'user@example.com' -> 'u***@example.com'; endereço EVM mantém só as
    pontas: '0x1234...abcd'."""
    if not wallet:
        return wallet
    if "@" in wallet:
        local, _, domain = wallet.partition("@")
        return f"{local[:1]}***@{domain}"
    if wallet.startswith("0x") and len(wallet) > 10:
        return f"{wallet[:6]}...{wallet[-4:]}"
    return wallet[:2] + "***"


def mask_ip(ip: str) -> str:
    """Mascara o último octeto/grupo do IP para logs (mantém utilidade para
    detectar padrões de abuso por sub-rede sem gravar o IP completo em texto
    plano nos logs)."""
    if not ip or ip == "unknown":
        return ip
    if "." in ip:  # IPv4
        parts = ip.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.xxx"
    if ":" in ip:  # IPv6
        parts = ip.split(":")
        return ":".join(parts[:4]) + ":xxxx"
    return ip

# timeout maior evita "database is locked" sob concorrência (o SQLite serializa
# escritas; com WAL, leituras não bloqueiam escritas concorrentes)
_connect_args = {"timeout": 15} if _is_sqlite else {}

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,  # obrigatório com o pooler do Supabase (derruba conexões ociosas)
    future=True,
)

async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        if _is_sqlite:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=15000"))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    amount REAL NOT NULL,
                    created_at INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'paid',
                    payout_ref TEXT
                )
            """))
        else:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS claims (
                    id SERIAL PRIMARY KEY,
                    wallet TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    amount DOUBLE PRECISION NOT NULL,
                    created_at BIGINT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'paid',
                    payout_ref TEXT
                )
            """))
            # migração idempotente para bancos já existentes (criados antes do
            # padrão outbox) — ALTER TABLE ... ADD COLUMN IF NOT EXISTS é
            # seguro em Postgres e não falha se a coluna já existir.
            await conn.execute(text("ALTER TABLE claims ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'paid'"))
            await conn.execute(text("ALTER TABLE claims ADD COLUMN IF NOT EXISTS payout_ref TEXT"))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS blocklist (
                identifier TEXT PRIMARY KEY,
                reason TEXT,
                created_at BIGINT NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS rate_locks (
                id TEXT PRIMARY KEY,
                next_allowed_at BIGINT NOT NULL
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_claims_wallet ON claims(wallet)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_claims_ip ON claims(ip)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_claims_created_at ON claims(created_at)"))

        # Fase 2 — tabela de autorização de admin (Supabase Auth cuida de
        # auth.users; esta tabela só existe no Postgres/Supabase, mas criamos
        # o CREATE TABLE condicional também pra manter dev local em SQLite
        # funcional sem admin de verdade — is_admin() simplesmente retorna
        # False se a tabela não tiver a linha correspondente).
        if not _is_sqlite:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS admin_profiles (
                    user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
                    role TEXT NOT NULL DEFAULT 'admin',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """))
        else:
            # dev local: mesma forma, sem FK pra auth.users (não existe em SQLite)
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS admin_profiles (
                    user_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL DEFAULT 'admin',
                    created_at INTEGER NOT NULL
                )
            """))

        # Fase 3 — conta do usuário final (dashboard: saldo, saque, histórico).
        # Supabase Auth cuida de auth.users; aqui só guardamos a wallet que o
        # usuário vinculou à própria conta (é contra essa wallet que batemos
        # os claims dele pra calcular o saldo). Em produção este schema
        # também é criado por `supabase/002_user_profiles.sql` (que também
        # adiciona a RLS) — o CREATE TABLE aqui é idempotente e só garante
        # que o dev local em SQLite funcione sem depender do Supabase.
        if not _is_sqlite:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
                    wallet TEXT UNIQUE,
                    referral_code TEXT UNIQUE,
                    referred_by uuid REFERENCES user_profiles(user_id),
                    streak_count INT NOT NULL DEFAULT 0,
                    last_claim_day DATE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """))
        else:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    wallet TEXT UNIQUE,
                    referral_code TEXT UNIQUE,
                    referred_by TEXT,
                    streak_count INTEGER NOT NULL DEFAULT 0,
                    last_claim_day TEXT,
                    created_at INTEGER NOT NULL
                )
            """))

        # Fase 3 — saques. Cada linha é um pedido do usuário; fica 'pending'
        # até o admin aprovar (dispara o payout real via FaucetPay,
        # `payouts.send_payout`) ou rejeitar (devolve o valor ao saldo
        # disponível, já que o cálculo de saldo exclui 'rejected').
        if _is_sqlite:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    wallet TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    payout_ref TEXT,
                    admin_note TEXT,
                    created_at INTEGER NOT NULL,
                    processed_at INTEGER
                )
            """))
        else:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id SERIAL PRIMARY KEY,
                    user_id uuid NOT NULL,
                    wallet TEXT NOT NULL,
                    amount DOUBLE PRECISION NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    payout_ref TEXT,
                    admin_note TEXT,
                    created_at BIGINT NOT NULL,
                    processed_at BIGINT
                )
            """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_withdrawals_wallet ON withdrawals(wallet)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_withdrawals_user_id ON withdrawals(user_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status)"))


async def try_acquire_slot(scope: str, identifier: str, cooldown_seconds: int, now: int | None = None):
    """
    Tenta reservar atomicamente um "slot" de resgate para `scope:identifier`
    (ex: scope="wallet", identifier="user@mail.com" ou scope="ip", identifier="1.2.3.4").

    Retorna (allowed: bool, wait_seconds: int).
    Se allowed=True, o lock já foi gravado (próximo resgate liberado só após
    `cooldown_seconds`) — não é preciso nenhuma escrita adicional pra fazer
    valer o cooldown.

    Uso de `release_slot` é necessário APENAS se o pagamento falhar depois de
    o slot já ter sido reservado, pra não penalizar o usuário por um erro que
    não foi culpa dele.
    """
    now = now if now is not None else int(time.time())
    key = f"{scope}:{identifier}"
    next_allowed = now + cooldown_seconds

    async with async_session() as session:
        async with session.begin():
            # 1) tenta atualizar um lock existente que já expirou
            result = await session.execute(
                text("""
                    UPDATE rate_locks SET next_allowed_at = :next_allowed
                    WHERE id = :key AND next_allowed_at <= :now
                """),
                {"next_allowed": next_allowed, "key": key, "now": now},
            )
            if result.rowcount == 1:
                return True, 0

            # 2) lock não existia ainda ou não expirou. Tenta criar (falha
            #    silenciosa em corrida concorrente graças ao ON CONFLICT DO NOTHING).
            insert_result = await session.execute(
                text("""
                    INSERT INTO rate_locks (id, next_allowed_at) VALUES (:key, :next_allowed)
                    ON CONFLICT (id) DO NOTHING
                """),
                {"key": key, "next_allowed": next_allowed},
            )
            if insert_result.rowcount == 1:
                return True, 0

            # 3) ainda em cooldown — busca o valor atual pra informar quanto falta
            row = (await session.execute(
                text("SELECT next_allowed_at FROM rate_locks WHERE id = :key"),
                {"key": key},
            )).fetchone()
            wait = max(0, (row[0] if row else next_allowed) - now)
            return False, wait


async def release_slot(scope: str, identifier: str):
    """Libera um slot reservado (usado quando o pagamento falha após o lock ser adquirido)."""
    key = f"{scope}:{identifier}"
    async with async_session() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM rate_locks WHERE id = :key"), {"key": key})


async def create_pending_claim(wallet: str, ip: str, amount: float) -> int:
    """Padrão outbox: grava o claim como 'pending' ANTES de chamar a FaucetPay.
    Se o processo morrer entre o payout e a confirmação, o registro já existe
    e pode ser reconciliado manualmente (em vez de o pagamento sair sem
    nenhum rastro no banco). Retorna o id do claim criado."""
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                text("""
                    INSERT INTO claims (wallet, ip, amount, created_at, status)
                    VALUES (:wallet, :ip, :amount, :created_at, 'pending')
                """ + ("" if _is_sqlite else " RETURNING id")),
                {"wallet": wallet, "ip": ip, "amount": amount, "created_at": int(time.time())},
            )
            if _is_sqlite:
                row = (await session.execute(text("SELECT last_insert_rowid()"))).fetchone()
                return row[0]
            return result.fetchone()[0]


async def mark_claim_paid(claim_id: int, payout_ref: str | None = None):
    async with async_session() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE claims SET status = 'paid', payout_ref = :ref WHERE id = :id"),
                {"id": claim_id, "ref": payout_ref},
            )


async def mark_claim_failed(claim_id: int, reason: str = ""):
    """Marca o claim como falho em vez de apagar — mantém rastro de tentativas
    que não resultaram em pagamento, útil pra auditoria e debugging."""
    async with async_session() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE claims SET status = 'failed', payout_ref = :ref WHERE id = :id"),
                {"id": claim_id, "ref": reason[:250] if reason else None},
            )


async def count_ip_claims_since(ip: str, since: int) -> int:
    # conta 'pending' e 'paid' (não 'failed') pro limite diário — uma
    # tentativa que falhou no payout não deve consumir a cota do usuário.
    async with async_session() as session:
        row = (await session.execute(
            text("SELECT COUNT(*) AS c FROM claims WHERE ip = :ip AND created_at > :since AND status != 'failed'"),
            {"ip": ip, "since": since},
        )).fetchone()
        return row[0] if row else 0


async def is_blocked(identifier: str) -> bool:
    async with async_session() as session:
        row = (await session.execute(
            text("SELECT 1 FROM blocklist WHERE identifier = :identifier"),
            {"identifier": identifier},
        )).fetchone()
        return row is not None


async def add_to_blocklist(identifier: str, reason: str = ""):
    """Usado pelo painel admin (`/admin/blocklist`) — bane wallet ou IP."""
    async with async_session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO blocklist (identifier, reason, created_at) VALUES (:identifier, :reason, :created_at)
                    ON CONFLICT (identifier) DO UPDATE SET reason = :reason
                """),
                {"identifier": identifier, "reason": reason, "created_at": int(time.time())},
            )


async def remove_from_blocklist(identifier: str):
    async with async_session() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM blocklist WHERE identifier = :identifier"), {"identifier": identifier})


async def list_blocklist(limit: int = 200):
    async with async_session() as session:
        rows = (await session.execute(
            text("SELECT identifier, reason, created_at FROM blocklist ORDER BY created_at DESC LIMIT :limit"),
            {"limit": limit},
        )).fetchall()
        return [{"identifier": r[0], "reason": r[1], "created_at": r[2]} for r in rows]


async def list_claims(limit: int = 50, offset: int = 0, status: str | None = None):
    query = """
        SELECT id, wallet, ip, amount, created_at, status, payout_ref FROM claims
        {where}
        ORDER BY created_at DESC LIMIT :limit OFFSET :offset
    """
    params = {"limit": limit, "offset": offset}
    where = ""
    if status:
        where = "WHERE status = :status"
        params["status"] = status
    async with async_session() as session:
        rows = (await session.execute(text(query.format(where=where)), params)).fetchall()
        return [
            {
                "id": r[0], "wallet": r[1], "ip": r[2], "amount": r[3],
                "created_at": r[4], "status": r[5], "payout_ref": r[6],
            }
            for r in rows
        ]


async def get_stats():
    day_ago = int(time.time()) - 86400
    async with async_session() as session:
        total_paid = (await session.execute(
            text("SELECT COALESCE(SUM(amount),0) AS s FROM claims WHERE created_at > :day_ago AND status = 'paid'"),
            {"day_ago": day_ago},
        )).fetchone()[0]
        claims_today = (await session.execute(
            text("SELECT COUNT(*) AS c FROM claims WHERE created_at > :day_ago AND status = 'paid'"),
            {"day_ago": day_ago},
        )).fetchone()[0]
        unique_wallets = (await session.execute(
            text("SELECT COUNT(DISTINCT wallet) AS c FROM claims WHERE status = 'paid'"),
        )).fetchone()[0]
    return {
        "total_paid_24h": round(total_paid, 8),
        "claims_today": claims_today,
        "unique_wallets": unique_wallets,
    }


# ---------------- Fase 2 — admin ----------------

async def is_admin(user_id: str) -> bool:
    """Checa se `user_id` (sub do JWT do Supabase Auth) tem linha em
    `admin_profiles`. Sem essa linha, o JWT é válido mas não dá acesso
    a `/admin/*` — a criação dessa linha é manual, via SQL editor do
    Supabase (ver HANDOFF, Fase 2.2)."""
    async with async_session() as session:
        row = (await session.execute(
            text("SELECT 1 FROM admin_profiles WHERE user_id = :user_id"),
            {"user_id": user_id},
        )).fetchone()
        return row is not None


# ---------------- Fase 3 — conta do usuário, saldo e saques ----------------

async def get_user_wallet(user_id: str) -> str | None:
    async with async_session() as session:
        row = (await session.execute(
            text("SELECT wallet FROM user_profiles WHERE user_id = :user_id"),
            {"user_id": user_id},
        )).fetchone()
        return row[0] if row else None


async def set_user_wallet(user_id: str, wallet: str) -> None:
    """Cria/atualiza a linha do usuário em `user_profiles` com a wallet
    vinculada à conta (é contra essa wallet que os claims dele são somados
    pra calcular o saldo). Lança ValueError se a wallet já estiver vinculada
    a OUTRA conta (constraint UNIQUE em `wallet`)."""
    now = int(time.time())
    async with async_session() as session:
        async with session.begin():
            try:
                if _is_sqlite:
                    await session.execute(
                        text("""
                            INSERT INTO user_profiles (user_id, wallet, created_at)
                            VALUES (:user_id, :wallet, :created_at)
                            ON CONFLICT (user_id) DO UPDATE SET wallet = :wallet
                        """),
                        {"user_id": user_id, "wallet": wallet, "created_at": now},
                    )
                else:
                    await session.execute(
                        text("""
                            INSERT INTO user_profiles (user_id, wallet, created_at)
                            VALUES (:user_id, :wallet, now())
                            ON CONFLICT (user_id) DO UPDATE SET wallet = :wallet
                        """),
                        {"user_id": user_id, "wallet": wallet},
                    )
            except IntegrityError:
                raise ValueError("Essa wallet já está vinculada a outra conta")


async def get_balance(wallet: str) -> dict:
    """Saldo = total creditado por claims 'paid' menos saques que já
    consomem saldo (pending + paid; 'rejected' não conta, o valor volta
    a ficar disponível automaticamente)."""
    async with async_session() as session:
        total_credited = (await session.execute(
            text("SELECT COALESCE(SUM(amount),0) FROM claims WHERE wallet = :wallet AND status = 'paid'"),
            {"wallet": wallet},
        )).fetchone()[0]
        total_withdrawn = (await session.execute(
            text("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE wallet = :wallet AND status = 'paid'"),
            {"wallet": wallet},
        )).fetchone()[0]
        pending_withdrawals = (await session.execute(
            text("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE wallet = :wallet AND status = 'pending'"),
            {"wallet": wallet},
        )).fetchone()[0]
    available = total_credited - total_withdrawn - pending_withdrawals
    return {
        "total_credited": round(total_credited, 8),
        "total_withdrawn": round(total_withdrawn, 8),
        "pending_withdrawals": round(pending_withdrawals, 8),
        "available": round(max(0.0, available), 8),
    }


async def create_withdrawal(user_id: str, wallet: str, amount: float) -> int | None:
    """Cria o pedido de saque de forma atômica: o INSERT só acontece se o
    saldo disponível (calculado dentro do mesmo statement) cobrir o valor
    pedido. Retorna o id criado, ou None se o saldo era insuficiente.

    Nota de concorrência: a checagem de saldo roda dentro do próprio
    INSERT (mesmo statement), então é atômica contra escritas já
    commitadas — mas duas requisições de saque verdadeiramente simultâneas
    do mesmo usuário ainda podem, em tese, ler o mesmo saldo antes de
    qualquer uma commitar (mesmo limite "best-effort" já documentado em
    `count_ip_claims_since`). Risco baixo aqui: é o próprio usuário pedindo
    saque da própria conta, não uma corrida entre partes adversárias."""
    now = int(time.time())
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                text("""
                    INSERT INTO withdrawals (user_id, wallet, amount, status, created_at)
                    SELECT :user_id, :wallet, :amount, 'pending', :created_at
                    WHERE (
                        (SELECT COALESCE(SUM(amount),0) FROM claims WHERE wallet = :wallet AND status = 'paid')
                        - (SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE wallet = :wallet AND status IN ('pending','paid'))
                    ) >= :amount
                """ + ("" if _is_sqlite else " RETURNING id")),
                {"user_id": user_id, "wallet": wallet, "amount": amount, "created_at": now},
            )
            if result.rowcount != 1:
                return None
            if _is_sqlite:
                row = (await session.execute(text("SELECT last_insert_rowid()"))).fetchone()
                return row[0]
            return result.fetchone()[0]


async def get_withdrawal(withdrawal_id: int) -> dict | None:
    async with async_session() as session:
        row = (await session.execute(
            text("""
                SELECT id, user_id, wallet, amount, status, payout_ref, admin_note, created_at, processed_at
                FROM withdrawals WHERE id = :id
            """),
            {"id": withdrawal_id},
        )).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "user_id": row[1], "wallet": row[2], "amount": row[3],
            "status": row[4], "payout_ref": row[5], "admin_note": row[6],
            "created_at": row[7], "processed_at": row[8],
        }


async def mark_withdrawal_paid(withdrawal_id: int, payout_ref: str | None = None) -> bool:
    """Marca como pago SÓ SE ainda estiver 'pending' (evita pagar duas vezes
    o mesmo saque em cliques duplicados/concorrentes). Retorna se atualizou."""
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                text("""
                    UPDATE withdrawals SET status = 'paid', payout_ref = :ref, processed_at = :now
                    WHERE id = :id AND status = 'pending'
                """),
                {"id": withdrawal_id, "ref": payout_ref, "now": int(time.time())},
            )
            return result.rowcount == 1


async def mark_withdrawal_rejected(withdrawal_id: int, reason: str = "") -> bool:
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                text("""
                    UPDATE withdrawals SET status = 'rejected', admin_note = :reason, processed_at = :now
                    WHERE id = :id AND status = 'pending'
                """),
                {"id": withdrawal_id, "reason": reason[:280] if reason else None, "now": int(time.time())},
            )
            return result.rowcount == 1


async def list_user_claims(wallet: str, limit: int = 50, offset: int = 0) -> list[dict]:
    """Histórico de ganhos (claims creditados) de uma wallet — usado no
    dashboard do usuário."""
    async with async_session() as session:
        rows = (await session.execute(
            text("""
                SELECT id, amount, created_at, status FROM claims
                WHERE wallet = :wallet
                ORDER BY created_at DESC LIMIT :limit OFFSET :offset
            """),
            {"wallet": wallet, "limit": limit, "offset": offset},
        )).fetchall()
        return [{"id": r[0], "amount": r[1], "created_at": r[2], "status": r[3]} for r in rows]


async def list_user_withdrawals(user_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    async with async_session() as session:
        rows = (await session.execute(
            text("""
                SELECT id, wallet, amount, status, payout_ref, admin_note, created_at, processed_at
                FROM withdrawals WHERE user_id = :user_id
                ORDER BY created_at DESC LIMIT :limit OFFSET :offset
            """),
            {"user_id": user_id, "limit": limit, "offset": offset},
        )).fetchall()
        return [
            {
                "id": r[0], "wallet": r[1], "amount": r[2], "status": r[3],
                "payout_ref": r[4], "admin_note": r[5], "created_at": r[6], "processed_at": r[7],
            }
            for r in rows
        ]


async def list_withdrawals(limit: int = 50, offset: int = 0, status: str | None = None) -> list[dict]:
    """Usado pelo painel admin — todos os saques, opcionalmente filtrados por status."""
    query = """
        SELECT id, user_id, wallet, amount, status, payout_ref, admin_note, created_at, processed_at
        FROM withdrawals
        {where}
        ORDER BY created_at DESC LIMIT :limit OFFSET :offset
    """
    params = {"limit": limit, "offset": offset}
    where = ""
    if status:
        where = "WHERE status = :status"
        params["status"] = status
    async with async_session() as session:
        rows = (await session.execute(text(query.format(where=where)), params)).fetchall()
        return [
            {
                "id": r[0], "user_id": r[1], "wallet": r[2], "amount": r[3], "status": r[4],
                "payout_ref": r[5], "admin_note": r[6], "created_at": r[7], "processed_at": r[8],
            }
            for r in rows
        ]
