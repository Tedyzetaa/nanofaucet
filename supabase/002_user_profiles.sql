-- Fase 3.1 — estrutura base para contas de usuário final (magic link),
-- preparando streak/referral sem ainda expor UI de gamificação.
-- Rodar uma vez no SQL editor do Supabase.

create table if not exists public.user_profiles (
    user_id uuid primary key references auth.users(id) on delete cascade,
    wallet text unique,                  -- vincula a wallet FaucetPay à conta
    referral_code text unique not null,  -- gerado no signup (8 chars alfanum)
    referred_by uuid references public.user_profiles(user_id),
    streak_count int not null default 0,
    last_claim_day date,
    created_at timestamptz not null default now()
);

alter table public.user_profiles enable row level security;

-- usuário só lê/edita o próprio perfil
drop policy if exists "own_profile_select" on public.user_profiles;
create policy "own_profile_select" on public.user_profiles
    for select using (auth.uid() = user_id);

drop policy if exists "own_profile_update" on public.user_profiles;
create policy "own_profile_update" on public.user_profiles
    for update using (auth.uid() = user_id);

-- inserts e leitura agregada (rankings) continuam via service_role no backend

-- ---------------------------------------------------------------------
-- Trigger: cria automaticamente a linha em user_profiles quando um novo
-- usuário confirma o magic link (insert em auth.users), já gerando um
-- referral_code único de 8 caracteres alfanuméricos.
-- ---------------------------------------------------------------------

create or replace function public.generate_referral_code()
returns text
language plpgsql
as $$
declare
    chars text := 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'; -- sem chars ambíguos (0/O, 1/I/L)
    code text;
    exists_already boolean;
begin
    loop
        code := '';
        for i in 1..8 loop
            code := code || substr(chars, floor(random() * length(chars) + 1)::int, 1);
        end loop;
        select exists(select 1 from public.user_profiles where referral_code = code) into exists_already;
        exit when not exists_already;
    end loop;
    return code;
end;
$$;

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.user_profiles (user_id, referral_code)
    values (new.id, public.generate_referral_code())
    on conflict (user_id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- Nota: este trigger dispara pra QUALQUER novo usuário em auth.users,
-- incluindo o admin criado manualmente na Fase 2.1. Isso é inofensivo
-- (só cria uma linha extra em user_profiles sem uso), mas se preferir
-- evitar, crie o admin depois de rodar este script, ou filtre por
-- domínio de e-mail dentro de handle_new_user().
