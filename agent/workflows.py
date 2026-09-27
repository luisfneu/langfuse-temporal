"""Workflow determinístico: orquestra as activities, nunca chama Claude/rede direto."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

# activities.py importa anthropic/langfuse (não-determinístico) — passa direto
# pro worker sandbox sem tentar reexecutar essas libs dentro do workflow.
with workflow.unsafe.imports_passed_through():
    from activities import (
        ClaudeTurnInput,
        ToolCallInput,
        call_claude,
        execute_tool,
    )

MAX_TURNS = 8
RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@workflow.defn
class AgentWorkflow:
    @workflow.run
    async def run(self, user_message: str) -> str:
        messages: list[dict] = [{"role": "user", "content": user_message}]

        for _ in range(MAX_TURNS):
            turn = await workflow.execute_activity(
                call_claude,
                ClaudeTurnInput(messages=messages),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RETRY_POLICY,
            )

            messages.append({"role": "assistant", "content": turn.content})

            if turn.stop_reason != "tool_use":
                return "".join(
                    block["text"] for block in turn.content if block["type"] == "text"
                )

            tool_results = []
            for block in turn.content:
                if block["type"] != "tool_use":
                    continue
                tool_out = await workflow.execute_activity(
                    execute_tool,
                    ToolCallInput(
                        name=block["name"],
                        tool_input=block["input"],
                        tool_use_id=block["id"],
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RETRY_POLICY,
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_out.tool_use_id,
                        "content": tool_out.content,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        return "Limite de iterações atingido sem resposta final."
