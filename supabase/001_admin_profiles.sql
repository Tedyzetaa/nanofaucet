-- Fase 2.2 — tabela de autorização de admin.
-- Rodar uma vez no SQL editor do Supabase (Dashboard → SQL Editor).
--
-- database.py já cria esta tabela automaticamente no startup do backend
-- (CREATE TABLE IF NOT EXISTS), mas a RLS policy abaixo precisa ser
-- aplicada manualmente aqui, porque o backend usa a service_role key
-- (que ignora RLS) e não há necessidade de replicar a policy em Python.

create table if not exists public.admin_profiles (
    user_id uuid primary key references auth.users(id) on delete cascade,
    role text not null default 'admin',
    created_at timestamptz not null default now()
);

alter table public.admin_profiles enable row level security;

-- só o service_role (usado pelo backend) pode ler/escrever esta tabela;
-- nenhum client-side JS deve acessá-la diretamente
drop policy if exists "service_role_only" on public.admin_profiles;
create policy "service_role_only" on public.admin_profiles
    for all using (false);

-- Depois de criar o usuário admin manualmente em
-- Authentication → Users, pegue o UUID dele e rode:
--
--   insert into public.admin_profiles (user_id) values ('<uuid-do-usuario>');
--
-- Sem essa linha, o login funciona (JWT válido) mas o backend responde
-- 403 em qualquer rota /admin/*.
