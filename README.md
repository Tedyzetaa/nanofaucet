# NANOFAUCET

Site + backend de faucet de criptomoedas, construídos a partir do
`Plano_Projeto_Faucet_Cripto.docx`. Captcha real (hCaptcha) e anúncios reais
(A-Ads + Adsterra) já integrados. Deploy ativo:

- Frontend (Vercel): https://nanofaucet-green.vercel.app
- Backend (Render): https://nanofaucet-ko6i.onrender.com

```
/  (tudo numa pasta só, decisão deliberada — ver CONTEXTO_PARA_IA.md)
├── index.html          # frontend público (matrix rain, boot sequence, hCaptcha, ads)
├── admin.html            # painel admin (login Supabase, claims, blocklist)
├── app.py               # API FastAPI (/claim, /stats, /health) + inclui o router /admin
├── admin.py              # rotas /admin/* (claims, blocklist, config) — protegidas por JWT
├── auth.py               # valida localmente o JWT do Supabase Auth (require_admin)
├── database.py           # camada de dados assíncrona — SQLite (dev) OU Postgres/Supabase via DATABASE_URL
├── supabase/              # SQL pra rodar manualmente no SQL editor do Supabase
│   ├── 001_admin_profiles.sql   # RLS da tabela de autorização de admin
│   └── 002_user_profiles.sql    # schema + trigger de auto-signup (streak/referral, Fase 3)
├── requirements.txt
├── .python-version       # fixa a versão do Python no Render (3.11.9) — evita quebra de build em versões novas sem wheel pré-compilada
├── .env.example
├── .gitignore            # garante que faucet.db e .env nunca sejam commitados
└── vercel.json           # serve index.html e admin.html + headers de segurança
```

> Para o histórico completo de decisões, bugs já resolvidos e o que não fazer
> sem perguntar, ver `CONTEXTO_PARA_IA.md`.

## 1. Rodar local

```bash
cp .env.example .env
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Depois abra `index.html` direto no navegador (ele já aponta para
`http://127.0.0.1:8000` quando detecta `localhost`/`file:`). Sem servidor de
arquivos necessário.

`DRY_RUN=true` no `.env` faz o claim funcionar sem pagar de verdade — use isso
pra testar o fluxo inteiro (captcha → resgate → cooldown → stats) antes de
colocar dinheiro real em jogo.

## 2. Variáveis de ambiente

| Variável | Padrão | Observação |
|---|---|---|
| `CLAIM_AMOUNT` | `0.00000010` | valor por resgate |
| `COOLDOWN_SECONDS` | `60` | tempo entre resgates da mesma wallet |
| `IP_COOLDOWN_SECONDS` | `5` | tempo mínimo entre requisições do mesmo IP (evita burst com várias wallets) |
| `MAX_CLAIMS_PER_IP_PER_DAY` | `50` | limite diário por IP |
| `FAUCETPAY_API_KEY` | (vazio) | sem isso, pagamento fica em modo simulado mesmo com `DRY_RUN=false` |
| `FAUCETPAY_CURRENCY` | `MATIC` | |
| `HCAPTCHA_SECRET` | (vazio) | **sem isso, qualquer captcha é aceito — nunca deixar vazio em produção** |
| `DRY_RUN` | `true` | `false` = pagamento real via FaucetPay |
| `DATABASE_URL` | `sqlite+aiosqlite:///faucet.db` | ver seção 4 — configure com a connection string (pooler, asyncpg) do Supabase em produção |
| `ALLOWED_ORIGINS` | domínio de produção + localhost | lista separada por vírgula, pra quando o domínio mudar |
| `LOG_LEVEL` | `INFO` | |
| `SUPABASE_URL` | (vazio) | só necessário pra habilitar o painel `/admin` — `Project Settings → API` |
| `SUPABASE_JWT_SECRET` | (vazio) | idem — sem isso o backend responde 500 em qualquer rota `/admin/*` (falha explícita, nunca abre sem checar) |

## 3. O que já está implementado

- **Anti-bot real**: widget hCaptcha integrado (não é mais placeholder)
- **Validação de wallet**: e-mail ou endereço EVM (`0x...`), normalizado para minúsculo
- **Cooldown atômico por wallet E por IP**: sem race condition (testado com 10 requisições
  simultâneas — apenas 1 claim é aceito, mesmo sob concorrência real)
- **Limite diário por IP**: mitiga farm de múltiplas contas no mesmo IP
- **Blocklist manual**: tabela `blocklist` pra banir wallet/IP suspeitos (`db.add_to_blocklist`)
- **Integração FaucetPay**: função `send_payout()` pronta, só falta a API key
- **Stats públicos em tempo real**: total pago 24h, resgates hoje, carteiras únicas
- **Logging estruturado**: tentativas bloqueadas, claims bem-sucedidos e erros de payout
  ficam nos logs do Render
- **CSP e headers de segurança** no `vercel.json`
- **Banco assíncrono de ponta a ponta**: `database.py` usa `AsyncSession`/`create_async_engine`;
  as rotas em `app.py` já fazem `await db.xxx(...)` — nenhuma query bloqueia o event loop do Uvicorn
- **Painel admin (`/admin.html`)**: login via Supabase Auth, protegido por JWT (`auth.py`),
  com visualização de claims e gerência de blocklist direto na UI (`admin.py`)

## 4. Migrar para Supabase (Postgres assíncrono)

`database.py` usa SQLAlchemy assíncrono (`asyncpg`) e é compatível com
Postgres sem mudar nenhuma query. Para migrar:

1. Crie um projeto no Supabase.
2. Project Settings → Database → copie a **Connection string do Transaction
   pooler (porta 6543)** — não a conexão direta (porta 5432): o Render free
   tier + Postgres direto esgota conexões rápido.
3. No Render, configure a env var:
   ```
   DATABASE_URL=postgresql+asyncpg://postgres.[ref]:[senha]@aws-0-[region].pooler.supabase.com:6543/postgres
   ```
   Note o prefixo `postgresql+asyncpg://` (não `+psycopg2`, que era usado
   antes da migração assíncrona).
4. Redeploy. O `init_db()` (chamado no `lifespan` do FastAPI) cria as
   tabelas automaticamente na primeira inicialização — nenhuma migração
   manual necessária pra esse schema simples.
5. Pra habilitar o painel `/admin`, rode manualmente no SQL editor do
   Supabase os arquivos em `supabase/001_admin_profiles.sql` e
   `supabase/002_user_profiles.sql`, depois crie o usuário admin (ver
   seção 8) e ele à mão em `admin_profiles`.

Isso resolve o principal risco estrutural do projeto: o SQLite atual é
efêmero no Render (reseta em redeploy ou spin-down do free tier), o que inclui
**perder a blocklist silenciosamente**.

Rollback: se a migração causar instabilidade, `DATABASE_URL` pode voltar a
apontar pro SQLite (`sqlite+aiosqlite:///faucet.db`) sem mudar código, e as
rotas `/admin/*` podem ser removidas do `include_router` em `app.py` sem
afetar `/claim`, `/stats`, `/health`.

## 5. Onde conseguir os ADS e o gateway de pagamento

| Rede | Foco | Link |
|---|---|---|
| **A-Ads** (já ativo) | Anúncios cripto, aprovação quase instantânea | https://a-ads.com |
| **Adsterra** (já ativo) | Rede grande, boa liquidez pra faucets | https://adsterra.com |
| **Coinzilla** | Focada 100% em cripto/web3, CPM geralmente mais alto | https://coinzilla.com |
| **FaucetPay** (pagamento) | Cadastro → Merchant Settings → API key | https://faucetpay.io |
| **hCaptcha** (captcha, já ativo) | Dashboard → sitekey/secret | https://www.hcaptcha.com |

## 6. Checklist antes de ir ao ar

- [ ] `.env` e `faucet.db` **nunca** commitados (`.gitignore` já cobre isso — mas
      confira o histórico do Git se o projeto já foi versionado antes desta
      correção; se `faucet.db` já foi commitado alguma vez, ele precisa ser
      removido do histórico, não só do commit atual)
- [ ] `HCAPTCHA_SECRET` configurado (nunca deixar vazio em produção)
- [ ] Rodar a conta: CPM real do A-Ads/Adsterra × `CLAIM_AMOUNT` × frequência de
      resgate → confirmar que a margem por resgate é positiva
- [ ] `DATABASE_URL` apontando pro Supabase (evita perder blocklist em redeploy)
- [ ] `FAUCETPAY_API_KEY` configurado e testado com valor pequeno primeiro
- [ ] Cloudflare ativo na frente do domínio
- [ ] Hot wallet da FaucetPay com saldo só de poucos dias de operação
- [ ] `ALLOWED_ORIGINS` restrito ao domínio real de produção
- [ ] Alerta de saldo baixo configurado (painel da FaucetPay ou monitor externo)
- [ ] Se o painel admin for usado: `SUPABASE_JWT_SECRET` configurado no Render
      e testado (request sem token → 401; token sem linha em `admin_profiles`
      → 403; token autorizado → 200)

## 7. Contas de usuário final (magic link) — base pronta, sem UI ainda

`supabase/002_user_profiles.sql` já cria `user_profiles` (wallet, streak,
referral_code, referred_by) e um trigger que gera automaticamente o
`referral_code` quando alguém confirma um magic link
(`supabase.auth.signInWithOtp({email})` no frontend). Login continua
**opcional** — resgatar sem conta segue funcionando exatamente como hoje.
O que falta, deliberadamente fora do escopo desta rodada:

- UI de login/streak/referral no `index.html`
- Endpoint(s) de backend pra vincular wallet ao perfil e consultar streak
- Validar a base de auth estável em produção por alguns dias antes de
  lançar a UI de gamificação

## 8. Painel admin (`/admin.html`)

1. Rode `supabase/001_admin_profiles.sql` e `supabase/002_user_profiles.sql`
   no SQL editor do Supabase.
2. Crie o usuário admin manualmente em Authentication → Users (email +
   senha forte) — **não existe endpoint de signup pra admin**, é proposital.
3. Copie o UUID desse usuário e rode:
   ```sql
   insert into public.admin_profiles (user_id) values ('<uuid>');
   ```
   Sem essa linha, o login funciona mas todo `/admin/*` responde 403.
4. Preencha `SUPABASE_URL`/`SUPABASE_JWT_SECRET` no Render (backend) e
   `SUPABASE_URL`/`SUPABASE_ANON_KEY` no topo do `admin.html` (frontend —
   a anon key não é secreta).
5. Acesse `/admin.html` (ou `/admin`, ver `vercel.json`), logue com
   email/senha. O token de sessão fica só em memória — fechar a aba exige
   logar de novo.

## 9. Próximo passo natural

Com o painel admin no ar, o próximo item de maior valor é a UI de
streak/referral pro usuário final (seção 7) — schema e trigger já prontos,
falta só a superfície visual e alguns endpoints de leitura/escrita de perfil.
