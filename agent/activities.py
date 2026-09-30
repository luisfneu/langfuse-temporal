"""Activities Temporal: toda chamada de rede (Claude, tools) mora aqui, nunca no workflow."""

from dataclasses import dataclass
from typing import Any

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

# lidos em import-time — worker.py chama load_dotenv() antes deste import
claude = anthropic.Anthropic()
langfuse = get_client()


@dataclass
class ClaudeTurnInput:
    messages: list[dict[str, Any]]
    session_id: str


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


@dataclass
class ToolCallOutput:
    tool_use_id: str
    content: str


@activity.defn
async def call_claude(data: ClaudeTurnInput) -> ClaudeTurnOutput:
    with propagate_attributes(session_id=data.session_id):
        with langfuse.start_as_current_observation(
            as_type="generation",
            name="agent-turn",
            model=MODEL,
            input=data.messages,
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
    with propagate_attributes(session_id=data.session_id):
        with langfuse.start_as_current_observation(
            as_type="tool",
            name=data.name,
            input=data.tool_input,
        ) as span:
            result = execute_tool_call(data.name, data.tool_input)
            span.update(output=result)

    langfuse.flush()

    return ToolCallOutput(tool_use_id=data.tool_use_id, content=result)
