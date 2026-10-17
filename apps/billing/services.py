import hashlib
from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.db import transaction, models, IntegrityError
from django.utils import timezone

from apps.billing.models import (
    Invoice,
    InvoiceItem,
    PaymentReceipt,
    PosRegisterShift,
    PaymentIdempotencyRecord,
)
from apps.crm.models import Account
from apps.inventory.models import (
    InventoryItem,
    Warehouse,
    StockBalance,
    StockMovement,
    InventoryBatch,
)
from apps.finance.models import (
    BankAccount,
    FinancialTransaction,
    JournalEntry,
    JournalEntryLine,
    CheckReceiptRegister,
)
from apps.finance.services import (
    ensure_default_chart_of_accounts,
    get_required_accounting_account,
    record_invoice_sales_journal_entry,
)


def execute_pos_sale(
    organization,
    user,
    warehouse,
    account=None,
    items_data=None,
    cart_items=None,
    payment_method=PaymentReceipt.PaymentMethod.CASH,
    price_tier='RETAIL',
    cash_received=None,
    notes='',
    bank_account=None,
    reference_number='',
    currency_paid=None,
    idempotency_key=None,
) -> Invoice:
    """
    Motor Transaccional de Venta Rápida en Mostrador (Terminal POS) con Integridad Financiera Integral:
    1. Valida pertenencia multi-tenant estricta (bodega, cliente, cuenta bancaria, productos, lotes).
    2. Idempotencia transaccional con firma SHA-256 (PaymentIdempotencyRecord): replay idéntico seguro
       y detección estricta de colisiones concurrentes y reutilización con datos divergentes.
    3. Aplica regla de escala de precios (Detalle, Contratista, Mayorista).
    4. Aplica estricta regla sanitaria FEFO:
       - Selecciona el lote más próximo a vencer no caducado.
       - Si el lote está vencido, rechaza con ValidationError (normativa MINSA).
       - Descuenta existencias del lote.
    5. Control estricto de existencias: bloquea ventas que generen inventario negativo en StockBalance.
    6. Registra movimiento inmutable OUT_SALE_POS en Kardex.
    7. Emite Factura Fiscal DGI (Invoice) y Recibo Oficial de Caja (ROC - PaymentReceipt).
    8. Actualiza saldos de tesorería bancaria (BankAccount) y emite FinancialTransaction si el método es bancario.
    9. Genera asientos contables cuadrados en el Libro Diario:
       - Póliza de Venta (SALES): Débito a Clientes (1103-01) vs Crédito a Ingresos (4101-01) e IVA (2102-01).
       - Póliza de Cobro (COLLECTIONS): Débito a Caja (1101-01) o Bancos (1102-01) vs Crédito a Clientes (1103-01).
    10. Vincula automáticamente la venta al Turno POS activo del cajero.
    """
    items_data = items_data or cart_items
    if not items_data:
        raise ValidationError("El carrito de venta POS no contiene productos para facturar.")

    clean_idempotency_key = (idempotency_key or '').strip()
    clean_ref = (reference_number or '').strip()
    curr_req = (currency_paid or organization.currency).strip().upper()

    # Normalizar firmas de items para cálculo determinista del hash SHA-256
    item_sigs = []
    for entry in items_data:
        raw_it = entry.get('item')
        it_id = str(raw_it.id if isinstance(raw_it, InventoryItem) else (entry.get('item_id') or raw_it or ''))
        qty_str = str(entry.get('quantity', 1))
        pr_str = str(entry.get('unit_price') or '')
        raw_b = entry.get('batch')
        b_id = str(raw_b.id if isinstance(raw_b, InventoryBatch) else (entry.get('batch_id') or raw_b or ''))
        tier_str = str(entry.get('price_tier') or price_tier or 'RETAIL').upper()
        item_sigs.append(f"{it_id}:{qty_str}:{pr_str}:{b_id}:{tier_str}")
    item_sigs.sort()

    w_id = str(warehouse.id if warehouse else '')
    acc_id = str(account.id if account else '')
    bank_id = str(bank_account.id if bank_account else '')

    payload_str = (
        f"{organization.id}:{w_id}:{acc_id}:{payment_method}:{bank_id}:"
        f"{clean_ref}:{price_tier}:{curr_req}:{';'.join(item_sigs)}"
    )
    req_hash = hashlib.sha256(payload_str.encode('utf-8')).hexdigest()

    # Verificación preliminar de idempotencia
    if clean_idempotency_key:
        existing_rec = PaymentIdempotencyRecord.objects.filter(
            organization=organization,
            idempotency_key=clean_idempotency_key,
        ).select_related('invoice', 'payment_receipt').first()

        if existing_rec:
            if existing_rec.request_hash == req_hash:
                inv = existing_rec.invoice
                inv.receipt = existing_rec.payment_receipt
                return inv
            else:
                raise ValidationError("La clave de idempotencia especificada ya fue utilizada con parámetros de solicitud diferentes.")

    # Ejecución transaccional atómica protegida contra colisiones de concurrencia
    try:
        with transaction.atomic():
            return _execute_pos_sale_inner(
                organization=organization,
                user=user,
                warehouse=warehouse,
                account=account,
                items_data=items_data,
                payment_method=payment_method,
                price_tier=price_tier,
                cash_received=cash_received,
                notes=notes,
                bank_account=bank_account,
                clean_ref=clean_ref,
                curr_req=curr_req,
                clean_idempotency_key=clean_idempotency_key,
                req_hash=req_hash,
            )
    except IntegrityError:
        # Colisión concurrente en la clave única de idempotencia
        if clean_idempotency_key:
            existing_rec = PaymentIdempotencyRecord.objects.filter(
                organization=organization,
                idempotency_key=clean_idempotency_key,
            ).select_related('invoice', 'payment_receipt').first()
            if existing_rec:
                if existing_rec.request_hash == req_hash:
                    inv = existing_rec.invoice
                    inv.receipt = existing_rec.payment_receipt
                    return inv
                else:
                    raise ValidationError("La clave de idempotencia especificada ya fue utilizada con parámetros de solicitud diferentes.")
        raise


def _execute_pos_sale_inner(
    organization,
    user,
    warehouse,
    account,
    items_data,
    payment_method,
    price_tier,
    cash_received,
    notes,
    bank_account,
    clean_ref,
    curr_req,
    clean_idempotency_key,
    req_hash,
) -> Invoice:
    # 1. Validación multi-tenant defensiva de bodega
    if warehouse:
        if warehouse.organization_id != organization.id:
            raise ValidationError("La bodega seleccionada no pertenece a la organización.")
    else:
        warehouse = Warehouse.objects.filter(organization=organization, is_active=True).first()
        if not warehouse:
            raise ValidationError("No existe ninguna bodega activa configurada para registrar la salida de inventario.")

    # 2. Validación multi-tenant o resolución de cliente de mostrador
    if account:
        if account.organization_id != organization.id:
            raise ValidationError("El cliente especificado no pertenece a la organización.")
    else:
        account = Account.objects.filter(
            organization=organization,
            commercial_name__icontains='Consumidor Final'
        ).first()
        if not account:
            account = Account.objects.create(
                organization=organization,
                commercial_name='Consumidor Final / Mostrador',
                legal_name='Cliente de Mostrador',
                tax_id='',
                account_type=Account.AccountType.INDIVIDUAL,
                lifecycle_stage=Account.LifecycleStage.ACTIVE,
            )

    # 3. Categorización estricta de medio de pago y validación defensiva de cuenta bancaria
    m_str = str(payment_method or '').strip().upper()

    is_cash = (
        m_str == PaymentReceipt.PaymentMethod.CASH
        or m_str in ['CASH', 'EFECTIVO']
    )
    is_transfer = (
        m_str in [
            PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            PaymentReceipt.PaymentMethod.TRANSFER_BANPRO,
            PaymentReceipt.PaymentMethod.TRANSFER_LAFISE,
            PaymentReceipt.PaymentMethod.TRANSFER_FICOHSA,
        ]
        or m_str.startswith('TRANSFER')
    )
    is_card = (
        m_str in [PaymentReceipt.PaymentMethod.CARD, 'CARD', 'TARJETA', 'TARJETA_CREDITO', 'TARJETA_DEBITO']
    )
    is_check = (
        m_str in [PaymentReceipt.PaymentMethod.CHECK, 'CHECK', 'CHEQUE']
    )

    locked_bank = None
    if is_transfer:
        if not bank_account:
            raise ValidationError("Las ventas cobradas mediante transferencia bancaria requieren una cuenta bancaria de destino explícita.")
        if bank_account.organization_id != organization.id:
            raise ValidationError("La cuenta bancaria no pertenece a la organización de la venta.")
        if not bank_account.is_active:
            raise ValidationError("La cuenta bancaria seleccionada se encuentra inactiva.")
        locked_bank = BankAccount.objects.select_for_update().get(id=bank_account.id, organization=organization)
    elif is_card or is_check:
        # En cobros con tarjeta o cheque recibidos en mostrador, los fondos NO están acreditados en banco.
        # Por tanto, no se bloquea ni se modifica BankAccount.current_balance en el momento de la venta.
        locked_bank = None
    elif is_cash:
        locked_bank = None
    else:
        # Método no reconocido específicamente: si es bancario exige cuenta sin fallback .first()
        if bank_account:
            if bank_account.organization_id != organization.id:
                raise ValidationError("La cuenta bancaria no pertenece a la organización de la venta.")
            if not bank_account.is_active:
                raise ValidationError("La cuenta bancaria seleccionada se encuentra inactiva.")
            locked_bank = BankAccount.objects.select_for_update().get(id=bank_account.id, organization=organization)
        else:
            locked_bank = None

    # Verificación de unicidad de referencia bancaria para transferencias confirmadas
    if clean_ref and locked_bank:
        existing_ref = PaymentReceipt.objects.filter(
            organization=organization,
            bank_account=locked_bank,
            reference_number=clean_ref,
        ).first()
        if existing_ref:
            raise ValidationError(
                f"La referencia bancaria '{clean_ref}' ya fue utilizada en esta cuenta bancaria para el recibo {existing_ref.number}."
            )

    # 4. Fase de Validación y Bloqueo Pesimista Previo de Inventario (Prevención de Carreras y Deadlocks)
    # Ordenamos por ID de producto para garantizar un orden de bloqueo canónico determinista
    prepared_items = []
    for entry in items_data:
        raw_item = entry.get('item')
        if isinstance(raw_item, InventoryItem):
            item_id = raw_item.id
        else:
            item_id = entry.get('item_id') or raw_item
        prepared_items.append((str(item_id), entry))
    prepared_items.sort(key=lambda x: x[0])

    locked_lines = []
    for item_id_str, entry in prepared_items:
        raw_item = entry.get('item')
        if isinstance(raw_item, InventoryItem):
            if raw_item.organization_id != organization.id:
                raise ValidationError(f"El producto '{raw_item.name}' no pertenece a la organización.")
            item = InventoryItem.objects.select_for_update().get(id=raw_item.id, organization=organization)
        else:
            try:
                item = InventoryItem.objects.select_for_update().get(id=item_id_str, organization=organization)
            except (InventoryItem.DoesNotExist, ValueError):
                raise ValidationError("El producto no existe o no pertenece a la empresa.")

        try:
            qty = Decimal(str(entry.get('quantity', 1)))
        except (ValueError, TypeError):
            raise ValidationError("La cantidad especificada es inválida.")

        if qty <= Decimal('0.00'):
            raise ValidationError("La cantidad a facturar debe ser estrictamente mayor a 0.")

        # Resolución de precio unitario según tier o entrada
        explicit_price = entry.get('unit_price')
        if explicit_price is not None and Decimal(str(explicit_price)) > Decimal('0.00'):
            unit_price = Decimal(str(explicit_price))
        else:
            tier = (entry.get('price_tier') or price_tier or 'RETAIL').upper()
            if tier == 'CONTRACTOR':
                unit_price = item.price_contractor if item.price_contractor > Decimal('0.00') else item.price_retail
            elif tier == 'WHOLESALE' or (item.wholesale_min_qty and qty >= item.wholesale_min_qty and item.price_wholesale > Decimal('0.00')):
                unit_price = item.price_wholesale if item.price_wholesale > Decimal('0.00') else item.price_retail
            else:
                unit_price = item.price_retail if item.price_retail > Decimal('0.00') else item.unit_cost

            if unit_price <= Decimal('0.00'):
                unit_price = item.unit_cost if item.unit_cost > Decimal('0.00') else Decimal('1.00')

        # Control FEFO & Lotes Farmacéuticos
        batch = None
        raw_batch = entry.get('batch')
        batch_id = entry.get('batch_id') or (raw_batch.id if isinstance(raw_batch, InventoryBatch) else raw_batch)

        if batch_id:
            batch = InventoryBatch.objects.select_for_update().filter(id=batch_id, organization=organization).first()
            if not batch:
                raise ValidationError("El lote sanitario especificado no existe o no pertenece a la empresa.")
            if batch.warehouse_id != warehouse.id:
                raise ValidationError("El lote sanitario no pertenece a la bodega seleccionada.")
            if batch.item_id != item.id:
                raise ValidationError("El lote especificado no corresponde al producto facturado.")
        elif item.requires_batch_control or organization.is_pharmacy_vertical() or item.batches.filter(warehouse=warehouse).exists():
            fefo_candidate = InventoryBatch.objects.select_for_update().filter(
                organization=organization,
                warehouse=warehouse,
                item=item,
                quantity_available__gt=Decimal('0.00')
            ).order_by('expiration_date').first()

            if fefo_candidate:
                batch = fefo_candidate
            else:
                any_batch = InventoryBatch.objects.select_for_update().filter(
                    organization=organization,
                    warehouse=warehouse,
                    item=item
                ).order_by('expiration_date').first()
                if any_batch and any_batch.expiry_status == 'EXPIRED':
                    raise ValidationError(
                        f"Operación rechazada: El lote {any_batch.batch_number} de '{item.name}' está vencido (bloqueado por normativa sanitaria MINSA)."
                    )
                raise ValidationError(f"No hay lotes disponibles para el producto '{item.name}' en la bodega seleccionada.")

        # Validación sanitaria de caducidad y existencia de lote
        if batch:
            if batch.expiry_status == 'EXPIRED':
                raise ValidationError(
                    f"Operación rechazada: El lote {batch.batch_number} de '{item.name}' está vencido (bloqueado por normativa sanitaria MINSA)."
                )
            if batch.quantity_available < qty:
                raise ValidationError(
                    f"Existencia insuficiente en el lote {batch.batch_number} ({batch.quantity_available} {item.unit} disponibles, se solicitaron {qty} {item.unit})."
                )

        # Descontar existencia global de la bodega (StockBalance) con protección contra stock negativo
        balance, _ = StockBalance.objects.select_for_update().get_or_create(
            organization=organization,
            warehouse=warehouse,
            item=item,
            defaults={'quantity': Decimal('0.00')}
        )
        if balance.quantity < qty:
            raise ValidationError(
                f"Existencia insuficiente para el producto '{item.name}' en la bodega '{warehouse.name}' "
                f"(disponibles: {balance.quantity} {item.unit}, solicitados: {qty} {item.unit})."
            )

        locked_lines.append({
            'item': item,
            'qty': qty,
            'unit_price': unit_price,
            'batch': batch,
            'balance': balance,
        })

    # 5. Generación e Inicialización Segura de Factura Fiscal DGI
    year = timezone.now().year
    prefix = f"FAC-{year}-"
    invoice = None
    for attempt in range(10):
        last_num = Invoice.objects.filter(
            organization_id=organization.id,
            number__startswith=prefix
        ).order_by('-number').values_list('number', flat=True).first()

        if last_num:
            try:
                seq = int(last_num.split('-')[-1]) + 1 + attempt
            except (ValueError, IndexError):
                seq = Invoice.objects.filter(organization_id=organization.id, number__startswith=prefix).count() + 1 + attempt
        else:
            seq = 1 + attempt

        cand_num = f"{prefix}{seq:04d}"
        try:
            with transaction.atomic():
                invoice = Invoice.objects.create(
                    organization=organization,
                    account=account,
                    number=cand_num,
                    payment_condition=Invoice.PaymentCondition.CASH,
                    status=Invoice.Status.ISSUED,
                    currency_code=organization.currency,
                    currency_symbol=organization.currency_symbol,
                    apply_tax=True,
                    tax_rate=Decimal('15.00'),
                    created_by=user,
                    notes=notes or "Venta en Mostrador / Terminal POS",
                )
                break
        except IntegrityError:
            continue

    if not invoice:
        invoice = Invoice.objects.create(
            organization=organization,
            account=account,
            payment_condition=Invoice.PaymentCondition.CASH,
            status=Invoice.Status.ISSUED,
            currency_code=organization.currency,
            currency_symbol=organization.currency_symbol,
            apply_tax=True,
            tax_rate=Decimal('15.00'),
            created_by=user,
            notes=notes or "Venta en Mostrador / Terminal POS",
        )

    # 6. Descontar existencias y asentar partidas y Kardex
    for line in locked_lines:
        item = line['item']
        qty = line['qty']
        unit_price = line['unit_price']
        batch = line['batch']
        balance = line['balance']

        if batch:
            batch.quantity_available -= qty
            batch.save(update_fields=['quantity_available', 'updated_at'])

        balance.quantity -= qty
        balance.save(update_fields=['quantity', 'updated_at'])

        # Partida de Factura Fiscal
        item_description = f"{item.name} [Lote: {batch.batch_number}]" if batch else item.name
        InvoiceItem.objects.create(
            invoice=invoice,
            item_type='PRODUCT',
            code=item.sku,
            description=item_description,
            unit=item.unit,
            quantity=qty,
            unit_price=unit_price,
            discount_percent=Decimal('0.00'),
        )

        # Kardex Inmutable OUT_SALE_POS
        StockMovement.objects.create(
            organization=organization,
            movement_type=StockMovement.MovementType.OUT_SALE_POS,
            warehouse=warehouse,
            item=item,
            batch=batch,
            invoice=invoice,
            quantity=qty,
            unit_cost_snapshot=item.unit_cost,
            balance_after=balance.quantity,
            reference_note=f"Venta Mostrador POS {invoice.number} - {account.commercial_name}",
            performed_by=user,
        )

    # 6. Cálculo final de impuestos y totales
    if organization.is_pharmacy_vertical():
        invoice.apply_tax = False

    invoice.recalculate_totals()
    invoice.save()

    # 7. Contabilizar Emisión de Factura Fiscal en el Libro Diario (Póliza SALES)
    accounts_chart = ensure_default_chart_of_accounts(organization)
    record_invoice_sales_journal_entry(organization, invoice, user, accounts=accounts_chart)

    # 8. Emisión de Recibo Oficial de Caja (ROC) al contado
    ref_to_use = clean_ref if clean_ref else f"POS-{invoice.number}"
    receipt_curr = curr_req or invoice.currency_code

    if receipt_curr == invoice.currency_code:
        amount_in_receipt_curr = invoice.net_receivable
    elif receipt_curr == 'USD' and invoice.currency_code == 'NIO':
        amount_in_receipt_curr = (invoice.net_receivable / invoice.exchange_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    elif receipt_curr == 'NIO' and invoice.currency_code == 'USD':
        amount_in_receipt_curr = (invoice.net_receivable * invoice.exchange_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        amount_in_receipt_curr = invoice.net_receivable

    receipt_method = PaymentReceipt.PaymentMethod.CARD if is_card else (
        PaymentReceipt.PaymentMethod.CHECK if is_check else (
            payment_method or PaymentReceipt.PaymentMethod.CASH
        )
    )

    receipt = PaymentReceipt.objects.create(
        organization=organization,
        invoice=invoice,
        bank_account=locked_bank,
        payment_date=timezone.now().date(),
        payment_method=receipt_method,
        reference_number=ref_to_use,
        currency_paid=receipt_curr,
        amount_in_payment_currency=amount_in_receipt_curr,
        amount_applied_to_invoice=invoice.net_receivable,
        exchange_rate_applied=invoice.exchange_rate,
        received_by=user,
        notes=notes or "Cancelación total de contado en terminal de mostrador (POS)",
    )

    # 9. Movimiento de Tesorería bancaria únicamente para transferencias confirmadas
    trx = None
    if is_transfer and locked_bank:
        if locked_bank.currency == receipt_curr:
            bank_amt = amount_in_receipt_curr
        elif locked_bank.currency == 'NIO' and receipt_curr == 'USD':
            bank_amt = (amount_in_receipt_curr * invoice.exchange_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        elif locked_bank.currency == 'USD' and receipt_curr == 'NIO':
            bank_amt = (amount_in_receipt_curr / invoice.exchange_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            bank_amt = amount_in_receipt_curr

        locked_bank.current_balance += bank_amt
        locked_bank.save(update_fields=['current_balance', 'updated_at'])

        trx = FinancialTransaction.objects.create(
            organization=organization,
            transaction_type=FinancialTransaction.TransactionType.CLIENT_COLLECTION,
            bank_account=locked_bank,
            amount=bank_amt,
            balance_after=locked_bank.current_balance,
            concept=f"Cobro Mostrador POS Recibo {receipt.number} - Factura {invoice.number}",
            reference_code=receipt.reference_number or receipt.number,
            performed_by=user,
        )
    elif is_check:
        # Registro en cartera y custodia física del cheque
        check_record = CheckReceiptRegister.objects.create(
            organization=organization,
            check_number=clean_ref or f"CHK-{invoice.number}",
            drawer_bank=(bank_account.bank_name if bank_account else ''),
            drawer_name=invoice.account.commercial_name if invoice.account else 'Cliente Mostrador',
            amount=amount_in_receipt_curr,
            currency=receipt_curr,
            received_date=receipt.payment_date,
            status=CheckReceiptRegister.Status.RECEIVED,
            payment_receipt=receipt,
            source_invoice=invoice,
            deposit_bank_account=bank_account if (bank_account and bank_account.organization_id == organization.id) else None,
            received_by=user,
        )
        invoice.check_record = check_record

    # 10. Contabilizar Cobro en el Libro Diario (Póliza COLLECTIONS)
    rate_nio = invoice.exchange_rate if invoice.currency_code == 'USD' else Decimal('1.0')
    amt_nio = (invoice.net_receivable * rate_nio).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    collection_entry = JournalEntry.objects.filter(
        organization=organization,
        source_payment_receipt=receipt,
        entry_type=JournalEntry.EntryType.COLLECTIONS,
    ).first()

    if not collection_entry:
        collection_entry = JournalEntry.objects.create(
            organization=organization,
            date=receipt.payment_date,
            entry_type=JournalEntry.EntryType.COLLECTIONS,
            concept=f"Cobro POS {receipt.number} Factura {invoice.number} - {invoice.account.commercial_name if invoice.account else 'Consumidor Final'}",
            reference=receipt.number,
            source_invoice=invoice,
            source_payment_receipt=receipt,
            created_by=user,
        )

        if is_transfer and locked_bank:
            acc_debit = get_required_accounting_account(accounts_chart, '1102-01', organization)
        elif is_card:
            acc_debit = get_required_accounting_account(accounts_chart, '1101-03', organization)
        elif is_check:
            acc_debit = get_required_accounting_account(accounts_chart, '1101-02', organization)
        else:
            acc_debit = get_required_accounting_account(accounts_chart, '1101-01', organization)

        acc_cxc = get_required_accounting_account(accounts_chart, '1103-01', organization)

        JournalEntryLine.objects.create(
            organization=organization,
            entry=collection_entry,
            account=acc_debit,
            concept=f"Ingreso Mostrador {receipt.get_payment_method_display()} {receipt.number}",
            debit=amt_nio,
            credit=Decimal('0.00'),
        )
        JournalEntryLine.objects.create(
            organization=organization,
            entry=collection_entry,
            account=acc_cxc,
            concept=f"Abono CxC Factura Mostrador {invoice.number}",
            debit=Decimal('0.00'),
            credit=amt_nio,
        )
        collection_entry.verify_and_save_balance()

    # 11. Vinculación automática a Turno POS activo de la organización
    active_shift = PosRegisterShift.objects.filter(
        organization=organization,
        status=PosRegisterShift.Status.OPEN,
    ).filter(
        models.Q(cashier=user) | models.Q(cashier__isnull=False)
    ).order_by('-opened_at').first()

    invoice.amount_paid = invoice.net_receivable
    invoice.balance_due = Decimal('0.00')
    invoice.status = Invoice.Status.PAID
    if active_shift:
        invoice.pos_shift = active_shift

    invoice.save(update_fields=['amount_paid', 'balance_due', 'status', 'pos_shift', 'updated_at'])

    if active_shift:
        if is_cash:
            from apps.billing.models import CashMovement
            CashMovement.objects.create(
                organization=organization,
                shift=active_shift,
                movement_type=CashMovement.MovementType.POS_SALE,
                amount=amt_nio,
                concept=f"Venta POS Mostrador {invoice.number}",
                reference=invoice.number,
                performed_by=user,
                invoice=invoice,
                payment_receipt=receipt,
            )
        from apps.billing.services_pos_shift import compute_shift_x_cut_metrics
        compute_shift_x_cut_metrics(active_shift)

    # 12. Asentar Registro de Idempotencia de Solicitud
    if clean_idempotency_key:
        PaymentIdempotencyRecord.objects.create(
            organization=organization,
            idempotency_key=clean_idempotency_key,
            invoice=invoice,
            payment_receipt=receipt,
            request_hash=req_hash,
            status='SUCCESS',
            response_payload={
                'invoice_id': str(invoice.pk),
                'invoice_number': invoice.number,
                'receipt_id': str(receipt.pk),
                'receipt_number': receipt.number,
                'net_receivable': str(invoice.net_receivable),
                'currency': invoice.currency_code,
            }
        )

    invoice.receipt = receipt
    invoice.transaction = trx
    invoice.sales_entry = invoice.journal_entries.filter(entry_type=JournalEntry.EntryType.SALES).first()
    invoice.collection_entry = collection_entry

    return invoice
