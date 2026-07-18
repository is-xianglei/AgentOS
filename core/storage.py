import asyncio
import mimetypes
from abc import ABC, abstractmethod
from dataclasses import dataclass

from core.config import settings
from core.errors import AgentException


@dataclass
class ObjectMeta:
    """对象轻量元数据,仅承载重建资源清单所需字段。"""

    key: str
    size: int


def skill_object_key(name: str, relative_path: str) -> str:
    """拼装 skill 资源的对象存储 key:{name}/{relative_path}。

    桶(skills)已专用于 skill,桶内直接以 name 分目录,不套额外前缀。
    例:skill_object_key("pdf-tools", "references/cli.md") -> "pdf-tools/references/cli.md"
    """
    return f"{name}/{relative_path}"


class ObjectStorage(ABC):
    """对象存储统一接口。"""

    @abstractmethod
    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        """写入单个对象。"""

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """读取单个对象为 bytes;不存在时抛领域错误。"""

    @abstractmethod
    async def list_prefix(self, prefix: str) -> list[ObjectMeta]:
        """列出某前缀下全部对象(key + size)。"""

    @abstractmethod
    async def delete_prefix(self, prefix: str) -> None:
        """删除某前缀下全部对象。"""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """判断对象是否存在。"""


class MinIOStorage(ObjectStorage):
    """基于官方 minio SDK 的对象存储实现。

    SDK 全部为同步阻塞调用,一律用 asyncio.to_thread(...) 到线程池执行。
    """

    def __init__(self) -> None:
        # 延迟导入 minio,避免未装依赖时影响模块加载(纯解析场景可不依赖存储)。
        from minio import Minio

        self._bucket = settings.minio_bucket
        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    async def ensure_bucket(self) -> None:
        """若 bucket 不存在则创建。"""
        try:
            exists = await asyncio.to_thread(self._client.bucket_exists, self._bucket)
            if not exists:
                await asyncio.to_thread(self._client.make_bucket, self._bucket)
        except Exception as exc:  # noqa: BLE001
            raise AgentException.message(f"创建Bucket失败: {exc}") from exc

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        import io

        try:
            await asyncio.to_thread(
                self._client.put_object,
                self._bucket,
                key,
                io.BytesIO(data),
                len(data),
                content_type=content_type or "application/octet-stream",
            )
        except Exception as exc:  # noqa: BLE001
            raise AgentException.message(f"写入对象失败: {key}: {exc}") from exc

    async def get(self, key: str) -> bytes:
        from minio.error import S3Error

        response = None
        try:
            response = await asyncio.to_thread(
                self._client.get_object, self._bucket, key
            )
            return await asyncio.to_thread(response.read)
        except S3Error as exc:
            # 对象不存在转领域错误,不泄漏底层异常。
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                raise AgentException.message(f"对象不存在: {key}") from exc
            raise AgentException.message(f"读取对象失败: {key}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise AgentException.message(f"读取对象失败: {key}: {exc}") from exc
        finally:
            if response is not None:
                await asyncio.to_thread(response.close)
                await asyncio.to_thread(response.release_conn)

    async def list_prefix(self, prefix: str) -> list[ObjectMeta]:
        def _list() -> list[ObjectMeta]:
            metas: list[ObjectMeta] = []
            for obj in self._client.list_objects(
                self._bucket, prefix=prefix, recursive=True
            ):
                metas.append(ObjectMeta(key=obj.object_name, size=obj.size or 0))
            return metas

        try:
            return await asyncio.to_thread(_list)
        except Exception as exc:  # noqa: BLE001
            raise AgentException.message(f"列举对象失败: {prefix}: {exc}") from exc

    async def delete_prefix(self, prefix: str) -> None:
        def _delete() -> None:
            for obj in self._client.list_objects(
                self._bucket, prefix=prefix, recursive=True
            ):
                self._client.remove_object(self._bucket, obj.object_name)

        try:
            await asyncio.to_thread(_delete)
        except Exception as exc:  # noqa: BLE001
            raise AgentException.message(f"删除前缀失败: {prefix}: {exc}") from exc

    async def exists(self, key: str) -> bool:
        from minio.error import S3Error

        try:
            await asyncio.to_thread(self._client.stat_object, self._bucket, key)
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                return False
            raise AgentException.message(f"探测对象失败: {key}: {exc}") from exc


# 进程内单例缓存。
_storage_instance: ObjectStorage | None = None


def get_object_storage() -> ObjectStorage:
    """返回进程内缓存的对象存储单例(本期恒为 MinIOStorage)。"""
    global _storage_instance
    if _storage_instance is None:
        _storage_instance = MinIOStorage()
    return _storage_instance


# mimetypes 兜底:确保常见脚本类型可被 guess_type 识别(供资源清单重建用)。
mimetypes.add_type("text/x-python", ".py")
mimetypes.add_type("text/markdown", ".md")
