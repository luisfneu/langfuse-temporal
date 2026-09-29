# agent-poc

POC de AI agent com **Temporal** (orquestração do loop de tool-use) + **Langfuse** (observabilidade/tracing) + **Claude API** (Opus 5).

## Arquitetura

```
trigger.py --> Temporal Workflow (AgentWorkflow) --> Activities
                                                        ├─ call_claude   (chama Claude, loga generation no Langfuse)
                                                        └─ execute_tool  (tools dummy: get_weather, calculate)
```

O workflow roda o loop `while stop_reason == "tool_use"`. Cada chamada ao Claude e cada execução de tool é uma Activity (Temporal dá retry automático). Cada Activity abre um span/generation no Langfuse — a trace completa (cada turno do Claude + cada tool call, com tokens) aparece na UI do Langfuse.

## Estrutura

```
agent-poc/
  infra/
    temporal/   docker-compose.yml + .env + dynamicconfig/   (Temporal server + UI, sem Elasticsearch)
    langfuse/   docker-compose.yml + .env                    (Langfuse web+worker + Postgres+ClickHouse+Redis+MinIO)
  agent/
    requirements.txt
    tools.py        tools dummy (get_weather, calculate)
    activities.py   call_claude, execute_tool (Temporal activities)
    workflows.py    AgentWorkflow (loop determinístico)
    worker.py       worker Temporal (registra workflow+activities)
    trigger.py      CLI pra disparar uma execução
    web.py          Flask — interface web (chat)
    templates/      index.html do chat
    .env            segredos locais (gitignored) — Langfuse já preenchido, falta ANTHROPIC_API_KEY
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

Terminal 2 — dispara uma execução:

```bash
cd agent && ./.venv/bin/python trigger.py "Qual o clima em Lisboa e quanto é (12 + 8) * 3?"
```

Sem argumento usa uma pergunta padrão que força as duas tools dummy.

## 5. Interface web (opcional)

Chat simples pra conversar com o agent sem usar a CLI. Precisa do worker rodando (passo 4).

```bash
cd agent && ./.venv/bin/python web.py
```

Abre `http://localhost:8090`. Cada pergunta dispara um workflow novo (sem memória entre perguntas — mesma limitação do `trigger.py`).

## 6. Verificar

- **Temporal UI** (`localhost:8080`): workflow `agent-poc-...` aparece com histórico completo — cada Activity (`call_claude`, `execute_tool`) com input/output/retries.
- **Langfuse UI** (`localhost:3000`, projeto `agent-poc`): trace com cada generation (`agent-turn`, modelo, tokens) e cada tool call (`get_weather`/`calculate`), tempo e custo.

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

- `thinking` desabilitado nas chamadas ao Claude (`thinking={"type": "disabled"}`) — simplifica o round-trip pelo Temporal (sem blocos de thinking pra serializar). Pra habilitar, ver `shared/model-migration.md` do skill `claude-api` — Opus 5 pensa por padrão e isso soma no `max_tokens`.
- Modelo fixo em `claude-haiku-4-5` (`agent/activities.py`) — trocar ali se quiser mais qualidade (`claude-opus-5`) ou meio-termo (`claude-sonnet-5`).
- Portas: Temporal Postgres em `5433` (não `5432`) pra não colidir com o Postgres do Langfuse.
- `infra/langfuse/.env` e `agent/.env` têm segredos — já no `.gitignore`, nunca commitar.
- `LANGFUSE_INIT_ORG_ID` precisa estar setado pro bootstrap automático rodar (sem ele, o Langfuse ignora todas as outras variáveis `LANGFUSE_INIT_*` silenciosamente, só avisa no log).
- **Langfuse v4 (self-host) mudou o schema do ClickHouse.** As tabelas antigas `traces`/`observations`/`analytics_traces`/`analytics_observations` ficam vazias mesmo com tudo funcionando — o dado real cai em `events_core`/`events_full`. Pra conferir via SQL direto, use essas; pra conferir via API, use `/api/public/v2/observations` (o mesmo "read path" que a UI usa), não o `/api/public/traces` antigo (que retorna 400 em modo `events_only`).
