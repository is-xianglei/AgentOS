"""执行引擎层：主代理与子代理的 LLM 执行循环，以及上下文压缩。

本层不属任何单一 feature 域——agent.py 编排 session、task、team、skill、
tool、permission、memory 多个域，故独立于各域之外，依赖方向为 runtime -> 各域。
"""
