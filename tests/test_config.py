import re
from pathlib import Path

import pytest

from core.config import Settings

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENV_KEY_PATTERN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _read_env_keys(path: Path) -> set[str]:
    """只读取环境变量键名，避免测试输出任何配置值。"""
    return {
        match.group(1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := _ENV_KEY_PATTERN.match(line)) is not None
    }


def _settings_env_keys() -> set[str]:
    prefix = str(Settings.model_config.get("env_prefix", ""))
    return {f"{prefix}{name.upper()}" for name in Settings.model_fields}


def test_env_example_keys_match_settings() -> None:
    assert _read_env_keys(_PROJECT_ROOT / ".env.example") == _settings_env_keys()


def test_local_env_keys_match_settings() -> None:
    env_path = _PROJECT_ROOT / ".env"
    if not env_path.exists():
        pytest.skip("本地 .env 不存在")
    assert _read_env_keys(env_path) == _settings_env_keys()
