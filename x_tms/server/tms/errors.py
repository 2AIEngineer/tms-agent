"""Erreurs normalisées au format RFC 7807 (application/problem+json)."""

from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_MEDIA_TYPE = "application/problem+json"


class ProblemError(Exception):
    def __init__(
        self, status: int, title: str, detail: str | None = None, type_: str = "about:blank", **extra
    ):
        self.status = status
        self.title = title
        self.detail = detail
        self.type = type_
        self.extra = extra


def not_found(resource: str, id_: str) -> ProblemError:
    return ProblemError(
        404, f"{resource} introuvable", f"Aucun(e) {resource.lower()} avec l'identifiant {id_!r}."
    )


def _problem(
    request: Request, status: int, title: str, detail=None, type_="about:blank", **extra
) -> JSONResponse:
    body = {"type": type_, "title": title, "status": status, "instance": request.url.path}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_MEDIA_TYPE)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem_error(request: Request, exc: ProblemError):
        return _problem(request, exc.status, exc.title, exc.detail, exc.type, **exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        title = HTTPStatus(exc.status_code).phrase
        detail = exc.detail if exc.detail != title else None
        return _problem(request, exc.status_code, title, detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()
        ]
        return _problem(
            request,
            422,
            "Requête invalide",
            "Un ou plusieurs paramètres sont invalides.",
            "https://tms.local/problems/validation-error",
            errors=errors,
        )
