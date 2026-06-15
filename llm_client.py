from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic
import os

root_dir = Path(__file__).resolve().parent

load_dotenv(root_dir / ".env")

client = Anthropic(
    base_url=os.getenv("ANTHROPIC_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_API_KEY"),
)

LLM_MODEL = os.getenv("ANTHROPIC_MODEL")