"""HTTP response conventions for the on-prem API."""
from starlette.responses import JSONResponse


def json_error(status, error, detail):
    return JSONResponse({"error": error, "detail": detail}, status_code=status)


async def application_error_handler(_request, error):
    return json_error(error.status, error.code, error.detail)


def request_session_id(request):
    return (request.headers.get("x-cityverse-session-id")
            or request.headers.get("x-session-id") or "local-session")[:256]
