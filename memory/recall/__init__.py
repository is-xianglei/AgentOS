"""记忆召回（读路径）：按会话轮次检索候选记忆并交由模型筛选。

依赖方向为 recall -> memory 顶层（models / repository / service / rollout_service），
不依赖 jobs 子包。
"""
