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
docker compose run --rm \
  -v "/lokaler/ordner/mit/pdf:/input:ro" \
  web python tools/sanitize_statement.py \
  "/input/Kontoauszug.pdf" sanitized/ing-layout.json
```

Ohne Docker in einer Python-Umgebung mit installierten Projektabhängigkeiten:

```bash
python tools/sanitize_statement.py \
  "/lokaler/pfad/Kontoauszug.pdf" sanitized/ing-layout.json
```

Die erzeugte JSON-Datei vor dem Weitergeben immer noch einmal manuell durchsuchen.
Der Ordner `sanitized/` ist absichtlich von Git ausgeschlossen.

Die anonymisierte Struktur lässt sich anschließend lokal prüfen:

```bash
python -m tools.inspect_ing_fixture sanitized/ing-layout.json \
  --output sanitized/ing-transactions.json
```

Der ING-Parser verwendet die Lesereihenfolge des PDFs und sichert Buchungs- und
Valutadatum zusätzlich über die Position der linken Datumsspalte ab. Informationsseiten
ohne diese Struktur werden ignoriert.

## Echten ING-Auszug lokal validieren

Das folgende Kommando gibt standardmäßig nur Anzahl, Seitenverteilung und die
laufenden Nummern unvollständiger Buchungen aus:

```bash
python -m tools.validate_ing_pdf "/lokaler/pfad/Kontoauszug.pdf"
```

Optional kann eine private JSON-Kontrollliste erzeugt werden. Sie enthält echte
Buchungsdaten, erhält Dateirechte `0600` und gehört ausschließlich in `sanitized/`:

```bash
python -m tools.validate_ing_pdf "/lokaler/pfad/Kontoauszug.pdf" \
  --output sanitized/private-validation.json
```

## Kontoauszug über die Weboberfläche importieren

1. Auf der Startseite unter `Konto anlegen` einen frei gewählten Kontonamen erfassen.
2. Auf der Startseite Dokumenttyp `Kontoauszug` und das Konto auswählen.
3. Das ING-PDF hochladen; die Verarbeitung erfolgt lokal und synchron.
4. Erkannte Buchungen in der Kontrolltabelle korrigieren oder zwischenspeichern.
5. Erst `Alle Buchungen bestätigen` markiert den Auszug als importiert.

Bereits hochgeladene identische Dateien werden über ihren SHA-256-Hash abgewiesen.

## Buchungen kategorisieren

Unter `/transactions/` stehen ausschließlich bestätigte Buchungen zur Verfügung.
Die Ansicht bietet Monats-, Konto-, Kategorie-, Tag-, Personen- und Textfilter sowie
Summen für Einnahmen, Ausgaben und Differenz.

Zuordnungen lassen sich entweder direkt je Tabellenzeile oder gesammelt für markierte
Buchungen speichern. Bei einer Sammelzuordnung können aus den Zahlungspartnern Regeln
angelegt werden. Automatische Regeln werden bei späteren Kontoauszügen direkt angewendet;
nicht automatische Regeln erscheinen als sichtbarer Vorschlag.

Unter `/settings/classification/` können Kategorien, Tags, Personen und Regeln angelegt
sowie aktiviert oder deaktiviert werden. Eine Deaktivierung entfernt keine bestehenden
Zuordnungen.

## Rechnungen und Belege

PDF-, JPG- und PNG-Belege werden beim Upload ausschließlich lokal verarbeitet. Digitale
PDFs werden direkt gelesen; für Scans und Fotos verwendet der Container Tesseract mit
deutscher und englischer Sprache sowie OCRmyPDF. Das Original bleibt unverändert.

Datum, Händler und Gesamtbetrag sind Vorschläge und werden vor dem Abschluss auf einer
Prüfseite angezeigt. Dort können außerdem Kategorie, Tags und Personen vergeben sowie
passende Kontobewegungen verknüpft werden. Kandidaten werden anhand eines Zeitfensters
von sieben Tagen und – sofern erkannt – des Betrags eingeschränkt.

Unter `/documents/` steht das nach Monat sortierte Archiv mit Volltextsuche, Filtern,
Vorschau und Download zur Verfügung. Die OCR läuft im aktuellen Prototyp synchron; bei
größeren Dokumenten kann der Upload deshalb einige Zeit benötigen.

Die Reihenfolge von Beleg und Kontoauszug ist unerheblich: Wird ein Kontoauszug erst
später bestätigt, prüft die Anwendung alle bisher unverknüpften Belege mit erkanntem
Datum und Betrag erneut. Mögliche Treffer werden als `Prüfung erforderlich` markiert,
aber aus Sicherheitsgründen nicht ohne Bestätigung fest verknüpft.
