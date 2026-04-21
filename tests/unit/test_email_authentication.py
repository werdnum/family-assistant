"""Unit tests for email authentication helpers.

Focuses on the header parsing paths used for client-IP extraction: we must not
trust attacker-controllable headers like ``X-Forwarded-For`` because that would
let a spoofed inbound email influence SPF evaluation.
"""

from __future__ import annotations

from family_assistant.email_intake.authentication import extract_client_ip


def test_extract_client_ip_prefers_x_mailgun_sending_ip() -> None:
    raw = (
        b"Received: from mail.example.com ([198.51.100.10])\r\n"
        b"\tby mx.mailgun.net with ESMTP; Mon, 21 Apr 2026 12:00:00 +0000\r\n"
        b"X-Mailgun-Sending-Ip: 203.0.113.7\r\n"
        b"From: buyer@example.com\r\n"
        b"\r\n"
        b"body\r\n"
    )

    assert extract_client_ip(raw) == "203.0.113.7"


def test_extract_client_ip_falls_back_to_received_header() -> None:
    raw = (
        b"Received: from mail.example.com ([198.51.100.10])\r\n"
        b"\tby mx.mailgun.net with ESMTP; Mon, 21 Apr 2026 12:00:00 +0000\r\n"
        b"From: buyer@example.com\r\n"
        b"\r\n"
        b"body\r\n"
    )

    assert extract_client_ip(raw) == "198.51.100.10"


def test_extract_client_ip_ignores_forged_x_forwarded_for() -> None:
    """A forged X-Forwarded-For header must not influence the extracted client IP.

    Inbound email headers are attacker-controllable: any sender can stamp their own
    ``X-Forwarded-For`` into a message. Trusting it would let attackers spoof the
    client IP used for SPF evaluation and bypass authentication.
    """
    raw = (
        b"X-Forwarded-For: 192.0.2.50\r\n"
        b"Received: from mail.example.com ([198.51.100.10])\r\n"
        b"\tby mx.mailgun.net with ESMTP; Mon, 21 Apr 2026 12:00:00 +0000\r\n"
        b"From: attacker@example.com\r\n"
        b"\r\n"
        b"body\r\n"
    )

    assert extract_client_ip(raw) == "198.51.100.10"


def test_extract_client_ip_ignores_forged_x_forwarded_for_without_received() -> None:
    """Without a Received header we still refuse the attacker-controllable XFF header."""
    raw = b"X-Forwarded-For: 192.0.2.50\r\nFrom: attacker@example.com\r\n\r\nbody\r\n"

    assert extract_client_ip(raw) is None


def test_extract_client_ip_returns_none_when_no_sources() -> None:
    raw = b"From: buyer@example.com\r\n\r\nbody\r\n"

    assert extract_client_ip(raw) is None
