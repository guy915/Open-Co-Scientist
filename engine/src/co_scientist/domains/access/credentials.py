from __future__ import annotations

import base64
import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from co_scientist.core.byok_scope import (
    ByokCredential,
    CustomModelCapabilities,
    redact_credential,
    scoped_byok,
)
from co_scientist.core.config import byok_default_model, settings
from co_scientist.domains.access import byok_models
from co_scientist.platform.llm.llm_scope import budgeted

if TYPE_CHECKING:
    from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-LLM-API-Key"
PROVIDER_HEADER = "X-LLM-Provider"
SUPERVISOR_API_KEY_HEADER = "X-LLM-Supervisor-API-Key"
SUPERVISOR_PROVIDER_HEADER = "X-LLM-Supervisor-Provider"

# Bound credential validation so a dead provider cannot hang run creation.
_VALIDATION_TIMEOUT_SECONDS = 30.0


class ByokRequestError(ValueError):
    """Malformed provider/key pairs are request errors."""


class ByokValidationError(Exception):
    """Rejected or unreachable provider credentials are refused before run
    creation.
    """


class ByokNotConfiguredError(RuntimeError):
    """Missing encryption support refuses credential-bearing requests."""


def _fernet() -> Fernet:
    """HKDF derives a purpose-bound Fernet key from any nonempty deployment
    secret.
    """
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    secret = settings.byok_encryption_key
    if not secret:
        raise ByokNotConfiguredError("bring-your-own-key support requires BYOK_ENCRYPTION_KEY")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"coscientist-byok-v1",
        info=b"fernet key",
    ).derive(secret.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_api_key(api_key: str) -> str:
    return _fernet().encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt_api_key(token: str) -> str:
    """Corrupt or rotated ciphertext fails loudly rather than silently
    billing the deployment's credential.
    """
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError(
            "stored BYOK credential cannot be decrypted; the encryption key may have rotated"
        ) from exc


def idempotency_secret_fingerprint(api_key: str) -> str:
    """A purpose-separated keyed digest identifies receipt intent without
    persisting plaintext credentials.
    """
    from cryptography.hazmat.primitives import hashes, hmac

    secret = settings.byok_encryption_key
    if not secret:
        raise ByokNotConfiguredError("bring-your-own-key support requires BYOK_ENCRYPTION_KEY")
    digest = hmac.HMAC(secret.encode("utf-8"), hashes.SHA256())
    digest.update(b"co-scientist/run-create-idempotency/v1\0" + api_key.encode("utf-8"))
    return digest.finalize().hex()


def run_creation_request_digest(
    request_fields: Mapping[str, Any],
    *,
    api_key: str | None = None,
    provider: str | None = None,
    supervisor_api_key: str | None = None,
    supervisor_provider: str | None = None,
) -> str:
    key = (api_key or "").strip()
    supervisor_key = (supervisor_api_key or "").strip()
    fingerprint = idempotency_secret_fingerprint(key) if key else None
    canonical = json.dumps(
        {
            "request": request_fields,
            "byok_provider": (provider or "").strip().lower() if key else None,
            "byok_secret_fingerprint": fingerprint,
            "byok_supervisor_provider": (supervisor_provider or "").strip().lower()
            if supervisor_key
            else None,
            "byok_supervisor_fingerprint": (
                idempotency_secret_fingerprint(supervisor_key) if supervisor_key else None
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest_input = b"co-scientist/run-create/v1\0" + canonical
    return hashlib.sha256(digest_input).hexdigest()


def _supervisor_route(
    headers: Mapping[str, str], provider: str, api_key: str
) -> tuple[str, str | None, str | None]:
    """A supervisor on another provider needs both its provider and its own
    key; one without the other is a malformed request. Returns the provider
    its model resolves against plus the credential fields to store.
    """
    sup_provider = (headers.get(SUPERVISOR_PROVIDER_HEADER) or "").strip()
    sup_key = (headers.get(SUPERVISOR_API_KEY_HEADER) or "").strip()
    if bool(sup_provider) != bool(sup_key):
        raise ByokRequestError(
            f"{SUPERVISOR_PROVIDER_HEADER} and {SUPERVISOR_API_KEY_HEADER} must be sent together"
        )
    sup_provider = sup_provider.lower()
    if not sup_provider or (sup_provider == provider and sup_key == api_key):
        return provider, None, None
    if byok_default_model(sup_provider) is None:
        raise ByokRequestError(f"unsupported provider: {sup_provider}")
    return sup_provider, sup_provider, sup_key


def credential_from_headers(
    headers: Mapping[str, str],
) -> ByokCredential | None:
    api_key = (headers.get(API_KEY_HEADER) or "").strip()
    if not api_key:
        offered = {model for models in byok_models.model_catalog().values() for model in models}
        for header in (byok_models.WORKER_MODEL_HEADER, byok_models.SUPERVISOR_MODEL_HEADER):
            requested = (headers.get(header) or "").strip()
            if requested and requested not in offered:
                raise ByokRequestError("Custom models require your own API key")
        return None
    provider = (headers.get(PROVIDER_HEADER) or "").strip().lower()
    if not provider:
        raise ByokRequestError(f"the {API_KEY_HEADER} header requires {PROVIDER_HEADER}")
    if byok_default_model(provider) is None:
        raise ByokRequestError(f"unsupported provider: {provider}")
    supervisor_route, sup_provider, sup_key = _supervisor_route(headers, provider, api_key)
    try:
        model = byok_models.resolve_model_choice(
            provider, headers.get(byok_models.WORKER_MODEL_HEADER), api_key=api_key
        )
        supervisor = byok_models.resolve_model_choice(
            supervisor_route,
            headers.get(byok_models.SUPERVISOR_MODEL_HEADER),
            api_key=sup_key or api_key,
        )
    except byok_models.ByokModelError as exc:
        raise ByokRequestError(str(exc)) from exc
    from co_scientist.domains.access.custom_models import cached_validation

    metadata = {}
    for name, route, key in (
        (model, provider, api_key),
        (supervisor, supervisor_route, sup_key or api_key),
    ):
        validated = cached_validation(route, name, key)
        if validated is not None and validated.supported and validated.capabilities is not None:
            metadata[name] = validated.capabilities
    return ByokCredential(
        provider=provider,
        api_key=api_key,
        model=model,
        supervisor_model=supervisor,
        supervisor_provider=sup_provider,
        supervisor_api_key=sup_key,
        custom_models=metadata,
    )


def store_run_credential(
    run_id: str,
    client_id: str,
    credential: ByokCredential,
    db_path: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    from co_scientist.platform.db import current_time, use_conn

    with use_conn(conn, db_path) as active:
        active.execute(
            "INSERT INTO run_credentials (run_id, client_id, provider, "
            "model, supervisor_model, encrypted_key, created_at, "
            "supervisor_provider, encrypted_supervisor_key, custom_models_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET client_id=excluded."
            "client_id, provider=excluded.provider, "
            "model=excluded.model, "
            "supervisor_model=excluded.supervisor_model, "
            "encrypted_key=excluded.encrypted_key, "
            "supervisor_provider=excluded.supervisor_provider, "
            "encrypted_supervisor_key=excluded.encrypted_supervisor_key, "
            "custom_models_json=excluded.custom_models_json",
            (
                run_id,
                client_id,
                credential.provider,
                credential.model,
                credential.supervisor_model,
                encrypt_api_key(credential.api_key),
                current_time(),
                credential.supervisor_provider,
                encrypt_api_key(credential.supervisor_api_key)
                if credential.supervisor_api_key
                else None,
                json.dumps(
                    {model: asdict(caps) for model, caps in credential.custom_models.items()}
                ),
            ),
        )


def get_run_credential(run_id: str, db_path: str | None = None) -> ByokCredential | None:
    from co_scientist.platform.db import connect

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT provider, model, supervisor_model, encrypted_key, "
            "supervisor_provider, encrypted_supervisor_key, custom_models_json "
            "FROM run_credentials "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    return ByokCredential(
        provider=row["provider"],
        api_key=decrypt_api_key(row["encrypted_key"]),
        model=row["model"],
        supervisor_model=row["supervisor_model"],
        supervisor_provider=row["supervisor_provider"],
        supervisor_api_key=decrypt_api_key(row["encrypted_supervisor_key"])
        if row["encrypted_supervisor_key"]
        else None,
        custom_models={
            model: CustomModelCapabilities(**caps)
            for model, caps in json.loads(row["custom_models_json"] or "{}").items()
        },
    )


async def _acompletion(**kwargs: Any) -> object:
    """Forwarded parameters retain Any because LiteLLM's heterogeneous
    completion signature cannot be represented by object.
    """
    from co_scientist.platform.llm import llm_request

    return await llm_request.acompletion(call_role="credential_probe", **kwargs)


@budgeted("credential_probe")
async def validate_byok_credential(credential: ByokCredential) -> None:
    """Credential validation is provider I/O and must complete outside
    SQLite write transactions.
    """
    from co_scientist.platform.llm.request.backend import is_authentication_error

    try:
        with scoped_byok(credential):
            for model, api_key in credential.keys_by_model().items():
                if model in credential.custom_models:
                    continue
                await _acompletion(
                    model=model,
                    api_key=api_key,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                    temperature=0,
                    timeout=_VALIDATION_TIMEOUT_SECONDS,
                    drop_params=True,
                )
    except ByokValidationError:
        raise
    except Exception as exc:
        if is_authentication_error(exc):
            raise ByokValidationError(
                "the provider rejected the API key (invalid or expired)"
            ) from exc
        # Provider error details must never echo the submitted credential.
        detail = redact_credential(str(exc), credential)
        raise ByokValidationError(
            f"the API key could not be verified with the provider: {detail}"
        ) from exc
