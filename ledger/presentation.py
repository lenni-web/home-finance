"""Präsentationsmodus: Euro-Beträge ausblenden und Prozentwerte zeigen."""

from functools import wraps

from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import redirect

from .models import AppSettings


def is_enabled():
    return AppSettings.current().presentation_mode


def presentation_base(income, expenses):
    """Bezugsgröße (= 100 %): Einnahmen des Zeitraums, ersatzweise die Ausgaben."""
    if income and income > 0:
        return income, "Einnahmen"
    if expenses:
        return abs(expenses), "Ausgaben"
    return None, ""


def base_context(income, expenses):
    base, label = presentation_base(income, expenses)
    return {"presentation_base": base, "presentation_base_label": label}


def hidden_in_presentation(*, download=False):
    """Sperrt Seiten, die Originalbelege oder bearbeitbare Beträge zeigen."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if is_enabled():
                if download:
                    return HttpResponseForbidden(
                        "Im Präsentationsmodus sind Originaldokumente nicht verfügbar."
                    )
                messages.info(
                    request,
                    "Diese Seite zeigt Originalbeträge und ist im Präsentationsmodus gesperrt.",
                )
                return redirect("dashboard")
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
