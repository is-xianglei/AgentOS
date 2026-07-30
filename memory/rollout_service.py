"""Memory 功能的稳定用户灰度决策。"""

import hashlib

from core.config import settings


class MemoryRolloutService:
    """按用户和功能稳定分桶，不依赖进程内随机状态。"""

    _BUCKETS = 100

    @classmethod
    def bucket(cls, user_id: int, feature: str, *, salt: str | None = None) -> int:
        """返回 0 到 99 的稳定桶编号。"""
        effective_salt = settings.memory_rollout_salt if salt is None else salt
        digest = hashlib.sha256(f"{effective_salt}:{user_id}:{feature}".encode()).digest()
        return int.from_bytes(digest[:8], "big") % cls._BUCKETS

    @classmethod
    def is_enabled(cls, user_id: int, feature: str, percentage: int) -> bool:
        """按百分比判断用户是否命中灰度。"""
        if not 0 <= percentage <= cls._BUCKETS:
            raise ValueError("灰度百分比必须在 0 到 100 之间")
        return percentage == cls._BUCKETS or (
            percentage > 0 and cls.bucket(user_id, feature) < percentage
        )

    @classmethod
    def recall_enabled(cls, user_id: int) -> bool:
        return cls.is_enabled(user_id, "recall", settings.memory_recall_rollout_percent)

    @classmethod
    def dream_enabled(cls, user_id: int) -> bool:
        return cls.is_enabled(user_id, "dream", settings.memory_dream_rollout_percent)

    @staticmethod
    def extraction_mode() -> str:
        """返回应冻结到提取 Job 的执行模式。"""
        return "shadow" if settings.memory_extraction_shadow_enabled else "active"
