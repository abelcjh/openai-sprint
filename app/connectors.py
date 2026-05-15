from __future__ import annotations

import email
import imaplib
import os
from dataclasses import dataclass
from email.message import Message
from typing import Iterable

import httpx

from .models import SourceType


IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
PDF_TYPES = {"application/pdf"}


@dataclass(frozen=True)
class ConnectorPayload:
    source_type: SourceType
    filename: str
    content: bytes
    text_hint: str = ""


@dataclass(frozen=True)
class ConnectorStatus:
    email_configured: bool
    whatsapp_webhook_configured: bool
    email_host: str
    email_user: str
    whatsapp_verify_token_set: bool


def connector_status() -> ConnectorStatus:
    return ConnectorStatus(
        email_configured=all(
            [
                os.getenv("EMAIL_IMAP_HOST"),
                os.getenv("EMAIL_IMAP_USER"),
                os.getenv("EMAIL_IMAP_PASSWORD"),
            ]
        ),
        whatsapp_webhook_configured=True,
        email_host=os.getenv("EMAIL_IMAP_HOST", ""),
        email_user=os.getenv("EMAIL_IMAP_USER", ""),
        whatsapp_verify_token_set=bool(os.getenv("WHATSAPP_VERIFY_TOKEN")),
    )


def fetch_email_payloads(limit: int = 5) -> list[ConnectorPayload]:
    host = _required_env("EMAIL_IMAP_HOST")
    user = _required_env("EMAIL_IMAP_USER")
    password = _required_env("EMAIL_IMAP_PASSWORD")
    mailbox = os.getenv("EMAIL_IMAP_MAILBOX", "INBOX")
    port = int(os.getenv("EMAIL_IMAP_PORT", "993"))
    search = os.getenv("EMAIL_IMAP_SEARCH", "UNSEEN")

    payloads: list[ConnectorPayload] = []
    with imaplib.IMAP4_SSL(host, port) as client:
        client.login(user, password)
        client.select(mailbox)
        _, ids_data = client.search(None, search)
        message_ids = ids_data[0].split()[-limit:]

        for message_id in message_ids:
            _, message_data = client.fetch(message_id, "(RFC822)")
            raw_message = next(
                part[1] for part in message_data if isinstance(part, tuple) and len(part) >= 2
            )
            message = email.message_from_bytes(raw_message)
            payloads.extend(_payloads_from_message(message))
            client.store(message_id, "+FLAGS", "\\Seen")
    return payloads


async def payloads_from_twilio_form(form: dict[str, str]) -> list[ConnectorPayload]:
    payloads: list[ConnectorPayload] = []
    body = (form.get("Body") or "").strip()
    sender = form.get("From", "whatsapp")

    if body:
        content = f"From: {sender}\n{body}".encode("utf-8")
        payloads.append(
            ConnectorPayload(
                source_type=SourceType.TEXT,
                filename="whatsapp-message.txt",
                content=content,
                text_hint=content.decode("utf-8"),
            )
        )

    media_count = int(form.get("NumMedia") or "0")
    for index in range(media_count):
        media_url = form.get(f"MediaUrl{index}")
        media_type = form.get(f"MediaContentType{index}", "")
        if not media_url:
            continue
        payloads.append(await _payload_from_media_url(media_url, media_type, f"whatsapp-media-{index}"))
    return payloads


async def payloads_from_meta_json(data: dict) -> list[ConnectorPayload]:
    payloads: list[ConnectorPayload] = []
    for message in _iter_meta_messages(data):
        text_body = message.get("text", {}).get("body", "")
        sender = message.get("from", "whatsapp")
        if text_body:
            content = f"From: {sender}\n{text_body}".encode("utf-8")
            payloads.append(
                ConnectorPayload(
                    source_type=SourceType.TEXT,
                    filename="whatsapp-cloud-message.txt",
                    content=content,
                    text_hint=content.decode("utf-8"),
                )
            )

        for media_key in ("image", "document"):
            media = message.get(media_key) or {}
            media_url = media.get("url")
            media_type = media.get("mime_type", "")
            filename = media.get("filename") or f"whatsapp-{media_key}"
            if media_url:
                payloads.append(await _payload_from_media_url(media_url, media_type, filename))
    return payloads


def _payloads_from_message(message: Message) -> list[ConnectorPayload]:
    payloads: list[ConnectorPayload] = []
    text_parts: list[str] = []

    for part in _walk_parts(message):
        content_type = part.get_content_type()
        filename = part.get_filename() or ""
        disposition = part.get_content_disposition()

        if content_type == "text/plain" and disposition != "attachment":
            text_parts.append(_decode_text_part(part))
            continue

        if content_type in PDF_TYPES or filename.casefold().endswith(".pdf"):
            content = part.get_payload(decode=True) or b""
            if content:
                payloads.append(
                    ConnectorPayload(
                        source_type=SourceType.PDF,
                        filename=filename or "email-invoice.pdf",
                        content=content,
                    )
                )
        elif content_type in IMAGE_TYPES:
            content = part.get_payload(decode=True) or b""
            if content:
                extension = content_type.split("/")[-1].replace("jpeg", "jpg")
                payloads.append(
                    ConnectorPayload(
                        source_type=SourceType.IMAGE,
                        filename=filename or f"email-invoice.{extension}",
                        content=content,
                    )
                )

    if text_parts and not payloads:
        text = "\n".join(part.strip() for part in text_parts if part.strip())
        payloads.append(
            ConnectorPayload(
                source_type=SourceType.TEXT,
                filename="email-message.txt",
                content=text.encode("utf-8"),
                text_hint=text,
            )
        )
    return payloads


def _walk_parts(message: Message) -> Iterable[Message]:
    if message.is_multipart():
        yield from message.walk()
    else:
        yield message


def _decode_text_part(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset)
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


async def _payload_from_media_url(media_url: str, media_type: str, fallback_name: str) -> ConnectorPayload:
    headers = {}
    token = os.getenv("WHATSAPP_MEDIA_AUTH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(media_url, headers=headers)
        response.raise_for_status()

    source_type = SourceType.PDF if media_type in PDF_TYPES or media_url.casefold().endswith(".pdf") else SourceType.IMAGE
    extension = _extension_for_media(media_type, source_type)
    filename = fallback_name if "." in fallback_name else f"{fallback_name}.{extension}"
    return ConnectorPayload(source_type=source_type, filename=filename, content=response.content)


def _extension_for_media(media_type: str, source_type: SourceType) -> str:
    if source_type == SourceType.PDF:
        return "pdf"
    if media_type == "image/png":
        return "png"
    if media_type == "image/webp":
        return "webp"
    if media_type == "image/gif":
        return "gif"
    return "jpg"


def _iter_meta_messages(data: dict) -> Iterable[dict]:
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                yield message


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required for email connector ingestion.")
    return value
