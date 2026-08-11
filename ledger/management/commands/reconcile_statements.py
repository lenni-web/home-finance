from django.core.management.base import BaseCommand

from ledger.models import StatementImport
from ledger.statement_reconciliation import recalculate_statement


class Command(BaseCommand):
    help = "Berechnet Anfangs-/Endsaldo und Abstimmungsstatus vorhandener Kontoauszüge neu."

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, dest="statement_id")

    def handle(self, *args, **options):
        statements = StatementImport.objects.select_related("document").order_by("pk")
        if options["statement_id"]:
            statements = statements.filter(pk=options["statement_id"])
        processed = 0
        failed = 0
        for statement in statements:
            try:
                recalculate_statement(statement)
                processed += 1
                self.stdout.write(
                    f"Import {statement.pk}: {statement.get_reconciliation_status_display()}"
                )
            except Exception as exc:
                failed += 1
                self.stderr.write(f"Import {statement.pk}: fehlgeschlagen ({exc})")
        self.stdout.write(self.style.SUCCESS(
            f"Abgeschlossen: {processed} geprüft, {failed} fehlgeschlagen."
        ))
