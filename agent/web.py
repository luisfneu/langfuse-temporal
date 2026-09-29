"""Interface web mínima pra conversar com o agent. Um workflow novo por pergunta."""

import asyncio
import uuid

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, render_template, request
from temporalio.client import Client

from workflows import AgentWorkflow

TASK_QUEUE = "agent-poc-task-queue"
TEMPORAL_ADDRESS = "localhost:7233"

app = Flask(__name__)


async def _ask(question: str) -> str:
    client = await Client.connect(TEMPORAL_ADDRESS)
    handle = await client.start_workflow(
        AgentWorkflow.run,
        question,
        id=f"agent-poc-web-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )
    return await handle.result()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/ask", methods=["POST"])
def ask():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "pergunta vazia"}), 400

    try:
        answer = asyncio.run(_ask(question))
    except RuntimeError as exc:
        return jsonify({"error": f"não consegui falar com o Temporal: {exc}"}), 502
    except Exception as exc:  # erro vindo do workflow/activity (ex: Claude 401)
        return jsonify({"error": str(exc)}), 500

    return jsonify({"answer": answer})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8090, debug=True)
