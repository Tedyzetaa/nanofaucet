# Segurança — NANOFAUCET

Este documento registra o que foi corrigido diretamente no código nesta
rodada e — mais importante — o que **exige ação manual fora do repositório**,
que nenhuma mudança de código resolve sozinha.

## ✅ Corrigido no código

| Item | O que mudou |
|---|---|
| `.gitignore` ausente | Criado, cobrindo `.env`, `*.db`, `__pycache__/`, etc. |
| `faucet.db` versionado | Removido do diretório de trabalho. |
| Captcha fail-open em produção | `lifespan()` agora recusa subir se `ENVIRONMENT=production` e `HCAPTCHA_SECRET` estiver vazio. |
| `/docs`, `/redoc`, `/openapi.json` expostos | Desabilitados quando `ENVIRONMENT=production`. |
| PII em texto plano nos logs | `wallet` e `ip` mascarados (`mask_wallet`, `mask_ip` em `database.py`) antes de qualquer `logger.*`. O banco continua com o dado completo — necessário para operação. |
| Payout sem outbox | `/claim` agora grava o claim como `pending` **antes** do payout, e atualiza para `paid`/`failed` depois — se o processo cair no meio, sobra um registro rastreável em vez de nada. |
| `innerHTML` sem escape no admin | `admin.html` agora escapa `wallet`, `reason`, `identifier` e mensagens de erro antes de injetar no DOM. |
| Falta de testes de concorrência | `tests/test_database.py` inclui um teste com 25 tentativas simultâneas de `try_acquire_slot` para o mesmo identificador — valida a atomicidade que o design já tinha. |
| Falta de CI | `.github/workflows/ci.yml`: roda testes, lint (`ruff`) e `pip-audit` a cada push/PR, e bloqueia merge se um `.db`/`.env` for commitado. |
| Sem observabilidade de erro | `sentry-sdk` adicionado (opcional — só ativa se `SENTRY_DSN` estiver setado). |

## ⚠️ Exige ação manual — não dá pra corrigir só no código

1. **Rotacionar `FAUCETPAY_API_KEY`, `HCAPTCHA_SECRET` e `SUPABASE_JWT_SECRET`.**
   Como não há como confirmar com certeza que essas chaves nunca estiveram
   em um commit local antes deste ajuste, o mais seguro é trocá-las nos
   respectivos painéis (FaucetPay, hCaptcha, Supabase → Settings → API) e
   atualizar as variáveis de ambiente no Render.

2. **Limpar o histórico do Git**, se o repositório já foi enviado ao GitHub
   com o `faucet.db` ou algum `.env` commitado em algum momento:
   ```bash
   # instale git-filter-repo: pip install git-filter-repo
   git filter-repo --path faucet.db --invert-paths
   git filter-repo --path .env --invert-paths
   # depois: force-push (coordene com quem mais tiver clone do repo)
   ```
   Sem isso, o arquivo continua recuperável no histórico mesmo depois de
   "deletado" em um commit novo.

3. **Definir `ENVIRONMENT=production` no Render.** O código agora depende
   dessa variável para acionar os comportamentos fail-closed (captcha e
   `/docs`). Sem ela, o app continua se comportando como se estivesse em
   desenvolvimento.

4. **Habilitar MFA para as contas de admin no Supabase Auth** — isso é uma
   configuração no painel do Supabase (Authentication → Providers/MFA), não
   algo que o código deste repositório controla.

5. **Confirmar rate limiting de login habilitado no Supabase Auth** (proteção
   contra brute-force no login do painel admin) — também é configuração de
   painel, não de código.

6. **Definir uma política de retenção de dados** (LGPD/GDPR): por quanto
   tempo `wallet` + `ip` ficam guardados na tabela `claims`. O código não
   impõe expurgo automático — isso é uma decisão de produto que precisa virar
   um job agendado (ex: Supabase cron, ou uma rotina simples via GitHub
   Actions agendado) apagando ou anonimizando claims com mais de N dias.

7. **Reconciliar claims `pending`/`failed` manualmente** contra o extrato da
   FaucetPay quando `DRY_RUN=false` — use `GET /admin/claims?status=pending`
   para achar registros presos (ex: processo caiu no meio do payout).
