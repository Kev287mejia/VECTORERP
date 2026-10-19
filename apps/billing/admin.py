from django.contrib import admin
from apps.core.admin_mixins import TenantAdminMixin
from apps.billing.models import Invoice, InvoiceItem, PaymentReceipt, PaymentIdempotencyRecord


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0


class PaymentReceiptInline(admin.TabularInline):
    model = PaymentReceipt
    extra = 0
    can_delete = False
    readonly_fields = (
        'number', 'payment_date', 'payment_method', 'bank_account',
        'reference_number', 'currency_paid', 'amount_in_payment_currency',
        'amount_applied_to_invoice'
    )

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Invoice)
class InvoiceAdmin(TenantAdminMixin, admin.ModelAdmin):
    list_display = ('number', 'account', 'status', 'issue_date', 'currency_code', 'gross_total', 'net_receivable', 'balance_due')
    list_filter = ('status', 'currency_code', 'payment_condition', 'issue_date')
    search_fields = ('number', 'account__commercial_name', 'account__tax_id')
    inlines = [InvoiceItemInline, PaymentReceiptInline]


@admin.register(PaymentReceipt)
class PaymentReceiptAdmin(TenantAdminMixin, admin.ModelAdmin):
    """
    Consola de Auditoría y Consulta de Recibos Oficiales de Caja (ROC).
    Impedida la creación, edición o eliminación administrativa directa para salvaguardar
    la integridad estricta del libro mayor de tesorería y pólizas contables.
    """
    list_display = ('number', 'invoice', 'bank_account', 'payment_date', 'payment_method', 'currency_paid', 'amount_in_payment_currency', 'amount_applied_to_invoice')
    list_filter = ('payment_method', 'currency_paid', 'payment_date')
    search_fields = ('number', 'invoice__number', 'reference_number')
    readonly_fields = [f.name for f in PaymentReceipt._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PaymentIdempotencyRecord)
class PaymentIdempotencyRecordAdmin(TenantAdminMixin, admin.ModelAdmin):
    """
    Auditoría de Claves de Idempotencia y Reintentos Transaccionales.
    Modo estrictamente de solo lectura.
    """
    list_display = ('idempotency_key', 'organization', 'invoice', 'payment_receipt', 'status', 'created_at')
    search_fields = ('idempotency_key', 'invoice__number', 'request_hash')
    readonly_fields = [f.name for f in PaymentIdempotencyRecord._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False
