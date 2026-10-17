from decimal import Decimal
from django.contrib import messages
from django.db import models, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.utils import timezone

from django.core.exceptions import ValidationError
from apps.billing.models import Invoice, InvoiceItem, PaymentReceipt, CashRegisterSession, PosRegisterShift
from apps.billing.services_pos_shift import open_pos_shift, compute_shift_x_cut_metrics, close_pos_shift_z_cut
from apps.crm.models import Quote, Account
from apps.inventory.models import InventoryItem, Warehouse, InventoryBatch
from apps.operations.models import WorkOrder, WorkOrderTimelineLog
from apps.tenancy.models import RoleLevel
from apps.tenancy.permissions import ModuleAccessRequiredMixin


class InvoiceListView(ModuleAccessRequiredMixin, View):
    """
    Directorio de Facturas Fiscales y Monitor de Cartera (CxC).
    Accesible en lectura para Coordinador Operativo (80+).
    """
    required_module = 'billing'
    min_role_level = RoleLevel.OPERATIONS_COORD
    template_name = 'billing/invoice_list.html'

    def get(self, request, *args, **kwargs):
        qs = (
            Invoice.objects.for_org(request.organization)
            .select_related('account', 'source_quote', 'source_work_order', 'created_by')
            .prefetch_related('payments')
        )

        status_filter = request.GET.get('status', '').strip()
        if status_filter:
            qs = qs.filter(status=status_filter)

        search_query = request.GET.get('q', '').strip()
        if search_query:
            qs = qs.filter(
                models.Q(number__icontains=search_query) |
                models.Q(account__commercial_name__icontains=search_query) |
                models.Q(account__tax_id__icontains=search_query) |
                models.Q(retention_certificate_number__icontains=search_query)
            )

        invoices = list(qs)

        # Cálculo de KPIs de Cartera en memoria
        active_invoices = [inv for inv in invoices if inv.status != Invoice.Status.VOID]
        kpi_gross_total = sum(inv.gross_total for inv in active_invoices)
        kpi_retentions = sum(inv.total_retentions for inv in active_invoices)
        kpi_net_receivable = sum(inv.net_receivable for inv in active_invoices)
        kpi_collected = sum(inv.amount_paid for inv in active_invoices)
        kpi_balance_due = sum(inv.balance_due for inv in active_invoices)

        return render(request, self.template_name, {
            'invoices': invoices,
            'status_filter': status_filter,
            'search_query': search_query,
            'status_choices': Invoice.Status.choices,
            'kpi_gross_total': kpi_gross_total,
            'kpi_retentions': kpi_retentions,
            'kpi_net_receivable': kpi_net_receivable,
            'kpi_collected': kpi_collected,
            'kpi_balance_due': kpi_balance_due,
            'can_manage_billing': request.role_level >= RoleLevel.COMMERCIAL_MGR,
        })


class InvoiceDetailView(ModuleAccessRequiredMixin, View):
    """
    Expediente Fiscal Detallado de Factura, Retenciones y Caja (ROC).
    Permite lectura a Nivel 80+ y gestión completa a Nivel 85+.
    """
    required_module = 'billing'
    min_role_level = RoleLevel.OPERATIONS_COORD
    template_name = 'billing/invoice_detail.html'

    def get(self, request, pk, *args, **kwargs):
        invoice = get_object_or_404(
            Invoice.objects.for_org(request.organization).select_related('account', 'source_quote', 'source_work_order', 'created_by'),
            pk=pk
        )
        items = list(invoice.items.all())
        payments = list(invoice.payments.all().select_related('received_by'))
        from apps.finance.models import BankAccount
        bank_accounts = list(BankAccount.objects.for_org(request.organization).filter(is_active=True))

        import uuid
        return render(request, self.template_name, {
            'invoice': invoice,
            'items': items,
            'payments': payments,
            'payment_methods': PaymentReceipt.PaymentMethod.choices,
            'bank_accounts': bank_accounts,
            'payment_idempotency_key': str(uuid.uuid4()),
            'can_manage_billing': request.role_level >= RoleLevel.COMMERCIAL_MGR,
        })


class ConvertQuoteToInvoiceView(ModuleAccessRequiredMixin, View):
    """
    Conversión 1-Clic: Convierte una Cotización Aprobada en Factura Fiscal (ISSUED).
    Exclusivo para Gerente Comercial (85+) y Owner (100).
    """
    required_module = 'billing'
    min_role_level = RoleLevel.COMMERCIAL_MGR

    def post(self, request, quote_pk, *args, **kwargs):
        quote = get_object_or_404(
            Quote.objects.for_org(request.organization),
            pk=quote_pk
        )

        with transaction.atomic():
            invoice = Invoice.objects.create(
                organization=request.organization,
                account=quote.account,
                source_quote=quote,
                status=Invoice.Status.ISSUED,
                currency_code=quote.currency_code,
                currency_symbol=quote.currency_symbol,
                exchange_rate=quote.exchange_rate,
                apply_tax=quote.apply_tax,
                tax_name=quote.tax_name,
                tax_rate=quote.tax_rate,
                created_by=request.user,
                notes=quote.notes,
            )

            for q_item in quote.items.all():
                InvoiceItem.objects.create(
                    organization=request.organization,
                    invoice=invoice,
                    item_type=q_item.item_type,
                    code=q_item.code,
                    description=q_item.description,
                    unit=q_item.unit,
                    quantity=q_item.quantity,
                    unit_price=q_item.unit_price,
                    discount_percent=q_item.discount_percent,
                )

            invoice.recalculate_totals()
            invoice.save()

            messages.success(
                request,
                f"Factura fiscal emitida exitosamente: {invoice.number} por {invoice.currency_symbol} {invoice.gross_total:,.2f}."
            )

        return redirect('billing:invoice_detail', pk=invoice.pk)


class ConvertWorkOrderToInvoiceView(ModuleAccessRequiredMixin, View):
    """
    Conversión 1-Clic: Factura una Orden de Trabajo con base en las partidas ejecutadas.
    Registra un evento inmutable en el historial operativo de la OT.
    Exclusivo para Gerente Comercial (85+) y Owner (100).
    """
    required_module = 'billing'
    min_role_level = RoleLevel.COMMERCIAL_MGR

    def post(self, request, wo_pk, *args, **kwargs):
        wo = get_object_or_404(
            WorkOrder.objects.for_org(request.organization),
            pk=wo_pk
        )

        with transaction.atomic():
            quote = wo.source_quote
            currency_code = quote.currency_code if quote else request.organization.currency
            currency_symbol = quote.currency_symbol if quote else request.organization.currency_symbol
            exchange_rate = quote.exchange_rate if quote else Decimal('36.6243')
            apply_tax = quote.apply_tax if quote else True
            tax_rate = quote.tax_rate if quote else Decimal('15.00')

            # Mapear precios unitarios y descuentos de la cotización
            quote_item_map = {}
            if quote:
                for qi in quote.items.all():
                    if qi.code:
                        quote_item_map[qi.code] = (qi.unit_price, qi.discount_percent)

            invoice = Invoice.objects.create(
                organization=request.organization,
                account=wo.account,
                source_quote=quote,
                source_work_order=wo,
                status=Invoice.Status.ISSUED,
                currency_code=currency_code,
                currency_symbol=currency_symbol,
                exchange_rate=exchange_rate,
                apply_tax=apply_tax,
                tax_rate=tax_rate,
                created_by=request.user,
                notes=f"Facturación derivada de la Orden de Trabajo {wo.number}.",
            )

            for w_item in wo.items.all():
                qty = w_item.executed_quantity if w_item.executed_quantity > Decimal('0.00') else w_item.planned_quantity
                price, disc = quote_item_map.get(w_item.code, (Decimal('0.00'), Decimal('0.00')))
                InvoiceItem.objects.create(
                    organization=request.organization,
                    invoice=invoice,
                    item_type=w_item.item_type,
                    code=w_item.code,
                    description=w_item.description,
                    unit=w_item.unit,
                    quantity=qty,
                    unit_price=price,
                    discount_percent=disc,
                )

            invoice.recalculate_totals()
            invoice.save()

            # Bitácora inmutable en la Orden de Trabajo
            WorkOrderTimelineLog.objects.create(
                organization=request.organization,
                work_order=wo,
                event_type='INVOICED',
                summary=f"Orden de Trabajo facturada bajo Folio {invoice.number}",
                details=f"Factura emitida por {invoice.currency_symbol} {invoice.gross_total:,.2f} con {invoice.items.count()} partidas.",
                actor=request.user,
            )

            messages.success(
                request,
                f"Orden de Trabajo {wo.number} facturada correctamente bajo Folio {invoice.number}."
            )

        return redirect('billing:invoice_detail', pk=invoice.pk)


class InvoiceApplyRetentionsView(ModuleAccessRequiredMixin, View):
    """
    Aplica o actualiza retenciones en la fuente (IR e IMI) sobre la base imponible gravable.
    Exclusivo para Gerente Comercial (85+) y Owner (100).
    """
    required_module = 'billing'
    min_role_level = RoleLevel.COMMERCIAL_MGR

    def post(self, request, pk, *args, **kwargs):
        invoice = get_object_or_404(
            Invoice.objects.for_org(request.organization),
            pk=pk
        )

        try:
            ir_rate = Decimal(request.POST.get('ir_retention_rate', '0.00'))
            imi_rate = Decimal(request.POST.get('imi_retention_rate', '0.00'))
            cert_num = request.POST.get('retention_certificate_number', '').strip()

            invoice.ir_retention_rate = ir_rate
            invoice.imi_retention_rate = imi_rate
            invoice.retention_certificate_number = cert_num
            invoice.recalculate_totals()
            invoice.save()

            messages.success(
                request,
                f"Retenciones actualizadas en {invoice.number}: IR {ir_rate}% ({invoice.currency_symbol} {invoice.ir_retention_amount:,.2f}) "
                f"+ IMI {imi_rate}% ({invoice.currency_symbol} {invoice.imi_retention_amount:,.2f}). "
                f"Neto a cobrar: {invoice.currency_symbol} {invoice.net_receivable:,.2f}."
            )
        except Exception as e:
            messages.error(request, f"Error al procesar retenciones: {str(e)}")

        return redirect('billing:invoice_detail', pk=invoice.pk)


class InvoiceRegisterPaymentView(ModuleAccessRequiredMixin, View):
    """
    Registra un Recibo Oficial de Caja (ROC) bimonetario para liquidar total o parcialmente una factura.
    Aplica conversión automática de moneda oficial BCN si se paga en divisa alterna.
    Exclusivo para Gerente Comercial (85+) y Owner (100).
    """
    required_module = 'billing'
    min_role_level = RoleLevel.COMMERCIAL_MGR

    def post(self, request, pk, *args, **kwargs):
        from apps.finance.services import record_client_collection
        from apps.finance.models import BankAccount

        invoice = get_object_or_404(
            Invoice.objects.for_org(request.organization),
            pk=pk
        )

        try:
            amount_paid = Decimal(request.POST.get('amount', '0.00'))
            currency_paid = request.POST.get('currency_paid', invoice.currency_code).strip().upper()
            payment_method = request.POST.get('payment_method', PaymentReceipt.PaymentMethod.TRANSFER_BAC)
            reference_number = request.POST.get('reference_number', '').strip()
            notes = request.POST.get('notes', '').strip()
            bank_account_id = request.POST.get('bank_account_id') or request.POST.get('bank_account')
            idempotency_key = request.POST.get('idempotency_key') or request.headers.get('Idempotency-Key')

            bank_account = None
            if bank_account_id:
                bank_account = BankAccount.objects.for_org(request.organization).filter(pk=bank_account_id).first()
                if not bank_account:
                    bank_account = BankAccount.objects.filter(pk=bank_account_id).first()

            receipt, trx, entry = record_client_collection(
                organization=request.organization,
                invoice=invoice,
                amount=amount_paid,
                payment_method=payment_method,
                user=request.user,
                bank_account=bank_account,
                currency_paid=currency_paid,
                reference_number=reference_number,
                notes=notes,
                idempotency_key=idempotency_key,
            )

            invoice.refresh_from_db()
            messages.success(
                request,
                f"Recibo Oficial de Caja emitido: {receipt.number} por {currency_paid} {amount_paid:,.2f} "
                f"(Acreditado a factura: {invoice.currency_symbol} {receipt.amount_applied_to_invoice:,.2f}). "
                f"Saldo restante: {invoice.currency_symbol} {invoice.balance_due:,.2f} [{invoice.get_status_display()}]."
            )

        except ValidationError as e:
            msg = e.message if hasattr(e, 'message') else str(e)
            messages.error(request, f"Error al registrar cobro: {msg}")
        except Exception as e:
            messages.error(request, f"Error al registrar cobro: {str(e)}")

        return redirect('billing:invoice_detail', pk=invoice.pk)


class InvoiceVoidView(ModuleAccessRequiredMixin, View):
    """
    Anula una factura fiscal si no posee pagos acreditados.
    Exclusivo para Gerente Comercial (85+) y Owner (100).
    """
    required_module = 'billing'
    min_role_level = RoleLevel.COMMERCIAL_MGR

    def post(self, request, pk, *args, **kwargs):
        invoice = get_object_or_404(
            Invoice.objects.for_org(request.organization),
            pk=pk
        )

        if invoice.amount_paid > Decimal('0.00'):
            messages.error(request, "No se puede anular una factura con cobros o recibos de caja aplicados.")
            return redirect('billing:invoice_detail', pk=invoice.pk)

        invoice.status = Invoice.Status.VOID
        invoice.save(update_fields=['status', 'updated_at'])
        messages.warning(request, f"La factura {invoice.number} ha sido formalmente anulada.")

        return redirect('billing:invoice_detail', pk=invoice.pk)


class InvoicePrintView(ModuleAccessRequiredMixin, View):
    """
    Comprobante Fiscal Imprimible / Estado de Cuenta Oficial DGI.
    Formato Swiss Grid monocromático de alta legibilidad para entrega al cliente.
    """
    required_module = 'billing'
    min_role_level = RoleLevel.OPERATIONS_COORD
    template_name = 'billing/invoice_print.html'

    def get(self, request, pk, *args, **kwargs):
        invoice = get_object_or_404(
            Invoice.objects.for_org(request.organization).select_related('account', 'created_by'),
            pk=pk
        )
        return render(request, self.template_name, {
            'invoice': invoice,
            'items': invoice.items.all(),
            'payments': invoice.payments.all(),
        })


class PointOfSaleTerminalView(ModuleAccessRequiredMixin, View):
    """
    Consola de Punto de Venta Rápido de Mostrador (POS) de Alta Velocidad (Swiss Grid).
    Permite cobro en mostrador, emisión de factura fiscal y recibo de caja instantáneo,
    conmutación de escala de precios (Ferretería/PYME) y trazabilidad FEFO (Farmacias).
    """
    required_module = 'billing'
    min_role_level = RoleLevel.TECHNICIAN
    template_name = 'billing/pos_terminal.html'

    def get(self, request, *args, **kwargs):
        import json
        org = request.organization
        items = list(
            InventoryItem.objects.for_org(org)
            .filter(is_active=True)
            .prefetch_related('batches', 'balances', 'balances__warehouse')
            .order_by('name')
        )
        warehouses = list(Warehouse.objects.for_org(org).filter(is_active=True))
        accounts = list(Account.objects.for_org(org).order_by('commercial_name')[:100])
        default_account = Account.objects.for_org(org).filter(commercial_name__icontains='Consumidor Final').first()

        # Generar catálogo JSON estructurado para el buscador instantáneo en Alpine.js
        catalog_data = []
        for it in items:
            total_stock = it.total_stock()
            batches_list = []
            for b in it.batches.all():
                batches_list.append({
                    'id': str(b.id),
                    'batch_number': b.batch_number,
                    'warehouse_id': str(b.warehouse_id),
                    'expiration_date': b.expiration_date.strftime('%d/%m/%Y'),
                    'quantity_available': float(b.quantity_available),
                    'expiry_status': b.expiry_status,
                })
            batches_list.sort(key=lambda x: x['expiration_date'])

            catalog_data.append({
                'id': str(it.id),
                'sku': it.sku,
                'name': it.name,
                'unit': it.unit,
                'price_retail': float(it.price_retail or it.unit_cost),
                'price_contractor': float(it.price_contractor or it.price_retail or it.unit_cost),
                'price_wholesale': float(it.price_wholesale or it.price_retail or it.unit_cost),
                'wholesale_min_qty': float(it.wholesale_min_qty),
                'requires_batch_control': it.requires_batch_control,
                'active_ingredient': it.active_ingredient or '',
                'sanitary_registration': it.sanitary_registration or '',
                'laboratory_brand': it.laboratory_brand or '',
                'requires_prescription': it.requires_prescription,
                'total_stock': float(total_stock),
                'batches': batches_list,
            })

        active_shift = PosRegisterShift.objects.filter(
            organization=org,
            status=PosRegisterShift.Status.OPEN,
        ).filter(
            models.Q(cashier=request.user) | models.Q(cashier__isnull=False)
        ).order_by('-opened_at').first()

        from apps.finance.models import BankAccount
        bank_accounts = list(BankAccount.objects.for_org(org).filter(is_active=True).order_by('bank_name', 'account_number'))

        return render(request, self.template_name, {
            'items': items,
            'catalog_json': json.dumps(catalog_data),
            'warehouses': warehouses,
            'accounts': accounts,
            'default_account': default_account,
            'payment_methods': PaymentReceipt.PaymentMethod.choices,
            'bank_accounts': bank_accounts,
            'active_shift': active_shift,
        })

    def post(self, request, *args, **kwargs):
        import json
        from django.core.exceptions import ValidationError
        from apps.billing.services import execute_pos_sale
        from apps.finance.models import BankAccount

        org = request.organization
        warehouse_id = request.POST.get('warehouse_id')
        account_id = request.POST.get('account_id')
        payment_method = request.POST.get('payment_method') or PaymentReceipt.PaymentMethod.CASH
        price_tier = (request.POST.get('price_tier') or 'RETAIL').upper()
        notes = request.POST.get('notes', '')
        bank_account_id = request.POST.get('bank_account_id')
        reference_number = request.POST.get('reference_number', '')
        idempotency_key = request.POST.get('idempotency_key') or request.headers.get('X-Idempotency-Key')
        currency_paid = request.POST.get('currency_paid')

        warehouse = Warehouse.objects.for_org(org).filter(id=warehouse_id).first() if warehouse_id else Warehouse.objects.for_org(org).filter(is_active=True).first()
        account = Account.objects.for_org(org).filter(id=account_id).first() if account_id else None
        bank_account = BankAccount.objects.for_org(org).filter(id=bank_account_id).first() if bank_account_id else None

        # Procesar items: payload JSON o entradas individuales
        items_payload = request.POST.get('items_payload') or request.POST.get('cart_items')
        items_data = []
        if items_payload:
            try:
                parsed = json.loads(items_payload)
                for entry in parsed:
                    items_data.append({
                        'item_id': entry.get('item_id') or entry.get('id'),
                        'quantity': Decimal(str(entry.get('quantity', 1))),
                        'unit_price': Decimal(str(entry.get('unit_price'))) if entry.get('unit_price') is not None else None,
                        'batch_id': entry.get('batch_id'),
                        'price_tier': entry.get('price_tier') or price_tier,
                    })
            except Exception as e:
                messages.error(request, f"Error al decodificar partidas del carrito: {str(e)}")
                return redirect('billing:pos_terminal')
        else:
            item_ids = request.POST.getlist('item_id') or ([request.POST.get('item_id')] if request.POST.get('item_id') else [])
            quantities = request.POST.getlist('quantity') or ([request.POST.get('quantity')] if request.POST.get('quantity') else [])
            prices = request.POST.getlist('unit_price') or ([request.POST.get('unit_price')] if request.POST.get('unit_price') else [])
            batch_ids = request.POST.getlist('batch_id') or ([request.POST.get('batch_id')] if request.POST.get('batch_id') else [])

            for idx, item_id in enumerate(item_ids):
                qty = quantities[idx] if idx < len(quantities) else 1
                pr = prices[idx] if idx < len(prices) and prices[idx] else None
                b_id = batch_ids[idx] if idx < len(batch_ids) and batch_ids[idx] else None
                items_data.append({
                    'item_id': item_id,
                    'quantity': Decimal(str(qty)),
                    'unit_price': Decimal(str(pr)) if pr else None,
                    'batch_id': b_id,
                    'price_tier': price_tier,
                })

        try:
            invoice = execute_pos_sale(
                organization=org,
                user=request.user,
                warehouse=warehouse,
                account=account,
                items_data=items_data,
                payment_method=payment_method,
                price_tier=price_tier,
                notes=notes,
                bank_account=bank_account,
                reference_number=reference_number,
                currency_paid=currency_paid,
                idempotency_key=idempotency_key,
            )
            messages.success(
                request,
                f"Factura de mostrador emitida exitosamente: {invoice.number} "
                f"({invoice.currency_symbol} {invoice.net_receivable:,.2f}) - Cancelada al contado."
            )
            return redirect('billing:invoice_detail', pk=invoice.pk)
        except ValidationError as e:
            messages.error(request, f"{e.message if hasattr(e, 'message') else str(e)}")
            return redirect('billing:pos_terminal')
        except Exception as e:
            messages.error(request, f"Error al emitir factura en mostrador: {str(e)}")
            return redirect('billing:pos_terminal')


class CashSessionListView(ModuleAccessRequiredMixin, View):
    """
    Monitor y Panel de Arqueo de Caja Z Bimonetario (C$ / $).
    Accesible en lectura para Coordinador Operativo (80+).
    Gestión de apertura y cierre reservada para Gerente Comercial (85+) y Administrador.
    """
    required_module = 'billing'
    min_role_level = RoleLevel.OPERATIONS_COORD
    template_name = 'billing/cash_sessions.html'

    def get(self, request, *args, **kwargs):
        org = request.organization
        active_session = CashRegisterSession.objects.filter(
            organization=org,
            status=CashRegisterSession.Status.OPEN,
        ).first()

        if active_session:
            active_session.recalculate_system_totals()

        sessions_history = list(
            CashRegisterSession.objects.filter(organization=org)
            .select_related('cashier')
            .order_by('-opened_at')
        )

        closed_sessions = [s for s in sessions_history if s.status == CashRegisterSession.Status.CLOSED]
        total_sealed_nio = sum(s.declared_cash_nio for s in closed_sessions)
        total_sealed_usd = sum(s.declared_cash_usd for s in closed_sessions)
        total_diff_nio = sum(s.difference_nio for s in closed_sessions)
        total_diff_usd = sum(s.difference_usd for s in closed_sessions)

        can_manage_cash = (getattr(request, 'role_level', 0) >= RoleLevel.COMMERCIAL_MGR)

        context = {
            'active_session': active_session,
            'sessions_history': sessions_history,
            'total_sessions': len(sessions_history),
            'total_closed_sessions': len(closed_sessions),
            'total_sealed_nio': total_sealed_nio,
            'total_sealed_usd': total_sealed_usd,
            'total_diff_nio': total_diff_nio,
            'total_diff_usd': total_diff_usd,
            'can_manage_cash': can_manage_cash,
        }
        return render(request, self.template_name, context)


class CashSessionOpenView(ModuleAccessRequiredMixin, View):
    """
    Apertura de Turno de Caja con Fondos Iniciales Bimonetarios (C$ / $).
    Requiere nivel mínimo COMMERCIAL_MGR (85+).
    """
    required_module = 'billing'
    min_role_level = RoleLevel.COMMERCIAL_MGR

    def post(self, request, *args, **kwargs):
        org = request.organization

        if CashRegisterSession.objects.filter(organization=org, status=CashRegisterSession.Status.OPEN).exists():
            messages.error(request, "Ya existe un turno de caja abierto en esta organización. Debe cerrar el turno activo antes de aperturar uno nuevo.")
            return redirect('billing:cash_session_list')

        try:
            opening_nio = Decimal(request.POST.get('opening_cash_nio', '0.00').strip() or '0.00')
            opening_usd = Decimal(request.POST.get('opening_cash_usd', '0.00').strip() or '0.00')
            exchange_rate = Decimal(request.POST.get('exchange_rate', '36.6243').strip() or '36.6243')
            notes = request.POST.get('notes', '').strip()

            session = CashRegisterSession(
                organization=org,
                cashier=request.user,
                status=CashRegisterSession.Status.OPEN,
                opening_cash_nio=opening_nio,
                opening_cash_usd=opening_usd,
                exchange_rate=exchange_rate,
                notes=notes,
            )
            session.save()
            session.recalculate_system_totals()
            session.save(update_fields=[
                'system_cash_nio', 'system_cash_usd',
                'system_transfer_nio', 'system_transfer_usd',
                'expected_cash_nio', 'expected_cash_usd',
            ])

            messages.success(
                request,
                f"Turno de caja {session.number} aperturado con éxito. "
                f"Fondo Inicial: C$ {opening_nio:,.2f} / $ {opening_usd:,.2f}."
            )
        except Exception as e:
            messages.error(request, f"Error al aperturar turno de caja: {str(e)}")

        return redirect('billing:cash_session_list')


class CashSessionCloseView(ModuleAccessRequiredMixin, View):
    """
    Cierre y Sellado de Arqueo de Caja Z (Corte Z) con Firma Criptográfica SHA-256.
    Requiere nivel mínimo COMMERCIAL_MGR (85+).
    """
    required_module = 'billing'
    min_role_level = RoleLevel.COMMERCIAL_MGR

    def post(self, request, pk, *args, **kwargs):
        org = request.organization
        session = get_object_or_404(CashRegisterSession, pk=pk, organization=org)

        try:
            declared_nio = Decimal(request.POST.get('declared_cash_nio', '0.00').strip() or '0.00')
            declared_usd = Decimal(request.POST.get('declared_cash_usd', '0.00').strip() or '0.00')
            notes = request.POST.get('notes', '').strip()

            session.close_and_seal_session(
                declared_nio=declared_nio,
                declared_usd=declared_usd,
                notes=notes,
            )
            messages.success(
                request,
                f"Arqueo de Caja Z {session.number} sellado exitosamente. "
                f"Sello SHA-256: {session.audit_hash[:16]}... (Diferencia: C$ {session.difference_nio:,.2f} / $ {session.difference_usd:,.2f})"
            )
        except ValidationError as e:
            messages.error(request, e.message if hasattr(e, 'message') else str(e))
        except Exception as e:
            messages.error(request, f"Error al cerrar arqueo de caja: {str(e)}")

        return redirect('billing:cash_session_list')


class CashSessionPrintView(ModuleAccessRequiredMixin, View):
    """
    Comprobante Físico de Arqueo de Caja Z (Corte Z) para Impresión Térmica / Auditoría.
    Accesible en lectura para Coordinador Operativo (80+).
    """
    required_module = 'billing'
    min_role_level = RoleLevel.OPERATIONS_COORD
    template_name = 'billing/cash_session_print.html'

    def get(self, request, pk, *args, **kwargs):
        org = request.organization
        session = get_object_or_404(CashRegisterSession, pk=pk, organization=org)

        if session.status == CashRegisterSession.Status.OPEN:
            session.recalculate_system_totals()

        return render(request, self.template_name, {
            'session': session,
            'org': org,
        })


class PosShiftConsoleView(ModuleAccessRequiredMixin, View):
    """
    Consola Integral de Control de Turnos de Caja POS, Movimientos y Remesas:
    - Apertura de Gaveta con Fondo Inicial.
    - Monitor en vivo de Corte X Parcial (recaudación en efectivo, tarjetas, entradas, salidas y remesas).
    - Registro de entradas y salidas de efectivo (gastos menores, aportes de sencillo).
    - Preparación, despacho, entrega y depósito bancario de remesas de efectivo.
    - Arqueo ciego por desglose de denominación y justificación obligatoria de discrepancias.
    - Sellado de Corte Z Operativo definitivo con Hash SHA-256 inmutable.
    """
    required_module = 'billing'
    min_role_level = RoleLevel.TECHNICIAN
    template_name = 'billing/pos_shifts.html'

    def get(self, request, *args, **kwargs):
        org = request.organization
        active_shift = PosRegisterShift.objects.filter(
            organization=org,
            status=PosRegisterShift.Status.OPEN,
        ).filter(
            models.Q(cashier=request.user) | models.Q(cashier__isnull=False)
        ).order_by('-opened_at').first()

        x_metrics = None
        active_movements = []
        active_deposits = []
        if active_shift:
            x_metrics = compute_shift_x_cut_metrics(active_shift)
            active_movements = list(active_shift.cash_movements.all().select_related('performed_by').order_by('-created_at')[:25])
            active_deposits = list(active_shift.bank_deposits.all().select_related('destination_bank_account', 'prepared_by', 'confirmed_by').order_by('-prepared_at'))

        from apps.finance.models import BankAccount
        bank_accounts = list(BankAccount.objects.filter(organization=org, is_active=True).order_by('bank_name'))

        closed_shifts = PosRegisterShift.objects.filter(
            organization=org,
            status=PosRegisterShift.Status.CLOSED_Z,
        ).select_related('cashier', 'supervisor').order_by('-closed_at')[:40]

        return render(request, self.template_name, {
            'active_shift': active_shift,
            'x_metrics': x_metrics,
            'active_movements': active_movements,
            'active_deposits': active_deposits,
            'active_remittances': active_deposits,
            'bank_accounts': bank_accounts,
            'closed_shifts': closed_shifts,
            'now': timezone.now(),
        })

    def post(self, request, *args, **kwargs):
        org = request.organization
        action = request.POST.get('action')

        if action == 'open_shift':
            opening_cash = request.POST.get('opening_cash_nio', '0.00')
            register_name = request.POST.get('register_name', 'CAJA-PRINCIPAL-01')
            try:
                shift = open_pos_shift(
                    organization=org,
                    cashier=request.user,
                    opening_cash_nio=opening_cash,
                    register_name=register_name,
                )
                messages.success(
                    request,
                    f"Turno {shift.shift_number} abierto exitosamente en {shift.register_name} con fondo inicial de C$ {shift.opening_cash_nio:,.2f}."
                )
            except ValidationError as e:
                messages.error(request, e.message if hasattr(e, 'message') else str(e))
            except Exception as e:
                messages.error(request, f"Error al abrir turno: {str(e)}")

            return redirect('billing:pos_shifts')

        # Buscar turno activo para acciones operativas
        shift_id = request.POST.get('shift_id')
        active_shift = None
        if shift_id:
            active_shift = PosRegisterShift.objects.filter(organization=org, pk=shift_id).first()
        if not active_shift:
            active_shift = PosRegisterShift.objects.filter(
                organization=org,
                status=PosRegisterShift.Status.OPEN,
            ).filter(
                models.Q(cashier=request.user) | models.Q(cashier__isnull=False)
            ).order_by('-opened_at').first()

        if action in ['cash_in', 'cash_out']:
            if not active_shift:
                messages.error(request, "No hay ningún turno abierto para registrar movimientos de efectivo.")
                return redirect('billing:pos_shifts')

            amount = request.POST.get('amount', '0.00')
            concept = request.POST.get('concept', '')
            reference = request.POST.get('reference', '')
            from apps.billing.services_cash import record_cash_movement
            from apps.billing.models import CashMovement

            m_type = CashMovement.MovementType.CASH_IN if action == 'cash_in' else CashMovement.MovementType.CASH_OUT
            try:
                mov = record_cash_movement(
                    shift=active_shift,
                    movement_type=m_type,
                    amount=Decimal(str(amount)),
                    concept=concept,
                    reference=reference,
                    performed_by=request.user,
                )
                messages.success(
                    request,
                    f"{mov.get_movement_type_display()} por C$ {mov.amount:,.2f} registrado exitosamente."
                )
            except ValidationError as e:
                messages.error(request, e.message if hasattr(e, 'message') else str(e))
            except Exception as e:
                messages.error(request, f"Error al registrar movimiento: {str(e)}")

            return redirect('billing:pos_shifts')

        elif action in ['prepare_deposit', 'prepare_cash_bank_deposit', 'prepare_remittance']:
            if not active_shift:
                messages.error(request, "No hay ningún turno abierto para preparar depósitos bancarios.")
                return redirect('billing:pos_shifts')

            bank_id = request.POST.get('bank_account_id')
            amount = request.POST.get('amount', '0.00')
            notes = request.POST.get('notes', '')

            from apps.finance.models import BankAccount
            bank = get_object_or_404(BankAccount.objects.filter(organization=org), pk=bank_id)
            from apps.billing.services_cash import prepare_cash_bank_deposit
            try:
                dep = prepare_cash_bank_deposit(
                    shift=active_shift,
                    destination_bank_account=bank,
                    amount=Decimal(str(amount)),
                    user=request.user,
                    notes=notes,
                )
                messages.success(
                    request,
                    f"Depósito bancario {dep.deposit_number} por C$ {dep.amount:,.2f} preparado hacia {bank.bank_name}. Dinero aún en gaveta."
                )
            except ValidationError as e:
                messages.error(request, e.message if hasattr(e, 'message') else str(e))
            except Exception as e:
                messages.error(request, f"Error al preparar depósito bancario: {str(e)}")

            return redirect('billing:pos_shifts')

        elif action in ['dispatch_deposit', 'dispatch_cash_for_deposit', 'dispatch_remittance']:
            dep_id = request.POST.get('deposit_id') or request.POST.get('remittance_id')
            carrier_name = request.POST.get('carrier_name', '')
            carrier_id = request.POST.get('carrier_id', '')

            from apps.billing.models import CashBankDeposit
            from apps.billing.services_cash import dispatch_cash_for_deposit
            dep = get_object_or_404(CashBankDeposit.objects.filter(organization=org), pk=dep_id)
            try:
                dispatched_dep = dispatch_cash_for_deposit(
                    deposit=dep,
                    user=request.user,
                    carrier_name=carrier_name,
                    carrier_id=carrier_id,
                )
                messages.success(
                    request,
                    f"Efectivo para depósito {dispatched_dep.deposit_number} entregado a {dispatched_dep.carrier_name or 'custodio'}. Efectivo descontado de gaveta y reconocido en tránsito."
                )
            except ValidationError as e:
                messages.error(request, e.message if hasattr(e, 'message') else str(e))
            except Exception as e:
                messages.error(request, f"Error al despachar efectivo para depósito: {str(e)}")

            return redirect('billing:pos_shifts')

        elif action in ['record_deposit_slip', 'record_bank_deposit_slip', 'deposit_remittance']:
            dep_id = request.POST.get('deposit_id') or request.POST.get('remittance_id')
            deposit_slip = request.POST.get('deposit_slip_number', '')
            deposit_date = request.POST.get('deposit_date')

            from apps.billing.models import CashBankDeposit
            from apps.billing.services_cash import record_bank_deposit_slip
            dep = get_object_or_404(CashBankDeposit.objects.filter(organization=org), pk=dep_id)
            try:
                dep_record = record_bank_deposit_slip(
                    deposit=dep,
                    deposit_slip_number=deposit_slip,
                    deposit_date=deposit_date,
                    user=request.user,
                )
                messages.success(
                    request,
                    f"Boleta de depósito bancario {dep_record.deposit_slip_number} registrada para el depósito {dep_record.deposit_number}. Pendiente de confirmación de acreditación bancaria."
                )
            except ValidationError as e:
                messages.error(request, e.message if hasattr(e, 'message') else str(e))
            except Exception as e:
                messages.error(request, f"Error al registrar boleta bancaria: {str(e)}")

            return redirect('billing:pos_shifts')

        elif action in ['confirm_deposit', 'confirm_bank_deposit', 'confirm_remittance']:
            dep_id = request.POST.get('deposit_id') or request.POST.get('remittance_id')
            deposit_slip = request.POST.get('deposit_slip_number', '')
            deposit_date = request.POST.get('deposit_date')

            from apps.billing.models import CashBankDeposit
            from apps.billing.services_cash import confirm_bank_deposit
            dep = get_object_or_404(CashBankDeposit.objects.filter(organization=org), pk=dep_id)
            try:
                conf_dep = confirm_bank_deposit(
                    deposit=dep,
                    user=request.user,
                    deposit_slip_number=deposit_slip,
                    deposit_date=deposit_date,
                )
                messages.success(
                    request,
                    f"Depósito bancario {conf_dep.deposit_number} confirmado y acreditado en banco exitosamente (+C$ {conf_dep.amount:,.2f}). Transacción TRX registrada para conciliación."
                )
            except ValidationError as e:
                messages.error(request, e.message if hasattr(e, 'message') else str(e))
            except Exception as e:
                messages.error(request, f"Error al confirmar acreditación bancaria: {str(e)}")

            return redirect('billing:pos_shifts')

        elif action in ['reject_deposit', 'reject_bank_deposit', 'reject_remittance']:
            dep_id = request.POST.get('deposit_id') or request.POST.get('remittance_id')
            reason = request.POST.get('rejection_reason', '')

            from apps.billing.models import CashBankDeposit
            from apps.billing.services_cash import reject_bank_deposit
            dep = get_object_or_404(CashBankDeposit.objects.filter(organization=org), pk=dep_id)
            try:
                rej_dep = reject_bank_deposit(
                    deposit=dep,
                    user=request.user,
                    rejection_reason=reason,
                )
                messages.warning(
                    request,
                    f"Depósito bancario {rej_dep.deposit_number} rechazado. Póliza compensatoria aplicada y fondos reintegrados."
                )
            except ValidationError as e:
                messages.error(request, e.message if hasattr(e, 'message') else str(e))
            except Exception as e:
                messages.error(request, f"Error al rechazar depósito bancario: {str(e)}")

            return redirect('billing:pos_shifts')

        elif action == 'close_z':
            if not active_shift:
                messages.error(request, "No se encontró ningún turno activo para cerrar.")
                return redirect('billing:pos_shifts')

            declared_cash = request.POST.get('declared_cash_nio')
            notes = request.POST.get('notes', '')
            difference_justification = request.POST.get('difference_justification', '')

            # Parsear posible desglose por denominación
            denomination_breakdown = {}
            denoms = ['1000', '500', '200', '100', '50', '20', '10', '5', '1', '0.50', '0.25', '0.10']
            has_breakdown = False
            for d in denoms:
                val = request.POST.get(f'denom_{d.replace(".", "_")}')
                if val and val.isdigit() and int(val) > 0:
                    denomination_breakdown[d] = int(val)
                    has_breakdown = True

            try:
                closed_shift = close_pos_shift_z_cut(
                    shift=active_shift,
                    declared_cash_nio=Decimal(str(declared_cash)) if declared_cash else None,
                    user=request.user,
                    supervisor=request.user if request.user != active_shift.cashier else None,
                    notes=notes,
                    denomination_breakdown=denomination_breakdown if has_breakdown else None,
                    difference_justification=difference_justification,
                )
                diff = closed_shift.cash_difference_nio
                if diff == Decimal('0.00'):
                    status_text = "CUADRADO EXACTO (C$ 0.00)"
                elif diff > Decimal('0.00'):
                    status_text = f"SOBRANTE DE C$ {diff:,.2f}"
                else:
                    status_text = f"FALTANTE DE C$ {abs(diff):,.2f}"

                messages.success(
                    request,
                    f"Corte Z {closed_shift.shift_number} sellado con éxito [{status_text}]. Sello SHA-256 generado."
                )
                return redirect('billing:pos_shift_print_z', pk=str(closed_shift.pk))
            except ValidationError as e:
                messages.error(request, e.message if hasattr(e, 'message') else str(e))
            except Exception as e:
                messages.error(request, f"Error al ejecutar Corte Z: {str(e)}")

            return redirect('billing:pos_shifts')

        return redirect('billing:pos_shifts')


class PosShiftZReportPrintView(ModuleAccessRequiredMixin, View):
    """
    Vista oficial de impresión de Acta de Arqueo y Resumen Z Operativo de Caja POS.
    Diseño All-Light Swiss Grid de alta densidad.
    IMPORTANTE: Documento de control operativo interno (no certificación tributaria oficial).
    """
    required_module = 'billing'
    min_role_level = RoleLevel.TECHNICIAN
    template_name = 'billing/pos_shift_print_z.html'

    def get(self, request, pk, *args, **kwargs):
        org = request.organization
        shift = get_object_or_404(
            PosRegisterShift.objects.filter(organization=org),
            pk=pk
        )
        invoices = list(
            shift.shift_invoices.all().select_related('account').prefetch_related('payments').order_by('issue_date', 'number')
        )
        movements = list(shift.cash_movements.all().select_related('performed_by').order_by('created_at'))
        bank_deposits = list(shift.bank_deposits.all().select_related('destination_bank_account', 'prepared_by', 'confirmed_by').order_by('prepared_at'))

        return render(request, self.template_name, {
            'shift': shift,
            'invoices': invoices,
            'movements': movements,
            'bank_deposits': bank_deposits,
            'remittances': bank_deposits,
            'current_org': org,
            'now': timezone.now(),
        })


