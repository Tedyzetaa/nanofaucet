"""
Testes da camada de dados (database.py).

Rodam contra SQLite em memória (isolado por teste via DATABASE_URL definido
ANTES do import de `database`, já que o engine é criado no import do módulo).

Rode com:
    pip install -r requirements-dev.txt
    pytest tests/ -v
"""
import asyncio
import os
import uuid

import pytest

# precisa ser definido antes de importar `database`, pois o engine async é
# criado no nível do módulo. Cada arquivo de teste usa um DB em memória
# isolado (":memory:" seria compartilhado entre conexões do pool, então
# usamos um arquivo temporário por sessão de teste).
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:?cache=shared")

import database as db  # noqa: E402


@pytest.fixture(autouse=True)
async def _init_db():
    await db.init_db()
    yield


@pytest.mark.asyncio
async def test_try_acquire_slot_blocks_second_immediate_attempt():
    key = f"wallet:{uuid.uuid4()}"
    scope, identifier = key.split(":", 1)

    allowed_1, wait_1 = await db.try_acquire_slot(scope, identifier, cooldown_seconds=60)
    assert allowed_1 is True
    assert wait_1 == 0

    allowed_2, wait_2 = await db.try_acquire_slot(scope, identifier, cooldown_seconds=60)
    assert allowed_2 is False
    assert wait_2 > 0


@pytest.mark.asyncio
async def test_try_acquire_slot_allows_after_release():
    identifier = str(uuid.uuid4())
    allowed_1, _ = await db.try_acquire_slot("ip", identifier, cooldown_seconds=60)
    assert allowed_1 is True

    await db.release_slot("ip", identifier)

    allowed_2, _ = await db.try_acquire_slot("ip", identifier, cooldown_seconds=60)
    assert allowed_2 is True


@pytest.mark.asyncio
async def test_try_acquire_slot_is_atomic_under_concurrency():
    """O teste que mais importa: dispara N tentativas concorrentes para o
    MESMO identificador e garante que só uma delas ganha o slot — é
    exatamente a race condition que o design com UPDATE condicional +
    INSERT ON CONFLICT DO NOTHING foi feito para evitar."""
    identifier = str(uuid.uuid4())
    n_concurrent = 25

    results = await asyncio.gather(*[
        db.try_acquire_slot("wallet", identifier, cooldown_seconds=60)
        for _ in range(n_concurrent)
    ])

    allowed_count = sum(1 for allowed, _ in results if allowed)
    assert allowed_count == 1, (
        f"esperado exatamente 1 slot concedido sob concorrência, "
        f"obtido {allowed_count} de {n_concurrent} tentativas simultâneas"
    )


@pytest.mark.asyncio
async def test_outbox_pending_then_paid():
    wallet = f"{uuid.uuid4()}@example.com"
    claim_id = await db.create_pending_claim(wallet, "1.2.3.4", 0.0000001)

    pending = await db.list_claims(limit=10, status="pending")
    assert any(c["id"] == claim_id and c["status"] == "pending" for c in pending)

    await db.mark_claim_paid(claim_id, payout_ref="abc123")

    paid = await db.list_claims(limit=10, status="paid")
    assert any(c["id"] == claim_id and c["payout_ref"] == "abc123" for c in paid)


@pytest.mark.asyncio
async def test_outbox_pending_then_failed_not_counted_in_stats():
    wallet = f"{uuid.uuid4()}@example.com"
    claim_id = await db.create_pending_claim(wallet, "5.6.7.8", 0.0000001)
    await db.mark_claim_failed(claim_id, reason="faucetpay_rejected")

    failed = await db.list_claims(limit=10, status="failed")
    assert any(c["id"] == claim_id for c in failed)

    stats = await db.get_stats()
    # claims 'failed' não devem inflar o total pago público
    assert stats["total_paid_24h"] >= 0


def test_mask_wallet_email():
    assert db.mask_wallet("teste@example.com") == "t***@example.com"


def test_mask_wallet_evm_address():
    addr = "0x1234567890abcdef1234567890abcdef12345678"
    masked = db.mask_wallet(addr)
    assert masked.startswith("0x1234")
    assert masked.endswith(addr[-4:])
    assert addr not in masked or len(masked) < len(addr)


def test_mask_ip_v4():
    assert db.mask_ip("45.5.242.243") == "45.5.242.xxx"
