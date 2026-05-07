from family_assistant.web.routers import webhooks


def test_extract_mime_body_content_skips_undecodable_parts() -> None:
    raw_mime = (
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="boundary"\r\n'
        b"\r\n"
        b"--boundary\r\n"
        b"Content-Type: text/plain; charset=not-a-real-charset\r\n"
        b"\r\n"
        b"Plain text that cannot be decoded with declared charset.\r\n"
        b"--boundary\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<p>HTML body still decodes.</p>\r\n"
        b"--boundary--\r\n"
    )

    content = webhooks._extract_mime_body_content(raw_mime)

    assert content.plain is None
    assert content.html is not None
    assert content.html.strip() == "<p>HTML body still decodes.</p>"
