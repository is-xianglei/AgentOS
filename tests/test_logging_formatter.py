"""日志基建单元测试:KV Formatter 渲染与 AGENTOS_LOG_LEVEL 级别校验。

Formatter 用直接构造 LogRecord 的方式验证,不装配根 logger,避免污染其他测试;
涉及 setup_logging() 的级别用例由 fixture 还原根 logger 状态。
"""

import logging
import sys

import pytest

from core.config import Settings
from core.errors import AgentException
from core.logging import _THIRD_PARTY_FLOOR_LOGGERS, _KeyValueFormatter, setup_logging

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _record(msg: str, *, exc_info: object = None, **extra: object) -> logging.LogRecord:
    """构造一条 LogRecord,extra 以属性形式挂上,等价于 logger.info(msg, extra={...})。"""
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname="/fake/path.py",
        lineno=42,
        msg=msg,
        args=None,
        exc_info=exc_info,
    )
    record.__dict__.update(extra)
    return record


def _boom_exc_info() -> object:
    """拿一份真实的 exc_info,用于验证异常日志的字段落点。"""
    try:
        raise ValueError("boom")
    except ValueError:
        return sys.exc_info()


@pytest.fixture
def restore_root_logger():
    """setup_logging() 会重装根 logger 并改第三方级别,用例结束后一并还原。"""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_third_party = {n: logging.getLogger(n).level for n in _THIRD_PARTY_FLOOR_LOGGERS}
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    for name, level in saved_third_party.items():
        logging.getLogger(name).setLevel(level)


# --- Formatter 渲染 ---------------------------------------------------------


def test_extra_fields_appended_to_message_line():
    """extra 字段以 key=value 追加到消息行尾。"""
    output = _KeyValueFormatter(_FORMAT).format(
        _record("记忆召回完成", session_id=42, selected=3)
    )
    assert output.endswith("记忆召回完成 session_id=42 selected=3")


def test_no_extra_leaves_no_trailing_space():
    """无 extra 时行尾不得有多余空格(AC1)。"""
    output = _KeyValueFormatter(_FORMAT).format(_record("无额外字段的日志"))
    assert output.endswith("无额外字段的日志")
    assert output == output.rstrip()


def test_exception_fields_stay_on_message_line():
    """异常日志的字段必须留在消息行,堆栈末行保持干净。

    这是防回归的核心用例:若把实现从 formatMessage() 改回覆盖 format(),
    字段会被拼到 traceback 最后一行,变成 "ValueError: boom tool=Bash"。
    """
    output = _KeyValueFormatter(_FORMAT).format(
        _record("工具执行失败", exc_info=_boom_exc_info(), tool="Bash")
    )
    lines = output.splitlines()

    assert lines[0].endswith("工具执行失败 tool=Bash")
    assert "Traceback (most recent call last):" in output
    assert lines[-1] == "ValueError: boom"
    assert "tool=Bash" not in lines[-1]


def test_builtin_record_fields_not_leaked():
    """LogRecord 内置属性不得被当作 extra 渲染出来。"""
    output = _KeyValueFormatter(_FORMAT).format(_record("普通日志", session_id=7))

    assert "session_id=7" in output
    for builtin in ("pathname=", "lineno=", "msecs=", "taskName=", "levelno=", "args="):
        assert builtin not in output


# --- 级别配置 ---------------------------------------------------------------


def test_log_level_defaults_to_info():
    """未配置 AGENTOS_LOG_LEVEL 时,代码默认值为 INFO。

    断言字段默认值而非实例值:实例会读 .env,受本地配置影响。
    """
    assert Settings.model_fields["log_level"].default == "INFO"


def test_valid_log_level_applies(monkeypatch, restore_root_logger):
    """合法级别生效,且大小写不敏感。"""
    from core import logging as core_logging

    monkeypatch.setattr(core_logging.settings, "log_level", "debug")
    setup_logging()

    assert logging.getLogger().level == logging.DEBUG


def test_invalid_log_level_raises_chinese_exception(monkeypatch):
    """非法级别抛中文 AgentException,不静默回落到默认值。"""
    from core import logging as core_logging

    monkeypatch.setattr(core_logging.settings, "log_level", "VERBOSE")

    with pytest.raises(AgentException) as excinfo:
        setup_logging()

    assert "日志级别配置非法" in str(excinfo.value)


def test_third_party_loggers_floored_at_info(monkeypatch, restore_root_logger):
    """根降到 DEBUG 时第三方 SDK 仍停在 INFO。

    openai/httpcore 的 DEBUG 会把完整 system prompt 与对话正文打进日志,
    这是 AGENTOS_LOG_LEVEL=DEBUG 引入的泄漏面,必须由下限挡住。
    """
    from core import logging as core_logging

    monkeypatch.setattr(core_logging.settings, "log_level", "DEBUG")
    setup_logging()

    assert logging.getLogger().level == logging.DEBUG
    for name in _THIRD_PARTY_FLOOR_LOGGERS:
        assert logging.getLogger(name).level == logging.INFO


def test_third_party_floor_follows_root_above_info(monkeypatch, restore_root_logger):
    """根高于 INFO 时第三方同步抬高,不会反而变得比根更吵。"""
    from core import logging as core_logging

    monkeypatch.setattr(core_logging.settings, "log_level", "WARNING")
    setup_logging()

    for name in _THIRD_PARTY_FLOOR_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING
