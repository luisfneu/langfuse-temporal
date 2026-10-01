"""Workflow long-running: 1 execução = 1 sessão inteira.

Perguntas chegam via Update (RPC síncrono contra o workflow rodando), não via
novo `start_workflow` a cada pergunta. `self.messages` acumula o histórico —
é isso que dá memória de conversa pro agent. `continue_as_new` reseta o event
history do Temporal de tempos em tempos, carregando o estado da sessão
adiante (workflow_id não muda, só o run_id).
"""

from datetime import timedelta
from typing import Any, Optional

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

MAX_TOOL_ITERATIONS = 8  # tool-use loop dentro de UMA pergunta
MAX_TURNS_PER_RUN = 20  # perguntas antes de resetar o event history (continue_as_new)
RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@workflow.defn
class AgentWorkflow:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.turns_this_run = 0
        self._ready_for_continue = False

    @workflow.run
    async def run(self, initial_messages: Optional[list[dict[str, Any]]] = None) -> None:
        if initial_messages:
            self.messages = initial_messages

        # fica vivo recebendo updates via `ask` até acumular turnos demais
        await workflow.wait_condition(
            lambda: self._ready_for_continue and workflow.all_handlers_finished()
        )
        workflow.continue_as_new(self.messages)

    @workflow.update
    async def ask(self, question: str) -> str:
        """Chamada pelo cliente (trigger.py/web.py) via execute_update_with_start_workflow."""
        self.messages.append({"role": "user", "content": question})
        answer = await self._run_tool_loop()

        self.turns_this_run += 1
        if self.turns_this_run >= MAX_TURNS_PER_RUN:
            self._ready_for_continue = True

        return answer

    async def _run_tool_loop(self) -> str:
        # workflow_id = session_id (ver client: id_conflict_policy=USE_EXISTING)
        session_id = workflow.info().workflow_id

        for _ in range(MAX_TOOL_ITERATIONS):
            turn = await workflow.execute_activity(
                call_claude,
                ClaudeTurnInput(messages=self.messages, session_id=session_id),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RETRY_POLICY,
            )

            self.messages.append({"role": "assistant", "content": turn.content})

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
                        session_id=session_id,
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

            self.messages.append({"role": "user", "content": tool_results})

        return "Limite de iterações atingido sem resposta final."
