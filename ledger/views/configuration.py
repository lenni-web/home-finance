from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from ..forms import AccountForm, EmailImportConfigForm
from ..models import Account, AppSettings, EmailImportConfig, EmailImportMessage
from ..tasks import poll_email_import_task


@login_required
def settings(request):
    email_config, _ = EmailImportConfig.objects.get_or_create(pk=1)
    return render(request, "ledger/settings.html", {
        "accounts": Account.objects.annotate(
            statement_count=Count("statementimport")
        ).order_by("name"),
        "account_form": AccountForm(),
        "email_form": EmailImportConfigForm(instance=email_config, prefix="email"),
        "email_config": email_config,
        "email_imports": EmailImportMessage.objects.filter(config=email_config)[:10],
    })


@login_required
def save_email_settings(request):
    if request.method != "POST":
        return redirect("settings")
    config, _ = EmailImportConfig.objects.get_or_create(pk=1)
    form = EmailImportConfigForm(request.POST, instance=config, prefix="email")
    if not form.is_valid():
        accounts = Account.objects.annotate(statement_count=Count("statementimport")).order_by("name")
        return render(request, "ledger/settings.html", {
            "accounts": accounts, "account_form": AccountForm(),
            "email_form": form, "email_config": config,
            "email_imports": EmailImportMessage.objects.filter(config=config)[:10],
        })
    config = form.save()
    action = request.POST.get("action")
    if action == "test":
        from ..email_import import test_imap_connection
        try:
            test_imap_connection(config)
            messages.success(request, "IMAP-Verbindung und Ordner wurden erfolgreich geprüft.")
        except Exception as exc:
            messages.error(request, f"IMAP-Verbindung fehlgeschlagen: {exc}")
    elif action == "fetch":
        poll_email_import_task.delay(config.pk, force=True)
        messages.success(request, "Der manuelle E-Mail-Abruf wurde gestartet.")
    else:
        messages.success(request, "E-Mail-Importeinstellungen wurden gespeichert.")
    return redirect("settings")


@login_required
def add_account(request):
    if request.method != "POST":
        return redirect("settings")
    form = AccountForm(request.POST)
    if form.is_valid():
        account = form.save()
        messages.success(request, f"Konto „{account.name}“ wurde angelegt und kann ausgewählt werden.")
    else:
        details = " ".join(error for errors in form.errors.values() for error in errors)
        messages.error(request, f"Das Konto konnte nicht angelegt werden: {details}")
    return redirect("settings")


@login_required
def edit_account(request, pk):
    account = get_object_or_404(Account, pk=pk)
    form = AccountForm(request.POST or None, instance=account)
    if request.method == "POST" and form.is_valid():
        updated = form.save()
        messages.success(request, f"Konto „{updated.name}“ wurde aktualisiert.")
        return redirect("settings")
    return render(request, "ledger/edit_account.html", {"form": form, "account": account})


@login_required
@require_POST
def toggle_presentation_mode(request):
    """Schaltet den Präsentationsmodus um und kehrt zur aufrufenden Seite zurück."""
    app_settings = AppSettings.current()
    app_settings.presentation_mode = not app_settings.presentation_mode
    app_settings.save(update_fields=["presentation_mode"])
    messages.success(
        request,
        "Präsentationsmodus eingeschaltet: Beträge werden als Prozentwerte angezeigt."
        if app_settings.presentation_mode else "Präsentationsmodus ausgeschaltet.",
    )
    target = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        target = "settings"
    return redirect(target)
