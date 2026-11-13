"""记忆异步作业：抽取、整理（dream）、保留清理，以及作业调度与 Memory Worker。

worker.py 的 Memory Worker 随 API 进程的 lifespan 启动和关闭（main.py），每个进程各跑一个，
驱动 service.py 的 MemoryJobRunner 与 retention.py 的 MemoryRetentionRunner；
保留清理的跨进程互斥与扫描游标在 maintenance_lock.py。

多数能力仅由 Worker 触发，但 dream 的回滚是用户主动操作，
经 memory/api.py 同步调用 MemoryDreamService.rollback_dream_for_api。

依赖方向为 jobs -> memory 顶层（models / repository / service / rollout_service），
不依赖 recall 子包。

以下三件事只有 Memory Worker 会做，不经过 lifespan 的入口（测试、脚本）里它们都不会发生：

- Dream 任务：MemoryJobRunner._claim 在传入 job_id（就地执行）时只认领 extract 类型，
  dream Job 入队后只能等 Worker 认领。
- 保留清理：_run_retention_if_due 只存在于 Worker 主循环，超期归档项和终态 Job 不会被清理。
- 就地执行失败后的重试：日志写的是「等待Memory Worker接管」，
  _RETRY_DELAYS_SECONDS 的退避重试也要靠 Worker 再次认领。
"""
