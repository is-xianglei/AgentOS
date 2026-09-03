# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

This directory contains guidelines for backend development. Fill in each file with your project's specific conventions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Module organization and file layout | ✅ 已完成 |
| [Database Guidelines](./database-guidelines.md) | ORM patterns, queries, migrations | ✅ 已完成 |
| [Error Handling](./error-handling.md) | Error types, handling strategies | ✅ 已完成 |
| [Quality Guidelines](./quality-guidelines.md) | Code standards, forbidden patterns | ✅ 已完成 |
| [Logging Guidelines](./logging-guidelines.md) | 结构化日志、级别配置、KV Formatter 契约、敏感信息边界与踩坑 | ✅ 已完成 |
| [LLM Provider Guidelines](./llm-provider-guidelines.md) | LLM 工厂入口、场景模型解析、双 provider 契约、消息中立协议与思考块、消息成本归因 | ✅ 已完成 |
| [Workflow Orchestration](./workflow-orchestration.md) | Workflow 数据模型与迁移、发消息入口的分发判据、三种模式失败语义、SubAgentSpec 桥接、`workflow` LLM 场景 | ✅ 已完成 |

---

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**语言**: 本项目所有规范文档使用**简体中文**编写，与项目 CLAUDE.md、代码注释、错误消息保持一致。
