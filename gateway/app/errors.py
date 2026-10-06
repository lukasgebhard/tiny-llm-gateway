"""OpenAI-style error responses."""

from fastapi.responses import JSONResponse


class GatewayError(Exception):
    def __init__(self, status_code: int, message: str, type_: str, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.type = type_
        self.code = code

    def to_dict(self) -> dict:
        return {"error": {"message": self.message, "type": self.type, "code": self.code}}

    def to_response(self, headers: dict[str, str] | None = None) -> JSONResponse:
        return JSONResponse(self.to_dict(), status_code=self.status_code, headers=headers)


def unauthorized(message: str = "Invalid or missing API key") -> GatewayError:
    return GatewayError(401, message, "authentication_error", "invalid_api_key")


def bad_request(message: str) -> GatewayError:
    return GatewayError(400, message, "invalid_request_error")


def model_not_found(model: str) -> GatewayError:
    return GatewayError(
        404, f"Model {model!r} does not exist", "invalid_request_error", "model_not_found"
    )


def external_not_allowed(model: str) -> GatewayError:
    return GatewayError(
        403,
        f"Model {model!r} is only served by external providers, "
        "and this API key is not allowed to use them",
        "permission_error",
        "external_models_not_allowed",
    )


def no_backend_available(model: str) -> GatewayError:
    return GatewayError(
        503,
        f"No backend for model {model!r} is currently available",
        "service_unavailable",
        "no_backend_available",
    )
