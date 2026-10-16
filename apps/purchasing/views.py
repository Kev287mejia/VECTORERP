import uuid
from decimal import Decimal
from django.contrib import messages
from django.db import models, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from apps.finance.models import BankAccount
from apps.inventory.models import InventoryItem, Warehouse
from apps.inventory.services import record_stock_entry
from apps.purchasing.models import PurchaseOrder, PurchaseOrderItem, Supplier, SupplierInvoice, SupplierPayment
from apps.purchasing.services import receive_purchase_order_stock, record_supplier_disbursement
from apps.tenancy.models import RoleLevel
from apps.tenancy.permissions import ModuleAccessRequiredMixin


class PurchaseOrderListView(ModuleAccessRequiredMixin, View):
    """
    Monitor Principal de Compras Locales, Abastecimiento y Cuentas por Pagar (CxP).
    Accesible para roles operativos con Nivel >= 80 (Coordinador de Operaciones en adelante).
    """
    required_module = 'purchasing'
    min_role_level = RoleLevel.OPERATIONS_COORD
    template_name = 'purchasing/order_list.html'

    def get(self, request, *args, **kwargs):
        qs = (
            PurchaseOrder.objects.for_org(request.organization)
            .select_related('supplier', 'target_warehouse', 'created_by', 'received_by')
            .prefetch_related('items', 'payments')
        )

        status_filter = request.GET.get('status', '').strip()
        if status_filter:
            qs = qs.filter(status=status_filter)

        warehouse_filter = request.GET.get('warehouse', '').strip()
        if warehouse_filter:
            qs = qs.filter(target_warehouse_id=warehouse_filter)

        search_query = request.GET.get('q', '').strip()
        if search_query:
            qs = qs.filter(
                models.Q(number__icontains=search_query) |
                models.Q(supplier__commercial_name__icontains=search_query) |
                models.Q(supplier__tax_id__icontains=search_query) |
                models.Q(supplier_invoice_number__icontains=search_query) |
                models.Q(withholding_certificate_number__icontains=search_query)
            )

        orders = list(qs)

        # KPIs de Abastecimiento y Obligaciones Fiscales
        active_orders = [po for po in orders if po.status != PurchaseOrder.Status.CANCELLED]
        kpi_gross_total = sum(po.gross_total for po in active_orders)
        kpi_tax_total = sum(po.tax_total for po in active_orders)
        kpi_retentions = sum(po.total_withheld for po in active_orders)
        kpi_net_payable = sum(po.net_payable for po in active_orders)
        kpi_paid = sum(po.amount_paid for po in active_orders)
        kpi_balance_due = sum(po.balance_due for po in active_orders)

        warehouses = list(Warehouse.objects.for_org(request.organization).filter(is_active=True))
        suppliers = list(Supplier.objects.for_org(request.organization).filter(is_active=True))

        return render(request, self.template_name, {
            'orders': orders,
            'status_filter': status_filter,
            'warehouse_filter': warehouse_filter,
            'search_query': search_query,
            'status_choices': PurchaseOrder.Status.choices,
            'warehouses': warehouses,
            'suppliers': suppliers,
            'kpi_gross_total': kpi_gross_total,
            'kpi_tax_total': kpi_tax_total,
            'kpi_retentions': kpi_retentions,
            'kpi_net_payable': kpi_net_payable,
            'kpi_paid': kpi_paid,
            'kpi_balance_due': kpi_balance_due,
            'can_pay_suppliers': request.role_level >= RoleLevel.COMMERCIAL_MGR,
        })


class PurchaseOrderDetailView(ModuleAccessRequiredMixin, View):
    """
    Expediente Técnico de Orden de Compra, Recepción en Kardex y Retenciones DGI.
    """
    required_module = 'purchasing'
    min_role_level = RoleLevel.OPERATIONS_COORD
    template_name = 'purchasing/order_detail.html'

    def get(self, request, pk, *args, **kwargs):
        po = get_object_or_404(
            PurchaseOrder.objects.for_org(request.organization)
            .select_related('supplier', 'target_warehouse', 'created_by', 'received_by'),
            pk=pk
        )
        items = list(po.items.all().select_related('inventory_item'))
        payments = list(po.payments.all().select_related('paid_by', 'bank_account'))
        inventory_items = list(InventoryItem.objects.for_org(request.organization).filter(is_active=True))
        bank_accounts = list(BankAccount.objects.for_org(request.organization).filter(is_active=True))
        supplier_invoices = list(po.supplier_invoices.all())

        return render(request, self.template_name, {
            'po': po,
            'items': items,
            'payments': payments,
            'inventory_items': inventory_items,
            'bank_accounts': bank_accounts,
            'supplier_invoices': supplier_invoices,
            'payment_methods': SupplierPayment.PaymentMethod.choices,
            'default_idempotency_key': str(uuid.uuid4()),
            'can_pay_suppliers': request.role_level >= RoleLevel.COMMERCIAL_MGR,
            'can_receive_stock': request.role_level >= RoleLevel.OPERATIONS_COORD and not po.stock_received,
        })


class PurchaseOrderCreateView(ModuleAccessRequiredMixin, View):
    """
    Apertura de una nueva Orden de Compra para abastecimiento de proyectos o almacén.
    """
    required_module = 'purchasing'
    min_role_level = RoleLevel.OPERATIONS_COORD

    def post(self, request, *args, **kwargs):
        supplier_id = request.POST.get('supplier_id')
        warehouse_id = request.POST.get('target_warehouse_id')
        supplier_invoice = request.POST.get('supplier_invoice_number', '').strip()
        currency_code = request.POST.get('currency_code', request.organization.currency).strip().upper()
        notes = request.POST.get('notes', '').strip()

        supplier = get_object_or_404(Supplier.objects.for_org(request.organization), pk=supplier_id)
        warehouse = get_object_or_404(Warehouse.objects.for_org(request.organization), pk=warehouse_id)

        currency_symbol = 'C$' if currency_code == 'NIO' else '$'
        exchange_rate = Decimal('36.6243')

        po = PurchaseOrder.objects.create(
            organization=request.organization,
            supplier=supplier,
            target_warehouse=warehouse,
            supplier_invoice_number=supplier_invoice,
            status=PurchaseOrder.Status.DRAFT,
            currency_code=currency_code,
            currency_symbol=currency_symbol,
            exchange_rate=exchange_rate,
            ir_withholding_rate=supplier.default_ir_rate,
            imi_withholding_rate=supplier.default_imi_rate,
            created_by=request.user,
            notes=notes,
        )
        messages.success(request, f"Orden de compra creada exitosamente con folio provisional {po.number}.")
        return redirect('purchasing:order_detail', pk=po.pk)


class PurchaseOrderAddItemView(ModuleAccessRequiredMixin, View):
    """
    Añade un material/insumo del catálogo a la Orden de Compra antes de recibirla.
    """
    required_module = 'purchasing'
    min_role_level = RoleLevel.OPERATIONS_COORD

    def post(self, request, pk, *args, **kwargs):
        po = get_object_or_404(PurchaseOrder.objects.for_org(request.organization), pk=pk)

        if po.stock_received or po.status in (PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.PAID):
            messages.error(request, "No se pueden añadir partidas a una orden de compra ya recibida o liquidada.")
            return redirect('purchasing:order_detail', pk=po.pk)

        item_id = request.POST.get('inventory_item_id')
        qty = Decimal(request.POST.get('quantity', '1.00'))
        cost = Decimal(request.POST.get('unit_cost', '0.00'))

        inv_item = get_object_or_404(InventoryItem.objects.for_org(request.organization), pk=item_id)

        if qty <= Decimal('0.00'):
            messages.error(request, "La cantidad debe ser mayor a 0.")
            return redirect('purchasing:order_detail', pk=po.pk)

        PurchaseOrderItem.objects.create(
            organization=request.organization,
            purchase_order=po,
            inventory_item=inv_item,
            description=inv_item.name,
            unit=inv_item.unit,
            quantity=qty,
            unit_cost=cost,
        )

        po.recalculate_totals()
        po.save()

        messages.success(request, f"Partida añadida: {inv_item.name} ({qty} {inv_item.unit}) a {po.currency_symbol} {cost:,.2f}.")
        return redirect('purchasing:order_detail', pk=po.pk)


class ReceivePurchaseOrderView(ModuleAccessRequiredMixin, View):
    """
    Recepción Física de Mercadería en Almacén, Kardex e Integración Contable.
    Inyecta existencias mediante receive_purchase_order_stock() con bloqueo pesimista
    select_for_update(), separación de factura del proveedor y asiento contable cuadrado.
    Accesible para Coordinador de Operaciones (80+).
    """
    required_module = 'purchasing'
    min_role_level = RoleLevel.OPERATIONS_COORD

    def post(self, request, pk, *args, **kwargs):
        po = get_object_or_404(
            PurchaseOrder.objects.for_org(request.organization)
            .select_related('supplier', 'target_warehouse'),
            pk=pk
        )

        supplier_invoice = request.POST.get('supplier_invoice_number', '').strip()

        try:
            po, movements, inv, journal_entry = receive_purchase_order_stock(
                organization=request.organization,
                purchase_order=po,
                user=request.user,
                supplier_invoice_number=supplier_invoice,
            )
            inv_msg = f" · Factura #{inv.invoice_number}" if inv else ""
            pol_msg = f" · Póliza {journal_entry.number}" if journal_entry else ""
            messages.success(
                request,
                f"Recepción completada en {po.target_warehouse.name}. "
                f"Se ingresaron {len(movements)} partida(s) al Kardex con actualización de costo promedio. "
                f"Cuenta por pagar generada por {po.currency_symbol} {po.net_payable:,.2f}"
                f"{inv_msg}{pol_msg}."
            )
        except Exception as e:
            messages.error(request, f"Error durante la recepción en Kardex: {str(e)}")

        return redirect('purchasing:order_detail', pk=po.pk)


class RegisterSupplierPaymentView(ModuleAccessRequiredMixin, View):
    """
    Emisión de Comprobante de Egreso (EGR) y Liquidación de Saldo con Proveedores.
    Exclusivo para Gerente Comercial (85+) y Owner (100).
    Bloquea a Coordinador Operativo (80) con HTTP 403.
    """
    required_module = 'purchasing'
    min_role_level = RoleLevel.COMMERCIAL_MGR

    def post(self, request, pk, *args, **kwargs):
        po = get_object_or_404(
            PurchaseOrder.objects.for_org(request.organization),
            pk=pk
        )

        try:
            amount = Decimal(request.POST.get('amount', '0.00'))
            currency_paid = request.POST.get('currency_paid', po.currency_code).strip().upper()
            payment_method = request.POST.get('payment_method', SupplierPayment.PaymentMethod.TRANSFER_BAC)
            bank_account_id = request.POST.get('bank_account_id')
            reference_number = request.POST.get('reference_number', '').strip()
            notes = request.POST.get('notes', '').strip()
            idempotency_key = request.POST.get('idempotency_key') or request.headers.get('X-Idempotency-Key')

            if amount <= Decimal('0.00'):
                messages.error(request, "El monto a liquidar debe ser estrictamente mayor a 0.")
                return redirect('purchasing:order_detail', pk=po.pk)

            bank_account = None
            if bank_account_id:
                bank_account = BankAccount.objects.for_org(request.organization).filter(pk=bank_account_id).first()
            elif payment_method != SupplierPayment.PaymentMethod.CASH:
                bank_account = BankAccount.objects.filter(
                    organization=request.organization,
                    is_active=True,
                    currency=currency_paid,
                ).first()

            supplier_invoice_id = request.POST.get('supplier_invoice_id')
            supplier_invoice = None
            if supplier_invoice_id:
                supplier_invoice = SupplierInvoice.objects.for_org(request.organization).filter(pk=supplier_invoice_id).first()

            egr, trx, pol = record_supplier_disbursement(
                organization=request.organization,
                purchase_order=po,
                amount=amount,
                user=request.user,
                payment_method=payment_method,
                bank_account=bank_account,
                supplier_invoice=supplier_invoice,
                reference_number=reference_number,
                currency_paid=currency_paid,
                notes=notes,
                idempotency_key=idempotency_key,
            )

            po.refresh_from_db()
            trx_msg = f" · Movimiento {trx.number}" if trx else ""
            pol_msg = f" · Póliza {pol.number}" if pol else ""
            messages.success(
                request,
                f"Comprobante de Egreso emitido: {egr.number} por {currency_paid} {amount:,.2f} "
                f"(Acreditado a orden: {po.currency_symbol} {egr.amount_applied_to_order:,.2f}). "
                f"Saldo pendiente: {po.currency_symbol} {po.balance_due:,.2f} [{po.get_status_display()}]."
                f"{trx_msg}{pol_msg}"
            )

        except Exception as e:
            messages.error(request, f"Error al emitir egreso bancario: {str(e)}")

        return redirect('purchasing:order_detail', pk=po.pk)


class WithholdingCertificatePrintView(ModuleAccessRequiredMixin, View):
    """
    Constancia Oficial de Retención en la Fuente DGI & Alcaldía Municipal (Ley 822).
    Genera el formato formal de retención practicada por el Agente Retenedor.
    """
    required_module = 'purchasing'
    min_role_level = RoleLevel.OPERATIONS_COORD
    template_name = 'purchasing/retention_certificate_print.html'

    def get(self, request, pk, *args, **kwargs):
        po = get_object_or_404(
            PurchaseOrder.objects.for_org(request.organization)
            .select_related('supplier', 'created_by', 'received_by'),
            pk=pk
        )
        return render(request, self.template_name, {
            'po': po,
            'supplier': po.supplier,
            'org': request.organization,
        })


class SupplierListView(ModuleAccessRequiredMixin, View):
    """
    Directorio Institucional de Proveedores y Contratistas.
    """
    required_module = 'purchasing'
    min_role_level = RoleLevel.OPERATIONS_COORD
    template_name = 'purchasing/supplier_list.html'

    def get(self, request, *args, **kwargs):
        suppliers = Supplier.objects.for_org(request.organization)
        search_query = request.GET.get('q', '').strip()
        if search_query:
            suppliers = suppliers.filter(
                models.Q(commercial_name__icontains=search_query) |
                models.Q(code__icontains=search_query) |
                models.Q(tax_id__icontains=search_query) |
                models.Q(contact_name__icontains=search_query)
            )

        return render(request, self.template_name, {
            'suppliers': list(suppliers),
            'search_query': search_query,
            'can_manage_suppliers': request.role_level >= RoleLevel.OPERATIONS_COORD,
        })

    def post(self, request, *args, **kwargs):
        """Creación rápida de proveedor."""
        name = request.POST.get('commercial_name', '').strip()
        tax_id = request.POST.get('tax_id', '').strip()
        contact_name = request.POST.get('contact_name', '').strip()
        email = request.POST.get('email', '').strip()
        phone = request.POST.get('phone', '').strip()
        city = request.POST.get('city', 'Managua').strip()
        credit_days = int(request.POST.get('credit_days', 30))
        ir_rate = Decimal(request.POST.get('default_ir_rate', '1.00'))
        imi_rate = Decimal(request.POST.get('default_imi_rate', '1.00'))

        if not name or not tax_id:
            messages.error(request, "Nombre Comercial y RUC son campos obligatorios.")
            return redirect('purchasing:supplier_list')

        supplier = Supplier.objects.create(
            organization=request.organization,
            commercial_name=name,
            tax_id=tax_id,
            contact_name=contact_name,
            email=email,
            phone=phone,
            city=city,
            credit_days=credit_days,
            default_ir_rate=ir_rate,
            default_imi_rate=imi_rate,
        )
        messages.success(request, f"Proveedor registrado exitosamente con código {supplier.code}.")
        return redirect('purchasing:supplier_list')
