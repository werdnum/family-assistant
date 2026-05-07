from family_assistant.web.routers import webhooks


def test_populate_missing_body_fields_converts_html_only_mime_to_markdown() -> None:
    html = (
        "<html><body>"
        "<h1>Order update</h1>"
        "<p>Hello <strong>Alice</strong>.</p>"
        '<p><a href="https://example.com/details">Details</a></p>'
        "</body></html>"
    )
    raw_mime = (
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n" + html.encode()
    )
    form_data: dict[str, object] = {}

    webhooks._populate_missing_body_fields_from_mime(form_data, raw_mime)

    assert form_data["body-html"] == html
    assert form_data["stripped-html"] == html
    assert form_data["body-plain"] == (
        "# Order update\n\nHello **Alice**.\n\n[Details](https://example.com/details)"
    )
    assert form_data["stripped-text"] == form_data["body-plain"]


def test_populate_missing_body_fields_preserves_mailgun_parsed_text() -> None:
    html = "<p>Raw MIME HTML body</p>"
    raw_mime = (
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n" + html.encode()
    )
    form_data: dict[str, object] = {
        "body-plain": "Mailgun parsed body",
        "stripped-text": "Mailgun stripped body",
    }

    webhooks._populate_missing_body_fields_from_mime(form_data, raw_mime)

    assert form_data["body-plain"] == "Mailgun parsed body"
    assert form_data["stripped-text"] == "Mailgun stripped body"
    assert form_data["body-html"] == html
    assert form_data["stripped-html"] == html


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
