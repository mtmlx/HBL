from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import html
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
from typing import Any
from urllib.parse import parse_qs, urlencode

import boto3
import httpx
import jwt
from jwt import PyJWKClient

from mtm_hbl.clickup_connector.client import ClickUpClient
from mtm_hbl.clickup_hbl_generator import (
    _data_from_clickup_task,
    _enforce_clickup_hbl_number,
    generate_hbl_from_clickup,
    parse_clickup_task_id,
)
from mtm_hbl.config import AppConfig, Settings
from mtm_hbl.verification.aws_repository import AwsVerificationConfig, void_verification_records


secretsmanager = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")

SESSION_COOKIE = "mtm_hbl_admin_session"
CSRF_COOKIE = "mtm_hbl_admin_csrf"
STATE_MAX_AGE_SECONDS = 600
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60


@dataclass(frozen=True)
class AdminUser:
    email: str
    name: str = ""


@dataclass(frozen=True)
class ActiveVerificationRecord:
    verification_id: str
    package_id: str
    status: str
    issued_at: str = ""


def admin_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    try:
        return asyncio.run(_dispatch(event))
    except _RedirectException as exc:
        return _redirect(exc.location)
    except Exception as exc:
        return _html_response(
            _page("HBL Admin Error", f"<h1>Request failed</h1><p>{_e(str(exc))}</p>"),
            status_code=500,
        )


async def _dispatch(event: dict[str, Any]) -> dict[str, Any]:
    method = str(event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod") or "GET").upper()
    path = _path(event)

    if path == "/admin/login" and method == "GET":
        return _login_redirect()
    if path == "/admin/callback" and method == "GET":
        return await _auth_callback(event)
    if path == "/admin/logout" and method == "POST":
        return _logout_response()

    user = _require_user(event)
    if path in {"/", "/admin"} and method == "GET":
        return _admin_form_response(user)
    if path == "/admin/reissue/preview" and method == "POST":
        _require_csrf(event)
        return await _preview_reissue(event, user)
    if path == "/admin/reissue/confirm" and method == "POST":
        _require_csrf(event)
        return await _confirm_reissue(event, user)

    return _html_response(_page("Not Found", "<h1>Not found</h1>"), status_code=404)


def _login_redirect() -> dict[str, Any]:
    tenant_id = _required_env("ENTRA_TENANT_ID")
    client_id = _required_env("ENTRA_CLIENT_ID")
    redirect_uri = f"{_admin_base_url().rstrip('/')}/admin/callback"
    nonce = token_urlsafe(24)
    state = _sign_token({"nonce": nonce, "exp": _unix_now() + STATE_MAX_AGE_SECONDS}, _session_secret())
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?{urlencode(params)}"
    return _redirect(url)


async def _auth_callback(event: dict[str, Any]) -> dict[str, Any]:
    query = event.get("queryStringParameters") or {}
    code = str(query.get("code") or "")
    state = str(query.get("state") or "")
    if not code or not state:
        raise ValueError("Microsoft Entra callback did not include code and state.")

    state_payload = _verify_token(state, _session_secret())
    nonce = str(state_payload.get("nonce") or "")
    token_payload = await _exchange_code_for_token(code)
    claims = _validate_id_token(str(token_payload.get("id_token") or ""), nonce)
    user = _user_from_claims(claims)
    _require_allowed_user(user)

    session = _sign_token(
        {
            "email": user.email,
            "name": user.name,
            "exp": _unix_now() + SESSION_MAX_AGE_SECONDS,
        },
        _session_secret(),
    )
    csrf = token_urlsafe(32)
    return {
        "statusCode": 302,
        "headers": {"Location": "/admin"},
        "cookies": [_session_cookie(session), _csrf_cookie(csrf)],
        "body": "",
    }


async def _preview_reissue(event: dict[str, Any], user: AdminUser) -> dict[str, Any]:
    form = _form_data(event)
    task_ref = str(form.get("task_ref", "")).strip()
    if not task_ref:
        raise ValueError("ClickUp task link or task ID is required.")

    preview = await _load_reissue_preview(task_ref)
    active_rows = "".join(
        f"<tr><td>{_e(record.package_id)}</td><td>{_e(record.verification_id)}</td><td>{_e(record.issued_at)}</td></tr>"
        for record in preview["active_records"]
    )
    active_table = (
        "<table><thead><tr><th>Package</th><th>Verification ID</th><th>Issued at</th></tr></thead>"
        f"<tbody>{active_rows}</tbody></table>"
        if active_rows
        else "<p class='warning'>No active verification records were found for this HBL. Confirm only if this is expected.</p>"
    )
    body = f"""
    <h1>Confirm Original Reissue</h1>
    <div class="panel">
      <p><strong>Requested by:</strong> {_e(user.email)}</p>
      <p><strong>Task:</strong> {_e(preview["task_id"])} - {_e(preview["task_name"])}</p>
      <p><strong>HBL:</strong> {_e(preview["hbl_number"])}</p>
      <p><strong>Current active records:</strong></p>
      {active_table}
    </div>
    <form method="post" action="/admin/reissue/confirm" class="danger-form">
      <input type="hidden" name="csrf_token" value="{_e(_csrf_from_cookie(event))}">
      <input type="hidden" name="task_ref" value="{_e(task_ref)}">
      <input type="hidden" name="expected_hbl_number" value="{_e(preview["hbl_number"])}">
      <label>Confirmation text</label>
      <input name="confirmation" autocomplete="off" placeholder="Type REISSUE {_e(preview["hbl_number"])}" required>
      <p class="warning">This will issue a new ORIGINAL/COPY package, replace the ClickUp HBL Original field, and void the currently active verification records after the replacement succeeds.</p>
      <button type="submit">Void Current and Issue Replacement</button>
      <a class="secondary" href="/admin">Cancel</a>
    </form>
    """
    return _html_response(_page("Confirm HBL Reissue", body, user=user))


async def _confirm_reissue(event: dict[str, Any], user: AdminUser) -> dict[str, Any]:
    form = _form_data(event)
    task_ref = str(form.get("task_ref", "")).strip()
    expected_hbl_number = str(form.get("expected_hbl_number", "")).strip()
    confirmation = re.sub(r"\s+", " ", str(form.get("confirmation", "")).strip()).casefold()
    if confirmation != f"reissue {expected_hbl_number}".casefold():
        raise ValueError(f"Confirmation must be exactly: REISSUE {expected_hbl_number}")

    preview = await _load_reissue_preview(task_ref)
    if preview["hbl_number"] != expected_hbl_number:
        raise ValueError("HBL number changed between preview and confirmation. Start again.")

    old_records: list[ActiveVerificationRecord] = preview["active_records"]
    result = await _issue_replacement(task_ref, user)
    old_ids = [record.verification_id for record in old_records]
    if old_ids:
        void_verification_records(
            _verification_config(),
            old_ids,
            superseded_by=result.package_id,
            reason=f"Manager-controlled reissue by {user.email}. Replacement original package issued.",
        )
    await _post_reissue_comment(preview["task_id"], user, old_records, result)

    first_url = next(iter(result.verification_urls.values()), "")
    body = f"""
    <h1>Reissue Complete</h1>
    <div class="success">
      <p><strong>HBL:</strong> {_e(result.hbl_number)}</p>
      <p><strong>New package:</strong> {_e(result.package_id)}</p>
      <p><strong>Voided records:</strong> {len(old_ids)}</p>
      <p><strong>ClickUp field:</strong> HBL Original updated.</p>
      <p><a href="{_e(first_url)}" target="_blank" rel="noopener">Open verification</a></p>
    </div>
    <p><a class="secondary" href="/admin">Issue another reissue</a></p>
    """
    return _html_response(_page("HBL Reissue Complete", body, user=user))


async def _load_reissue_preview(task_ref: str) -> dict[str, Any]:
    settings = _lambda_settings()
    client = ClickUpClient(settings, _clickup_access_token())
    app_config = AppConfig(settings.config_dir)
    task_id = parse_clickup_task_id(task_ref)
    task = await client.get_task(task_id)
    clickup_values = client.extract_configured_fields(task, app_config)
    data = _data_from_clickup_task(task, clickup_values, app_config)
    _enforce_clickup_hbl_number(data, clickup_values)
    hbl_number = data.shipment.mtm_hbl_no or task.name
    if not hbl_number:
        raise ValueError("Could not determine HBL number from the ClickUp task.")
    active_records = _active_verification_records(hbl_number)
    return {
        "task_id": task_id,
        "task_name": task.name,
        "hbl_number": hbl_number,
        "active_records": active_records,
    }


async def _issue_replacement(task_ref: str, user: AdminUser):
    settings = _lambda_settings()
    client = ClickUpClient(settings, _clickup_access_token())
    return await generate_hbl_from_clickup(
        task_ref=task_ref,
        client=client,
        settings=settings,
        app_config=AppConfig(settings.config_dir),
        mode="issue",
        output_dir=Path("/tmp") / "hbl_admin_reissue" / parse_clickup_task_id(task_ref),
        logo_path=Path(os.getenv("HBL_LOGO_PATH", "assets/mtm_logix_logo.png")),
        attach_to_clickup=True,
        post_comment=True,
        verification_base_url=settings.hbl_verification_base_url,
        bucket=settings.hbl_verification_bucket,
        table=settings.hbl_verification_table,
        region=settings.aws_region,
        issued_by=os.getenv("HBL_ISSUED_BY", "Andrea Piedad Velasquez Castellon"),
        prevent_original_overwrite=False,
    )


async def _post_reissue_comment(task_id: str, user: AdminUser, old_records: list[ActiveVerificationRecord], result) -> None:
    settings = _lambda_settings()
    client = ClickUpClient(settings, _clickup_access_token())
    task = await client.get_task(task_id)
    assignee_id = next((assignee.id for assignee in task.assignees if assignee.id), "")
    old_packages = sorted({record.package_id for record in old_records})
    first_url = next(iter(result.verification_urls.values()), "")
    comment = (
        "Original HBL reissued through the manager portal.\n\n"
        f"Requested by: {user.email}\n"
        f"Voided prior package(s): {', '.join(old_packages) if old_packages else 'None found'}\n"
        f"Replacement package: {result.package_id}\n"
        f"New verification: {first_url}\n\n"
        "The HBL Original field has been replaced with the corrected original package."
    )
    await client.post_comment(task_id, comment, assignee_id=assignee_id)


def _active_verification_records(hbl_number: str) -> list[ActiveVerificationRecord]:
    table = dynamodb.Table(_required_env("HBL_VERIFICATION_TABLE"))
    response = table.scan(
        FilterExpression="hbl_number = :h AND #status = :s",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":h": hbl_number, ":s": "ISSUED"},
        ProjectionExpression="verification_id, package_id, #status, issued_at",
    )
    items = list(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(
            FilterExpression="hbl_number = :h AND #status = :s",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":h": hbl_number, ":s": "ISSUED"},
            ProjectionExpression="verification_id, package_id, #status, issued_at",
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))
    return [
        ActiveVerificationRecord(
            verification_id=str(item.get("verification_id", "")),
            package_id=str(item.get("package_id", "")),
            status=str(item.get("status", "")),
            issued_at=str(item.get("issued_at", "")),
        )
        for item in items
        if item.get("verification_id")
    ]


def _require_user(event: dict[str, Any]) -> AdminUser:
    cookies = _cookies(event)
    session = cookies.get(SESSION_COOKIE, "")
    if not session:
        raise _RedirectException("/admin/login")
    try:
        payload = _verify_token(session, _session_secret())
    except ValueError as exc:
        raise _RedirectException("/admin/login") from exc
    user = AdminUser(email=str(payload.get("email") or ""), name=str(payload.get("name") or ""))
    _require_allowed_user(user)
    return user


def _require_allowed_user(user: AdminUser) -> None:
    allowed = _allowed_emails()
    if user.email.casefold() not in allowed:
        raise ValueError(f"{user.email} is not authorized for HBL reissue.")


def _allowed_emails() -> set[str]:
    raw = os.getenv(
        "HBL_ADMIN_ALLOWED_EMAILS",
        "andrea@mtmlogix.com,mario@mtmlogix.com,silvia@mtmlogix.com",
    )
    return {email.strip().casefold() for email in raw.split(",") if email.strip()}


async def _exchange_code_for_token(code: str) -> dict[str, Any]:
    tenant_id = _required_env("ENTRA_TENANT_ID")
    client_id = _required_env("ENTRA_CLIENT_ID")
    redirect_uri = f"{_admin_base_url().rstrip('/')}/admin/callback"
    data = {
        "client_id": client_id,
        "client_secret": _entra_client_secret(),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data=data,
        )
    response.raise_for_status()
    return response.json()


def _validate_id_token(id_token: str, nonce: str) -> dict[str, Any]:
    tenant_id = _required_env("ENTRA_TENANT_ID")
    client_id = _required_env("ENTRA_CLIENT_ID")
    jwks_client = PyJWKClient(f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys")
    signing_key = jwks_client.get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=client_id,
        issuer=f"https://login.microsoftonline.com/{tenant_id}/v2.0",
    )
    if nonce and claims.get("nonce") != nonce:
        raise ValueError("Microsoft Entra nonce validation failed.")
    return claims


def _user_from_claims(claims: dict[str, Any]) -> AdminUser:
    email = str(
        claims.get("preferred_username")
        or claims.get("email")
        or claims.get("upn")
        or ""
    ).strip()
    if not email:
        raise ValueError("Microsoft Entra token did not include a usable email.")
    return AdminUser(email=email, name=str(claims.get("name") or email))


def _entra_client_secret() -> str:
    direct = os.getenv("ENTRA_CLIENT_SECRET", "").strip()
    if direct:
        return direct
    return _secret_string(_required_env("ENTRA_CLIENT_SECRET_NAME")).strip()


def _clickup_access_token() -> str:
    direct = os.getenv("CLICKUP_ACCESS_TOKEN", "").strip()
    if direct:
        return direct
    secret = _secret_string(_required_env("CLICKUP_ACCESS_TOKEN_SECRET_NAME"))
    try:
        payload = json.loads(secret)
    except json.JSONDecodeError:
        return secret.strip()
    return str(payload.get("access_token", "")).strip()


def _session_secret() -> str:
    direct = os.getenv("HBL_ADMIN_SESSION_SECRET", "").strip()
    if direct:
        return direct
    return _secret_string(_required_env("HBL_ADMIN_SESSION_SECRET_NAME")).strip()


def _secret_string(name: str) -> str:
    response = secretsmanager.get_secret_value(SecretId=name)
    return response.get("SecretString", "")


def _lambda_settings() -> Settings:
    return Settings(
        runs_dir=Path(os.getenv("RUNS_DIR", "/tmp/runs")),
        config_dir=Path(os.getenv("CONFIG_DIR", "config")),
        token_store_path=Path("/tmp/clickup_token.json"),
    )


def _verification_config() -> AwsVerificationConfig:
    settings = _lambda_settings()
    return AwsVerificationConfig(
        bucket_name=_required_env("HBL_VERIFICATION_BUCKET"),
        table_name=_required_env("HBL_VERIFICATION_TABLE"),
        region_name=settings.aws_region,
        verification_base_url=_required_env("HBL_VERIFICATION_BASE_URL"),
    )


def _admin_base_url() -> str:
    return os.getenv("HBL_ADMIN_BASE_URL", "").strip() or "https://hbl.mtmlogix.com"


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured.")
    return value


def _form_data(event: dict[str, Any]) -> dict[str, str]:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _require_csrf(event: dict[str, Any]) -> None:
    token = _form_data(event).get("csrf_token", "")
    cookie_token = _csrf_from_cookie(event)
    if not token or not cookie_token or not hmac.compare_digest(token, cookie_token):
        raise ValueError("CSRF validation failed. Reload the page and try again.")


def _csrf_from_cookie(event: dict[str, Any]) -> str:
    return _cookies(event).get(CSRF_COOKIE, "")


def _cookies(event: dict[str, Any]) -> dict[str, str]:
    raw_cookies: list[str] = []
    raw_cookies.extend(str(value) for value in event.get("cookies") or [])
    headers = event.get("headers") or {}
    cookie_header = headers.get("cookie") or headers.get("Cookie")
    if cookie_header:
        raw_cookies.append(str(cookie_header))
    cookies: dict[str, str] = {}
    for raw in raw_cookies:
        for part in raw.split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


def _sign_token(payload: dict[str, Any], secret: str) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), sha256).digest()
    return f"{body}.{_b64(signature)}"


def _verify_token(token: str, secret: str) -> dict[str, Any]:
    try:
        body, supplied_signature = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("Invalid session token.") from exc
    expected_signature = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), sha256).digest())
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ValueError("Invalid session signature.")
    payload = json.loads(_b64decode(body).decode("utf-8"))
    if int(payload.get("exp") or 0) < _unix_now():
        raise ValueError("Session expired.")
    return payload


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _unix_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _path(event: dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "/")


def _redirect(location: str) -> dict[str, Any]:
    return {"statusCode": 302, "headers": {"Location": location}, "body": ""}


def _logout_response() -> dict[str, Any]:
    return {
        "statusCode": 302,
        "headers": {"Location": "/admin/login"},
        "cookies": [
            f"{SESSION_COOKIE}=; Max-Age=0; HttpOnly; Secure; SameSite=Lax; Path=/",
            f"{CSRF_COOKIE}=; Max-Age=0; Secure; SameSite=Lax; Path=/",
        ],
        "body": "",
    }


def _session_cookie(value: str) -> str:
    return f"{SESSION_COOKIE}={value}; Max-Age={SESSION_MAX_AGE_SECONDS}; HttpOnly; Secure; SameSite=Lax; Path=/"


def _csrf_cookie(value: str) -> str:
    return f"{CSRF_COOKIE}={value}; Max-Age={SESSION_MAX_AGE_SECONDS}; Secure; SameSite=Lax; Path=/"


def _html_response(body: str, *, status_code: int = 200) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store",
            "x-frame-options": "DENY",
            "content-security-policy": "default-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        },
        "body": body,
    }


def _admin_form_response(user: AdminUser) -> dict[str, Any]:
    csrf = token_urlsafe(32)
    body = f"""
    <h1>Void and Reissue HBL Original</h1>
    <div class="panel">
      <p>This portal is restricted to authorized MTM users. It issues a replacement ORIGINAL package and voids the prior verification records only after the new package is successfully uploaded to ClickUp.</p>
    </div>
    <form method="post" action="/admin/reissue/preview">
      <input type="hidden" name="csrf_token" value="{_e(csrf)}">
      <label>ClickUp task link or task ID</label>
      <input name="task_ref" placeholder="https://app.clickup.com/t/8451352/86e277kqk" required autofocus>
      <button type="submit">Review Current Original</button>
    </form>
    """
    page = _page("HBL Admin", body, user=user)
    response = _html_response(page)
    response["cookies"] = [_csrf_cookie(csrf)]
    return response


def _page(title: str, body: str, *, user: AdminUser | None = None) -> str:
    user_html = ""
    if user:
        user_html = f"""
        <div class="userbar">
          <span>{_e(user.name or user.email)}</span>
          <form method="post" action="/admin/logout"><button class="link-button" type="submit">Log out</button></form>
        </div>
        """
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_e(title)}</title>
  <style>
    :root {{ color-scheme: light; --ink:#111827; --muted:#4b5563; --line:#d1d5db; --brand:#0f766e; --danger:#b91c1c; --bg:#f8fafc; }}
    body {{ margin:0; font-family: Arial, Helvetica, sans-serif; background:var(--bg); color:var(--ink); }}
    main {{ max-width: 980px; margin: 38px auto; padding: 0 22px; }}
    header {{ background:#fff; border-bottom:1px solid var(--line); padding:18px 24px; display:flex; justify-content:space-between; align-items:center; }}
    .brand {{ font-weight:700; letter-spacing:.02em; }}
    .userbar {{ display:flex; gap:14px; align-items:center; color:var(--muted); font-size:14px; }}
    h1 {{ font-size:28px; margin:0 0 20px; }}
    .panel, form, .success {{ background:#fff; border:1px solid var(--line); padding:20px; margin:18px 0; }}
    label {{ display:block; font-weight:700; margin-bottom:8px; }}
    input {{ width:100%; box-sizing:border-box; font-size:17px; padding:12px; border:1px solid var(--line); }}
    button, .secondary {{ display:inline-block; margin-top:16px; padding:11px 16px; border:0; background:var(--brand); color:#fff; font-weight:700; text-decoration:none; cursor:pointer; }}
    .danger-form button {{ background:var(--danger); }}
    .secondary {{ background:#374151; margin-left:8px; }}
    .link-button {{ background:transparent; color:var(--brand); padding:0; margin:0; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; margin-top:8px; }}
    th, td {{ border:1px solid var(--line); padding:9px; text-align:left; font-size:14px; }}
    th {{ background:#f3f4f6; }}
    .warning {{ color:var(--danger); font-weight:700; }}
    .success {{ border-color:#86efac; background:#f0fdf4; }}
  </style>
</head>
<body>
  <header><div class="brand">MTM Logix HBL Control</div>{user_html}</header>
  <main>{body}</main>
</body>
</html>"""


def _e(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


class _RedirectException(Exception):
    def __init__(self, location: str) -> None:
        super().__init__(location)
        self.location = location
