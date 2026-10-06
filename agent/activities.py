"""Activities Temporal: toda chamada de rede (Claude, tools) mora aqui, nunca no workflow."""

from dataclasses import dataclass
from typing import Any, Optional

import anthropic
from langfuse import get_client, propagate_attributes
from temporalio import activity

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

MAX_CALCULATE_EXPR_LENGTH = 200  # guardrail: evita expressão gigante travando o avaliador

# lidos em import-time — worker.py chama load_dotenv() antes deste import
claude = anthropic.Anthropic()
langfuse = get_client()


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


@dataclass
class ClaudeTurnInput:
    messages: list[dict[str, Any]]
    session_id: str
    trace_seed: str
    user_id: Optional[str] = None


@dataclass
class ClaudeTurnOutput:
    stop_reason: str
    content: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int


@dataclass
class ToolCallInput:
    name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    session_id: str
    trace_seed: str
    user_id: Optional[str] = None


@dataclass
class ToolCallOutput:
    tool_use_id: str
    content: str


@activity.defn
async def call_claude(data: ClaudeTurnInput) -> ClaudeTurnOutput:
    # mesma seed (vinda do workflow) => mesmo trace_id sempre — é isso que
    # junta call_claude + execute_tool do MESMO turno numa árvore só, em vez
    # de cada activity virar uma trace raiz separada.
    trace_id = langfuse.create_trace_id(seed=data.trace_seed)

    with propagate_attributes(session_id=data.session_id, user_id=data.user_id):
        with langfuse.start_as_current_observation(
            as_type="generation",
            name="agent-turn",
            model=MODEL,
            input=data.messages,
            trace_context={"trace_id": trace_id},
        ) as generation:
            response = claude.messages.create(
                model=MODEL,
                max_tokens=1024,
                thinking={"type": "disabled"},
                system=SYSTEM_PROMPT,
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

            generation.update(
                output=content,
                usage_details={
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
                cost_details=_cost_details(
                    response.usage.input_tokens, response.usage.output_tokens
                ),
            )

    langfuse.flush()

    return ClaudeTurnOutput(
        stop_reason=response.stop_reason,
        content=content,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


@activity.defn
async def execute_tool(data: ToolCallInput) -> ToolCallOutput:
    trace_id = langfuse.create_trace_id(seed=data.trace_seed)

    with propagate_attributes(session_id=data.session_id, user_id=data.user_id):
        if data.name == "calculate":
            violation = _calculate_guardrail_violation(data.tool_input)
            if violation:
                with langfuse.start_as_current_observation(
                    as_type="guardrail",
                    name="calculate-expr-limit",
                    input=data.tool_input,
                    trace_context={"trace_id": trace_id},
                ) as guardrail:
                    guardrail.update(
                        output=f"bloqueado: {violation}",
                        level="WARNING",
                        status_message=violation,
                    )
                langfuse.flush()
                return ToolCallOutput(
                    tool_use_id=data.tool_use_id,
                    content=f"Bloqueado pelo guardrail: {violation}",
                )

        with langfuse.start_as_current_observation(
            as_type="tool",
            name=data.name,
            input=data.tool_input,
            trace_context={"trace_id": trace_id},
        ) as span:
            result = execute_tool_call(data.name, data.tool_input)
            span.update(output=result)

    langfuse.flush()

    return ToolCallOutput(tool_use_id=data.tool_use_id, content=result)
