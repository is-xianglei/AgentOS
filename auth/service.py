import hashlib
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors import AgentException
from user.models import UserRecord
from user.repository import UserRepository
from workspace.models import WorkspaceRecord
from workspace.service import WorkspaceService


def _pick_default_workspace(workspaces: list[WorkspaceRecord]) -> WorkspaceRecord | None:
    """未指定工作区时的默认选择：优先个人工作区，否则取第一个；没有工作区返回 None。"""
    for workspace in workspaces:
        if workspace.workspace_type == "personal":
            return workspace
    if workspaces:
        return workspaces[0]
    return None


class AuthService:
    """认证服务：注册、登录、刷新 Token 与切换工作区，负责签发和校验 JWT。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = UserRepository(db)

    def _hash_password(self, password: str) -> str:
        """密码哈希（MD5）"""
        return hashlib.md5(password.encode("utf-8")).hexdigest()

    def _verify_password(self, password: str, hashed_password: str) -> bool:
        """验证密码"""
        return self._hash_password(password) == hashed_password

    async def register(
        self,
        email: str,
        username: str,
        password: str,
        full_name: str | None = None,
    ) -> dict:
        """用户注册：创建用户和个人工作区，并签发带该工作区的 Token。"""
        existing_email = await self.repo.get_by_email(email)
        if existing_email:
            raise AgentException.message("邮箱已被注册")

        hashed_password = self._hash_password(password)
        user = await self.repo.create(
            username=username,
            email=email,
            password=hashed_password,
            full_name=full_name,
        )
        # 不在此 commit:user 的 INSERT 已由 repo.create 内的 flush 落到事务中,
        # user.id 可用;整体提交交给请求边界(get_db),与下面的工作区创建同事务,
        # 若工作区创建失败则 user 一并回滚,避免孤儿用户。

        workspace_service = WorkspaceService(self.db)
        workspace: WorkspaceRecord = await workspace_service.create_workspace(
            creator_user_id=user.id,
            name=f"{username}_personal",
            slug=f"{username}-personal",
            display_name=f"{full_name or username}的个人空间",
            workspace_type="personal",
        )

        tokens = self._generate_tokens_with_workspace(user, workspace.id)

        return {
            "user": user,
            "workspace_id": workspace.id,
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
        }

    async def login(self, email: str, password: str) -> dict:
        """邮箱密码登录，Token 绑定默认工作区（优先个人工作区）。"""
        user = await self.repo.get_by_email(email)
        # 先验密码再查状态：不知道密码的人只能拿到同一句报错，无法探测邮箱是否注册或已停用。
        if not user or not self._verify_password(password, user.password):
            raise AgentException.message("用户名或密码错误")
        # 全局软删除过滤下查不到已删除用户，此分支正常走不到；保留作纵深防御。
        if user.is_deleted:
            raise AgentException.message("账号已被删除")
        if user.suspended:
            raise AgentException.message("账号已被禁用")

        # 只改实体字段，提交交给请求边界统一处理。
        user.last_login_at = datetime.now(UTC)

        workspace_service = WorkspaceService(self.db)
        user_workspaces = await workspace_service.list_user_workspaces(user.id)
        default_workspace = _pick_default_workspace(user_workspaces)
        workspace_id = None
        if default_workspace is not None:
            workspace_id = default_workspace.id

        if workspace_id:
            tokens = self._generate_tokens_with_workspace(user, workspace_id)
        else:
            # 用户没有任何有效工作区时，签发不带 workspace_id 的 Token。
            tokens = self._generate_tokens(user)

        return {
            "user": user,
            "workspace_id": workspace_id,
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
        }

    async def verify_token(self, token: str) -> UserRecord:
        """验证 Access Token"""
        try:
            payload = jwt.decode(
                token,
                settings.jwt_secret_key,
                algorithms=[settings.jwt_algorithm],
            )

            if payload.get("type") != "access":
                raise AgentException.message("无效的 Token 类型")

            user_id = int(payload.get("sub"))
            user = await self.repo.get_by_id(user_id)

            if not user or user.is_deleted or user.suspended:
                raise AgentException.message("用户不存在或已被禁用")

            return user

        except jwt.ExpiredSignatureError as exc:
            raise AgentException.message("Token 已过期") from exc
        except jwt.InvalidTokenError as exc:
            raise AgentException.message("无效的 Token") from exc

    async def refresh_token(self, refresh_token: str) -> dict:
        """用 Refresh Token 换新的 Access Token，Refresh Token 原样返回、不轮换。"""
        try:
            payload = jwt.decode(
                refresh_token,
                settings.jwt_secret_key,
                algorithms=[settings.jwt_algorithm],
            )

            if payload.get("type") != "refresh":
                raise AgentException.message("无效的 Token 类型")

            user_id = int(payload.get("sub"))
            user = await self.repo.get_by_id(user_id)

            if not user or user.is_deleted or user.suspended:
                raise AgentException.message("用户不存在或已被禁用")

            workspace_id = payload.get("workspace_id")
            if type(workspace_id) is int:
                # 签发时的成员关系可能已失效，换发前重新校验。
                await WorkspaceService(self.db).require_active_member(workspace_id, user.id)
                tokens = self._generate_tokens_with_workspace(user, workspace_id)
            else:
                # 兼容升级前签发的 refresh token：只从实时有效成员关系中选择工作区。
                workspaces = await WorkspaceService(self.db).list_user_workspaces(user.id)
                selected = _pick_default_workspace(workspaces)
                if selected is not None:
                    tokens = self._generate_tokens_with_workspace(user, selected.id)
                else:
                    tokens = self._generate_tokens(user)

            return {
                "access_token": tokens["access_token"],
                "refresh_token": refresh_token,
            }

        except jwt.ExpiredSignatureError as exc:
            raise AgentException.message("Refresh Token 已过期") from exc
        except jwt.InvalidTokenError as exc:
            raise AgentException.message("无效的 Refresh Token") from exc

    async def switch_workspace(self, user_id: int, workspace_id: int) -> dict:
        """切换工作区并生成新的 Token。

        Token 的 workspace_id 是后续请求的可信作用域来源，签发前必须确认成员身份，
        否则任意登录用户都能凭任意 workspace_id 换到该工作区的访问权。
        成员与工作区状态校验由数据所属的 workspace 域负责。
        """
        user = await self.repo.get_by_id(user_id)
        if not user or user.is_deleted or user.suspended:
            raise AgentException.message("用户不存在或已被禁用")

        await WorkspaceService(self.db).require_active_member(workspace_id, user_id)

        tokens = self._generate_tokens_with_workspace(user, workspace_id)

        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
        }

    # 下面两个方法的 claims 只差 workspace_id，刻意各自写全，便于逐字段核对签发内容。
    def _generate_tokens(self, user: UserRecord) -> dict:
        """生成不带工作区的 JWT，仅用于用户没有任何有效工作区的情况。"""
        # Access Token
        access_payload = {
            "sub": str(user.id),
            "email": user.email,
            "username": user.username,
            "type": "access",
            "exp": datetime.now(UTC) + timedelta(minutes=settings.jwt_access_token_expire_minutes),
            "iat": datetime.now(UTC),
        }

        access_token = jwt.encode(
            access_payload,
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )

        # Refresh Token
        refresh_payload = {
            "sub": str(user.id),
            "type": "refresh",
            "exp": datetime.now(UTC) + timedelta(days=settings.jwt_refresh_token_expire_days),
            "iat": datetime.now(UTC),
        }

        refresh_token = jwt.encode(
            refresh_payload,
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
        }

    def _generate_tokens_with_workspace(self, user: UserRecord, workspace_id: int) -> dict:
        """生成带 workspace_id 的 JWT，后续请求以该 claim 作为当前工作区。"""
        # Access Token
        access_payload = {
            "sub": str(user.id),
            "email": user.email,
            "username": user.username,
            "workspace_id": workspace_id,
            "type": "access",
            "exp": datetime.now(UTC) + timedelta(minutes=settings.jwt_access_token_expire_minutes),
            "iat": datetime.now(UTC),
        }

        access_token = jwt.encode(
            access_payload,
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )

        # Refresh Token
        refresh_payload = {
            "sub": str(user.id),
            "workspace_id": workspace_id,
            "type": "refresh",
            "exp": datetime.now(UTC) + timedelta(days=settings.jwt_refresh_token_expire_days),
            "iat": datetime.now(UTC),
        }

        refresh_token = jwt.encode(
            refresh_payload,
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
        }
