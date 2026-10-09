"""Activities Temporal: toda chamada de rede (Claude, tools) mora aqui, nunca no workflow."""

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import anthropic
from langfuse import get_client, propagate_attributes
from temporalio import activity
from temporalio.exceptions import ApplicationError

from tools import TOOL_DEFINITIONS, execute_tool_call

MODEL = "claude-haiku-4-5"
SYSTEM_PROMPT = (
    "Você é um agente de teste de infraestrutura. Use as tools disponíveis "
    "(get_weather, calculate) quando a pergunta precisar delas. Responda em "
    "português, de forma direta e curta."
)

# preço por 1M tokens (USD) — tabela cacheada do skill claude-api.
# Langfuse não reconhece modelo Claude automaticamente num self-host v4 novo,
# então calcula-se o custo aqui e manda explícito (cost_details sempre
# sobrepõe o catálogo de preço do Langfuse, se um dia existir).
PRICING_PER_MTOK = {
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
}

# memória longa: acima de MAX_HISTORY_MESSAGES mensagens acumuladas, as mais
# antigas viram resumo (activity summarize_messages) em vez de serem mandadas
# inteiras pro Claude a cada turno — sem isso o custo por turno cresce O(n^2)
# numa sessão longa (histórico inteiro replicado a cada pergunta).
MAX_HISTORY_MESSAGES = 16
KEEP_RECENT_MESSAGES = 8

# guardrails + limites de custo lidos de JSON externo — dá pra ajustar sem
# mexer no código, só precisa restart do worker. Cai pro default hardcoded se
# o arquivo sumir ou vier inválido.
_CONFIG_PATH = Path(__file__).parent / "guardrails_config.json"
_DEFAULT_CONFIG: dict[str, Any] = {
    "blocked_weather_locations": [
        "coreia do norte", "coreia-do-norte", "north korea", "dprk", "pyongyang", "piongue",
    ],
    "max_calculate_expr_length": 200,
    "max_session_cost_usd": 0.50,
}


def _normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _load_config() -> dict[str, Any]:
    try:
        merged = {**_DEFAULT_CONFIG, **json.loads(_CONFIG_PATH.read_text())}
    except (FileNotFoundError, json.JSONDecodeError):
        merged = dict(_DEFAULT_CONFIG)
    merged["blocked_weather_locations"] = {
        _normalize(loc) for loc in merged["blocked_weather_locations"]
    }
    return merged


_CONFIG = _load_config()
MAX_CALCULATE_EXPR_LENGTH: int = _CONFIG["max_calculate_expr_length"]
MAX_SESSION_COST_USD: float = _CONFIG["max_session_cost_usd"]
BLOCKED_WEATHER_LOCATIONS: set[str] = _CONFIG["blocked_weather_locations"]

# erro de config/request (api key errada, payload inválido) não se resolve
# tentando de novo — vira ApplicationError non_retryable pra não queimar as
# 3 tentativas do RetryPolicy à toa. Erro transiente (rate limit, 5xx,
# timeout, conexão) cai fora daqui e segue pro retry normal do Temporal.
_NON_RETRYABLE_ANTHROPIC_ERRORS = (
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.BadRequestError,
    anthropic.NotFoundError,
    anthropic.UnprocessableEntityError,
)

# lidos em import-time — worker.py chama load_dotenv() antes deste import
claude = anthropic.Anthropic()
langfuse = get_client()


def _call_claude_messages(**kwargs: Any):
    try:
        return claude.messages.create(**kwargs)
    except _NON_RETRYABLE_ANTHROPIC_ERRORS as exc:
        raise ApplicationError(
            str(exc), type="AnthropicNonRetryable", non_retryable=True
        ) from exc


def _weather_guardrail_violation(tool_input: dict[str, Any]) -> Optional[str]:
    """None = passou. String = motivo do bloqueio."""
    city = _normalize(tool_input.get("city", ""))
    if city in BLOCKED_WEATHER_LOCATIONS or "coreia do norte" in city:
        return f"consulta de clima bloqueada pra '{tool_input.get('city')}' (local na lista de bloqueio)"
    return None


def _calculate_guardrail_violation(tool_input: dict[str, Any]) -> Optional[str]:
    """None = passou. String = motivo do bloqueio."""
    expr = tool_input.get("expression", "")
    if len(expr) > MAX_CALCULATE_EXPR_LENGTH:
        return (
            f"expressão com {len(expr)} caracteres excede o limite de "
            f"{MAX_CALCULATE_EXPR_LENGTH}"
        )
    return None


def _cost_details(input_tokens: int, output_tokens: int) -> dict[str, float]:
    price = PRICING_PER_MTOK.get(MODEL)
    if not price:
        return {}
    input_cost = input_tokens / 1_000_000 * price["input"]
    output_cost = output_tokens / 1_000_000 * price["output"]
    return {
        "input": input_cost,
        "output": output_cost,
        "total": input_cost + output_cost,
    }


def _flatten_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        btype = block.get("type")
        if btype == "text":
            parts.append(block["text"])
        elif btype == "tool_use":
            parts.append(f"[chamou tool {block['name']} com {block['input']}]")
        elif btype == "tool_result":
            parts.append(f"[resultado da tool: {block['content']}]")
    return " ".join(parts)


@dataclass
class ClaudeTurnInput:
    messages: list[dict[str, Any]]
    session_id: str
    trace_seed: str
    user_id: Optional[str] = None
    summary: str = ""
    parent_span_id: Optional[str] = None


@dataclass
class ClaudeTurnOutput:
    stop_reason: str
    content: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    cost_usd: float
    observation_id: str


@dataclass
class ToolCallInput:
    name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    session_id: str
    trace_seed: str
    user_id: Optional[str] = None
    parent_span_id: Optional[str] = None


@dataclass
class ToolCallOutput:
    tool_use_id: str
    content: str
    observation_id: str


def _trace_context(trace_id: str, parent_span_id: Optional[str]) -> dict[str, str]:
    # parent_span_id vem da activity anterior do MESMO turno (gerada via
    # `span.id` do Langfuse) — sem isso, cada activity cai como item raiz
    # solto na trace em vez de filho na árvore (OTel context não atravessa
    # fronteira de Activity do Temporal, então tem que passar o id na mão).
    ctx: dict[str, str] = {"trace_id": trace_id}
    if parent_span_id:
        ctx["parent_span_id"] = parent_span_id
    return ctx


@dataclass
class SummarizeInput:
    old_messages: list[dict[str, Any]]
    existing_summary: str
    session_id: str
    trace_seed: str


@dataclass
class SummarizeOutput:
    summary: str
    cost_usd: float


@dataclass
class LogEventInput:
    session_id: str
    trace_seed: str
    name: str
    comment: str = ""


@activity.defn
async def call_claude(data: ClaudeTurnInput) -> ClaudeTurnOutput:
    # mesma seed (vinda do workflow) => mesmo trace_id sempre — é isso que
    # junta call_claude + execute_tool do MESMO turno numa árvore só, em vez
    # de cada activity virar uma trace raiz separada.
    trace_id = langfuse.create_trace_id(seed=data.trace_seed)

    system_prompt = SYSTEM_PROMPT
    if data.summary:
        system_prompt += (
            f"\n\nResumo da conversa até aqui (turnos antigos já saíram do "
            f"histórico bruto): {data.summary}"
        )

    with propagate_attributes(session_id=data.session_id, user_id=data.user_id):
        with langfuse.start_as_current_observation(
            as_type="generation",
            name="agent-turn",
            model=MODEL,
            input=data.messages,
            trace_context=_trace_context(trace_id, data.parent_span_id),
        ) as generation:
            response = _call_claude_messages(
                model=MODEL,
                max_tokens=1024,
                thinking={"type": "disabled"},
                system=system_prompt,
                tools=TOOL_DEFINITIONS,
                messages=data.messages,
            )

            content: list[dict[str, Any]] = []
            for block in response.content:
                if block.type == "text":
                    content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )

            cost = _cost_details(response.usage.input_tokens, response.usage.output_tokens)
            generation.update(
                output=content,
                usage_details={
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
                cost_details=cost,
            )
            observation_id = generation.id

    langfuse.flush()

    return ClaudeTurnOutput(
        stop_reason=response.stop_reason,
        content=content,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cost_usd=cost.get("total", 0.0),
        observation_id=observation_id,
    )


@activity.defn
async def execute_tool(data: ToolCallInput) -> ToolCallOutput:
    trace_id = langfuse.create_trace_id(seed=data.trace_seed)

    with propagate_attributes(session_id=data.session_id, user_id=data.user_id):
        violation: Optional[str] = None
        guardrail_name = ""

        if data.name == "calculate":
            violation = _calculate_guardrail_violation(data.tool_input)
            guardrail_name = "calculate-expr-limit"
        elif data.name == "get_weather":
            violation = _weather_guardrail_violation(data.tool_input)
            guardrail_name = "weather-blocklist"

        if violation:
            with langfuse.start_as_current_observation(
                as_type="guardrail",
                name=guardrail_name,
                input=data.tool_input,
                trace_context=_trace_context(trace_id, data.parent_span_id),
            ) as guardrail:
                guardrail.update(
                    output=f"bloqueado: {violation}",
                    level="WARNING",
                    status_message=violation,
                )
                observation_id = guardrail.id
            langfuse.create_score(
                trace_id=trace_id,
                name="guardrail_blocked",
                value=1.0,
                data_type="NUMERIC",
                comment=violation,
            )
            langfuse.flush()
            return ToolCallOutput(
                tool_use_id=data.tool_use_id,
                content=f"Bloqueado pelo guardrail: {violation}",
                observation_id=observation_id,
            )

        with langfuse.start_as_current_observation(
            as_type="tool",
            name=data.name,
            input=data.tool_input,
            trace_context=_trace_context(trace_id, data.parent_span_id),
        ) as span:
            result = execute_tool_call(data.name, data.tool_input)
            span.update(output=result)
            observation_id = span.id

    langfuse.flush()

    return ToolCallOutput(tool_use_id=data.tool_use_id, content=result, observation_id=observation_id)


@activity.defn
async def summarize_messages(data: SummarizeInput) -> SummarizeOutput:
    """Comprime turnos antigos num resumo curto — chamado quando o histórico
    bruto passa de MAX_HISTORY_MESSAGES, pra não mandar a conversa inteira
    (custo O(n^2)) pro Claude a cada pergunta nova."""
    trace_id = langfuse.create_trace_id(seed=data.trace_seed)

    transcript = "\n".join(
        f"{m['role']}: {_flatten_message_content(m['content'])}" for m in data.old_messages
    )
    prompt = (
        "Resuma a conversa abaixo em no máximo 5 frases, em português, mantendo "
        "fatos e decisões relevantes (nomes, preferências, resultados de tools). "
        "Seja objetivo, sem introdução.\n\n"
    )
    if data.existing_summary:
        prompt += f"Resumo anterior: {data.existing_summary}\n\n"
    prompt += f"Conversa a resumir:\n{transcript}"

    with propagate_attributes(session_id=data.session_id):
        with langfuse.start_as_current_observation(
            as_type="generation",
            name="history-summary",
            model=MODEL,
            input=prompt,
            trace_context={"trace_id": trace_id},
        ) as generation:
            response = _call_claude_messages(
                model=MODEL, max_tokens=300, messages=[{"role": "user", "content": prompt}]
            )
            summary = "".join(b.text for b in response.content if b.type == "text").strip()
            cost = _cost_details(response.usage.input_tokens, response.usage.output_tokens)
            generation.update(
                output=summary,
                usage_details={
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
                cost_details=cost,
            )

    langfuse.flush()
    return SummarizeOutput(summary=summary, cost_usd=cost.get("total", 0.0))


@activity.defn
async def log_event(data: LogEventInput) -> None:
    """Score leve no Langfuse pra sinalizar eventos do loop (ex: limite de
    iteração atingido) sem precisar de um pipeline de eval completo."""
    trace_id = langfuse.create_trace_id(seed=data.trace_seed)
    with propagate_attributes(session_id=data.session_id):
        langfuse.create_score(
            trace_id=trace_id,
            name=data.name,
            value=1.0,
            data_type="NUMERIC",
            comment=data.comment or None,
        )
    langfuse.flush()
