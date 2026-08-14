-- Fase 3.2 — tabela de saques (dashboard do usuário: saldo + botão "sacar").
-- Rodar uma vez no SQL editor do Supabase (Dashboard → SQL Editor), depois
-- de já ter rodado 002_user_profiles.sql (o saque depende da wallet
-- vinculada em user_profiles).
--
-- database.py já cria esta tabela automaticamente no startup do backend
-- (CREATE TABLE IF NOT EXISTS), mas a RLS abaixo precisa ser aplicada
-- manualmente aqui — mesmo motivo do 001_admin_profiles.sql: o backend usa
-- a connection string do pooler (que ignora RLS), e toda a lógica de
-- negócio (checar saldo, valor mínimo, aprovar/rejeitar) mora no FastAPI,
-- não em RLS. Nenhum client-side JS deve ler/escrever esta tabela direto.

create table if not exists public.withdrawals (
    id bigint generated always as identity primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    wallet text not null,
    amount double precision not null,
    status text not null default 'pending', -- pending | paid | rejected
    payout_ref text,
    admin_note text,
    created_at bigint not null,
    processed_at bigint
);

create index if not exists idx_withdrawals_wallet on public.withdrawals(wallet);
create index if not exists idx_withdrawals_user_id on public.withdrawals(user_id);
create index if not exists idx_withdrawals_status on public.withdrawals(status);

alter table public.withdrawals enable row level security;

drop policy if exists "service_role_only" on public.withdrawals;
create policy "service_role_only" on public.withdrawals
    for all using (false);

-- Nota: se no futuro quiser permitir que o frontend leia o histórico de
-- saques direto do Supabase (sem passar pelo backend), troque a policy
-- acima por uma "own_withdrawals_select" com `using (auth.uid() = user_id)`
-- — mas isso não é necessário hoje, o dashboard já lê tudo via /me/withdrawals.
