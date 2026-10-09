# agent-poc

POC de AI agent com **Temporal** (orquestração + memória de sessão) + **Langfuse** (observabilidade/tracing) + **Claude API** (Haiku 4.5).

## Arquitetura

```
trigger.py / web.py --> Update "ask" (com start) --> Workflow AgentWorkflow (1 por sessão, long-running)
                                                         │
                                                         ├─ acumula self.messages (memória da conversa)
                                                         ├─ acumula self.total_cost_usd (cap de budget por sessão)
                                                         └─ Activities por pergunta:
                                                              ├─ call_claude        (chama Claude, loga generation no Langfuse)
                                                              ├─ execute_tool       (get_weather real via Open-Meteo, calculate local; guardrails)
                                                              ├─ summarize_messages (comprime histórico antigo quando fica grande demais)
                                                              └─ log_event          (score leve no Langfuse, ex: limite de iteração atingido)
```

**1 sessão = 1 workflow long-running**, não 1 workflow por pergunta. `workflow_id` é o `session_id`. Cliente manda pergunta via `execute_update_with_start_workflow` (RPC síncrono: se o workflow já existe, entra nele; se não, cria) — nunca com `start_workflow` direto. O workflow fica `Running` esperando a próxima pergunta; depois de `MAX_TURNS_PER_RUN` perguntas ele dá `continue_as_new` (reseta o event history do Temporal, carrega `self.messages` adiante, `workflow_id` não muda).

Cada pergunta roda o loop `while stop_reason == "tool_use"` como Activity (retry automático do Temporal). Cada Activity abre um span/generation no Langfuse marcado com `session_id` = `workflow_id` — todas as perguntas da mesma sessão aparecem agrupadas em **Sessions** na UI do Langfuse.

## Estrutura

```
agent-poc/
  infra/
    temporal/   docker-compose.yml + .env + dynamicconfig/   (Temporal server + UI, sem Elasticsearch)
    langfuse/   docker-compose.yml + .env                    (Langfuse web+worker + Postgres+ClickHouse+Redis+MinIO)
  agent/
    requirements.txt
    tools.py                 get_weather (API real, Open-Meteo, sem key) e calculate (local, ast)
    activities.py            call_claude, execute_tool, summarize_messages, log_event (Temporal activities)
    guardrails_config.json   blocklist de clima, limite de expressão, cap de custo por sessão (editável sem redeploy)
    workflows.py             AgentWorkflow — long-running, 1 por sessão, update "ask"
    worker.py                worker Temporal (registra workflow+activities)
    trigger.py               CLI pra conversar com o agent (mantém sessão via env var)
    web.py                   Flask — interface web (chat)
    templates/               index.html do chat
    tests/                   pytest dos guardrails (test_guardrails.py)
    .env                     segredos locais (gitignored) — Langfuse já preenchido, falta ANTHROPIC_API_KEY
```

## Pré-requisitos

- Docker Desktop rodando
- Python 3.13 (`/opt/homebrew/bin/python3.13`)
- Uma `ANTHROPIC_API_KEY` válida

## 1. Subir a infra

```bash
cd infra/temporal && docker compose up -d && cd -
cd infra/langfuse && docker compose up -d && cd -
```

Primeira vez baixa ~6 imagens do Langfuse (Postgres, ClickHouse, Redis, MinIO, web, worker) — demora alguns minutos. Depois disso sobe em segundos.

Verificar que tudo subiu saudável:

```bash
cd infra/temporal && docker compose ps && cd -
cd infra/langfuse && docker compose ps && cd -   # todos (healthy) exceto web/worker (sem healthcheck, normal)
```

UIs:
- Temporal UI: http://localhost:8080
- Langfuse UI: http://localhost:3000 — login já criado automaticamente via `LANGFUSE_INIT_*` (ver `infra/langfuse/.env`: `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD`). Não precisa passar pelo signup.

## 2. Configurar a chave da Anthropic

`agent/.env` já vem com as chaves do Langfuse preenchidas (geradas no bootstrap acima). Só falta a chave da Anthropic:

```bash
# editar agent/.env e trocar a linha:
ANTHROPIC_API_KEY=coloque-sua-chave-aqui
# pela sua chave real
```

## 3. Ambiente Python

Já criado em `agent/.venv` com as deps instaladas (`temporalio`, `anthropic`, `langfuse`, `python-dotenv`). Pra recriar do zero:

```bash
cd agent
python3.13 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## 4. Rodar

Terminal 1 — worker (fica escutando, deixa rodando):

```bash
cd agent && ./.venv/bin/python worker.py
```

Terminal 2 — conversa com o agent:

```bash
cd agent && ./.venv/bin/python trigger.py "Qual o clima em Lisboa e quanto é (12 + 8) * 3?"
```

Sem argumento usa uma pergunta padrão. Cada chamada sem `AGENT_SESSION_ID` setado cria uma sessão nova (sem memória da anterior). Pra continuar a mesma conversa:

```bash
export AGENT_SESSION_ID=cli-xxxx   # o trigger.py imprime esse valor no final de cada resposta
./.venv/bin/python trigger.py "e meu nome, lembra?"
```

Opcional, `export AGENT_USER_ID=seu-nome` pra marcar quem é o dono da sessão (vira `userId` no Langfuse, ver passo 6).

## 5. Interface web (opcional)

Chat com memória de conversa, sem usar a CLI. Precisa do worker rodando (passo 4).

```bash
cd agent && ./.venv/bin/python web.py
```

Abre `http://localhost:8090`. Um `session_id` é gerado ao carregar a página (mostrado no canto do header) e reaproveitado em toda pergunta daquela aba — o agent lembra o que foi dito antes, dentro da mesma aba. Recarregar a página começa sessão nova.

Campo **usuário** no topo: o que for digitado ali vira `user_id` nas traces (persiste no `localStorage` do navegador — sobrevive a reload, é o que deixa o Langfuse juntar várias sessões como sendo da mesma pessoa).

Botão **nova sessão**: cancela o workflow da sessão atual no Temporal (`POST /close` → `handle.cancel()`, vira `Canceled` em vez de ficar `Running` até o idle timeout) e abre uma sessão nova, zerada. Se a sessão já tinha fechado sozinha, o cancel só falha silenciosamente (não é erro pro usuário).

## 6. Rodar os testes

```bash
cd agent && ./.venv/bin/pytest tests/ -q
```

Cobre os guardrails (`_weather_guardrail_violation`, `_calculate_guardrail_violation`, normalização de texto) e o fallback do `guardrails_config.json` quando o arquivo some.

## 7. Verificar

- **Temporal UI** (`localhost:8080`): o workflow fica `Running` com o `workflow_id` = `session_id` — cada pergunta aparece como um novo Update no histórico, com as Activities (`call_claude`, `execute_tool`) por baixo. Sem pergunta nova por 30min (configurável, ver Pontos de atenção) o workflow fecha sozinho como `Completed`.
- **Langfuse UI** (`localhost:3000`, projeto `agent-poc`):
  - **Sessions** → todas as perguntas da mesma sessão agrupadas, cada uma com generation (modelo, tokens) e tool calls
  - **Users** → se preencheu `user_id`, aparece aqui, com todas as sessões daquele usuário (é a tela de "You aren't tracking users yet" — some depois do primeiro `user_id` chegar)
  - **Scores** → `guardrail_blocked` (toda vez que um guardrail bloqueia) e `iteration_limit_reached` (loop de tool-use bateu `MAX_TOOL_ITERATIONS` sem resposta final) — sinal leve de "eval", sem pipeline de avaliação completo
- **CLI/web**: cada resposta mostra o custo acumulado da sessão (`total_cost_usd`), persistido em `SessionState.total_cost_usd` (sobrevive a `continue_as_new`)

## Comandos úteis

```bash
# parar tudo (mantém dados)
cd infra/temporal && docker compose stop && cd -
cd infra/langfuse && docker compose stop && cd -

# derrubar e apagar dados (reset total)
cd infra/temporal && docker compose down -v && cd -
cd infra/langfuse && docker compose down -v && cd -

# logs
cd infra/langfuse && docker compose logs -f langfuse-web
```

## Pontos de atenção

- `thinking` desabilitado nas chamadas ao Claude (`thinking={"type": "disabled"}`) — simplifica o round-trip pelo Temporal (sem blocos de thinking pra serializar).
- Modelo fixo em `claude-haiku-4-5` (`agent/activities.py`, constante `MODEL`) — trocar ali se quiser mais qualidade (`claude-opus-5`) ou ficar no meio-termo (`claude-sonnet-5`).
- **Sessão expira sozinha por inatividade.** `SESSION_IDLE_TIMEOUT` em `agent/workflows.py` (default 30min, override via `AGENT_SESSION_IDLE_SECONDS` ao subir o worker). Sem pergunta nova dentro do prazo, o workflow fecha como `Completed` — pergunta nova depois disso é sessão zerada (perde o histórico). Enquanto ativa, continua viva normalmente; a cada `MAX_TURNS_PER_RUN=20` perguntas dá `continue_as_new` (só reseta o event history do Temporal, sessão continua, timeout de inatividade reseta a cada pergunta).
  Pra encerrar uma sessão na mão antes do prazo: `docker exec temporal-admin-tools temporal workflow terminate --workflow-id <session_id> --address temporal:7233`.
  **Cuidado ao mudar `SESSION_IDLE_TIMEOUT`/`MAX_TURNS_PER_RUN` com sessões já `Running`** — são constantes que viram parte do código determinístico do workflow; mudar o valor com histórico já gravado no formato antigo quebra o replay (erro de non-determinism). Termine as sessões abertas antes de reiniciar o worker com um valor novo. **A mesma regra vale pra qualquer mudança de lógica em `workflows.py`** (ex: as mudanças de resumo de histórico/cap de custo desta rodada) — sempre checar `temporal workflow list` por sessões `Running` antes de reiniciar o worker; se não tiver nenhuma (era o caso aqui), é seguro.
- **Histórico comprimido automaticamente.** Acima de `MAX_HISTORY_MESSAGES=16` mensagens (`agent/activities.py`), os turnos mais antigos (tudo exceto os últimos `KEEP_RECENT_MESSAGES=8`) viram um resumo de até 5 frases via activity `summarize_messages`, que entra no `system` prompt das próximas chamadas em vez do histórico bruto. Sem isso, cada pergunta nova reenvia a conversa inteira pro Claude (custo cresce O(n²) numa sessão longa).
- **Cap de custo por sessão.** `max_session_cost_usd` em `agent/guardrails_config.json` (default `0.50`). Quando `SessionState.total_cost_usd` (acumulado de `call_claude` + `summarize_messages`) atinge o teto, o workflow para de chamar o Claude e responde direto pedindo nova sessão — sem queimar mais tokens. CLI e web mostram o custo acumulado a cada resposta.
- **Guardrails e limites configuráveis sem redeploy.** `agent/guardrails_config.json` (blocklist de clima, limite de caracteres da expressão, cap de custo) é lido em import-time por `activities.py`, com fallback hardcoded se o arquivo sumir ou vier inválido — editar o JSON e reiniciar o worker é suficiente, não precisa mexer em código.
- **Erros do Claude não tratam tudo como retryable.** `activities.py` separa erro de config/request (401 auth, 400 bad request, 403, 404, 422 — não adianta tentar de novo) dos erros transientes (429 rate limit, 5xx, timeout, conexão — esses sim usam o `RetryPolicy(maximum_attempts=3)` normal). O primeiro grupo vira `ApplicationError(non_retryable=True)` e falha rápido em vez de queimar as 3 tentativas à toa.
- **`get_weather` bate em API real** (geocoding-api.open-meteo.com + api.open-meteo.com, sem key). Worker precisa de internet de saída; erro de rede propaga e usa o `RetryPolicy` normal da activity, cidade não encontrada é resposta tratada (não erro).
- Portas: Temporal Postgres em `5433` (não `5432`) pra não colidir com o Postgres do Langfuse.
- `infra/langfuse/.env` e `agent/.env` têm segredos — já no `.gitignore`, nunca commitar.
- `LANGFUSE_INIT_ORG_ID` precisa estar setado pro bootstrap automático rodar (sem ele, o Langfuse ignora todas as outras variáveis `LANGFUSE_INIT_*` silenciosamente, só avisa no log).
- **Langfuse v4 (self-host) mudou o schema do ClickHouse.** As tabelas antigas `traces`/`observations`/`analytics_traces`/`analytics_observations` ficam vazias mesmo com tudo funcionando — o dado real cai em `events_core`/`events_full`. Pra conferir via SQL direto, use essas; pra conferir via API, use `/api/public/v2/observations` (o mesmo "read path" que a UI usa), não o `/api/public/traces` antigo (que retorna 400 em modo `events_only`).
