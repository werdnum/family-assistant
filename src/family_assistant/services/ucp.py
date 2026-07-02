"""Universal Commerce Protocol profile and request signing helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from publicsuffix2 import PublicSuffixList

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from family_assistant.config_models import AppConfig, UCPConfig


logger = logging.getLogger(__name__)

UCP_PROFILE_PATH = "/.well-known/ucp"
# Some static hosts (S3, Netlify, IIS) cannot serve the extension-less
# well-known path, so discovery falls back to this ``.json`` variant on a 404.
UCP_PROFILE_FALLBACK_PATH = "/.well-known/ucp.json"
SHOPPING_SERVICE_NAME = "dev.ucp.shopping"
# The shopping checkout capability. Modern checkout-only merchants advertise it
# under ``capabilities`` while omitting the ``services`` block entirely.
SHOPPING_CHECKOUT_CAPABILITY = "dev.ucp.shopping.checkout"
MCP_TRANSPORT = "mcp"
REST_TRANSPORT = "rest"
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class ShoppingEndpoint:
    """A usable shopping endpoint plus the transport the merchant speaks there.

    ``transport`` is one of ``MCP_TRANSPORT`` / ``REST_TRANSPORT`` so the caller
    knows whether to drive the endpoint with the MCP JSON-RPC body or the REST
    verbs/paths from ``rest.openapi.json``.
    """

    transport: str
    endpoint: str


@dataclass(frozen=True, slots=True)
class UCPSignedRequest:
    """Prepared UCP request data with HTTP Message Signature headers."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


class UCPConfigurationError(ValueError):
    """Raised when UCP configuration is incomplete or invalid."""


@dataclass(frozen=True, slots=True)
class MerchantUCPProfile:
    """Discovered UCP capabilities advertised by a merchant origin."""

    origin: str
    mcp_endpoints: tuple[str, ...]
    service_names: tuple[str, ...]
    capability_names: tuple[str, ...]
    version: str | None
    rest_endpoints: tuple[str, ...] = ()

    @property
    def supports_shopping(self) -> bool:
        """Whether the merchant advertises any shopping endpoint (MCP or REST)."""
        return bool(self.mcp_endpoints or self.rest_endpoints)

    @property
    def mcp_endpoint(self) -> str | None:
        """The first advertised shopping MCP endpoint, if any."""
        return self.mcp_endpoints[0] if self.mcp_endpoints else None

    def usable_shopping_endpoint(
        self, *, trusted_suffixes: tuple[str, ...] = ()
    ) -> ShoppingEndpoint | None:
        """The first advertised endpoint safe to call for this merchant.

        The profile is untrusted merchant-controlled metadata, so the signed
        request must not be redirected to an arbitrary host. An endpoint is
        usable when it is same-origin as the merchant, same-site as the merchant
        (a sibling subdomain of the same registrable domain — still the
        merchant's own site, e.g. ``eve.theiconic.com.au`` for
        ``www.theiconic.com.au``), or hosted on a configured trusted
        commerce-platform suffix (``myshopify.com`` covers Shopify storefronts
        on custom domains, whose UCP endpoint lives on the ``*.myshopify.com``
        shop host). Anything else is ignored so the caller falls back rather than
        calling an unrelated host. This is also what the browser shopping hint
        gates on.

        Both transports are considered. Candidates are ranked by trust class —
        same-origin first, then same-site, then trusted suffix — so a profile
        that lists a platform or same-site binding ahead of the merchant-local
        one still resolves to the safest available endpoint rather than the first
        broad match. Within a trust class the MCP binding is preferred over a
        REST one (it is the richer transport), so a merchant advertising both is
        driven over MCP.
        """
        candidates: tuple[ShoppingEndpoint, ...] = tuple(
            ShoppingEndpoint(MCP_TRANSPORT, endpoint) for endpoint in self.mcp_endpoints
        ) + tuple(
            ShoppingEndpoint(REST_TRANSPORT, endpoint)
            for endpoint in self.rest_endpoints
        )
        for predicate in (
            lambda endpoint: same_origin(endpoint, self.origin),
            lambda endpoint: same_site(endpoint, self.origin),
            lambda endpoint: host_matches_trusted_suffix(endpoint, trusted_suffixes),
        ):
            for candidate in candidates:
                if predicate(candidate.endpoint):
                    return candidate
        return None


_DEFAULT_PORTS = {"https": 443, "http": 80}


def _origin_key(url: str) -> tuple[str, str, int | None] | None:
    """Return a normalized ``(scheme, host, port)`` key, or ``None`` if invalid.

    Normalizing lets an explicit default port (``:443``) or a differently-cased
    host compare equal to its canonical form, so a valid same-origin binding is
    not mistaken for a cross-origin one.
    """
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        return None
    return (scheme, host, port if port is not None else _DEFAULT_PORTS.get(scheme))


def same_origin(url_a: str, url_b: str) -> bool:
    """Whether two URLs share a scheme/host/effective-port origin."""
    key_a = _origin_key(url_a)
    return key_a is not None and key_a == _origin_key(url_b)


def _https_host(url: str) -> str | None:
    """Return the lowercased host of a default-port HTTPS URL, or ``None``.

    Returns ``None`` for non-HTTPS, unparseable, malformed-port, or non-default
    port URLs. Used only by the same-site / trusted-suffix checks, which trust
    the standard HTTPS service on a host but not arbitrary other ports: a
    non-default port (e.g. ``:8443``) could reach a different service on the
    same registrable domain or trusted platform host — an SSRF surface.
    Same-origin matching, which legitimately carries an explicit port, uses
    ``_origin_key`` instead, not this.
    """
    try:
        parsed = urlparse(url)
        # Accessing .port validates it: an out-of-range port (e.g. :99999)
        # raises ValueError, so a malformed endpoint is rejected here.
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or port not in {None, 443}:
        return None
    host = (parsed.hostname or "").lower()
    return host or None


# A vendored, current Public Suffix List (including the PRIVATE section) rather
# than publicsuffix2's bundled 2019 snapshot: an outdated list collapses
# unrelated tenants on multi-tenant suffixes added since 2019 (``vercel.app``,
# ``pages.dev``, ``fly.dev``) to the platform domain, which would let a profile
# on ``foo.vercel.app`` pass off ``bar.vercel.app`` as same-site. Refresh the
# file from https://publicsuffix.org/list/public_suffix_list.dat periodically.
_PUBLIC_SUFFIX_LIST_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "public_suffix_list.dat"
)
_PUBLIC_SUFFIX_LIST = PublicSuffixList(psl_file=str(_PUBLIC_SUFFIX_LIST_PATH))


def _registrable_domain(host: str) -> str | None:
    """Registrable domain (eTLD+1) for ``host``, or ``None`` without a real one.

    ``strict=True`` returns ``None`` for hosts with no public-suffix match — IP
    literals and internal names like ``foo.internal`` — which the public suffix
    list would otherwise collapse to a shared token (``1``, ``internal``).
    """
    return _PUBLIC_SUFFIX_LIST.get_sld(host, strict=True)


def same_site(url_a: str, url_b: str) -> bool:
    """Whether two HTTPS URLs share a registrable domain (eTLD+1).

    Same-site-but-not-same-origin covers a merchant serving its UCP endpoint
    from a sibling subdomain of its storefront (``eve.theiconic.com.au`` for
    ``www.theiconic.com.au``) — still the merchant's own site. The registrable
    domain is resolved against a vendored current public suffix list so
    multi-label suffixes (``com.au``) and multi-tenant platform suffixes
    (``vercel.app``) are handled correctly, and hosts without a real public
    suffix (IP literals, internal names) never match — keeping an SSRF-prone
    profile fetched from an IP/private zone or a co-tenant on a shared platform
    from sliding an endpoint on a different host through this check.
    """
    host_a = _https_host(url_a)
    host_b = _https_host(url_b)
    if host_a is None or host_b is None:
        return False
    domain_a = _registrable_domain(host_a)
    return bool(domain_a) and domain_a == _registrable_domain(host_b)


def host_matches_trusted_suffix(url: str, suffixes: tuple[str, ...]) -> bool:
    """Whether an HTTPS URL's host equals or is a subdomain of a trusted suffix.

    Trusted suffixes name commerce platforms whose backend hosts are safe to
    post to even cross-site (``myshopify.com`` for Shopify shop hosts). Matching
    is on whole labels, so ``status-anxiety-2.myshopify.com`` matches
    ``myshopify.com`` while ``notmyshopify.com`` and ``myshopify.com.evil.com``
    do not.
    """
    host = _https_host(url)
    if host is None:
        return False
    for suffix in suffixes:
        normalized = suffix.strip().lstrip(".").lower()
        if normalized and (host == normalized or host.endswith(f".{normalized}")):
            return True
    return False


def _is_trusted_redirect_target(
    next_url: str, origin_url: str, trusted_suffixes: tuple[str, ...]
) -> bool:
    """Whether a discovery redirect to ``next_url`` is safe to follow.

    Mirrors the endpoint-trust policy applied to advertised bindings
    (:meth:`MerchantUCPProfile.usable_shopping_endpoint`) so discovery and the signed
    POST trust the same hosts: the merchant frequently redirects
    ``/.well-known/ucp`` to a sibling subdomain or, for a Shopify store on a
    custom domain, to its ``*.myshopify.com`` shop host. A hop is followed when
    it is same-origin, same-site (a sibling subdomain of the *original* merchant
    origin's registrable domain), or on a configured trusted platform suffix.
    Every hop is checked against the original origin, not the previous hop, so a
    chain cannot walk off the merchant's site one same-site step at a time.
    Anything else is treated as a discovery miss so a merchant-controlled
    ``Location`` cannot point the probe at an unrelated or internal host (SSRF).
    """
    return (
        same_origin(next_url, origin_url)
        or same_site(next_url, origin_url)
        or host_matches_trusted_suffix(next_url, trusted_suffixes)
    )


def merchant_origin(url: str) -> str | None:
    """Return the ``https://host`` origin for ``url``, or ``None`` if not HTTPS.

    Returns ``None`` for malformed URLs (``urlparse`` raising ``ValueError``)
    rather than propagating, since callers treat a missing origin as "fall back".
    """
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc
    except ValueError:
        return None
    if parsed.scheme != "https" or not netloc:
        return None
    return f"https://{netloc}"


def _shopping_endpoints(
    base_url: str, services: Mapping[str, object], transport: str
) -> tuple[str, ...]:
    """Return every advertised HTTPS shopping endpoint for ``transport``.

    All candidates are returned (not just the first) so callers can apply their
    own selection policy — e.g. the shopping tools prefer a same-origin binding
    even when a cross-origin one is listed first. Bindings whose endpoint cannot
    be parsed (merchant-controlled metadata) are skipped rather than allowed to
    break discovery.

    Relative endpoints resolve against ``base_url`` — the URL the profile was
    actually fetched from, which may differ from the merchant origin when
    discovery followed a trusted redirect (e.g. to a ``*.myshopify.com`` shop
    host) — so a redirected profile's relative binding points at the host that
    served it rather than the original storefront.
    """
    bindings = services.get(SHOPPING_SERVICE_NAME)
    if not isinstance(bindings, list):
        return ()
    endpoints: list[str] = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        if binding.get("transport") != transport:
            continue
        endpoint = binding.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            continue
        try:
            resolved = (
                endpoint if urlparse(endpoint).scheme else urljoin(base_url, endpoint)
            )
            parsed_resolved = urlparse(resolved)
            netloc = parsed_resolved.netloc
            scheme = parsed_resolved.scheme
        except ValueError:
            continue
        if scheme == "https" and netloc:
            endpoints.append(resolved)
    return tuple(endpoints)


def _api_parent_base(profile_url: str) -> str | None:
    """Return the API base directory that hosts a merchant's ``.well-known`` profile.

    For a profile fetched from ``https://host/ucommerce/.well-known/ucp`` the
    parent base is ``https://host/ucommerce`` — the API root a checkout-only
    merchant's REST routes (``/checkout-sessions`` …) hang off when the profile
    omits an explicit ``services`` binding. The path prefix ahead of
    ``/.well-known/`` is preserved rather than collapsed to the bare origin, so a
    merchant serving UCP under a subpath still resolves correctly (a
    root-hosted profile yields the bare origin). Returns ``None`` for a
    non-HTTPS URL or one without a ``/.well-known/`` path segment.
    """
    try:
        parsed = urlparse(profile_url)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    marker = "/.well-known/"
    index = parsed.path.find(marker)
    if index == -1:
        return None
    return f"https://{parsed.netloc}{parsed.path[:index]}"


def _parse_merchant_profile(
    origin: str,
    endpoint_base: str,
    payload: object,
    *,
    api_parent_base: str | None = None,
) -> MerchantUCPProfile | None:
    """Parse a fetched UCP payload into a :class:`MerchantUCPProfile`.

    ``origin`` is the original merchant origin and becomes ``profile.origin`` —
    the anchor every trust check runs against. ``endpoint_base`` is the URL the
    profile was actually fetched from (the final hop after any trusted redirect)
    and is used only to resolve relative endpoint bindings, so a redirected
    profile's relative endpoint resolves to the host that served it while trust
    stays anchored to the merchant the user chose. ``api_parent_base`` is the
    directory that hosts the profile (see :func:`_api_parent_base`), used as the
    REST endpoint fallback for a checkout-only merchant that omits ``services``.
    """
    if not isinstance(payload, dict):
        return None
    ucp = payload.get("ucp")
    if not isinstance(ucp, dict):
        return None
    services = ucp.get("services")
    services = services if isinstance(services, dict) else {}
    capabilities = ucp.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    version = ucp.get("version")
    mcp_endpoints = _shopping_endpoints(endpoint_base, services, MCP_TRANSPORT)
    rest_endpoints = _shopping_endpoints(endpoint_base, services, REST_TRANSPORT)
    # Modern checkout-only merchants (UCP 2026-04-08) omit the ``services`` block
    # entirely, advertising only ``capabilities``. With no explicit binding the
    # REST API lives at the profile's parent directory (the path that hosts
    # ``/.well-known/ucp``), so register that as the REST endpoint when the
    # merchant advertises the shopping checkout capability. Trust is still
    # enforced per-call by ``usable_shopping_endpoint``, and this parent base is
    # same-origin as the merchant, so it passes that gate.
    if (
        not mcp_endpoints
        and not rest_endpoints
        and api_parent_base is not None
        and SHOPPING_CHECKOUT_CAPABILITY in capabilities
    ):
        rest_endpoints = (api_parent_base,)
    return MerchantUCPProfile(
        origin=origin,
        mcp_endpoints=mcp_endpoints,
        rest_endpoints=rest_endpoints,
        service_names=tuple(services.keys()),
        capability_names=tuple(capabilities.keys()),
        version=version if isinstance(version, str) else None,
    )


MAX_DISCOVERY_REDIRECTS = 5


async def _get_following_trusted_redirects(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    timeout: float | None,
    trusted_suffixes: tuple[str, ...] = (),
    now: Callable[[], float] = time.monotonic,
) -> tuple[httpx.Response, str] | None:
    """GET ``url``, following 301/302/etc only to trusted redirect targets.

    Live merchants frequently serve ``/.well-known/ucp`` behind a redirect
    (commonly a trailing-slash or path canonicalization, but also a hop to a
    sibling subdomain or a Shopify store's ``*.myshopify.com`` shop host), and
    httpx does not follow redirects by default — so the 3xx would otherwise be
    treated as an empty body and discovery would silently fail. Redirects are
    followed manually rather than with ``follow_redirects=True`` because the
    profile URL is merchant-controlled: an arbitrary ``Location`` could
    otherwise point the discovery GET at an internal host (SSRF). Each hop must
    be a trusted target relative to the original ``url`` — same-origin,
    same-site, or on a configured ``trusted_suffixes`` platform, matching where
    the signed POST is allowed to go — and the chain is bounded. Returns the
    final response together with the URL it was fetched from (so the caller can
    resolve relative endpoints against the host that actually served the
    profile), or ``None`` when a redirect leaves the trusted set (or the bound
    is exceeded), so the caller treats it as a discovery miss.

    ``timeout`` bounds the *whole* redirect chain, not each hop: each GET is
    given only the remaining budget, so a slow same-origin redirect chain cannot
    stall a shopping request for ``(MAX_DISCOVERY_REDIRECTS + 1) * timeout``.
    ``now`` is injectable for deterministic tests.
    """
    deadline = None if timeout is None else now() + timeout
    current_url = url
    for _ in range(MAX_DISCOVERY_REDIRECTS + 1):
        if deadline is not None:
            remaining = deadline - now()
            if remaining <= 0:
                logger.debug("UCP discovery exceeded its time budget for %s", url)
                return None
            response = await client.get(current_url, headers=headers, timeout=remaining)
        else:
            response = await client.get(current_url, headers=headers)
        if not response.is_redirect:
            return response, current_url
        location = response.headers.get("location")
        if not location:
            return response, current_url
        # The Location is merchant-controlled; a malformed value (e.g. a bad
        # IPv6 host) makes urljoin raise, so treat that as a discovery miss
        # rather than letting it crash endpoint resolution / the browser probe.
        try:
            next_url = urljoin(current_url, location)
        except ValueError:
            logger.debug(
                "UCP discovery got a malformed redirect Location: %r", location
            )
            return None
        if not _is_trusted_redirect_target(next_url, url, trusted_suffixes):
            return None
        current_url = next_url
    return None


async def discover_merchant_ucp_profile(
    url: str,
    *,
    client: httpx.AsyncClient,
    timeout: float | None = None,
    trusted_suffixes: tuple[str, ...] = (),
    now: Callable[[], float] = time.monotonic,
) -> MerchantUCPProfile | None:
    """Fetch and parse a merchant's ``/.well-known/ucp`` profile.

    ``url`` may be any URL on the merchant; only its HTTPS origin is used. When
    the extension-less ``/.well-known/ucp`` path returns ``404`` — as some static
    hosts (S3, Netlify, IIS) do for files without an extension — discovery makes
    one secondary attempt against ``/.well-known/ucp.json`` before giving up.
    ``timeout`` bounds the whole discovery GET — including both probes and any
    redirect chain (each request gets only the remaining shared budget) —
    independent of the caller's client-wide timeout, so a slow/tarpit
    ``/.well-known/ucp`` cannot stall a subsequent request; when ``None`` the
    client's default timeout applies.
    ``trusted_suffixes`` are the configured commerce-platform host suffixes a
    discovery redirect may land on cross-site (in addition to same-origin and
    same-site hops), mirroring where the signed POST is allowed to go; pass the
    operator's ``ucp_config.trusted_endpoint_suffixes`` so a Shopify store on a
    custom domain that redirects to its ``*.myshopify.com`` shop host is
    followed instead of treated as a discovery miss. ``now`` is the monotonic
    clock used for the timeout budget, injectable so tests can drive the
    deadline deterministically.
    Returns ``None`` when the origin is not HTTPS, the profile is unreachable or
    not valid JSON, or it advertises no shopping service. Network and decode
    errors are swallowed so callers can fall back without special handling.
    """
    origin = merchant_origin(url)
    if origin is None:
        return None

    headers = {"Accept": "application/json"}
    # Share one timeout budget across the well-known probe and its .json fallback
    # so the secondary attempt cannot double the worst-case discovery latency.
    deadline = None if timeout is None else now() + timeout

    response: httpx.Response | None = None
    final_url: str | None = None
    for profile_path in (UCP_PROFILE_PATH, UCP_PROFILE_FALLBACK_PATH):
        remaining = None if deadline is None else deadline - now()
        if remaining is not None and remaining <= 0:
            logger.debug("UCP discovery exceeded its time budget for %s", origin)
            return None
        profile_url = f"{origin}{profile_path}"
        try:
            result = await _get_following_trusted_redirects(
                client,
                profile_url,
                headers=headers,
                timeout=remaining,
                trusted_suffixes=trusted_suffixes,
                now=now,
            )
        except httpx.HTTPError as exc:
            logger.debug("UCP discovery request failed for %s: %s", profile_url, exc)
            return None
        if result is None:
            logger.debug(
                "UCP discovery for %s redirected to an untrusted host; not followed",
                profile_url,
            )
            return None
        candidate, candidate_url = result
        # A static host that cannot serve the extension-less path returns 404;
        # retry once against the .json variant before treating it as a miss.
        if (
            candidate.status_code == httpx.codes.NOT_FOUND
            and profile_path != UCP_PROFILE_FALLBACK_PATH
        ):
            logger.debug(
                "UCP discovery for %s returned 404; retrying %s%s",
                profile_url,
                origin,
                UCP_PROFILE_FALLBACK_PATH,
            )
            continue
        response, final_url = candidate, candidate_url
        break

    if (
        response is None or final_url is None
    ):  # pragma: no cover - loop always sets both or returns early
        return None
    if response.is_error:
        logger.debug(
            "UCP discovery for %s returned HTTP %s", origin, response.status_code
        )
        return None
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.debug("UCP discovery for %s returned an undecodable body", origin)
        return None

    # Resolve relative endpoints against the host the profile was actually
    # fetched from (the final hop after any trusted redirect), while keeping the
    # original merchant origin as the trust anchor. Falls back to the original
    # origin if the final URL is somehow not an HTTPS origin.
    endpoint_base = merchant_origin(final_url) or origin
    return _parse_merchant_profile(
        origin,
        endpoint_base,
        payload,
        api_parent_base=_api_parent_base(final_url),
    )


def _base64_url_no_padding(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64_standard(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _without_none_values(data: dict[str, object | None]) -> dict[str, object]:
    return {key: value for key, value in data.items() if value is not None}


def _load_signing_private_key(
    ucp_config: UCPConfig,
) -> ec.EllipticCurvePrivateKey | None:
    private_key_pem = ucp_config.signing_private_key
    if not private_key_pem and ucp_config.signing_private_key_path:
        private_key_pem = Path(ucp_config.signing_private_key_path).read_text(
            encoding="utf-8"
        )
    if not private_key_pem:
        return None

    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None
        )
    except (TypeError, ValueError, UnsupportedAlgorithm) as exc:
        msg = "UCP signing private key must be an unencrypted PEM EC private key."
        raise UCPConfigurationError(msg) from exc

    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        msg = "UCP signing private key must be an EC private key."
        raise UCPConfigurationError(msg)
    if not isinstance(private_key.curve, (ec.SECP256R1, ec.SECP384R1)):
        msg = "UCP signing private key must use P-256 or P-384."
        raise UCPConfigurationError(msg)
    return private_key


def _jwk_from_private_key(
    private_key: ec.EllipticCurvePrivateKey,
    *,
    key_id: str,
) -> dict[str, object]:
    public_numbers = private_key.public_key().public_numbers()
    if isinstance(private_key.curve, ec.SECP256R1):
        coordinate_size = 32
        curve_name = "P-256"
        algorithm = "ES256"
    elif isinstance(private_key.curve, ec.SECP384R1):
        coordinate_size = 48
        curve_name = "P-384"
        algorithm = "ES384"
    else:
        msg = "UCP signing private key must use P-256 or P-384."
        raise UCPConfigurationError(msg)

    return {
        "kid": key_id,
        "kty": "EC",
        "crv": curve_name,
        "x": _base64_url_no_padding(public_numbers.x.to_bytes(coordinate_size)),
        "y": _base64_url_no_padding(public_numbers.y.to_bytes(coordinate_size)),
        "use": "sig",
        "alg": algorithm,
    }


def _profile_url(app_config: AppConfig) -> str:
    if app_config.ucp_config.profile_url:
        return app_config.ucp_config.profile_url
    return f"{app_config.server_url.rstrip('/')}{app_config.ucp_config.profile_path}"


def _validate_https_profile_url(profile_url: str) -> None:
    parsed_profile_url = urlparse(profile_url)
    if parsed_profile_url.scheme != "https" or not parsed_profile_url.netloc:
        msg = "UCP profile URL must be an HTTPS URL for signed requests."
        raise UCPConfigurationError(msg)


def ucp_profile_url(app_config: AppConfig) -> str:
    """Return the public UCP profile URL advertised for this application."""
    return _profile_url(app_config)


def ucp_agent_header(app_config: AppConfig) -> str:
    """Return the UCP-Agent header value for this application's profile."""
    return _format_ucp_agent_header(_profile_url(app_config))


def has_ucp_signing_key(app_config: AppConfig) -> bool:
    """Return True when UCP request signing key material is configured."""
    ucp_config = app_config.ucp_config
    return bool(
        ucp_config.signing_key_id
        and (ucp_config.signing_private_key or ucp_config.signing_private_key_path)
    )


def load_ucp_signing_key(
    app_config: AppConfig,
) -> ec.EllipticCurvePrivateKey | None:
    """Load and validate the configured UCP signing private key.

    Returns ``None`` when no key material is configured, and raises
    ``UCPConfigurationError`` when material is present but malformed, encrypted,
    or the wrong type. Lets callers fail fast on a misconfigured key before doing
    network work that cannot succeed without a usable key.
    """
    return _load_signing_private_key(app_config.ucp_config)


def build_ucp_profile(app_config: AppConfig) -> dict[str, object]:
    """Build the public UCP platform profile for this Family Assistant instance."""
    ucp_config = app_config.ucp_config
    if not ucp_config.enabled:
        msg = "UCP profile is disabled."
        raise UCPConfigurationError(msg)

    signing_keys: list[dict[str, object]] = [
        key.model_dump(exclude_none=True) for key in ucp_config.additional_signing_keys
    ]
    private_key = _load_signing_private_key(ucp_config)
    if private_key is not None:
        if not ucp_config.signing_key_id:
            msg = "UCP_SIGNING_KEY_ID is required when UCP signing key material is configured."
            raise UCPConfigurationError(msg)
        signing_keys.insert(
            0, _jwk_from_private_key(private_key, key_id=ucp_config.signing_key_id)
        )

    return {
        "ucp": _without_none_values({
            "version": ucp_config.version,
            "services": {
                name: [
                    entry.model_dump(exclude_none=True, by_alias=True)
                    for entry in entries
                ]
                for name, entries in ucp_config.services.items()
            },
            "capabilities": {
                name: [
                    entry.model_dump(exclude_none=True, by_alias=True)
                    for entry in entries
                ]
                for name, entries in ucp_config.capabilities.items()
            },
            "payment_handlers": {
                name: [
                    entry.model_dump(exclude_none=True, by_alias=True)
                    for entry in entries
                ]
                for name, entries in ucp_config.payment_handlers.items()
            },
        }),
        "signing_keys": signing_keys,
    }


def _json_body_bytes(body: object | None) -> bytes | None:
    if body is None:
        return None
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _content_digest(body: bytes) -> str:
    return f"sha-256=:{_base64_standard(hashlib.sha256(body).digest())}:"


def _format_ucp_agent_header(profile_url: str) -> str:
    if '"' in profile_url or "\\" in profile_url:
        msg = "UCP profile URL must not contain quote or backslash characters."
        raise UCPConfigurationError(msg)
    return f'profile="{profile_url}"'


def _signature_input_header(components: list[str], key_id: str) -> str:
    quoted_components = " ".join(f'"{component}"' for component in components)
    escaped_key_id = key_id.replace("\\", "\\\\").replace('"', '\\"')
    return f'sig1=({quoted_components});keyid="{escaped_key_id}"'


def _component_value(
    component: str,
    *,
    method: str,
    authority: str,
    path: str,
    query: str,
    headers: Mapping[str, str],
) -> str:
    if component == "@method":
        return method
    if component == "@authority":
        return authority
    if component == "@path":
        return path
    if component == "@query":
        return f"?{query}"
    return headers[component]


def _signature_base(
    components: list[str],
    *,
    method: str,
    authority: str,
    path: str,
    query: str,
    headers: Mapping[str, str],
    key_id: str,
) -> bytes:
    lines = [
        f'"{component}": '
        f"{_component_value(component, method=method, authority=authority, path=path, query=query, headers=headers)}"
        for component in components
    ]
    params = _signature_input_header(components, key_id).split("=", 1)[1]
    lines.append(f'"@signature-params": {params}')
    return "\n".join(lines).encode("utf-8")


def _signature_hash(private_key: ec.EllipticCurvePrivateKey) -> hashes.HashAlgorithm:
    if isinstance(private_key.curve, ec.SECP256R1):
        return hashes.SHA256()
    if isinstance(private_key.curve, ec.SECP384R1):
        return hashes.SHA384()
    msg = "UCP signing private key must use P-256 or P-384."
    raise UCPConfigurationError(msg)


def _raw_ecdsa_signature(
    private_key: ec.EllipticCurvePrivateKey,
    signature_base: bytes,
) -> bytes:
    der_signature = private_key.sign(
        signature_base,
        ec.ECDSA(_signature_hash(private_key)),
    )
    r_value, s_value = utils.decode_dss_signature(der_signature)
    coordinate_size = 32 if isinstance(private_key.curve, ec.SECP256R1) else 48
    return r_value.to_bytes(coordinate_size) + s_value.to_bytes(coordinate_size)


def sign_ucp_request(
    app_config: AppConfig,
    *,
    method: str,
    url: str,
    body: object | None = None,
    idempotency_key: str | None = None,
    additional_headers: Mapping[str, str] | None = None,
) -> UCPSignedRequest:
    """Prepare UCP HTTP headers and body bytes for a signed platform request."""
    ucp_config = app_config.ucp_config
    private_key = _load_signing_private_key(ucp_config)
    if private_key is None or not ucp_config.signing_key_id:
        msg = (
            "UCP request signing requires UCP_SIGNING_KEY_ID and either "
            "UCP_SIGNING_PRIVATE_KEY or UCP_SIGNING_PRIVATE_KEY_PATH."
        )
        raise UCPConfigurationError(msg)

    normalized_method = method.upper()
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https":
        msg = "UCP requests must use an https URL."
        raise UCPConfigurationError(msg)
    if not parsed_url.netloc:
        msg = "UCP request URL must include a host."
        raise UCPConfigurationError(msg)

    request_body = _json_body_bytes(body)
    profile_url = _profile_url(app_config)
    _validate_https_profile_url(profile_url)

    headers = {
        "UCP-Agent": _format_ucp_agent_header(profile_url),
        **(dict(additional_headers or {})),
    }
    if request_body is not None:
        headers["Content-Type"] = headers.get("Content-Type", "application/json")
        headers["Content-Digest"] = _content_digest(request_body)
    if normalized_method in STATE_CHANGING_METHODS:
        headers["Idempotency-Key"] = idempotency_key or str(uuid4())
    elif idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    lowercase_headers = {key.lower(): value for key, value in headers.items()}
    components = ["@method", "@authority", "@path"]
    if parsed_url.query:
        components.append("@query")
    components.append("ucp-agent")
    if "idempotency-key" in lowercase_headers:
        components.append("idempotency-key")
    if request_body is not None:
        components.extend(["content-digest", "content-type"])

    signature_input = _signature_input_header(components, ucp_config.signing_key_id)
    base = _signature_base(
        components,
        method=normalized_method,
        authority=parsed_url.netloc,
        path=parsed_url.path or "/",
        query=parsed_url.query,
        headers=lowercase_headers,
        key_id=ucp_config.signing_key_id,
    )
    signature = _raw_ecdsa_signature(private_key, base)
    headers["Signature-Input"] = signature_input
    headers["Signature"] = f"sig1=:{_base64_standard(signature)}:"

    return UCPSignedRequest(
        method=normalized_method,
        url=url,
        headers=headers,
        body=request_body,
    )
