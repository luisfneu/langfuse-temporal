"""Interface web pra conversar com o agent. 1 sessão (do browser) = 1 workflow long-running."""

import asyncio
import uuid

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, render_template, request
from temporalio.client import Client, WithStartWorkflowOperation
from temporalio.common import WorkflowIDConflictPolicy

from workflows import AgentWorkflow, AskRequest

TASK_QUEUE = "agent-poc-task-queue"
TEMPORAL_ADDRESS = "localhost:7233"

app = Flask(__name__)


async def _ask(question: str, session_id: str, user_id: str):
    client = await Client.connect(TEMPORAL_ADDRESS)

    start_op = WithStartWorkflowOperation(
        AgentWorkflow.run,
        id=session_id,
        task_queue=TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    return await client.execute_update_with_start_workflow(
        AgentWorkflow.ask,
        AskRequest(question=question, user_id=user_id),
        start_workflow_operation=start_op,
    )


async def _cancel(session_id: str) -> None:
    client = await Client.connect(TEMPORAL_ADDRESS)
    handle = client.get_workflow_handle(session_id)
    await handle.cancel(reason="sessão encerrada pelo usuário (botão nova sessão)")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/ask", methods=["POST"])
def ask():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    session_id = (data.get("session_id") or "").strip() or f"web-{uuid.uuid4()}"
    user_id = (data.get("user_id") or "").strip()
    if not question:
        return jsonify({"error": "pergunta vazia"}), 400

    try:
        response = asyncio.run(_ask(question, session_id, user_id))
    except RuntimeError as exc:
        return jsonify({"error": f"não consegui falar com o Temporal: {exc}"}), 502
    except Exception as exc:  # erro vindo do workflow/activity (ex: Claude 401)
        return jsonify({"error": str(exc)}), 500

    return jsonify({"answer": response.answer, "total_cost_usd": response.total_cost_usd})


@app.route("/close", methods=["POST"])
def close():
    """Cancela o workflow da sessão (botão 'nova sessão' na UI). Melhor
    esforço: se a sessão já tinha encerrado sozinha (idle timeout) ou nunca
    existiu, não é erro — só não tem nada pra cancelar."""
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"error": "session_id vazio"}), 400

    try:
        asyncio.run(_cancel(session_id))
    except Exception as exc:
        return jsonify({"cancelled": False, "detail": str(exc)})

    return jsonify({"cancelled": True})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8090, debug=True)
