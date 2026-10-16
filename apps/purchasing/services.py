import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.inventory.services import record_stock_entry
from apps.finance.models import BankAccount, FinancialTransaction, JournalEntry
from apps.finance.services import (
    record_purchase_order_journal_entry,
    record_supplier_payment_journal_entry,
)
from apps.purchasing.models import (
    PurchaseOrder,
    SupplierInvoice,
    SupplierPayment,
    SupplierDisbursementIdempotencyRecord,
)


@transaction.atomic
def receive_purchase_order_stock(
    organization,
    purchase_order,
    user,
    supplier_invoice_number: str = '',
    supplier_invoice_date=None,
):
    """
    Orquesta atómicamente la recepción física de mercancías en almacén:
    1. Bloqueo pesimista con select_for_update() sobre PurchaseOrder para impedir doble recepción concurrente.
    2. Validación de pertenencia multi-tenant (organización, proveedor, bodega y catálogo).
    3. Validación del estado de la orden (rechaza órdenes ya recibidas, canceladas o liquidadas).
    4. Separación y creación del documento fiscal/comercial del proveedor (SupplierInvoice).
    5. Actualización de existencias y costo promedio ponderado en Kardex (StockMovement inmutable)
       con vinculación explícita a la orden de compra y partida recibida.
    6. Asiento contable automático, cuadrado e bimonetario en Libro Diario (PURCHASES)
       con Débito a Inventarios (Activo 1106-01 / 5101-01) y Crédito a Proveedores (2101-01).
    7. Manejo estricto de atomicidad: cualquier fallo intermedio revierte toda la operación.
    """
    if purchase_order.organization_id != organization.id:
        raise ValidationError("La orden de compra no pertenece a la organización activa.")

    # 1. Bloqueo pesimista de la Orden de Compra
    po = (
        PurchaseOrder.objects.select_for_update()
        .select_related('supplier', 'target_warehouse')
        .get(pk=purchase_order.pk, organization=organization)
    )

    if po.stock_received or po.status in (
        PurchaseOrder.Status.RECEIVED,
        PurchaseOrder.Status.PAID,
        PurchaseOrder.Status.CANCELLED,
    ):
        raise ValidationError(
            f"La orden de compra {po.number} ya fue recepcionada previamente en bodega o se encuentra en estado {po.get_status_display()}."
        )

    # 2. Validación de bodega
    warehouse = po.target_warehouse
    if not warehouse or warehouse.organization_id != organization.id or not warehouse.is_active:
        raise ValidationError("La bodega de destino no pertenece a la organización o se encuentra inactiva.")

    # 3. Validación de partidas
    items = list(po.items.select_related('inventory_item').all())
    if not items:
        raise ValidationError("La orden de compra no contiene partidas para ingresar a bodega.")

    for line in items:
        if line.organization_id != organization.id or line.inventory_item.organization_id != organization.id:
            raise ValidationError(f"La partida '{line.description}' o su ítem no pertenecen a la organización autorizada.")
        if line.quantity <= Decimal('0.00'):
            raise ValidationError(f"La cantidad de la partida '{line.description}' debe ser mayor a 0.")

    # 4. Separación de Factura del Proveedor (SupplierInvoice)
    clean_inv_num = (supplier_invoice_number or po.supplier_invoice_number or '').strip()
    supplier_invoice = None

    if clean_inv_num:
        existing_inv = SupplierInvoice.objects.filter(
            organization=organization,
            supplier=po.supplier,
            invoice_number=clean_inv_num,
        ).first()

        if existing_inv:
            if existing_inv.purchase_order_id and existing_inv.purchase_order_id != po.id:
                raise ValidationError(
                    f"La factura del proveedor '{clean_inv_num}' ya fue registrada previamente para la orden {existing_inv.purchase_order.number}."
                )
            supplier_invoice = existing_inv
        else:
            supplier_invoice = SupplierInvoice.objects.create(
                organization=organization,
                supplier=po.supplier,
                purchase_order=po,
                invoice_number=clean_inv_num,
                issue_date=supplier_invoice_date or (po.issue_date or timezone.now().date()),
                due_date=po.due_date,
                currency_code=po.currency_code,
                exchange_rate=po.exchange_rate,
                subtotal=po.subtotal,
                tax_total=po.tax_total,
                gross_total=po.gross_total,
                ir_withheld_amount=po.ir_withheld_amount,
                imi_withheld_amount=po.imi_withheld_amount,
                net_payable=po.net_payable,
                amount_paid=po.amount_paid,
                balance_due=po.balance_due,
                status=SupplierInvoice.Status.PENDING,
                created_by=user,
                notes=f"Generada automáticamente por recepción de OC {po.number}",
            )

        po.supplier_invoice_number = clean_inv_num

    # 5. Ingreso a Bodega y Kardex por partida
    movements = []
    for line in items:
        ref_note = f"Recepción {po.number} · Factura Proveedor {po.supplier_invoice_number or 'S/F'}"
        mov = record_stock_entry(
            organization=organization,
            warehouse=warehouse,
            item=line.inventory_item,
            quantity=line.quantity,
            unit_cost=line.unit_cost,
            reference_note=ref_note,
            user=user,
            purchase_order=po,
            purchase_order_item=line,
        )
        movements.append(mov)
        line.quantity_received = line.quantity
        line.save(update_fields=['quantity_received'])

    # 6. Actualización de estado en PurchaseOrder
    po.stock_received = True
    po.status = PurchaseOrder.Status.RECEIVED
    po.received_by = user
    po.received_at = timezone.now()
    po.recalculate_totals()
    po.save()

    # 7. Asiento contable de compra en Libro Diario
    journal_entry = record_purchase_order_journal_entry(
        organization=organization,
        purchase_order=po,
        user=user,
        supplier_invoice=supplier_invoice,
    )

    return po, movements, supplier_invoice, journal_entry


@transaction.atomic
def record_supplier_disbursement(
    organization,
    purchase_order,
    amount: Decimal,
    user,
    payment_method: str = SupplierPayment.PaymentMethod.TRANSFER_BAC,
    bank_account=None,
    supplier_invoice=None,
    reference_number: str = '',
    currency_paid: str = None,
    payment_date=None,
    exchange_rate_applied: Decimal = None,
    notes: str = '',
    idempotency_key: str = None,
) -> tuple:
    """
    Servicio centralizado y seguro de liquidación y desembolso a proveedores.
    Garantiza atómicamente:
    1. Bloqueo pesimista con select_for_update() sobre la orden de compra y la factura del proveedor.
    2. Validación multi-tenant de organización, proveedor y cuenta bancaria.
    3. Bloqueo pesimista y verificación de fondos disponibles en la cuenta bancaria.
    4. Conversión bimonetaria a la moneda de la orden y validación de saldo pendiente (previene sobrepagos).
    5. Verificación de unicidad de referencias bancarias por cuenta bancaria e institución.
    6. Emisión inmutable del Comprobante de Egreso (SupplierPayment EGR-*).
    7. Actualización del saldo bancario y registro inmutable en Libro Mayor de Tesorería (FinancialTransaction).
    8. Generación automática y balanceada de la póliza contable (PAYMENTS) en el Libro Diario.
    9. Sincronización exacta y consistente del saldo pendiente y estado en PurchaseOrder y SupplierInvoice.
    10. Soporte estricto de Idempotencia mediante SupplierDisbursementIdempotencyRecord (token SHA-256).
    """
    if purchase_order.organization_id != organization.id:
        raise ValidationError("La orden de compra no pertenece a la organización activa.")

    amt = Decimal(str(amount))
    if amt <= Decimal('0.00'):
        raise ValidationError("El monto a liquidar debe ser estrictamente mayor a 0.")

    curr = (currency_paid or purchase_order.currency_code).strip().upper()
    clean_ref = (reference_number or '').strip()

    # 1. Chequeo de Idempotencia previo
    computed_hash = None
    if idempotency_key:
        payload_data = {
            'po_id': str(purchase_order.id),
            'invoice_id': str(supplier_invoice.id) if supplier_invoice else '',
            'amount': str(amt),
            'currency': curr,
            'payment_method': payment_method,
            'bank_account_id': str(bank_account.id) if bank_account else '',
            'reference_number': clean_ref,
        }
        computed_hash = hashlib.sha256(json.dumps(payload_data, sort_keys=True).encode('utf-8')).hexdigest()

        existing_record = SupplierDisbursementIdempotencyRecord.objects.filter(
            organization=organization,
            idempotency_key=idempotency_key,
        ).first()

        if existing_record:
            if existing_record.request_hash == computed_hash:
                # Reintento idéntico seguro: recuperar registros existentes
                egr = existing_record.supplier_payment
                trx = FinancialTransaction.objects.filter(
                    organization=organization,
                    reference_code=clean_ref or (egr.number if egr else ''),
                ).first() if egr else None
                pol = egr.journal_entries.filter(entry_type=JournalEntry.EntryType.PAYMENTS).first() if egr else None
                return egr, trx, pol
            else:
                raise ValidationError("La clave de idempotencia especificada ya fue utilizada con parámetros de solicitud diferentes.")

    # 2. Bloqueo pesimista de la Orden de Compra
    locked_po = (
        PurchaseOrder.objects.select_for_update()
        .select_related('supplier')
        .get(pk=purchase_order.pk, organization=organization)
    )

    if locked_po.supplier.organization_id != organization.id:
        raise ValidationError("El proveedor no pertenece a la organización activa.")

    if locked_po.status in (PurchaseOrder.Status.CANCELLED, PurchaseOrder.Status.DRAFT):
        raise ValidationError(f"No se pueden registrar pagos en una orden con estado {locked_po.get_status_display()}.")

    if locked_po.status == PurchaseOrder.Status.PAID or locked_po.balance_due <= Decimal('0.00'):
        raise ValidationError("La orden de compra ya se encuentra totalmente cancelada / pagada.")

    # 3. Resolución y bloqueo pesimista de la Factura de Proveedor
    locked_inv = None
    if supplier_invoice:
        # Validación multi-tenant de la factura suministrada
        if supplier_invoice.organization_id != organization.id:
            raise ValidationError("La factura del proveedor no pertenece a la organización activa.")
        if supplier_invoice.purchase_order_id and supplier_invoice.purchase_order_id != locked_po.id:
            raise ValidationError("La factura del proveedor no corresponde a la orden de compra especificada.")
        if supplier_invoice.supplier_id != locked_po.supplier_id:
            raise ValidationError("El proveedor de la factura no coincide con la orden de compra.")
        if supplier_invoice.currency_code != locked_po.currency_code:
            raise ValidationError(
                f"Incompatibilidad monetaria: La factura del proveedor ({supplier_invoice.currency_code}) "
                f"no coincide con la moneda de la orden de compra ({locked_po.currency_code})."
            )

        locked_inv = SupplierInvoice.objects.select_for_update().get(pk=supplier_invoice.pk, organization=organization)
        if locked_inv.status == SupplierInvoice.Status.VOID:
            raise ValidationError("La factura del proveedor se encuentra anulada.")
        if locked_inv.status == SupplierInvoice.Status.PAID or locked_inv.balance_due <= Decimal('0.00'):
            raise ValidationError("La factura del proveedor ya se encuentra totalmente liquidada / pagada.")
        if locked_inv.currency_code != locked_po.currency_code:
            raise ValidationError(
                f"Incompatibilidad monetaria: La factura del proveedor ({locked_inv.currency_code}) "
                f"no coincide con la moneda de la orden de compra ({locked_po.currency_code})."
            )
    else:
        # No se especificó factura explícita: evaluar facturas asociadas a la orden
        active_invoices = list(locked_po.supplier_invoices.exclude(status=SupplierInvoice.Status.VOID))
        if len(active_invoices) > 1:
            raise ValidationError(
                f"La orden de compra {locked_po.number} tiene múltiples facturas de proveedor asociadas ({len(active_invoices)}). "
                f"Debe indicar explícitamente la factura que se desea liquidar."
            )
        elif len(active_invoices) == 1:
            single_inv = active_invoices[0]
            if single_inv.status == SupplierInvoice.Status.PAID or single_inv.balance_due <= Decimal('0.00'):
                raise ValidationError("La factura del proveedor asociada a la orden de compra ya se encuentra totalmente liquidada.")
            if single_inv.currency_code != locked_po.currency_code:
                raise ValidationError(
                    f"Incompatibilidad monetaria: La factura del proveedor ({single_inv.currency_code}) "
                    f"no coincide con la moneda de la orden de compra ({locked_po.currency_code})."
                )
            locked_inv = SupplierInvoice.objects.select_for_update().get(pk=single_inv.pk, organization=organization)
        else:
            # 0 facturas asociadas: pago directo a nivel de orden de compra
            locked_inv = None

    # 4. Bloqueo pesimista y validación de la Cuenta Bancaria
    locked_bank = None
    if bank_account:
        if bank_account.organization_id != organization.id:
            raise ValidationError("La cuenta bancaria no pertenece a la organización activa.")
        try:
            locked_bank = BankAccount.objects.select_for_update().get(pk=bank_account.pk, organization=organization)
        except BankAccount.DoesNotExist:
            raise ValidationError("La cuenta bancaria no pertenece a la organización activa.")

        if not locked_bank.is_active:
            raise ValidationError("La cuenta bancaria seleccionada se encuentra inactiva.")
        if locked_bank.currency != curr:
            raise ValidationError(
                f"La moneda de la cuenta bancaria ({locked_bank.currency}) es incompatible con la moneda de pago ({curr})."
            )
        if locked_bank.current_balance < amt:
            raise ValidationError(
                f"Saldo bancario insuficiente en {locked_bank.get_bank_name_display()} ({locked_bank.account_number}). "
                f"Disponible: {locked_bank.currency} {locked_bank.current_balance:,.2f}, Requerido: {locked_bank.currency} {amt:,.2f}."
            )

    # 5. Contención de diferencial cambiario y conversión bimonetaria a la moneda de la Orden de Compra
    if (
        locked_po.currency_code != 'NIO'
        and exchange_rate_applied
        and exchange_rate_applied != locked_po.exchange_rate
    ):
        raise ValidationError(
            f"Operación cambiaria no soportada: La orden de compra {locked_po.number} está denominada en moneda extranjera "
            f"({locked_po.currency_code}) y fue reconocida contablemente a una tasa de {locked_po.exchange_rate:.4f}. "
            f"No se permite liquidar a una tasa diferente ({exchange_rate_applied:.4f}) porque no existe una política contable "
            f"aprobada para el diferencial cambiario (ganancia/pérdida en cambio). Debe utilizar la tasa de reconocimiento oficial "
            f"({locked_po.exchange_rate:.4f}) o liquidar sin alterar la tasa contable registrada."
        )

    if locked_inv and locked_po.currency_code != 'NIO' and locked_inv.exchange_rate != locked_po.exchange_rate:
        raise ValidationError(
            f"Incompatibilidad cambiaria: La tasa de cambio de la factura ({locked_inv.exchange_rate:.4f}) "
            f"difiere de la tasa de reconocimiento contable de la orden de compra ({locked_po.exchange_rate:.4f})."
        )

    tc = exchange_rate_applied or locked_po.exchange_rate
    if curr == locked_po.currency_code:
        applied_to_po = amt
    elif curr == 'USD' and locked_po.currency_code == 'NIO':
        applied_to_po = (amt * tc).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    elif curr == 'NIO' and locked_po.currency_code == 'USD':
        applied_to_po = (amt / tc).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        applied_to_po = amt

    if applied_to_po > locked_po.balance_due:
        raise ValidationError(
            f"El monto aplicado ({locked_po.currency_symbol} {applied_to_po:,.2f}) excede el saldo pendiente "
            f"de la orden ({locked_po.currency_symbol} {locked_po.balance_due:,.2f})."
        )

    if locked_inv and applied_to_po > locked_inv.balance_due:
        raise ValidationError(
            f"El monto aplicado ({locked_inv.currency_code} {applied_to_po:,.2f}) excede el saldo pendiente de la factura del proveedor "
            f"({locked_inv.currency_code} {locked_inv.balance_due:,.2f})."
        )

    # 6. Verificación de Unicidad de Referencia Externa en Bancos
    if clean_ref and locked_bank:
        existing_ref = SupplierPayment.objects.filter(
            organization=organization,
            bank_account=locked_bank,
            reference_number=clean_ref,
        ).first()
        if existing_ref:
            raise ValidationError(
                f"La referencia bancaria '{clean_ref}' ya fue registrada previamente en la cuenta {locked_bank.account_number}."
            )

    # 7. Creación del Comprobante de Egreso (SupplierPayment)
    try:
        egr = SupplierPayment.objects.create(
            organization=organization,
            purchase_order=locked_po,
            supplier_invoice=locked_inv,
            bank_account=locked_bank,
            payment_date=payment_date or timezone.now().date(),
            payment_method=payment_method,
            reference_number=clean_ref,
            currency_paid=curr,
            amount_in_payment_currency=amt,
            exchange_rate_applied=tc,
            amount_applied_to_order=applied_to_po,
            paid_by=user,
            notes=notes,
        )
    except IntegrityError as e:
        if 'unique_supplier_payment_reference_per_bank' in str(e):
            raise ValidationError(
                f"Conflicto de integridad: La referencia bancaria '{clean_ref}' ya fue registrada concurrentemente."
            )
        raise

    # 8. Actualización de Tesorería (BankAccount y FinancialTransaction)
    trx = None
    if locked_bank:
        locked_bank.current_balance -= amt
        locked_bank.save(update_fields=['current_balance', 'updated_at'])

        trx = FinancialTransaction.objects.create(
            organization=organization,
            transaction_type=FinancialTransaction.TransactionType.VENDOR_DISBURSEMENT,
            bank_account=locked_bank,
            amount=amt,
            balance_after=locked_bank.current_balance,
            concept=f"Pago a Proveedor {locked_po.supplier.commercial_name} - Egreso {egr.number} (OC {locked_po.number})",
            reference_code=clean_ref or egr.number,
            performed_by=user,
        )

    # 9. Asiento Contable Cuadrado en Libro Diario (PAYMENTS)
    journal_entry = record_supplier_payment_journal_entry(
        organization=organization,
        supplier_payment=egr,
        user=user,
    )

    # 10. Sincronización explícita y atómica de la Orden de Compra
    total_paid_po = (
        locked_po.payments.aggregate(t=models.Sum('amount_applied_to_order'))['t']
        or Decimal('0.00')
    )
    locked_po.amount_paid = total_paid_po
    locked_po.recalculate_totals()
    locked_po.save(update_fields=['amount_paid', 'balance_due', 'status', 'updated_at'])

    # 11. Sincronización explícita y atómica de la Factura de Proveedor (si aplica)
    if locked_inv:
        locked_inv.recalculate_totals()
        locked_inv.save(update_fields=['amount_paid', 'balance_due', 'status', 'updated_at'])

    # 12. Persistencia de Idempotencia
    if idempotency_key and computed_hash:
        try:
            SupplierDisbursementIdempotencyRecord.objects.create(
                organization=organization,
                idempotency_key=idempotency_key,
                purchase_order=locked_po,
                supplier_invoice=locked_inv,
                supplier_payment=egr,
                request_hash=computed_hash,
                status='SUCCESS',
                response_payload={
                    'payment_id': str(egr.id),
                    'number': egr.number,
                    'amount_applied': str(applied_to_po),
                },
            )
        except IntegrityError:
            # Carrera concurrente con la misma clave de idempotencia
            existing_rec = SupplierDisbursementIdempotencyRecord.objects.filter(
                organization=organization,
                idempotency_key=idempotency_key,
            ).first()
            if existing_rec and existing_rec.request_hash == computed_hash:
                return existing_rec.supplier_payment, trx, journal_entry
            raise ValidationError("La clave de idempotencia especificada ya fue utilizada con parámetros de solicitud diferentes.")

    return egr, trx, journal_entry
