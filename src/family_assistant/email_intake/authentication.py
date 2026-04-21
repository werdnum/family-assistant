"""Local DKIM/SPF/DMARC verification for inbound email webhooks.

Replaces the previous scheme of trusting Mailgun's parsed `dmarc`/`spf`/`dkim` form fields
and the `Authentication-Results` header. DKIM signatures are verified cryptographically
against DNS-published public keys and DMARC policy is evaluated locally.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from email.utils import getaddresses, parseaddr
from typing import TYPE_CHECKING, Protocol

from authheaders import check_dkim, check_dmarc, check_spf

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class DnsResolver(Protocol):
    """Resolve a TXT record for a domain name.

    Returns the TXT record value as a string (e.g. ``"v=DMARC1; p=reject"``) or ``None``
    when no record exists. The resolver must handle both lowercased and original-case
    domain names.
    """

    def __call__(self, name: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class EmailAuthenticationResult:
    """Normalised authentication result for an inbound email."""

    dkim: str
    """DKIM verification: ``"pass"``, ``"fail"``, ``"none"``, ``"temperror"``, ``"permerror"``."""

    spf: str
    """SPF verification using envelope sender + client IP. ``"none"`` when not evaluated."""

    dmarc: str
    """DMARC evaluation: combines DKIM/SPF alignment against the From: domain."""

    dmarc_policy: str
    """DMARC policy published by the From: domain (``"none"``/``"quarantine"``/``"reject"``)."""

    from_domain: str
    """Domain extracted from the ``From:`` header."""

    dkim_domain: str
    """Domain advertised in the verified DKIM signature (``d=`` tag). Empty if no DKIM."""

    details: str
    """Free-form details for logging (DMARC comment, SPF reason, etc.)."""

    @property
    def dmarc_passed(self) -> bool:
        return self.dmarc == "pass"

    @property
    def dkim_passed(self) -> bool:
        return self.dkim == "pass"

    @property
    def spf_passed(self) -> bool:
        return self.spf == "pass"


def verify_email_authentication(
    raw_mime: bytes,
    *,
    envelope_from: str | None = None,
    client_ip: str | None = None,
    helo: str | None = None,
    dns_resolver: DnsResolver | None = None,
) -> EmailAuthenticationResult:
    """Verify DKIM, SPF, and DMARC for a raw RFC 822 message.

    Args:
        raw_mime: The raw MIME message bytes, exactly as received upstream. DKIM hashing
            is byte-sensitive, so callers must avoid re-encoding the message.
        envelope_from: SMTP ``MAIL FROM`` address (typically ``form["sender"]`` from Mailgun).
            Required for SPF evaluation.
        client_ip: IP address of the peer SMTP client, if known. Required for SPF
            evaluation. When ``None`` or missing, SPF is reported as ``"none"``.
        helo: SMTP ``HELO``/``EHLO`` name, if known. Falls back to the envelope domain.
        dns_resolver: Optional injectable DNS resolver, used in tests. In production the
            underlying libraries query real DNS.

    Returns:
        :class:`EmailAuthenticationResult` with normalised statuses.
    """
    dkim_dnsfunc, dmarc_dnsfunc = _adapt_dns_resolver(dns_resolver)

    dkim_result = check_dkim(raw_mime, dnsfunc=dkim_dnsfunc)

    spf_result = None
    spf_status = "none"
    if client_ip and envelope_from:
        effective_helo = helo or _domain_of(envelope_from) or envelope_from
        try:
            spf_result = check_spf(client_ip, envelope_from, effective_helo)
        except Exception as exc:
            logger.warning("SPF evaluation error for %s: %s", envelope_from, exc)
            spf_result = None
        else:
            spf_status = spf_result.result or "none"

    dmarc_result = check_dmarc(
        raw_mime,
        spf_result=spf_result,
        dkim_result=dkim_result,
        dnsfunc=dmarc_dnsfunc,
    )

    dkim_status = dkim_result.result or "none"
    dmarc_status = dmarc_result.result or "none"

    return EmailAuthenticationResult(
        dkim=dkim_status,
        spf=spf_status,
        dmarc=dmarc_status,
        dmarc_policy=getattr(dmarc_result, "policy", "") or "",
        from_domain=getattr(dmarc_result, "header_from", "") or "",
        dkim_domain=getattr(dkim_result, "header_d", "") or "",
        details=str(getattr(dmarc_result, "result_comment", "") or ""),
    )


def extract_from_domain(raw_mime: bytes) -> str | None:
    """Return the domain portion of the RFC 5322 ``From:`` header, lowercased."""
    from_addresses = _from_addresses(raw_mime)
    if not from_addresses:
        return None
    domain = _domain_of(from_addresses[0])
    return domain.lower() if domain else None


def extract_client_ip(raw_mime: bytes) -> str | None:
    """Extract the peer SMTP client IP from Mailgun-provided inbound headers.

    Prefers the ``X-Mailgun-Sending-Ip``/``X-Envelope-From`` headers Mailgun adds to
    forwarded MIME. Falls back to the topmost ``Received:`` header, extracting the
    ``from ... (ip)`` portion.
    """
    headers = _parse_headers(raw_mime)

    for header_name in ("x-mailgun-sending-ip", "x-forwarded-for"):
        value = headers.get(header_name)
        if value:
            match = _IP_PATTERN.search(value)
            if match:
                return match.group(0)

    received_values = _all_headers(raw_mime, "received")
    for value in received_values:
        match = _RECEIVED_IP_PATTERN.search(value)
        if match:
            return match.group(1)
    return None


_IP_PATTERN = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-fA-F:]{2,}:[0-9a-fA-F:]*\b"
)
_RECEIVED_IP_PATTERN = re.compile(r"\[((?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F:]+)\]")


def _parse_headers(raw_mime: bytes) -> dict[str, str]:
    """Return a case-insensitive dict of headers (last value wins)."""
    headers: dict[str, str] = {}
    for name, value in _header_pairs(raw_mime):
        headers[name.lower()] = value
    return headers


def _all_headers(raw_mime: bytes, name: str) -> list[str]:
    """Return every value for a header name, in order of appearance."""
    target = name.lower()
    return [
        value for hname, value in _header_pairs(raw_mime) if hname.lower() == target
    ]


def _header_pairs(raw_mime: bytes) -> list[tuple[str, str]]:
    """Parse the header block into ``(name, value)`` pairs without touching the body."""
    try:
        text = raw_mime.decode("utf-8", errors="replace")
    except AttributeError:
        return []
    separator = _header_body_split(text)
    header_block = text[:separator]
    pairs: list[tuple[str, str]] = []
    current_name: str | None = None
    current_value: list[str] = []
    for line in header_block.splitlines():
        if not line:
            continue
        if line[0] in {" ", "\t"} and current_name is not None:
            current_value.append(line.strip())
            continue
        if current_name is not None:
            pairs.append((current_name, " ".join(current_value).strip()))
        name_part, _, value_part = line.partition(":")
        if not _:
            current_name = None
            current_value = []
            continue
        current_name = name_part.strip()
        current_value = [value_part.strip()]
    if current_name is not None:
        pairs.append((current_name, " ".join(current_value).strip()))
    return pairs


def _header_body_split(text: str) -> int:
    for marker in ("\r\n\r\n", "\n\n"):
        idx = text.find(marker)
        if idx != -1:
            return idx
    return len(text)


def _from_addresses(raw_mime: bytes) -> list[str]:
    values = _all_headers(raw_mime, "from")
    addresses: list[str] = []
    for value in values:
        for _, addr in getaddresses([value]):
            if addr:
                addresses.append(addr)
    return addresses


def _domain_of(address: str) -> str | None:
    _, parsed = parseaddr(address)
    if "@" not in parsed:
        return None
    return parsed.split("@", 1)[1].strip().lower() or None


def _adapt_dns_resolver(
    dns_resolver: DnsResolver | None,
) -> tuple[Callable[..., bytes | None] | None, Callable[..., str | None] | None]:
    if dns_resolver is None:
        return None, None

    def dkim_dnsfunc(name: bytes, timeout: int = 5) -> bytes | None:
        # dkimpy passes bytes names; authheaders expects str.
        lookup = (
            name.decode("ascii", errors="replace") if isinstance(name, bytes) else name
        )
        value = dns_resolver(lookup)
        if value is None:
            return None
        return value.encode("ascii", errors="replace")

    def dmarc_dnsfunc(name: str, *_args: object, **_kwargs: object) -> str | None:
        return dns_resolver(name)

    return dkim_dnsfunc, dmarc_dnsfunc
