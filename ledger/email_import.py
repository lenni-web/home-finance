import base64
import hashlib
import imaplib
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from .models import Document, EmailImportConfig, EmailImportMessage


ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png"}
MAX_ATTACHMENT_SIZE = 30 * 1024 * 1024


def _cipher():
    raw_key = str(settings.EMAIL_CREDENTIAL_KEY).encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(raw_key).digest())
    return Fernet(key)


def encrypt_password(password):
    return _cipher().encrypt(password.encode("utf-8")).decode("ascii")


def decrypt_password(value):
    try:
        return _cipher().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "Das IMAP-Passwort kann nicht entschlüsselt werden. "
            "Wurde EMAIL_CREDENTIAL_KEY geändert?"
        ) from exc


def open_imap(config):
    if config.security == EmailImportConfig.Security.SSL:
        client = imaplib.IMAP4_SSL(config.host, config.port, timeout=20)
    else:
        client = imaplib.IMAP4(config.host, config.port, timeout=20)
        client.starttls()
    client.login(config.username, config.get_password())
    return client


def test_imap_connection(config):
    client = open_imap(config)
    try:
        status, _ = client.select(config.folder, readonly=True)
        if status != "OK":
            raise ValueError(f"IMAP-Ordner „{config.folder}“ konnte nicht geöffnet werden.")
    finally:
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass


def _allowed_sender(config, sender):
    allowed = {
        line.strip().casefold()
        for line in config.allowed_senders.replace(",", "\n").splitlines()
        if line.strip()
    }
    return not allowed or sender.casefold() in allowed


def _attachment_kind(content_type):
    return Document.Kind.INVOICE if content_type == "application/pdf" else Document.Kind.RECEIPT


def _import_message(config, uid, raw_message):
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    sender = parseaddr(message.get("From", ""))[1]
    if not _allowed_sender(config, sender):
        EmailImportMessage.objects.create(
            config=config, mailbox_uid=uid, message_id=message.get("Message-ID", ""),
            sender=sender, subject=str(message.get("Subject", "")),
            error_message="Absender ist nicht freigegeben.",
        )
        return 0

    attachments = []
    for part in message.iter_attachments():
        filename = Path(part.get_filename() or "anhang").name[:255]
        content_type = part.get_content_type().lower()
        payload = part.get_payload(decode=True) or b""
        if content_type not in ALLOWED_TYPES or not payload or len(payload) > MAX_ATTACHMENT_SIZE:
            continue
        digest = hashlib.sha256(payload).hexdigest()
        if Document.objects.filter(sha256=digest).exists():
            continue
        attachments.append((filename, content_type, payload))

    with transaction.atomic():
        log = EmailImportMessage.objects.create(
            config=config, mailbox_uid=uid, message_id=message.get("Message-ID", ""),
            sender=sender, subject=str(message.get("Subject", "")),
            attachment_count=len(attachments),
        )
        for filename, content_type, payload in attachments:
            document = Document(
                kind=_attachment_kind(content_type),
                title=str(message.get("Subject", ""))[:255],
                original_filename=filename,
                processing_status=Document.ProcessingStatus.PENDING,
            )
            document.file.save(filename, ContentFile(payload), save=True)
            from .tasks import process_document_task
            transaction.on_commit(lambda pk=document.pk: process_document_task.delay(pk))
    return log.attachment_count


def poll_mailbox(config, *, force=False):
    if not config.enabled and not force:
        return 0
    now = timezone.now()
    if not force and config.last_checked_at:
        due_at = config.last_checked_at + timedelta(minutes=config.poll_interval_minutes)
        if due_at > now:
            return 0
    config.last_checked_at = now
    config.save(update_fields=["last_checked_at", "updated_at"])
    imported = 0
    client = None
    try:
        client = open_imap(config)
        status, _ = client.select(config.folder)
        if status != "OK":
            raise ValueError(f"IMAP-Ordner „{config.folder}“ konnte nicht geöffnet werden.")
        status, data = client.uid("search", None, "UNSEEN")
        if status != "OK":
            raise ValueError("Ungelesene E-Mails konnten nicht abgefragt werden.")
        for raw_uid in (data[0] or b"").split():
            uid = raw_uid.decode("ascii")
            if EmailImportMessage.objects.filter(config=config, mailbox_uid=uid).exists():
                continue
            status, fetched = client.uid("fetch", raw_uid, "(BODY.PEEK[])")
            if status != "OK":
                continue
            raw_message = next(
                (item[1] for item in fetched if isinstance(item, tuple) and len(item) > 1), None
            )
            if raw_message:
                imported += _import_message(config, uid, raw_message)
                if config.mark_as_read:
                    client.uid("store", raw_uid, "+FLAGS", "(\\Seen)")
        config.last_success_at = timezone.now()
        config.last_error = ""
        config.save(update_fields=["last_success_at", "last_error", "updated_at"])
        return imported
    except Exception as exc:
        config.last_error = str(exc)[:2000]
        config.save(update_fields=["last_error", "updated_at"])
        raise
    finally:
        if client is not None:
            try:
                client.logout()
            except imaplib.IMAP4.error:
                pass
