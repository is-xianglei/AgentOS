from pathlib import Path

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTOS_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        # extra="ignore",  # .env 中未在此声明的键忽略,不报错
    )

    # --- LLM(Anthropic) -----------------------------------------------------
    anthropic_base_url: str | None = None
    anthropic_api_key: str | None = None
    anthropic_model: str | None = None
    max_tool_iterations: int = 50  # 工具调用上限,防止模型在工具循环里无限调用

    # --- SubAgent ------------------------------------------------------------
    # 模型档位:留空表示继承主会话模型(anthropic_model)。
    # 优先级为「调用方显式指定 > spec 声明 > 本项兜底 > 继承主会话」,
    # 见 tools/subagents/definition.py 的 resolve_model()。default 按兜底处理,
    # 否则一配上去会把 Explore 的速度档位一并顶掉,与"默认值"语义相反。
    subagent_default_model: str | None = None  # 未声明模型的类型的兜底
    subagent_fast_model: str | None = None  # Explore 用,速度优先档位
    # ReAct 轮次上限,按类型分档。verification 须跑构建+测试+lint+对抗探测。
    subagent_max_rounds: int = 6
    subagent_plan_max_rounds: int = 12
    subagent_verification_max_rounds: int = 24
    # 每会话并发 SubAgent 上限,防止无限 spawn 导致连接与 token 失控。
    subagent_max_concurrency: int = 3

    # --- 数据库 --------------------------------------------------------------
    database_url: str | None = None

    # --- Redis ---------------------------------------------------------------
    redis_url: str | None = None
    redis_password: str | None = None
    disable_redis_events: bool = False

    # --- 调试 / 多实例隔离 ---------------------------------------------------
    team_task_debug_isolation: bool = False
    instance_id: str | None = None
    create_tables: bool = False  # 启动时自动建表(仅开发用)

    # --- 对象存储 ---------------------------------------------------
    minio_endpoint: str | None = None  # 形如 "localhost:9000"(不含 scheme)
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    minio_bucket: str = "skills"
    minio_secure: bool = False  # 是否走 https,默认 false(本地 MinIO 多为 http)

    # --- skill 脚本执行 -----------------------
    skill_exec_enabled: bool = True
    skill_exec_timeout: int = 30  # 单次执行超时(秒)
    skill_exec_max_output: int = 65536  # stdout/stderr 各自字节上限

    # --- 长期 Memory -----------------------
    memory_selector_model: str | None = None
    memory_selector_timeout_seconds: float = 5.0
    memory_selector_query_max_chars: int = 12_000
    memory_recall_max_items: int = 5
    memory_recall_item_max_bytes: int = 4 * 1024
    memory_recall_item_max_lines: int = 200
    memory_session_max_bytes: int = 60 * 1024
    memory_extraction_enabled: bool = True
    memory_extraction_inline: bool = True
    memory_extractor_model: str | None = None
    memory_extractor_timeout_seconds: float = 20.0
    memory_extractor_prompt_version: str = "v1"
    memory_extractor_max_messages: int = 10
    memory_extractor_max_chars: int = 20_000
    memory_extractor_max_items: int = 5
    memory_job_max_attempts: int = 5
    memory_job_lease_seconds: int = 300
    memory_job_claim_batch_size: int = 10
    memory_worker_poll_seconds: float = 1.0
    memory_dream_enabled: bool = True
    memory_dream_model: str | None = None
    memory_dream_timeout_seconds: float = 60.0
    memory_dream_prompt_version: str = "v1"
    memory_dream_min_items: int = 10
    memory_dream_min_sessions: int = 5
    memory_dream_interval_seconds: int = 24 * 60 * 60
    memory_dream_scan_interval_seconds: int = 60 * 60
    memory_dream_lease_seconds: int = 60 * 60
    memory_dream_max_input_chars: int = 120_000
    memory_dream_max_operations: int = 100
    memory_retention_enabled: bool = True
    memory_archived_item_retention_days: int = Field(default=90, ge=0, le=36_500)
    memory_terminal_job_retention_days: int = Field(default=30, ge=0, le=36_500)
    memory_retention_batch_size: int = Field(default=100, ge=1, le=10_000)
    memory_retention_interval_seconds: int = Field(default=60 * 60, ge=1)
    memory_rollout_salt: str = "agentos-memory-v1"
    memory_recall_rollout_percent: int = Field(default=0, ge=0, le=100)
    memory_dream_rollout_percent: int = Field(default=0, ge=0, le=100)
    memory_extraction_shadow_enabled: bool = True

    # --- JWT 认证 -----------------------
    jwt_secret_key: str = "change-this-secret-key-in-production"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """把 dotenv_settings 排到 env_settings 之前,实现 .env 覆盖 shell env。

        pydantic-settings 默认优先级为 env > dotenv,与本项目"防 shell 残留变量
        劫持"的历史语义相反。此处显式调整为 init > dotenv > env > secrets。
        """
        return (
            init_settings,
            dotenv_settings,
            env_settings,
            file_secret_settings,
        )


# 全局单例:导入时即完成加载 / 解析 / 校验。
settings = Settings()


# --- 非环境变量的代码常量 ----------------------------------------------------
# 解释器白名单:扩展名 -> 解释器 argv 前缀(不在表中的扩展名一律拒绝执行,不做猜测执行)。
SKILL_EXEC_INTERPRETERS: dict[str, list[str]] = {
    ".py": ["python"],
    ".sh": ["bash"],
    ".js": ["node"],
}
