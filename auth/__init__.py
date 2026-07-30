"""认证域：注册、登录与 JWT 令牌生命周期。

本域不持有 ORM 实体，注册与切换工作区会同时触及 user 与 workspace，
故独立成包而非置于 user/ 之下，依赖方向为 auth -> user / workspace。
"""
