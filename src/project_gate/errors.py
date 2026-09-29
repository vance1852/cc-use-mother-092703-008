"""重大项目阶段门控服务向 API 和 CLI 暴露的稳定错误。"""


class GateError(RuntimeError):
    code = "gate_error"
    status = 400

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details


class NotFound(GateError):
    code = "not_found"
    status = 404


class Conflict(GateError):
    code = "conflict"
    status = 409


class Forbidden(GateError):
    code = "forbidden"
    status = 403


class InvalidState(GateError):
    code = "invalid_state"
    status = 409


class GatingBlocked(GateError):
    """所有适用门槛未全部满足，阶段令不得签发。"""

    code = "gating_blocked"
    status = 409


class ValidationFailed(GateError):
    code = "validation_failed"
    status = 422
