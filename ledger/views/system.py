import shutil
from datetime import timedelta

from django.conf import settings as django_settings
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from ..models import BackupRecord, Document, EmailImportConfig, ServiceHeartbeat


@login_required
def operational_status(request):
    now = timezone.now()
    worker = ServiceHeartbeat.objects.filter(name="worker").first()
    email_heartbeat = ServiceHeartbeat.objects.filter(name="email_import").first()
    latest_backup = BackupRecord.objects.first()
    email_config = EmailImportConfig.objects.first()
    failed_documents = Document.objects.filter(
        processing_status=Document.ProcessingStatus.FAILED
    ).count()
    disk = shutil.disk_usage(django_settings.MEDIA_ROOT)
    database_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        database_ok = False

    worker_ok = bool(worker and worker.last_seen_at >= now - timedelta(minutes=3))
    backup_ok = bool(latest_backup and latest_backup.created_at >= now - timedelta(hours=26))
    email_ok = not email_config or not email_config.enabled or (
        not email_config.last_error
        and email_config.last_success_at
        and email_config.last_success_at >= now - timedelta(
            minutes=max(email_config.poll_interval_minutes * 3, 15)
        )
    )
    checks = [
        ("Datenbank", database_ok, "Verbindung erfolgreich" if database_ok else "Nicht erreichbar"),
        (
            "Hintergrund-Worker", worker_ok,
            "Letztes Signal "
            f"{timezone.localtime(worker.last_seen_at):%d.%m.%Y %H:%M}"
            if worker else "Noch kein Signal",
        ),
        (
            "Automatisches Backup", backup_ok,
            f"{latest_backup.filename} · "
            f"{timezone.localtime(latest_backup.created_at):%d.%m.%Y %H:%M}"
            if latest_backup else "Noch kein protokolliertes Backup",
        ),
        (
            "E-Mail-Import", email_ok,
            "Nicht aktiviert" if not email_config or not email_config.enabled
            else (email_config.last_error or "Letzter Abruf erfolgreich"),
        ),
    ]
    return render(request, "ledger/operational_status.html", {
        "checks": checks,
        "all_ok": all(item[1] for item in checks),
        "failed_documents": failed_documents,
        "disk_free_gb": disk.free / (1024 ** 3),
        "disk_percent_free": disk.free / disk.total * 100,
        "revision": django_settings.DEPLOY_REVISION,
        "version": django_settings.DEPLOY_TAG,
        "worker": worker,
        "email_heartbeat": email_heartbeat,
        "latest_backup": latest_backup,
    })


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return HttpResponse("database unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
