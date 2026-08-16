---
name: trellis-local
description: "本仓库的 Trellis 本地约定。在创建 Trellis 任务、给任务目录命名、或改动本项目 Trellis 工作流时使用。"
---

# Trellis 本地约定

本技能只记录 **本仓库** 对 Trellis 的定制，不覆盖 Trellis 通用机制。

## 任务目录用简体中文命名

`.trellis/tasks/` 下的任务目录，slug 部分一律用**简体中文**，不用英文 kebab-case。

```bash
# ✅ 正确：显式传中文 --slug
python3 ./.trellis/scripts/task.py create "统一大模型调用入口" --slug "统一大模型调用入口"
# 得到 .trellis/tasks/08-15-统一大模型调用入口

# ❌ 错误：不传 --slug
python3 ./.trellis/scripts/task.py create "统一大模型调用入口"
# 报错 Error: could not generate slug from title
```

**必须显式传 `--slug` 的原因**：`_slugify()`
（`.trellis/scripts/common/task_store.py:67`）把所有非 ASCII 字符替换成 `-`
再 strip，中文标题会被清成空串，于是 `task_store.py:272` 判空报错。
该文件是 Trellis 模板托管文件（记录在 `.trellis/.template-hashes.json`），
改它会与 `trellis update` 冲突，所以本项目选择显式传参而非改脚本。

一般 `--slug` 直接复用 `title`，两者保持一致。

## 日期前缀保持两位补零

目录形如 `08-15-统一大模型调用入口`，**不要**写成 `8-15-...`。
`generate_task_date_prefix()`（`.trellis/scripts/common/paths.py:420`）固定用
`strftime("%m-%d")`；`task_store.py:284` 还有一条 `^(\d{2})-(\d{2})-(.+)$`
的防重复前缀校验依赖两位数字。两位补零也保证目录按字符串排序时月份不乱序。

## 已验证可用的命令

以下命令在中文目录名下实测通过（`create` / `start` / `current` / `list` /
`add-context` / `list-context` / `validate` / `set-branch` / `archive` /
`list-archive`），归档时的 git 自动提交也正常。

## 注意事项

1. **活动任务指针**存在 `.trellis/.runtime/sessions/<session>.json` 的
   `current_task` 字段（每个会话一份），不是单一文件。手工重命名任务目录后，
   用 `task.py start <新路径>` 重新指向，并检查其余 session 文件里的陈旧路径。
2. **git 中文路径显示**：git 默认 `core.quotepath=true`，`git status` 会把中文
   路径显示成 `\347\273\237...` 八进制转义。执行 `git config core.quotepath false`
   可正常显示。本仓库 `.gitignore:63` 的 `tasks/` 已忽略整个 `.trellis/tasks/`，
   任务目录改名不进版本库。
3. **git 分支名**不必跟随任务目录用中文。分支名沿用英文 kebab-case，
   避免部分 CI／工具链对非 ASCII 分支名的兼容问题。
