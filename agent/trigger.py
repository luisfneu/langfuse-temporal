import asyncio
import os
import sys
import uuid

from dotenv import load_dotenv

load_dotenv()  # silencia warning do langfuse ao importar workflows->activities

from temporalio.client import Client, WithStartWorkflowOperation
from temporalio.common import WorkflowIDConflictPolicy

from workflows import AgentWorkflow

TASK_QUEUE = "agent-poc-task-queue"
DEFAULT_QUESTION = "Qual o clima em Lisboa e quanto é (12 + 8) * 3?"


async def main() -> None:
    question = " ".join(sys.argv[1:]) or DEFAULT_QUESTION

    # exporta AGENT_SESSION_ID pra continuar uma sessão já existente
    # (ex: export AGENT_SESSION_ID=cli-xxxx) — sem isso, cria sessão nova.
    session_id = os.environ.get("AGENT_SESSION_ID") or f"cli-{uuid.uuid4()}"

    client = await Client.connect("localhost:7233")

    start_op = WithStartWorkflowOperation(
        AgentWorkflow.run,
        id=session_id,
        task_queue=TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    answer = await client.execute_update_with_start_workflow(
        AgentWorkflow.ask,
        question,
        start_workflow_operation=start_op,
    )

    print(f"session: {session_id}")
    print(f"pergunta: {question}")
    print("\n--- resposta do agent ---")
    print(answer)
    print(f"\n(pra continuar essa sessão: export AGENT_SESSION_ID={session_id})")


if __name__ == "__main__":
    asyncio.run(main())
