import asyncio
import sys
import uuid

from temporalio.client import Client

from workflows import AgentWorkflow

TASK_QUEUE = "agent-poc-task-queue"
DEFAULT_QUESTION = "Qual o clima em Lisboa e quanto é (12 + 8) * 3?"


async def main() -> None:
    question = " ".join(sys.argv[1:]) or DEFAULT_QUESTION
    client = await Client.connect("localhost:7233")

    handle = await client.start_workflow(
        AgentWorkflow.run,
        question,
        id=f"agent-poc-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )
    print(f"workflow iniciado: {handle.id}")
    print(f"pergunta: {question}")

    result = await handle.result()
    print("\n--- resposta do agent ---")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
