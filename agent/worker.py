import asyncio

from dotenv import load_dotenv

load_dotenv()  # precisa rodar antes de importar activities (lê ANTHROPIC_API_KEY/LANGFUSE_*)

from temporalio.client import Client
from temporalio.worker import Worker

from activities import call_claude, execute_tool
from workflows import AgentWorkflow

TASK_QUEUE = "agent-poc-task-queue"


async def main() -> None:
    client = await Client.connect("localhost:7233")
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[AgentWorkflow],
        activities=[call_claude, execute_tool],
    )
    print(f"worker escutando task queue '{TASK_QUEUE}' (ctrl+C pra parar)")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
