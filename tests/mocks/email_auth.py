"""Helpers for building DKIM-signed MIME messages and fake DNS resolvers in tests."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from functools import lru_cache

import dkim
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@dataclass(frozen=True, slots=True)
class TestKeyPair:
    """An RSA key pair usable for DKIM signing and a DKIM TXT record value."""

    private_pem: bytes
    public_b64: str

    @property
    def dkim_record(self) -> str:
        return f"v=DKIM1; k=rsa; p={self.public_b64}"


@lru_cache(maxsize=1)
def default_test_key() -> TestKeyPair:
    """Return a cached 2048-bit RSA key pair for test signing."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_b64 = base64.b64encode(public_der).decode("ascii")
    return TestKeyPair(private_pem=private_pem, public_b64=public_b64)


class FakeDnsResolver:
    """In-memory DNS resolver. Maps domain names to TXT record values."""

    def __init__(self, records: dict[str, str] | None = None) -> None:
        self._records: dict[str, str] = {}
        if records:
            for name, value in records.items():
                self.add(name, value)

    def add(self, name: str, value: str) -> None:
        self._records[name.lower().rstrip(".")] = value

    def __call__(self, name: str) -> str | None:
        return self._records.get(name.lower().rstrip("."))


def build_signed_message(
    *,
    from_address: str = "alice@example.com",
    to_address: str = "orders@example.net",
    subject: str = "Order confirmation",
    body: str = "Your order is confirmed.",
    message_id: str | None = None,
    date: str = "Mon, 21 Apr 2026 12:00:00 +0000",
    domain: str | None = None,
    selector: str = "test",
    key_pair: TestKeyPair | None = None,
    extra_headers: list[tuple[str, str]] | None = None,
) -> bytes:
    """Build a DKIM-signed RFC 822 message.

    Signs over the common headers (From, To, Subject, Date, Message-ID) using simple
    canonicalization compatible with dkimpy's verifier.
    """
    if key_pair is None:
        key_pair = default_test_key()
    if domain is None:
        domain = from_address.split("@", 1)[1]
    if message_id is None:
        message_id = f"<test-{abs(hash((from_address, subject, body)))}@{domain}>"

    lines = [
        f"From: {from_address}",
        f"To: {to_address}",
        f"Subject: {subject}",
        f"Date: {date}",
        f"Message-ID: {message_id}",
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=UTF-8",
    ]
    if extra_headers:
        for name, value in extra_headers:
            lines.append(f"{name}: {value}")
    lines.append("")
    lines.append(body)
    lines.append("")

    raw = "\r\n".join(lines).encode("utf-8")

    signature = dkim.sign(
        message=raw,
        selector=selector.encode("ascii"),
        domain=domain.encode("ascii"),
        privkey=key_pair.private_pem,
        include_headers=[b"From", b"To", b"Subject", b"Date", b"Message-ID"],
    )
    return signature + raw


def build_dns_for(
    *,
    domain: str = "example.com",
    selector: str = "test",
    dmarc_policy: str = "reject",
    dmarc_adkim: str = "r",
    dmarc_aspf: str = "r",
    key_pair: TestKeyPair | None = None,
) -> FakeDnsResolver:
    """Build a :class:`FakeDnsResolver` with DKIM and DMARC records for ``domain``."""
    if key_pair is None:
        key_pair = default_test_key()

    records = {
        f"{selector}._domainkey.{domain}": key_pair.dkim_record,
        f"_dmarc.{domain}": (
            f"v=DMARC1; p={dmarc_policy}; adkim={dmarc_adkim}; aspf={dmarc_aspf}"
        ),
    }
    return FakeDnsResolver(records)
