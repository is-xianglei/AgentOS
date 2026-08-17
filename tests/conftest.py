"""pytest 全局夹具。

异步用例统一声明 `pytestmark = pytest.mark.anyio`,由 .venv 中 anyio 自带的
pytest 插件驱动(已注册 pytest11 入口点),不引入 pytest-asyncio。
Phase 0 的基线测试均为同步,本夹具为后续装配器等异步测试预置骨架。
"""

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """限定 anyio 只跑 asyncio 后端,与生产运行时一致。"""
    return "asyncio"
