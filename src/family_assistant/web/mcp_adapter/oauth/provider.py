"""The ``OAuthAuthorizationServerProvider`` behind the MCP adapter's OAuth endpoints.

The SDK's handlers own the protocol (request parsing, PKCE verification,
client authentication); this provider owns the state. Registered clients are
rows in ``oauth_clients``; access tokens are ``api_tokens`` rows of type
``mcp`` with a ``refresh`` row hanging off them, exactly as the iOS app's
credentials are, so revocation, listing and the bearer-token lookup in
``AuthService`` need nothing new. Pending consents and authorization codes
live in process memory: they are single use and expire within minutes (see
docs/design/mcp-adapter.md, "Deliberate simplifications").
"""

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage import api_tokens as api_tokens_storage
from family_assistant.storage import oauth_clients as oauth_clients_storage
from family_assistant.storage.base import api_tokens_table
from family_assistant.storage.database import Database, DatabaseTransaction
from family_assistant.web.auth import MCP_CONSENT_PATH, MCP_TOKEN_TYPE

logger = logging.getLogger(__name__)

# The one scope the adapter grants: ask the assistant. There is no finer-grained
# authority to hand out because the only tool runs as the user under the
# operator's configured profile.
ASSISTANT_SCOPE = "assistant"

CLIENT_REGISTRATION_OPTIONS = ClientRegistrationOptions(
    enabled=True,
    valid_scopes=[ASSISTANT_SCOPE],
    default_scopes=[ASSISTANT_SCOPE],
)
REVOCATION_OPTIONS = RevocationOptions(enabled=True)

ACCESS_TOKEN_TTL = timedelta(hours=24)
REFRESH_TOKEN_TTL = timedelta(days=90)
AUTHORIZATION_CODE_TTL_SECONDS = 5 * 60
# A consent request outlives an authorization code: the user may have to sign
# in first, and OIDC round trips are slow.
PENDING_CONSENT_TTL_SECONDS = 10 * 60
# Dynamic registrations a deployment keeps. A household has a handful of
# connectors; the bound exists so an unauthenticated caller cannot grow the
# table without limit.
MAX_REGISTERED_CLIENTS = 200
# Both stores are bounded so an unauthenticated /authorize cannot grow memory
# without limit; evicting the oldest entry only costs that user another click.
MAX_PENDING_ENTRIES = 1000

CONSENT_PATH = MCP_CONSENT_PATH


@dataclass(frozen=True)
class PendingConsent:
    """An /authorize request waiting for the user's decision."""

    request_id: str
    client: OAuthClientInformationFull
    params: AuthorizationParams
    created_at: float


class ConsentedAuthorizationCode(AuthorizationCode):
    """An authorization code bound to the user who approved it."""

    user_identifier: str


class StoredAccessToken(AccessToken):
    """An ``mcp`` row, with what revocation and rotation need to find it."""

    token_id: int
    user_identifier: str


class StoredRefreshToken(RefreshToken):
    """A ``refresh`` row whose parent is an unrevoked ``mcp`` row."""

    token_id: int
    parent_token_id: int
    user_identifier: str


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes even for DateTime(timezone=True).
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _expiry_timestamp(value: datetime | None) -> int | None:
    return int(_as_utc(value).timestamp()) if value is not None else None


class _MintedPair(NamedTuple):
    access: api_tokens_storage.MintedApiToken
    refresh: api_tokens_storage.MintedApiToken


async def _mint_pair() -> _MintedPair:
    """Generate and hash both secrets before any transaction is opened.

    bcrypt is blocking, and on SQLite a transaction holds the engine-wide
    lock for its whole duration.
    """
    access, refresh = await asyncio.gather(
        asyncio.to_thread(api_tokens_storage.mint_api_token),
        asyncio.to_thread(api_tokens_storage.mint_api_token),
    )
    return _MintedPair(access, refresh)


async def _insert_pair(
    txn: DatabaseTransaction,
    client: OAuthClientInformationFull,
    user_identifier: str,
    minted: _MintedPair,
) -> None:
    """Insert the ``mcp`` row and its ``refresh`` row as one unit.

    A failed second write would otherwise commit a live credential whose
    refresh token was never returned.
    """
    name = f"{client.client_name or client.client_id} (MCP connector)"
    now = datetime.now(UTC)
    access_token_id = await api_tokens_storage.add_api_token(
        db_context=txn,
        user_identifier=user_identifier,
        name=name,
        hashed_token=minted.access.hashed_secret,
        prefix=minted.access.prefix,
        created_at=minted.access.created_at,
        expires_at=now + ACCESS_TOKEN_TTL,
        token_type=MCP_TOKEN_TYPE,
        oauth_client_id=client.client_id,
    )
    await api_tokens_storage.add_api_token(
        db_context=txn,
        user_identifier=user_identifier,
        name=f"{name} (refresh)",
        hashed_token=minted.refresh.hashed_secret,
        prefix=minted.refresh.prefix,
        created_at=minted.refresh.created_at,
        expires_at=now + REFRESH_TOKEN_TTL,
        token_type="refresh",
        parent_token_id=access_token_id,
        oauth_client_id=client.client_id,
    )


def _token_response(minted: _MintedPair, scopes: list[str]) -> OAuthToken:
    return OAuthToken(
        access_token=minted.access.full_token,
        expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
        scope=" ".join(scopes),
        refresh_token=minted.refresh.full_token,
    )


class StoreFullError(Exception):
    """The store holds ``MAX_PENDING_ENTRIES`` live entries."""


class _ExpiringStore[T]:
    """A bounded in-process dict whose entries expire after ``ttl_seconds``.

    At capacity a new entry is refused rather than evicting a live one: an
    unauthenticated caller able to fill the store may deny new flows for its
    TTL, but cannot make a real user's in-progress consent vanish.
    """

    def __init__(self, ttl_seconds: float, created_at: Callable[[T], float]) -> None:
        self._ttl = ttl_seconds
        self._created_at = created_at
        self._entries: dict[str, T] = {}

    def _evict(self) -> None:
        now = time.monotonic()
        for key in [
            k for k, v in self._entries.items() if now - self._created_at(v) > self._ttl
        ]:
            del self._entries[key]

    def put(self, key: str, value: T) -> None:
        self._evict()
        if len(self._entries) >= MAX_PENDING_ENTRIES:
            raise StoreFullError
        self._entries[key] = value

    def pop(self, key: str) -> T | None:
        self._evict()
        return self._entries.pop(key, None)

    def get(self, key: str) -> T | None:
        self._evict()
        return self._entries.get(key)


class FamilyAssistantOAuthProvider(
    OAuthAuthorizationServerProvider[
        ConsentedAuthorizationCode, StoredRefreshToken, StoredAccessToken
    ]
):
    """Issues and verifies MCP access tokens for one application instance.

    ``engine`` is a callable because the database engine is injected into
    ``app.state`` after ``create_app`` has built the routes.
    """

    def __init__(self, engine: Callable[[], AsyncEngine]) -> None:
        self._engine = engine
        self._registration_lock = asyncio.Lock()
        self._pending_consents = _ExpiringStore[PendingConsent](
            PENDING_CONSENT_TTL_SECONDS, lambda c: c.created_at
        )
        # Keyed by code; ``created_at`` is monotonic for eviction, while the
        # code's own ``expires_at`` is wall-clock time for the SDK's check.
        self._codes = _ExpiringStore[tuple[ConsentedAuthorizationCode, float]](
            AUTHORIZATION_CODE_TTL_SECONDS, lambda entry: entry[1]
        )

    def _db(self) -> Database:
        return Database(self._engine())

    # --- Clients ---

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await oauth_clients_storage.get_client(self._db(), client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Persist a dynamic registration, keeping the table bounded.

        Registration is unauthenticated, so the table must not grow without
        limit: past the cap, registrations that never produced a live token
        are pruned oldest first, and if every stored client is in use the new
        one is refused rather than evicting a working connector.
        """
        # Admission is serialised in-process and the decision and insert share
        # one transaction, so concurrent registrations cannot each observe the
        # same headroom and all take it. The adapter's flow state is already
        # per-process (pending consents, codes), so a process-level lock is
        # the matching bound.
        # The SDK's RegistrationError is a frozen dataclass, which cannot be
        # re-raised through a context manager, so the decision is taken inside
        # the transaction and the refusal raised after it.
        async with self._registration_lock, self._db().transaction() as txn:
            admitted = await self._admit_registration(txn)
            if admitted:
                await oauth_clients_storage.add_client(txn, client_info)
        if not admitted:
            raise RegistrationError(
                "invalid_client_metadata",
                "Too many registered clients; revoke an unused connector first.",
            )

    @staticmethod
    async def _admit_registration(txn: DatabaseTransaction) -> bool:
        """Whether one more client fits, pruning idle registrations to make room."""
        if await oauth_clients_storage.count_clients(txn) < MAX_REGISTERED_CLIENTS:
            return True
        await oauth_clients_storage.prune_idle_clients(
            txn, keep_at_most=MAX_REGISTERED_CLIENTS - 1
        )
        return await oauth_clients_storage.count_clients(txn) < MAX_REGISTERED_CLIENTS

    # --- Authorization and consent ---

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the user to the consent page.

        The URL is relative: the consent page lives in this application, and
        the SDK's handler hands it to the browser as a redirect ``Location``.
        """
        request_id = secrets.token_urlsafe(32)
        try:
            self._pending_consents.put(
                request_id,
                PendingConsent(
                    request_id=request_id,
                    client=client,
                    params=params,
                    created_at=time.monotonic(),
                ),
            )
        except StoreFullError:
            raise AuthorizeError(
                "temporarily_unavailable",
                "Too many connection requests are waiting for approval; try again shortly.",
            ) from None
        return f"{CONSENT_PATH}?{urlencode({'request_id': request_id})}"

    def pending_consent(self, request_id: str) -> PendingConsent | None:
        """The consent request the page is rendering, if it is still live."""
        return self._pending_consents.get(request_id)

    def approve_consent(self, request_id: str, user_identifier: str) -> str | None:
        """Mint an authorization code for the user; returns the client redirect.

        None when the request is unknown or has expired.
        """
        pending = self._pending_consents.pop(request_id)
        if pending is None:
            return None
        params = pending.params
        code = secrets.token_urlsafe(32)
        self._codes.put(
            code,
            (
                ConsentedAuthorizationCode(
                    code=code,
                    scopes=params.scopes or [ASSISTANT_SCOPE],
                    expires_at=time.time() + AUTHORIZATION_CODE_TTL_SECONDS,
                    client_id=pending.client.client_id,
                    code_challenge=params.code_challenge,
                    redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=params.resource,
                    user_identifier=user_identifier,
                ),
                time.monotonic(),
            ),
        )
        logger.info(
            "User %s authorized OAuth client %s (%s)",
            user_identifier,
            pending.client.client_id,
            pending.client.client_name,
        )
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state
        )

    def deny_consent(self, request_id: str) -> str | None:
        """Drop the request; returns the client redirect carrying the refusal."""
        pending = self._pending_consents.pop(request_id)
        if pending is None:
            return None
        return construct_redirect_uri(
            str(pending.params.redirect_uri),
            error="access_denied",
            error_description="The user declined the request.",
            state=pending.params.state,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> ConsentedAuthorizationCode | None:
        # Popped, not read: a code is single use whether or not the exchange
        # that follows succeeds (RFC 6749 section 4.1.2).
        entry = self._codes.pop(authorization_code)
        if entry is None or entry[0].client_id != client.client_id:
            return None
        return entry[0]

    # --- Token issuance ---

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: ConsentedAuthorizationCode,
    ) -> OAuthToken:
        return await self._issue_tokens(
            client, authorization_code.user_identifier, authorization_code.scopes
        )

    async def _issue_tokens(
        self,
        client: OAuthClientInformationFull,
        user_identifier: str,
        scopes: list[str],
    ) -> OAuthToken:
        minted = await _mint_pair()
        async with self._db().transaction() as txn:
            await _insert_pair(txn, client, user_identifier, minted)
        logger.info(
            "Issued MCP access token to client %s for user %s",
            client.client_id,
            user_identifier,
        )
        return _token_response(minted, scopes)

    # --- Refresh ---

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> StoredRefreshToken | None:
        db = self._db()
        row = await api_tokens_storage.validate_token_by_value(
            db, refresh_token, expected_type="refresh"
        )
        if row is None or row["oauth_client_id"] != client.client_id:
            return None
        parent = await db.fetch_one(
            select(api_tokens_table.c.id).where(
                api_tokens_table.c.id == row["parent_token_id"],
                api_tokens_table.c.token_type == MCP_TOKEN_TYPE,
                api_tokens_table.c.oauth_client_id == client.client_id,
                api_tokens_table.c.is_revoked == False,  # noqa: E712 - SQL comparison
            )
        )
        if parent is None:
            return None
        return StoredRefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=[ASSISTANT_SCOPE],
            expires_at=_expiry_timestamp(row["expires_at"]),
            token_id=row["id"],
            parent_token_id=row["parent_token_id"],
            user_identifier=row["user_identifier"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: StoredRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate in place: the ``mcp`` row is the grant, and it keeps its id.

        The refresh token is consumed by a conditional write on its own row, so
        two requests racing with the same token cannot both mint a replacement.
        The access row then takes the new secret and expiry under the same
        ``is_revoked = false`` condition, so a revoke that lands first (from the
        token page, say) wins and the rotation fails with ``invalid_grant``.
        Only the refresh row is replaced. Because the row a user sees on the
        token page never changes identity, revoking it disconnects the client
        however many times it has refreshed.
        """
        minted = await _mint_pair()
        now = datetime.now(UTC)
        async with self._db().transaction() as txn:
            consumed = await txn.execute(
                update(api_tokens_table)
                .where(
                    api_tokens_table.c.id == refresh_token.token_id,
                    api_tokens_table.c.is_revoked == False,  # noqa: E712 - SQL comparison
                )
                .values(is_revoked=True, last_used_at=now)
                .returning(api_tokens_table.c.id)
            )
            if consumed.scalar_one_or_none() is None:
                raise TokenError("invalid_grant", "refresh token is no longer valid")
            rotated = await txn.execute(
                update(api_tokens_table)
                .where(
                    api_tokens_table.c.id == refresh_token.parent_token_id,
                    api_tokens_table.c.token_type == MCP_TOKEN_TYPE,
                    api_tokens_table.c.is_revoked == False,  # noqa: E712 - SQL comparison
                )
                .values(
                    hashed_token=minted.access.hashed_secret,
                    prefix=minted.access.prefix,
                    expires_at=now + ACCESS_TOKEN_TTL,
                    last_used_at=now,
                )
                .returning(api_tokens_table.c.name)
            )
            grant_name = rotated.scalar_one_or_none()
            if grant_name is None:
                raise TokenError("invalid_grant", "refresh token is no longer valid")
            await api_tokens_storage.add_api_token(
                db_context=txn,
                user_identifier=refresh_token.user_identifier,
                name=f"{grant_name} (refresh)",
                hashed_token=minted.refresh.hashed_secret,
                prefix=minted.refresh.prefix,
                created_at=minted.refresh.created_at,
                expires_at=now + REFRESH_TOKEN_TTL,
                token_type="refresh",
                parent_token_id=refresh_token.parent_token_id,
                oauth_client_id=client.client_id,
            )
        logger.info(
            "Rotated MCP access token for client %s, user %s",
            client.client_id,
            refresh_token.user_identifier,
        )
        return _token_response(minted, scopes)

    # --- Verification and revocation ---

    async def load_access_token(self, token: str) -> StoredAccessToken | None:
        row = await api_tokens_storage.validate_token_by_value(
            self._db(), token, expected_type=MCP_TOKEN_TYPE
        )
        if row is None or row["oauth_client_id"] is None:
            return None
        return StoredAccessToken(
            token=token,
            client_id=row["oauth_client_id"],
            scopes=[ASSISTANT_SCOPE],
            expires_at=_expiry_timestamp(row["expires_at"]),
            token_id=row["id"],
            user_identifier=row["user_identifier"],
        )

    async def revoke_token(self, token: StoredAccessToken | StoredRefreshToken) -> None:
        # Either half of a pair revokes the access token, and with it (by the
        # cascade in revoke_api_token) the refresh token.
        access_token_id = (
            token.parent_token_id
            if isinstance(token, StoredRefreshToken)
            else token.token_id
        )
        await api_tokens_storage.revoke_api_token(
            self._db(), access_token_id, token.user_identifier
        )
