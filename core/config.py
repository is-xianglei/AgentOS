from pathlib import Path

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
