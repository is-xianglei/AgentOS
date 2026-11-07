"""工具平台数据包入口：工具目录、调用记录及其执行网关。

刻意不在此导出任何内容：database/registry.py 会导入 tools.models，若此处导出 service，
会把一次 ORM 导入放大成整套内置工具及其依赖的 Service，形成
session.repository -> tools -> harness.adapters.tools.builtin.* -> task/team.service
-> session.service 的加载期环。消费者一律从具体子模块导入。
"""
