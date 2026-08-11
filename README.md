# Ausgabenarchiv – Prototyp

Lokale Django-Anwendung für Kontoauszüge als PDF, Rechnungen, Kassenbelege,
Kategorien, Tags und Personenzuordnungen.

## Entwicklungsprinzip

- Git enthält Quellcode, Datenbankschema und Beispielkonfiguration.
- Django-Migrationen übertragen jede Schemaänderung reproduzierbar.
- `media/`, PostgreSQL-Daten und `.env` werden niemals eingecheckt.
- Releases erhalten Tags wie `v0.1.0`; Server-Updates erfolgen nur auf ein solches Tag.
- Dokumente und Datenbank werden gemeinsam gesichert und wiederhergestellt.

## Lokal starten

```bash
cp .env.example .env
# Passwörter und Secret in .env ändern
docker compose up --build
```

Danach liegt die Oberfläche unter `http://localhost:8000`. Für die Verwaltung:

```bash
docker compose exec web python manage.py createsuperuser
```

## Übertragung auf Debian

1. Git-Repository auf den Server klonen.
2. `.env` nur auf dem Server anlegen.
3. `docker compose up -d --build` ausführen.
4. Bei Updates zuerst Backup erstellen, dann das gewünschte Git-Tag auschecken.
5. `docker compose up -d --build`; Migrationen werden beim Start angewendet.

Produktiv kommen HTTPS, ein Reverse Proxy, regelmäßige Backups und ein separater
Hintergrundprozess für OCR hinzu.

## Nächster Entwicklungsschritt

Der Kontoauszug-Importer erhält pro Bank einen versionierten Parser. Extrahierte
Buchungen landen zunächst im Prüfstatus und werden erst nach Bestätigung übernommen.

## Kontoauszug lokal anonymisieren

Das Original-PDF muss für die Parserentwicklung nicht weitergegeben werden. Das
Hilfsprogramm liest es lokal und erzeugt eine datensparsame JSON-Testdatei mit
Seitenmaßen und Textpositionen. Freie Texte werden pseudonymisiert; Datumswerte und
Beträge werden durch synthetische Werte ersetzt. PDF-Metadaten, Bilder und Anhänge
werden nicht übernommen.

Innerhalb des Containers:

```bash
mkdir -p sanitized
docker compose run --rm web python tools/sanitize_statement.py \
  /pfad/im/container/kontoauszug.pdf sanitized/ing-layout.json
```

Ohne Docker in einer Python-Umgebung mit installierten Projektabhängigkeiten:

```bash
python tools/sanitize_statement.py \
  "/lokaler/pfad/Kontoauszug.pdf" sanitized/ing-layout.json
```

Die erzeugte JSON-Datei vor dem Weitergeben immer noch einmal manuell durchsuchen.
Der Ordner `sanitized/` ist absichtlich von Git ausgeschlossen.
