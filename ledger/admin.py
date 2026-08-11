from django.contrib import admin

from .models import Account, Category, Document, DocumentPerson, Person, StatementImport, Tag, Transaction


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("document_date", "kind", "title", "merchant", "total_amount", "category")
    list_filter = ("kind", "category", "tags", "people")
    search_fields = ("title", "merchant", "original_filename", "extracted_text")
    date_hierarchy = "document_date"


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("booking_date", "counterparty", "amount", "category", "reviewed")
    list_filter = ("reviewed", "category", "tags", "people")
    search_fields = ("counterparty", "description")
    date_hierarchy = "booking_date"


admin.site.register([Account, Category, Tag, Person, StatementImport, DocumentPerson])

