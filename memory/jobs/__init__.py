"""记忆异步作业：抽取、整理（dream）、保留清理，以及作业调度与 Worker 入口。

worker.py 是独立进程入口（python -m memory.jobs.worker），
驱动 service.py 的 MemoryJobRunner 与 retention.py 的 MemoryRetentionRunner。

多数能力仅由 Worker 触发，但 dream 的回滚是用户主动操作，
经 memory/api.py 同步调用 MemoryDreamService.rollback_dream_for_api。

依赖方向为 jobs -> memory 顶层（models / repository / service / rollout_service），
不依赖 recall 子包。

但有三件事只有 Worker 会做

- Dream 任务：memory/jobs/service.py:199 的 _claim 里，传了 job_id 就只认领 extract 类型。dream job 入队后没有 Worker 就一直躺在表里。
- Retention 清理：_run_retention_if_due 只存在于 Worker 主循环，归档项和终态 Job 永远不会被清。
- inline 失败后的重试：inline 异常时日志写的是「等待独立 Worker 接管」——没 Worker 就没人接管，靠 _RETRY_DELAYS_SECONDS 的退避重试全部失效。

"""
