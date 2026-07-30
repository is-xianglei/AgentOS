"""记忆异步作业：抽取、整理（dream）、保留清理，以及作业调度与 Worker 入口。

worker.py 是独立进程入口（python -m memory.jobs.worker），
驱动 service.py 的 MemoryJobRunner 与 retention.py 的 MemoryRetentionRunner。

多数能力仅由 Worker 触发，但 dream 的回滚是用户主动操作，
经 memory/api.py 同步调用 MemoryDreamService.rollback_dream_for_api。

依赖方向为 jobs -> memory 顶层（models / repository / service / rollout_service），
不依赖 recall 子包。
"""
