"""统一配置入口。

用 load_dotenv(override=True) 加载项目根 .env,并让 .env 覆盖 shell 中已 export
的同名环境变量,避免配置被 shell 残留变量劫持(例如 ANTHROPIC_BASE_URL)。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# app/core/config.py -> 项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _PROJECT_ROOT / ".env"

# override=True: .env 文件值优先于 shell 已 export 的同名变量。
load_dotenv(_ENV_FILE, override=True)


def get(key: str, default: str | None = None) -> str | None:
    """读取配置项。"""
    return os.getenv(key, default)


def get_int(key: str, default: int) -> int:
    """读取整数配置项,缺失或非法时回退默认值。"""
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


# 常用配置项快捷访问。
DATABASE_URL = get("DATABASE_URL")
ANTHROPIC_BASE_URL = get("ANTHROPIC_BASE_URL")
ANTHROPIC_API_KEY = get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = get("ANTHROPIC_MODEL")
MAX_TOOL_ITERATIONS = get_int("AGENTOS_MAX_TOOL_ITERATIONS", 50)
