"""OAuth 2.1 authorization server backed by SQLite.

The MCP SDK implements the protocol surface (metadata discovery, dynamic client
registration, PKCE verification, the token endpoint). This module supplies the
storage and the human approval step: every authorization must be confirmed on a
consent screen with the operator passphrase before a code is issued.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenVerifier,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import audit
from .config import Config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    data      TEXT NOT NULL,
    created   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
    code      TEXT PRIMARY KEY,
    data      TEXT NOT NULL,
    expires   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token     TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,          -- 'access' | 'refresh'
    data      TEXT NOT NULL,
    expires   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
    request_id TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    expires    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tokens_kind ON tokens(kind);
"""


class Store:
    """Thread-safe SQLite wrapper. Rows carry an expiry and are swept lazily."""

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        path.chmod(0o600)

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            self._conn.commit()
            return rows

    def put(self, table: str, key_col: str, key: str, data: dict, expires: float | None = None) -> None:
        if expires is None:
            self._exec(
                f"INSERT OR REPLACE INTO {table} ({key_col}, data, created) VALUES (?,?,?)",
                (key, json.dumps(data), int(time.time())),
            )
        else:
            extra = ", kind" if table == "tokens" else ""
            vals = ",?" if table == "tokens" else ""
            args: tuple[Any, ...] = (key, json.dumps(data), expires)
            if table == "tokens":
                args = (key, json.dumps(data), expires, data.get("_kind", "access"))
            self._exec(
                f"INSERT OR REPLACE INTO {table} ({key_col}, data, expires{extra}) "
                f"VALUES (?,?,?{vals})",
                args,
            )

    def get(self, table: str, key_col: str, key: str, with_expiry: bool = True) -> dict | None:
        rows = self._exec(f"SELECT * FROM {table} WHERE {key_col} = ?", (key,))
        if not rows:
            return None
        row = rows[0]
        if with_expiry and row["expires"] < time.time():
            self.delete(table, key_col, key)
            return None
        return json.loads(row["data"])

    def delete(self, table: str, key_col: str, key: str) -> None:
        self._exec(f"DELETE FROM {table} WHERE {key_col} = ?", (key,))

    def sweep(self) -> None:
        now = time.time()
        for table in ("auth_codes", "tokens", "pending"):
            self._exec(f"DELETE FROM {table} WHERE expires < ?", (now,))

    def client_count(self) -> int:
        rows = self._exec("SELECT COUNT(*) AS n FROM clients", ())
        return rows[0]["n"] if rows else 0

    def active_tokens(self) -> int:
        rows = self._exec(
            "SELECT COUNT(*) AS n FROM tokens WHERE kind='access' AND expires > ?",
            (time.time(),),
        )
        return rows[0]["n"] if rows else 0

    def revoke_all(self) -> int:
        rows = self._exec("SELECT COUNT(*) AS n FROM tokens", ())
        n = rows[0]["n"] if rows else 0
        self._exec("DELETE FROM tokens", ())
        self._exec("DELETE FROM auth_codes", ())
        return n


class StaticTokenVerifier(TokenVerifier):
    """Single long-lived bearer token, for LAN clients that cannot do OAuth.

    Appropriate on a trusted network: the token is the whole credential, so it
    is exactly as sensitive as an SSH private key. Not appropriate for anything
    reachable from the internet -- use OAuth mode there.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    async def verify_token(self, token: str) -> AccessToken | None:
        if not self.cfg.static_token:
            return None
        if not hmac.compare_digest(token, self.cfg.static_token):
            audit.record("auth.token_rejected", presented_prefix=token[:8])
            return None
        return AccessToken(
            token=token,
            client_id="lan-static-token",
            scopes=["bridge:admin"],
            expires_at=None,
            resource=self.cfg.mcp_endpoint,
            subject="lan-static-token",
        )


class BridgeAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store

    # ---------- client registration ----------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get("clients", "client_id", client_id, with_expiry=False)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.put(
            "clients", "client_id", client_info.client_id,
            json.loads(client_info.model_dump_json(exclude_none=True)),
        )
        audit.record(
            "oauth.client_registered",
            client_id=client_info.client_id,
            client_name=client_info.client_name,
            redirect_uris=[str(u) for u in client_info.redirect_uris],
        )

    # ---------- authorization ----------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to the consent screen."""
        request_id = secrets.token_urlsafe(24)
        self.store.put(
            "pending", "request_id", request_id,
            {
                "client_id": client.client_id,
                "client_name": client.client_name or client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or [],
                "resource": params.resource,
            },
            expires=time.time() + 600,
        )
        audit.record("oauth.authorize_requested",
                     client_id=client.client_id, request_id=request_id)
        return f"{self.cfg.public_url}/consent?request_id={request_id}"

    def approve(self, request_id: str, passphrase: str) -> tuple[bool, str]:
        """Validate the consent form. Returns (ok, redirect_url_or_error)."""
        pending = self.store.get("pending", "request_id", request_id)
        if pending is None:
            return False, "This approval request expired or was already used."

        if not hmac.compare_digest(passphrase, self.cfg.admin_passphrase):
            audit.record("oauth.consent_denied", request_id=request_id,
                         reason="bad passphrase", client_id=pending["client_id"])
            return False, "Incorrect passphrase."

        self.store.delete("pending", "request_id", request_id)
        code = f"code_{secrets.token_urlsafe(32)}"
        expires_at = time.time() + self.cfg.auth_code_ttl
        self.store.put(
            "auth_codes", "code", code,
            {
                "code": code,
                "client_id": pending["client_id"],
                "redirect_uri": pending["redirect_uri"],
                "redirect_uri_provided_explicitly": pending["redirect_uri_provided_explicitly"],
                "expires_at": expires_at,
                "scopes": pending["scopes"],
                "code_challenge": pending["code_challenge"],
                "resource": pending["resource"],
            },
            expires=expires_at,
        )
        audit.record("oauth.consent_granted",
                     request_id=request_id, client_id=pending["client_id"])
        return True, construct_redirect_uri(
            pending["redirect_uri"], code=code, state=pending["state"]
        )

    def pending_request(self, request_id: str) -> dict | None:
        return self.store.get("pending", "request_id", request_id)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self.store.get("auth_codes", "code", authorization_code)
        if data is None or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate(data)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: burn the code before minting anything.
        self.store.delete("auth_codes", "code", authorization_code.code)
        return self._issue(client, authorization_code.scopes, authorization_code.resource)

    # ---------- tokens ----------

    def _issue(
        self, client: OAuthClientInformationFull, scopes: list[str], resource: str | None
    ) -> OAuthToken:
        access = f"mcpb_{secrets.token_urlsafe(32)}"
        refresh = f"mcpr_{secrets.token_urlsafe(32)}"
        now = time.time()
        access_exp = now + self.cfg.access_token_ttl
        refresh_exp = now + self.cfg.refresh_token_ttl

        self.store.put("tokens", "token", access, {
            "_kind": "access", "token": access, "client_id": client.client_id,
            "scopes": scopes, "expires_at": int(access_exp), "resource": resource,
            "subject": client.client_id,
        }, expires=access_exp)
        self.store.put("tokens", "token", refresh, {
            "_kind": "refresh", "token": refresh, "client_id": client.client_id,
            "scopes": scopes, "expires_at": int(refresh_exp),
        }, expires=refresh_exp)

        audit.record("oauth.token_issued", client_id=client.client_id,
                     scopes=scopes, ttl=self.cfg.access_token_ttl)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.cfg.access_token_ttl,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self.store.get("tokens", "token", token)
        if data is None or data.get("_kind") != "access":
            return None
        data.pop("_kind", None)
        return AccessToken.model_validate(data)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self.store.get("tokens", "token", refresh_token)
        if data is None or data.get("_kind") != "refresh":
            return None
        if data["client_id"] != client.client_id:
            return None
        data.pop("_kind", None)
        return RefreshToken.model_validate(data)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the presented refresh token is consumed.
        self.store.delete("tokens", "token", refresh_token.token)
        return self._issue(client, scopes or refresh_token.scopes, None)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.delete("tokens", "token", token.token)
        audit.record("oauth.token_revoked", client_id=token.client_id)
