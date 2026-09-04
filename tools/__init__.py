"""工具系统包入口。

刻意不再导出 ToolRegistry / build_tool_registry：database/registry.py 会导入 tools.models，
若此处再导出 registry，会把一次 ORM 导入放大成整套工具及其依赖的 Service，
形成 session.repository -> tools -> tools.builtin.* -> task/team.service -> session.service 的
加载期环。消费者一律 from tools.registry import ...。
"""
