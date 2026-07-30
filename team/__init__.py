"""团队域：团队成员、成员间消息、子代理运行记录与任务认领的协程级隔离。

子代理执行循环仍在 services/subagent_runner.py：它与 agent_runtime 同属
LLM 执行引擎，后续归入 runtime/，不随本域迁入。
"""
