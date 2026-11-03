"""LLM 调用的中立异常:各家 SDK 异常在客户端内部转换成这里的类型,调用方不认供应商异常。

只表达调用方需要区分的信息:传输不可用、上游 HTTP 状态码、其余 SDK 失败。
不继承 AgentException:后者面向 API 响应,而这里的消息来自上游 SDK,只用于服务端排障。
SDK 自带的重试用完后才会转换,调用方不应再假设异常来自哪家 SDK。
"""


class LLMCallError(Exception):
    """LLM SDK 调用失败(重试用完之后);__cause__ 保留原始 SDK 异常。

    消息沿用原始 SDK 异常的 str(),保证已有日志和事件文本不因转换而改变。
    status_code 只用于诊断展示:响应校验失败这类非状态错误也可能带状态码,
    是否按 HTTP 状态处理必须看是否为 LLMStatusError,不能看该字段是否为空。
    """

    def __init__(
        self,
        message: str,
        *,
        sdk_error_type: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        # 原始 SDK 异常类名(如 APITimeoutError),供 Job 记录和日志排障。
        self.sdk_error_type = sdk_error_type
        self.status_code = status_code


class LLMConnectionError(LLMCallError):
    """未拿到上游响应:连接失败或请求超时。"""


class LLMStatusError(LLMCallError):
    """上游返回了错误状态码;流式中途下发的错误事件也归此类,状态码沿用 SDK 给出的值。"""

    status_code: int

    def __init__(self, message: str, *, sdk_error_type: str, status_code: int) -> None:
        super().__init__(message, sdk_error_type=sdk_error_type, status_code=status_code)


def error_type_name(exc: BaseException) -> str:
    """返回用于排障记录的异常类名:中立 LLM 异常取原始 SDK 类名,其余取自身类名。"""
    if isinstance(exc, LLMCallError):
        return exc.sdk_error_type
    return type(exc).__name__
