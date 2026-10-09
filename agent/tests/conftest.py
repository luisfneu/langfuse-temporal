import sys
from pathlib import Path

from dotenv import load_dotenv

AGENT_DIR = Path(__file__).resolve().parent.parent

# activities.py lê a Anthropic API key em import-time — mesma dependência que
# worker.py/trigger.py já têm, por isso carrega o mesmo .env aqui.
load_dotenv(AGENT_DIR / ".env")
sys.path.insert(0, str(AGENT_DIR))
