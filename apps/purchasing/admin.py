from django.contrib import admin
from apps.purchasing.models import Supplier, PurchaseOrder, PurchaseOrderItem, SupplierInvoice, SupplierPayment


class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 0
    fields = ('inventory_item', 'description', 'unit', 'quantity', 'quantity_received', 'unit_cost', 'line_total')
    readonly_fields = ('line_total', 'quantity_received')


class SupplierPaymentInline(admin.TabularInline):
    model = SupplierPayment
    extra = 0
    fields = ('number', 'payment_date', 'payment_method', 'currency_paid', 'amount_in_payment_currency', 'amount_applied_to_order')
    readonly_fields = ('number', 'payment_date', 'payment_method', 'currency_paid', 'amount_in_payment_currency', 'amount_applied_to_order')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('code', 'commercial_name', 'tax_id', 'contact_name', 'phone', 'city', 'credit_days', 'is_active')
    search_fields = ('code', 'commercial_name', 'tax_id', 'contact_name')
    list_filter = ('is_active', 'city')


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ('number', 'supplier', 'target_warehouse', 'status', 'issue_date', 'gross_total', 'net_payable', 'amount_paid', 'balance_due', 'stock_received')
    search_fields = ('number', 'supplier__commercial_name', 'supplier_invoice_number', 'withholding_certificate_number')
    list_filter = ('status', 'target_warehouse', 'currency_code', 'stock_received')
    readonly_fields = (
        'number', 'subtotal', 'tax_total', 'gross_total',
        'ir_withheld_amount', 'imi_withheld_amount', 'net_payable',
        'amount_paid', 'balance_due', 'stock_received', 'received_at',
        'withholding_certificate_number'
    )
    inlines = [PurchaseOrderItemInline, SupplierPaymentInline]


@admin.register(SupplierInvoice)
class SupplierInvoiceAdmin(admin.ModelAdmin):
    list_display = ('invoice_number', 'supplier', 'purchase_order', 'issue_date', 'due_date', 'gross_total', 'net_payable', 'amount_paid', 'balance_due', 'status')
    search_fields = ('invoice_number', 'supplier__commercial_name', 'purchase_order__number')
    list_filter = ('status', 'currency_code', 'issue_date')
    readonly_fields = (
        'subtotal', 'tax_total', 'gross_total',
        'ir_withheld_amount', 'imi_withheld_amount', 'net_payable',
        'amount_paid', 'balance_due'
    )


@admin.register(SupplierPayment)
class SupplierPaymentAdmin(admin.ModelAdmin):
    list_display = ('number', 'purchase_order', 'payment_date', 'payment_method', 'currency_paid', 'amount_in_payment_currency', 'amount_applied_to_order')
    search_fields = ('number', 'purchase_order__number', 'reference_number')
    list_filter = ('payment_method', 'currency_paid')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
