"""Per-user Stripe Link Agent Wallet service wrapping ``stripe-link-sdk``.

Constructs ``link.AsyncClient`` backed by :class:`OAuthCredentialResolver` bound
strictly to the turn's ``exec_context.user_id`` (never a user id supplied in
tool arguments). Ordinary wallet and spend-request methods never request
``include=["card"]``; :meth:`LinkWalletService.retrieve_spend_request_with_card`
is reserved for ``browser_fill_payment`` after status and origin verification
and wraps PAN and CVC strictly in :class:`pydantic.SecretStr` fields.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

import link
from link import (
    AsyncClient,
    LinkAPIError,
    LinkConfigurationError,
    LinkResponseError,
    LinkSDKError,
    LinkTransportError,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)

from family_assistant.services.link_provider import (
    LINK_API_BASE_URL,
    LINK_PROVIDER,
    LinkScope,
)
from family_assistant.services.oauth_credentials import OAuthNoActingUserError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import httpx
    from link.models import (
        Card,
        PaymentMethod,
        ShippingAddressRecord,
        SpendRequest,
        UserInfo,
    )
    from link.params import (
        GetAccessTokenOptions,
        LineItemParams,
        SpendRequestRecurringParams,
        TotalParams,
    )

    from family_assistant.services.oauth_credentials import OAuthCredentialResolver
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

_LINK_USER_AGENT = "FamilyAssistant/1.0"
_MAX_API_ERROR_MESSAGE_CHARS = 300


class LinkWalletError(Exception):
    """Actionable, credential-free error talking to the Link Agent Wallet API."""


class RetrievedBillingAddress(BaseModel):
    """Billing address associated with an approved Link virtual card."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str | None = Field(default=None, max_length=256)
    line1: str | None = Field(default=None, max_length=256)
    line2: str | None = Field(default=None, max_length=256)
    city: str | None = Field(default=None, max_length=128)
    state: str | None = Field(default=None, max_length=128)
    postal_code: str | None = Field(default=None, max_length=64)
    country: str | None = Field(default=None, max_length=128)


class RetrievedLinkCard(BaseModel):
    """Virtual card credentials held in SecretStr fields so repr/dump/errors never leak them."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    number: SecretStr
    exp_month: int = Field(ge=1, le=12)
    exp_year: int = Field(ge=2000, le=2199)
    cvc: SecretStr
    cardholder_name: str | None = Field(default=None, max_length=256)
    billing_address: RetrievedBillingAddress | None = None

    @field_validator("number", "cvc")
    @classmethod
    def _reject_blank_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value


def _build_retrieved_card(raw_card: Card | None) -> RetrievedLinkCard | None:
    """Wrap a Link SDK ``Card`` into :class:`RetrievedLinkCard` (SecretStr-typed)."""
    if raw_card is None:
        return None
    if not raw_card.number.strip() or not raw_card.cvc or not raw_card.cvc.strip():
        return None
    exp_year = (
        raw_card.exp_year + 2000 if 0 <= raw_card.exp_year < 100 else raw_card.exp_year
    )
    billing_address: RetrievedBillingAddress | None = None
    cardholder_name: str | None = None
    if raw_card.billing_address is not None:
        addr = raw_card.billing_address
        cardholder_name = addr.name.strip() or None
        billing_address = RetrievedBillingAddress(
            name=addr.name,
            line1=addr.line1,
            line2=addr.line2,
            city=addr.city,
            state=addr.state,
            postal_code=addr.postal_code,
            country=addr.country,
        )
    try:
        return RetrievedLinkCard(
            number=SecretStr(raw_card.number.strip()),
            exp_month=raw_card.exp_month,
            exp_year=exp_year,
            cvc=SecretStr(raw_card.cvc.strip()),
            cardholder_name=cardholder_name,
            billing_address=billing_address,
        )
    except ValidationError:
        logger.warning("Link returned an invalid virtual card payload")
        raise LinkWalletError(
            "Link returned an incomplete or invalid virtual card credential."
        ) from None


def _sanitize_api_error(exc: LinkAPIError) -> str:
    """Return a concise, safe error message for a Link API failure."""
    details = exc.details
    if isinstance(details, dict):
        error = details.get("error")
        if isinstance(error, dict):
            for key in ("message", "code"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    return f"Link API error ({exc.status}): {value.strip()[:_MAX_API_ERROR_MESSAGE_CHARS]}"
        elif isinstance(error, str) and error.strip():
            return f"Link API error ({exc.status}): {error.strip()[:_MAX_API_ERROR_MESSAGE_CHARS]}"
        message = details.get("message")
        if isinstance(message, str) and message.strip():
            return f"Link API error ({exc.status}): {message.strip()[:_MAX_API_ERROR_MESSAGE_CHARS]}"
    return f"Link API request failed with status {exc.status}."


class LinkWalletService:
    """User-scoped facade over ``stripe-link-sdk``'s :class:`AsyncClient`."""

    def __init__(
        self,
        exec_context: ToolExecutionContext,
        resolver: OAuthCredentialResolver,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_base_url: str = LINK_API_BASE_URL,
    ) -> None:
        self._exec_context = exec_context
        self._resolver = resolver
        self._http_client = http_client or resolver.http_client
        self._api_base_url = api_base_url

    @classmethod
    def for_context(
        cls,
        exec_context: ToolExecutionContext,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_base_url: str = LINK_API_BASE_URL,
    ) -> LinkWalletService:
        """Build a service bound to the turn's acting user and Link resolver."""
        if not exec_context.user_id:
            raise OAuthNoActingUserError(LINK_PROVIDER.display_name)
        resolvers = exec_context.credential_resolvers or {}
        resolver = resolvers.get(LINK_PROVIDER.name)
        if resolver is None:
            raise LinkWalletError(
                "Link Agent Wallet integration is not enabled for this deployment."
            )
        return cls(
            exec_context,
            resolver,
            http_client=http_client,
            api_base_url=api_base_url,
        )

    @property
    def configured_scopes(self) -> frozenset[str]:
        """Return the deployment-configured scopes for Link."""
        return self._resolver.configured_scopes

    def _client_for_scope(self, scope: LinkScope) -> AsyncClient:
        """Create an AsyncClient whose bearer token comes from the user's resolver."""

        async def _get_access_token(options: GetAccessTokenOptions) -> str:
            if options.force_refresh and self._exec_context.user_id:
                self._resolver.evict_cached_token(self._exec_context.user_id)
            return await self._resolver.access_token_for(
                self._exec_context, scope.value
            )

        return AsyncClient(
            get_access_token=_get_access_token,
            api_base_url=self._api_base_url,
            http_client=self._http_client,
            default_headers={"User-Agent": _LINK_USER_AGENT},
        )

    async def _touch_last_used(self) -> None:
        """Best-effort update of ``last_used_at`` after a successful Link API call."""
        user_id = self._exec_context.user_id
        if not user_id:
            return
        try:
            await self._exec_context.db_context.oauth_connections.update_last_used(
                user_id, LINK_PROVIDER.name
            )
        except Exception:
            logger.debug("Failed to update Link connection last_used_at", exc_info=True)

    async def _call[T](
        self,
        scope: LinkScope,
        operation: str,
        fn: Callable[[AsyncClient], Awaitable[T]],
        *,
        redact_errors: bool = False,
    ) -> T:
        """Execute a Link SDK call and translate SDK errors cleanly."""
        try:
            async with self._client_for_scope(scope) as client:
                result = await fn(client)
        except LinkAPIError as exc:
            logger.warning(
                "Link API failure during %s: status=%s", operation, exc.status
            )
            if redact_errors:
                raise LinkWalletError(
                    f"Link API request failed with status {exc.status}."
                ) from None
            raise LinkWalletError(_sanitize_api_error(exc)) from exc
        except LinkTransportError as exc:
            if redact_errors:
                logger.warning(
                    "Link transport error during %s: %s",
                    operation,
                    type(exc).__name__,
                )
                raise LinkWalletError(
                    "Could not reach the Link API right now — please try again."
                ) from None
            logger.warning("Link transport error during %s: %s", operation, exc)
            raise LinkWalletError(
                "Could not reach the Link API right now — please try again."
            ) from exc
        except (LinkResponseError, LinkConfigurationError, LinkSDKError) as exc:
            if redact_errors:
                logger.warning(
                    "Link SDK error during %s: %s", operation, type(exc).__name__
                )
                raise LinkWalletError(
                    f"Unexpected Link error while attempting to {operation}."
                ) from None
            logger.warning("Link SDK error during %s: %s", operation, exc)
            raise LinkWalletError(
                f"Unexpected Link error while attempting to {operation}."
            ) from exc
        await self._touch_last_used()
        return result

    async def get_user_info(self) -> UserInfo:
        """Retrieve Link user info (default currency, spend limits, verification)."""
        return await self._call(
            LinkScope.USERINFO_READ,
            "retrieve wallet info",
            lambda client: client.user_info.retrieve(),
        )

    async def list_payment_methods(self) -> list[PaymentMethod]:
        """List saved payment methods with agentic-payment eligibility capabilities."""
        return await self._call(
            LinkScope.PAYMENT_METHODS_AGENTIC,
            "list payment methods",
            lambda client: client.payment_methods.list(),
        )

    async def list_shipping_addresses(self) -> list[ShippingAddressRecord]:
        """List saved shipping addresses from the user's Link wallet."""
        return await self._call(
            LinkScope.PAYMENT_METHODS_AGENTIC,
            "list shipping addresses",
            lambda client: client.shipping_addresses.list(),
        )

    async def create_spend_request(
        self,
        *,
        amount: int,
        currency: str,
        context: str,
        merchant_name: str,
        merchant_url: str,
        payment_details: str | None = None,
        line_items: Sequence[LineItemParams] | None = None,
        totals: Sequence[TotalParams] | None = None,
        expires_at: int | str | None = None,
        recurring: SpendRequestRecurringParams | None = None,
    ) -> SpendRequest:
        """Create a ``card`` spend request with a fresh idempotency key and approval."""
        idempotency_key = f"fa-{uuid.uuid4().hex}"
        return await self._call(
            LinkScope.PAYMENT_METHODS_AGENTIC,
            "create spend request",
            lambda client: client.spend_requests.create(
                context=context,
                idempotency_key=idempotency_key,
                credential_type="card",
                request_approval=True,
                amount=amount,
                currency=currency,
                merchant_name=merchant_name,
                merchant_url=merchant_url,
                payment_details=payment_details,
                line_items=line_items,
                totals=totals,
                expires_at=expires_at,
                recurring=recurring,
            ),
        )

    async def retrieve_spend_request(
        self, spend_request_id: str
    ) -> SpendRequest | None:
        """Retrieve spend request metadata (never passing ``include=['card']``)."""
        return await self._call(
            LinkScope.PAYMENT_METHODS_AGENTIC,
            "retrieve spend request",
            lambda client: client.spend_requests.retrieve(spend_request_id),
        )

    async def retrieve_spend_request_with_card(
        self, spend_request_id: str
    ) -> tuple[SpendRequest, RetrievedLinkCard | None] | None:
        """Retrieve an approved spend request with its virtual card in SecretStr fields.

        Only call this from ``browser_fill_payment`` after confirming the spend
        request is approved and its ``merchant_url`` origin matches the active
        browser page. Strips raw card fields from the returned ``SpendRequest``
        so card digits exist only inside :class:`RetrievedLinkCard`.
        """
        sr = await self._call(
            LinkScope.PAYMENT_METHODS_AGENTIC,
            "retrieve spend request card",
            lambda client: client.spend_requests.retrieve(
                spend_request_id, include=["card"]
            ),
            redact_errors=True,
        )
        if sr is None:
            return None
        retrieved_card = _build_retrieved_card(sr.card)
        sanitized_sr = sr.model_copy(update={"card": None, "link_pay_token": None})
        return sanitized_sr, retrieved_card

    async def cancel_spend_request(self, spend_request_id: str) -> SpendRequest:
        """Cancel an unused spend request."""
        return await self._call(
            LinkScope.PAYMENT_METHODS_AGENTIC,
            "cancel spend request",
            lambda client: client.spend_requests.cancel(spend_request_id),
        )


__all__ = [
    "LinkWalletError",
    "LinkWalletService",
    "RetrievedBillingAddress",
    "RetrievedLinkCard",
    "link",
]
