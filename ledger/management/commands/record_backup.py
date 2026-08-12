from pathlib import Path

from django.core.management.base import BaseCommand

from ledger.models import BackupRecord


class Command(BaseCommand):
    help = "Protokolliert ein erfolgreich erstelltes Produktionsbackup."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--size", type=int, required=True)
        parser.add_argument("--encrypted", action="store_true")
        parser.add_argument("--revision", default="")

    def handle(self, *args, **options):
        path = Path(options["path"])
        BackupRecord.objects.create(
            filename=path.name,
            size_bytes=options["size"],
            encrypted=options["encrypted"],
            git_revision=options["revision"],
        )
        self.stdout.write(self.style.SUCCESS(f"Backup protokolliert: {path.name}"))
