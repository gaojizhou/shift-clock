"""领域异常：服务层抛出，路由层映射为对应 HTTP 状态码（避免靠中文消息字符串匹配）。"""


class DomainError(ValueError):
    """业务错误基类（继承 ValueError 以兼容既有 except ValueError）。"""

    status_code = 400


class NotFoundError(DomainError):
    status_code = 404


class ConflictError(DomainError):
    status_code = 409
