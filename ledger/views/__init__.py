"""View-Funktionen, nach Themen auf Module verteilt; ``urls.py`` greift hier zu."""

from .budgets import budgets, delete_budget
from .classification import (
    delete_rule, edit_classification, edit_rule, manage_classification, toggle_classification,
)
from .configuration import add_account, edit_account, save_email_settings, settings
from .documents import (
    automatic_matches, delete_failed_document, document_archive, document_download,
    document_review, retry_failed_document, revoke_automatic_match, upload_document,
)
from .reports import analytics, dashboard, open_tasks
from .statements import statement_review
from .system import health, operational_status
from .transactions import transaction_overview

__all__ = [
    "budgets", "delete_budget",
    "add_account", "analytics", "automatic_matches", "dashboard", "delete_failed_document",
    "delete_rule", "document_archive", "document_download", "document_review", "edit_account",
    "edit_classification", "edit_rule", "health", "manage_classification", "open_tasks",
    "operational_status", "retry_failed_document", "revoke_automatic_match",
    "save_email_settings", "settings", "statement_review", "toggle_classification",
    "transaction_overview", "upload_document",
]
