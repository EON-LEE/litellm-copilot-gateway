"""The one GitHub Copilot login every gateway component uses.

The credential is the GitHub OAuth token in ``<token dir>/access-token``,
written by ``ccgw login``. The token dir comes from GITHUB_COPILOT_TOKEN_DIR
(process environment, then ``<data>/.env``) and defaults to LiteLLM's location;
to use another GitHub account, point it at another directory or log in again.
The catalog refresh, LiteLLM's ``github_copilot`` provider and copilot-api all
read this token. Git and GitHub CLI credentials (GH_TOKEN, GITHUB_TOKEN, the
gh keyring) are never read. Token values are never printed, logged, or put on
a command line.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from .settings import write_private

# GitHub Copilot OAuth app and scope used by LiteLLM's own authenticator.
CLIENT_ID = "Iv1.b507a08c87ecfe98"
SCOPE = "read:user"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
TOKEN_FILE = "access-token"
# LiteLLM caches the short-lived Copilot key here; it belongs to the token above.
API_KEY_FILE = "api-key.json"
HEADERS = {"accept": "application/json", "content-type": "application/json",
           "user-agent": "copilot-gateway", "editor-version": "vscode/1.100.0"}


class AuthError(RuntimeError):
    pass


def read_token(token_dir) -> str:
    try:
        token = (Path(token_dir) / TOKEN_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    if not token:
        raise AuthError(f"no GitHub Copilot login at {token_dir}; run `ccgw login`")
    return token


def fingerprint(token_dir) -> str:
    """Identifies the login and its location without revealing the token."""
    location = os.path.normcase(os.path.abspath(str(token_dir)))
    return hashlib.sha256(f"{location}\0{read_token(token_dir)}".encode("utf-8")).hexdigest()[:16]


def secure(token_dir) -> None:
    """Make the token files owner-only on POSIX; LiteLLM creates them with the default umask."""
    if os.name != "posix":
        return
    for name in (TOKEN_FILE, API_KEY_FILE):
        path = Path(token_dir) / name
        try:
            if path.stat().st_mode & 0o077:
                path.chmod(0o600)
        except FileNotFoundError:
            continue


def _request(url, token=None, body=None):
    headers = dict(HEADERS)
    if token:
        headers["authorization"] = f"token {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise AuthError(f"{url} returned HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise AuthError(f"cannot reach {url}: {getattr(error, 'reason', error)}") from None
    except (UnicodeError, json.JSONDecodeError):
        raise AuthError(f"{url} returned invalid JSON") from None
    if not isinstance(value, dict):
        raise AuthError(f"{url} returned an unexpected response")
    return value


def github_login(token) -> str:
    """The GitHub account that owns the token (diagnostics; not an allow-list)."""
    login = _request(USER_URL, token).get("login")
    if not isinstance(login, str) or not login:
        raise AuthError("GitHub returned no login for this token")
    return login


def copilot_key(token) -> dict:
    """Mint a short-lived Copilot key; fails when the account has no Copilot access."""
    try:
        value = _request(COPILOT_TOKEN_URL, token)
    except AuthError as error:
        raise AuthError(f"this GitHub account cannot use Copilot ({error})") from None
    if not isinstance(value.get("token"), str) or not value["token"]:
        raise AuthError("Copilot token endpoint returned no token")
    return value


def device_login(prompt=print, sleep=time.sleep, clock=time.monotonic) -> str:
    """GitHub device flow for the Copilot OAuth app; polls until approval or the code's own expiry."""
    code = _request(DEVICE_CODE_URL, body={"client_id": CLIENT_ID, "scope": SCOPE})
    for field in ("device_code", "user_code", "verification_uri"):
        if not isinstance(code.get(field), str) or not code[field]:
            raise AuthError(f"GitHub device-code response has no {field}")
    interval = max(int(code.get("interval") or 5), 1)
    deadline = clock() + int(code.get("expires_in") or 900)
    prompt(f"Open {code['verification_uri']} and enter code {code['user_code']}, "
           "signed in as the GitHub account whose Copilot plan the gateway should use.")
    while clock() < deadline:
        sleep(interval)
        reply = _request(ACCESS_TOKEN_URL, body={"client_id": CLIENT_ID, "device_code": code["device_code"],
                                                 "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        token = reply.get("access_token")
        if isinstance(token, str) and token:
            return token
        error = reply.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise AuthError(f"GitHub device login failed: {error or 'no access token returned'}")
    raise AuthError("the code expired before it was approved; run `ccgw login` again")


def login(token_dir, prompt=print, flow=device_login) -> str:
    """Store a new login, only after it is proven to reach Copilot. Returns the GitHub login."""
    token = flow(prompt=prompt)
    key = copilot_key(token)
    name = github_login(token)
    token_dir = Path(token_dir)
    if not token_dir.exists():
        token_dir.mkdir(parents=True)
        if os.name == "posix":
            token_dir.chmod(0o700)
    write_private(token_dir / TOKEN_FILE, token)
    # Same format LiteLLM writes; replacing it drops the previous login's cached key.
    write_private(token_dir / API_KEY_FILE, json.dumps(key))
    return name
