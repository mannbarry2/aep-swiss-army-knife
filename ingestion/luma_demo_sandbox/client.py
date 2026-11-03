"""Thin requests wrapper for platform.adobe.io: token, headers, 401 refresh,
429/5xx backoff that names the endpoint, and a per-endpoint health tally."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Callable

import requests

BASE_URL = "https://platform.adobe.io"
IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
SCOPES = "openid,AdobeID,read_organizations,additional_info.projectedProductContext,session"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5

Log = Callable[[str], None]


class ApiError(RuntimeError):
    def __init__(self, resp: requests.Response):
        self.status = resp.status_code
        self.body = resp.text[:1000]
        super().__init__(f"{resp.request.method} {resp.url} -> HTTP {resp.status_code}: {self.body}")


def endpoint_name(url: str) -> str:
    """'https://platform.adobe.io/data/foundation/catalog/batches/x' -> '/data/foundation/catalog'."""
    path = url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]
    host = url.split("://", 1)[-1].split("/", 1)[0]
    if "platform.adobe.io" not in host:
        return host
    return "/" + "/".join(path.split("/")[:3])


def backoff_seconds(resp: requests.Response, attempt: int) -> float:
    retry_after = resp.headers.get("Retry-After", "")
    if retry_after.isdigit():
        return min(float(retry_after), 60.0)
    return min(2.0 ** attempt, 30.0)


class Health:
    """Calls, retries and the slowest answer per endpoint (house rule 18)."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = defaultdict(int)
        self.retries: dict[str, list[str]] = defaultdict(list)
        self.slowest: dict[str, float] = defaultdict(float)

    def report(self) -> list[str]:
        lines = []
        for name in sorted(self.calls):
            reasons = self.retries[name]
            why = f", retried {len(reasons)}x ({', '.join(sorted(set(reasons)))})" if reasons else ""
            lines.append(f"  {name:<40} {self.calls[name]:>4} call(s), slowest {self.slowest[name]:.1f}s{why}")
        return lines


def send(session: requests.Session, method: str, url: str, *, headers: dict[str, str],
         refresh: Callable[[], dict[str, str]] | None = None, health: Health | None = None,
         log: Log = print, sleep: Callable[[float], None] = time.sleep,
         **kwargs: Any) -> requests.Response:
    """One HTTP call with retries. `refresh()` returns fresh auth headers after a 401."""
    health = health or Health()
    name = endpoint_name(url)
    refreshed = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            resp = session.request(method, url, headers=headers, timeout=120, **kwargs)
        except requests.Timeout:
            if attempt == MAX_ATTEMPTS:
                raise
            health.retries[name].append("no answer in 120s")
            log(f"  retrying {method} {name}: no answer in 120s (attempt {attempt}/{MAX_ATTEMPTS})")
            continue
        health.calls[name] += 1
        health.slowest[name] = max(health.slowest[name], time.monotonic() - started)
        if resp.status_code == 401 and refresh and not refreshed:
            headers = {**headers, **refresh()}
            refreshed = True
            continue
        if resp.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
            wait = backoff_seconds(resp, attempt)
            health.retries[name].append(f"HTTP {resp.status_code}")
            log(f"  retrying {method} {name}: HTTP {resp.status_code} "
                f"(attempt {attempt}/{MAX_ATTEMPTS}, waiting {wait:.0f}s)")
            sleep(wait)
            continue
        return resp
    return resp


class Platform:
    """Authenticated client bound to one org + sandbox. `conf` comes from credential_loader."""

    def __init__(self, conf: dict[str, str], sandbox: str,
                 session: requests.Session | None = None, log: Log = print):
        self.conf = conf
        self.sandbox = sandbox
        self.session = session or requests.Session()
        self.log = log
        self.health = Health()
        self._token = ""
        self._expires_at = 0.0

    @property
    def api_key(self) -> str:
        return self.conf.get("api_key") or self.conf["client_id"]

    def token(self, force: bool = False) -> str:
        """Cached in memory only, never printed; refreshed a minute before expiry."""
        if force or not self._token or time.time() > self._expires_at - 60:
            resp = self.session.post(self.conf.get("oauth_url") or IMS_TOKEN_URL, data={
                "grant_type": "client_credentials",
                "client_id": self.conf["client_id"],
                "client_secret": self.conf["client_secret"],
                "scope": self.conf.get("scopes") or SCOPES,
            }, timeout=30)
            if resp.status_code != 200:
                raise ApiError(resp)
            body = resp.json()
            self._token = body["access_token"]
            self._expires_at = time.time() + float(body.get("expires_in", 86400))
        return self._token

    def auth_headers(self, force: bool = False) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token(force=force)}",
            "x-api-key": self.api_key,
            "x-gw-ims-org-id": self.conf["org_id"],
            "x-sandbox-name": self.sandbox,
        }

    def call(self, method: str, path: str, *, accept: str = "application/json",
             content_type: str | None = "application/json", ok: tuple[int, ...] = (200, 201),
             **kwargs: Any) -> requests.Response:
        headers = {**self.auth_headers(), "Accept": accept}
        if content_type:
            headers["Content-Type"] = content_type
        resp = send(self.session, method, BASE_URL + path, headers=headers,
                    refresh=lambda: self.auth_headers(force=True), health=self.health,
                    log=self.log, **kwargs)
        if resp.status_code not in ok:
            raise ApiError(resp)
        return resp

    def get_json(self, path: str, **kwargs: Any) -> Any:
        return self.call("GET", path, content_type=None, **kwargs).json()
