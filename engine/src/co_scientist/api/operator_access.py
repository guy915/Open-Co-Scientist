import hmac

from fastapi import Request

from co_scientist.core.config import settings


def has_admin_token(request: Request) -> bool:
    token = settings.logs_admin_token
    supplied = request.headers.get("X-Logs-Token", "")
    return bool(token and supplied and hmac.compare_digest(supplied.encode(), token.encode()))


def is_operator(request: Request) -> bool:
    return has_admin_token(request)
