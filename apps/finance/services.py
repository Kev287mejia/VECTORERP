from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.operations.models import WorkOrderTimelineLog
from apps.finance.models import (
    BankAccount,
    PettyCashFund,
    PettyCashExpense,
    PettyCashReplenishment,
    FinancialTransaction,
)


@transaction.atomic
def allocate_petty_cash_fund(
    organization,
    fund: PettyCashFund,
    bank_account: BankAccount,
    amount: Decimal,
    user
) -> FinancialTransaction:
    """
    Desembolsa la apertura o dotación inicial de una Caja Chica desde una cuenta bancaria.
    Bloquea la cuenta con select_for_update().
    """
    amt = Decimal(str(amount))
    if amt <= Decimal('0.00'):
        raise ValidationError("El monto a dotar debe ser estrictamente mayor a 0.00.")

    bank = BankAccount.objects.select_for_update().get(pk=bank_account.pk, organization=organization)
    caja = PettyCashFund.objects.select_for_update().get(pk=fund.pk, organization=organization)

    if bank.current_balance < amt:
        raise ValidationError(
            f"Saldo bancario insuficiente en {bank.bank_name} ({bank.account_number}). "
            f"Disponible: C$ {bank.current_balance:,.2f}, Requerido: C$ {amt:,.2f}."
        )

    if caja.current_balance + amt > caja.allocated_amount:
        raise ValidationError(
            f"La dotación de C$ {amt:,.2f} excede el monto máximo autorizado del fondo "
            f"({caja.code} - C$ {caja.allocated_amount:,.2f}). Saldo actual: C$ {caja.current_balance:,.2f}."
        )

    # 1. Descuento bancario
    bank.current_balance -= amt
    bank.save(update_fields=['current_balance', 'updated_at'])

    # 2. Incremento en caja chica
    caja.current_balance += amt
    caja.save(update_fields=['current_balance', 'updated_at'])

    # 3. Asiento inmutable en libro mayor financiero
    trx = FinancialTransaction.objects.create(
        organization=organization,
        transaction_type=FinancialTransaction.TransactionType.PETTY_CASH_ALLOCATION,
        bank_account=bank,
        petty_cash_fund=caja,
        amount=amt,
        balance_after=bank.current_balance,
        concept=f"Apertura / Dotación de Fondo Fijo {caja.code} ({caja.name})",
        reference_code=f"CHQ-{caja.code}",
        performed_by=user,
    )
    return trx


@transaction.atomic
def record_petty_cash_expense(
    organization,
    fund: PettyCashFund,
    expense_category: str,
    vendor_name: str,
    amount: Decimal,
    description: str,
    user,
    work_order=None,
    vendor_tax_id: str = '',
    invoice_number: str = '',
    spent_at=None,
) -> PettyCashExpense:
    """
    Registra un gasto operativo en el frente de obra.
    Bloquea la caja chica con select_for_update() impidiendo sobregiros o saldos negativos.
    Si se imputa a una OT, asienta el costo y la bitácora técnica automáticamente.
    """
    amt = Decimal(str(amount))
    if amt <= Decimal('0.00'):
        raise ValidationError("El monto del comprobante de gasto debe ser mayor a 0.00.")

    caja = PettyCashFund.objects.select_for_update().get(pk=fund.pk, organization=organization)

    if caja.current_balance < amt:
        raise ValidationError(
            f"Saldo insuficiente en la caja chica '{caja.name}'. "
            f"Disponible en mano: C$ {caja.current_balance:,.2f}, Gasto presentado: C$ {amt:,.2f}. "
            f"Se requiere solicitar reposición de fondo fijo."
        )

    # 1. Descontar saldo físico de la caja
    caja.current_balance -= amt
    caja.save(update_fields=['current_balance', 'updated_at'])

    # 2. Crear comprobante de gasto
    expense = PettyCashExpense.objects.create(
        organization=organization,
        fund=caja,
        work_order=work_order,
        expense_category=expense_category,
        vendor_name=vendor_name,
        vendor_tax_id=vendor_tax_id,
        invoice_number=invoice_number,
        amount=amt,
        description=description,
        spent_at=spent_at or timezone.now().date(),
        created_by=user,
        is_replenished=False,
    )

    # 3. Asiento inmutable en libro mayor financiero
    FinancialTransaction.objects.create(
        organization=organization,
        transaction_type=FinancialTransaction.TransactionType.PETTY_CASH_EXPENSE,
        petty_cash_fund=caja,
        amount=amt,
        balance_after=caja.current_balance,
        concept=f"Comprobante {expense.voucher_number}: {description} ({vendor_name})",
        reference_code=invoice_number or expense.voucher_number,
        performed_by=user,
    )

    # 4. Trazabilidad en la OT de Operaciones (si aplica)
    if work_order:
        WorkOrderTimelineLog.objects.create(
            organization=organization,
            work_order=work_order,
            event_type='PETTY_CASH_EXPENSE',
            summary=f"Gasto operativo imputado: C$ {amt:,.2f} ({expense.get_expense_category_display()})",
            details=(
                f"Folio Recibo: {expense.voucher_number}. "
                f"Proveedor: {vendor_name} (Factura #{invoice_number or 'S/N'}). "
                f"Caja Chica: {caja.name}. "
                f"Justificación: {description}. "
                f"Registrado por: {user.full_name or user.email}."
            ),
            actor=user,
        )

    return expense


@transaction.atomic
def submit_petty_cash_replenishment(
    organization,
    fund: PettyCashFund,
    notes: str,
    user
) -> PettyCashReplenishment:
    """
    Genera el arqueo de caja chica agrupando todos los comprobantes pendientes
    y emite la solicitud de reposición para revisión de Coordinación/Gerencia.
    """
    caja = PettyCashFund.objects.select_for_update().get(pk=fund.pk, organization=organization)
    pending_expenses = list(caja.expenses.filter(is_replenished=False, replenishment__isnull=True))

    if not pending_expenses:
        raise ValidationError("No existen comprobantes de gastos pendientes de reposición en esta caja chica.")

    total_amount = sum((exp.amount for exp in pending_expenses), Decimal('0.00'))

    replenishment = PettyCashReplenishment.objects.create(
        organization=organization,
        fund=caja,
        status=PettyCashReplenishment.Status.SUBMITTED,
        total_expenses_amount=total_amount,
        requested_amount=total_amount,
        notes=notes,
        submitted_by=user,
    )

    # Vincular gastos a esta reposición
    PettyCashExpense.objects.filter(
        pk__in=[exp.pk for exp in pending_expenses]
    ).update(replenishment=replenishment, updated_at=timezone.now())

    return replenishment


@transaction.atomic
def disburse_petty_cash_replenishment(
    organization,
    replenishment: PettyCashReplenishment,
    bank_account: BankAccount,
    user
) -> FinancialTransaction:
    """
    Aprueba y desembolsa la reposición de fondo fijo desde la cuenta bancaria.
    Restaura el saldo físico de la caja chica y marca los gastos como liquidados.
    """
    rep = PettyCashReplenishment.objects.select_for_update().get(pk=replenishment.pk, organization=organization)
    if rep.status == PettyCashReplenishment.Status.DISBURSED:
        raise ValidationError("Esta reposición ya fue desembolsada anteriormente.")

    bank = BankAccount.objects.select_for_update().get(pk=bank_account.pk, organization=organization)
    caja = PettyCashFund.objects.select_for_update().get(pk=rep.fund.pk, organization=organization)

    amt = rep.requested_amount
    if bank.current_balance < amt:
        raise ValidationError(
            f"Saldo bancario insuficiente en {bank.bank_name}. "
            f"Disponible: C$ {bank.current_balance:,.2f}, Monto de reposición: C$ {amt:,.2f}."
        )

    # 1. Descuento bancario
    bank.current_balance -= amt
    bank.save(update_fields=['current_balance', 'updated_at'])

    # 2. Reintegro a la caja chica (topeado al fondo fijo asignado)
    caja.current_balance += amt
    caja.save(update_fields=['current_balance', 'updated_at'])

    # 3. Actualizar estado de la reposición
    rep.status = PettyCashReplenishment.Status.DISBURSED
    rep.approved_by = user
    rep.approved_at = timezone.now()
    rep.disbursed_from_bank = bank
    rep.disbursed_at = timezone.now()
    rep.save(update_fields=[
        'status', 'approved_by', 'approved_at',
        'disbursed_from_bank', 'disbursed_at', 'updated_at'
    ])

    # 4. Marcar gastos como liquidados formalmente
    rep.expenses.all().update(is_replenished=True, updated_at=timezone.now())

    # 5. Asiento inmutable en libro mayor financiero
    trx = FinancialTransaction.objects.create(
        organization=organization,
        transaction_type=FinancialTransaction.TransactionType.PETTY_CASH_REPLENISH,
        bank_account=bank,
        petty_cash_fund=caja,
        amount=amt,
        balance_after=bank.current_balance,
        concept=f"Desembolso de Reposición {rep.code} para {caja.name}",
        reference_code=rep.code,
        performed_by=user,
    )
    return trx


def ensure_default_chart_of_accounts(organization):
    """
    Garantiza la existencia del catálogo básico NIIF / DGI Nicaragua para el tenant.
    Idempotente: crea únicamente las cuentas faltantes.
    """
    from apps.finance.models import AccountingAccount

    default_accounts = [
        ('1101-01', 'Caja General y Mostrador', AccountingAccount.AccountType.ASSET),
        ('1101-02', 'Cheques en Cartera y Valores por Depositar', AccountingAccount.AccountType.ASSET),
        ('1101-03', 'Tarjetas y Vouchers por Liquidar / Adquirentes', AccountingAccount.AccountType.ASSET),
        ('1101-04', 'Efectivo y Remesas en Tránsito para Depósito', AccountingAccount.AccountType.ASSET),
        ('1102-01', 'Bancos Institucionales', AccountingAccount.AccountType.ASSET),
        ('1103-01', 'Clientes por Cobrar (CxC)', AccountingAccount.AccountType.ASSET),
        ('1104-01', 'IVA Crédito Fiscal Acreditable (15%)', AccountingAccount.AccountType.ASSET),
        ('1105-01', 'Anticipo Retención IR Sufrida (1% - 2%)', AccountingAccount.AccountType.ASSET),
        ('1105-02', 'Anticipo Retención IMI Municipal Sufrida (1%)', AccountingAccount.AccountType.ASSET),
        ('1106-01', 'Inventarios de Mercancías y Materiales', AccountingAccount.AccountType.ASSET),
        ('2101-01', 'Proveedores Nacionales (CxP)', AccountingAccount.AccountType.LIABILITY),
        ('2102-01', 'IVA Débito Fiscal por Pagar (15%)', AccountingAccount.AccountType.LIABILITY),
        ('2103-01', 'Retenciones IR en la Fuente por Enterar DGI', AccountingAccount.AccountType.LIABILITY),
        ('2103-02', 'Retenciones IMI Alcaldía por Enterar', AccountingAccount.AccountType.LIABILITY),
        ('3101-01', 'Capital Social Pagado / Aporte Inicial', AccountingAccount.AccountType.EQUITY),
        ('3201-01', 'Resultados Acumulados de Ejercicios Anteriores', AccountingAccount.AccountType.EQUITY),
        ('4101-01', 'Ingresos por Ventas de Bienes y Servicios', AccountingAccount.AccountType.INCOME),
        ('4201-01', 'Ganancia por Diferencial Cambiario', AccountingAccount.AccountType.INCOME),
        ('5101-01', 'Costo de Ventas, Compras e Insumos', AccountingAccount.AccountType.EXPENSE),
        ('6101-01', 'Gastos Generales y de Administración', AccountingAccount.AccountType.EXPENSE),
        ('6102-01', 'Comisiones Bancarias y Procesamiento de Tarjetas', AccountingAccount.AccountType.EXPENSE),
        ('6201-01', 'Pérdida por Diferencial Cambiario', AccountingAccount.AccountType.EXPENSE),
    ]
    accounts_map = {}
    for code, name, acct_type in default_accounts:
        acc, _ = AccountingAccount.objects.get_or_create(
            organization=organization,
            code=code,
            defaults={'name': name, 'account_type': acct_type, 'is_active': True}
        )
        accounts_map[code] = acc
    return accounts_map


def get_required_accounting_account(accounts_chart, code, organization):
    """
    Retorna la cuenta contable requerida desde el mapa o base de datos.
    Falla de manera explícita y segura con ValidationError si no existe o está inactiva.
    """
    from django.core.exceptions import ValidationError
    acc = accounts_chart.get(code) if accounts_chart else None
    if not acc:
        from apps.finance.models import AccountingAccount
        acc = AccountingAccount.objects.filter(organization=organization, code=code, is_active=True).first()
    if not acc or not acc.is_active:
        raise ValidationError(f"La cuenta contable requerida '{code}' no está configurada o se encuentra inactiva para la empresa.")
    return acc


def record_invoice_sales_journal_entry(organization, invoice, user, accounts=None):
    """
    Contabiliza la emisión de una Factura de Venta en el Libro Diario (SALES).
    Idempotente: si la factura ya posee póliza de ventas, retorna la existente sin duplicar.
    Blindado contra condiciones de carrera concurrentes mediante manejo atómico de IntegrityError.
    Convierte importes a Córdobas (NIO) si la factura fue emitida en divisa extranjera (USD).
    """
    from decimal import Decimal, ROUND_HALF_UP
    from django.db import IntegrityError, transaction
    from apps.finance.models import JournalEntry, JournalEntryLine

    existing = invoice.journal_entries.filter(entry_type=JournalEntry.EntryType.SALES).first()
    if existing:
        return existing

    if accounts is None:
        accounts = ensure_default_chart_of_accounts(organization)

    rate = invoice.exchange_rate if invoice.currency_code == 'USD' else Decimal('1.0')
    taxable_base = invoice.subtotal - getattr(invoice, 'discount_total', Decimal('0.00'))
    net_receivable_nio = (invoice.net_receivable * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    subtotal_nio = (taxable_base * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    tax_total_nio = (invoice.tax_total * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    ir_retention_nio = (invoice.ir_retention_amount * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    imi_retention_nio = (invoice.imi_retention_amount * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    try:
        with transaction.atomic():
            existing = invoice.journal_entries.filter(entry_type=JournalEntry.EntryType.SALES).first()
            if existing:
                return existing

            entry = JournalEntry.objects.create(
                organization=organization,
                date=invoice.issue_date or timezone.now().date(),
                entry_type=JournalEntry.EntryType.SALES,
                concept=f"Facturación DGI {invoice.number} - {invoice.account.commercial_name if invoice.account else 'Cliente'}",
                reference=invoice.number,
                source_invoice=invoice,
                created_by=user,
            )

            # DEBE: Clientes por Cobrar (Neto a cobrar)
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry,
                account=accounts['1103-01'],
                concept=f"CxC Factura {invoice.number}",
                debit=net_receivable_nio,
                credit=Decimal('0.00'),
            )

            # DEBE: Retención IR sufrida (si aplica)
            if ir_retention_nio > Decimal('0.00'):
                JournalEntryLine.objects.create(
                    organization=organization,
                    entry=entry,
                    account=accounts['1105-01'],
                    concept=f"Retención IR 1%-2% Sufrida {invoice.number}",
                    debit=ir_retention_nio,
                    credit=Decimal('0.00'),
                )

            # DEBE: Retención IMI sufrida (si aplica)
            if imi_retention_nio > Decimal('0.00'):
                JournalEntryLine.objects.create(
                    organization=organization,
                    entry=entry,
                    account=accounts['1105-02'],
                    concept=f"Retención IMI 1% Sufrida {invoice.number}",
                    debit=imi_retention_nio,
                    credit=Decimal('0.00'),
                )

            # HABER: Ventas / Ingresos (Base Imponible)
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry,
                account=accounts['4101-01'],
                concept=f"Ingreso Gravado/Exento {invoice.number}",
                debit=Decimal('0.00'),
                credit=subtotal_nio,
            )

            # HABER: IVA Débito Fiscal (si aplica)
            if tax_total_nio > Decimal('0.00'):
                JournalEntryLine.objects.create(
                    organization=organization,
                    entry=entry,
                    account=accounts['2102-01'],
                    concept=f"IVA 15% Débito Fiscal {invoice.number}",
                    debit=Decimal('0.00'),
                    credit=tax_total_nio,
                )

            entry.verify_and_save_balance()
            return entry
    except IntegrityError:
        # Carrera de concurrencia: otro hilo insertó la póliza respetando el constraint único
        existing = invoice.journal_entries.filter(entry_type=JournalEntry.EntryType.SALES).first()
        if existing:
            return existing
        raise


def record_purchase_order_journal_entry(organization, purchase_order, user, accounts=None, supplier_invoice=None):
    """
    Contabiliza la recepción física y devengo de una Orden de Compra en el Libro Diario (PURCHASES).
    Idempotente: si la orden de compra ya posee póliza de compras, retorna la existente sin duplicar.
    Blindado contra condiciones de carrera concurrentes mediante manejo atómico de IntegrityError.
    Convierte importes a Córdobas (NIO) si la orden fue emitida en divisa extranjera (USD) a la tasa oficial BCN.
    Debita la cuenta de Activo Circulante de Inventarios ('1106-01') bajo NIIF / IAS 2, con fallback a '5101-01' si no existe.
    """
    from decimal import Decimal, ROUND_HALF_UP
    from django.db import IntegrityError, transaction
    from apps.finance.models import JournalEntry, JournalEntryLine

    existing = purchase_order.journal_entries.filter(entry_type=JournalEntry.EntryType.PURCHASES).first()
    if existing:
        return existing

    if accounts is None:
        accounts = ensure_default_chart_of_accounts(organization)

    rate = purchase_order.exchange_rate if purchase_order.currency_code == 'USD' else Decimal('1.0')
    subtotal_nio = (purchase_order.subtotal * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    tax_total_nio = (purchase_order.tax_total * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    ir_retention_nio = (purchase_order.ir_withheld_amount * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    imi_retention_nio = (purchase_order.imi_withheld_amount * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    net_payable_nio = (purchase_order.net_payable * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # Identificar cuenta de inventarios o costos
    inventory_account = accounts.get('1106-01') or accounts.get('5101-01')

    try:
        with transaction.atomic():
            existing = purchase_order.journal_entries.filter(entry_type=JournalEntry.EntryType.PURCHASES).first()
            if existing:
                return existing

            entry = JournalEntry.objects.create(
                organization=organization,
                date=purchase_order.received_at.date() if purchase_order.received_at else (purchase_order.issue_date or timezone.now().date()),
                entry_type=JournalEntry.EntryType.PURCHASES,
                concept=f"Compra Recibida {purchase_order.number} - {purchase_order.supplier.commercial_name if purchase_order.supplier else 'Proveedor'}",
                reference=purchase_order.number,
                source_purchase_order=purchase_order,
                source_supplier_invoice=supplier_invoice,
                created_by=user,
            )

            # 1. DEBE: Inventario de Mercancías y Materiales (Subtotal en NIO)
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry,
                account=inventory_account,
                concept=f"Inventario/Materiales OC {purchase_order.number}",
                debit=subtotal_nio,
                credit=Decimal('0.00'),
            )

            # 2. DEBE: IVA Crédito Fiscal Acreditable 15% (si aplica)
            if tax_total_nio > Decimal('0.00'):
                JournalEntryLine.objects.create(
                    organization=organization,
                    entry=entry,
                    account=accounts['1104-01'],
                    concept=f"IVA Crédito Fiscal 15% OC {purchase_order.number}",
                    debit=tax_total_nio,
                    credit=Decimal('0.00'),
                )

            # 3. HABER: Retención IR DGI por Enterar (si aplica)
            if ir_retention_nio > Decimal('0.00'):
                JournalEntryLine.objects.create(
                    organization=organization,
                    entry=entry,
                    account=accounts['2103-01'],
                    concept=f"Retención IR DGI {purchase_order.withholding_certificate_number or purchase_order.number}",
                    debit=Decimal('0.00'),
                    credit=ir_retention_nio,
                )

            # 4. HABER: Retención IMI Alcaldía por Enterar (si aplica)
            if imi_retention_nio > Decimal('0.00'):
                JournalEntryLine.objects.create(
                    organization=organization,
                    entry=entry,
                    account=accounts['2103-02'],
                    concept=f"Retención IMI Alcaldía {purchase_order.withholding_certificate_number or purchase_order.number}",
                    debit=Decimal('0.00'),
                    credit=imi_retention_nio,
                )

            # 5. HABER: Proveedores Nacionales (CxP) (Neto a Pagar en NIO)
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry,
                account=accounts['2101-01'],
                concept=f"CxP Fact. Proveedor {purchase_order.supplier_invoice_number or purchase_order.number}",
                debit=Decimal('0.00'),
                credit=net_payable_nio,
            )

            entry.verify_and_save_balance()
            return entry
    except IntegrityError:
        # Carrera de concurrencia: otro hilo insertó la póliza respetando el constraint único
        existing = purchase_order.journal_entries.filter(entry_type=JournalEntry.EntryType.PURCHASES).first()
        if existing:
            return existing
        raise


def record_supplier_payment_journal_entry(organization, supplier_payment, user, accounts=None):
    """
    Contabiliza la liquidación de un Comprobante de Egreso (EGR) en el Libro Diario (PAYMENTS).
    Idempotente: si el pago ya posee póliza de egreso, retorna la existente sin duplicar.
    Blindado contra condiciones de carrera concurrentes mediante manejo atómico de IntegrityError.
    Reconoce de forma segura y exacta el Diferencial Cambiario Realizado (Fase 1C.6):
    - Cuentas por Pagar (2101-01) se amortiza al valor contable histórico en NIO.
      En la última liquidación que extingue la obligación, extingue exactamente el saldo contable residual remanente.
    - Tesorería (1102-01 / 1101-01) se acredita al valor real desembolsado en NIO a la tasa de liquidación.
    - La diferencia resultante se reconoce como Pérdida por Diferencial Cambiario (DEBE 6201-01) o Ganancia por Diferencial Cambiario (HABER 4201-01).
    """
    from decimal import Decimal, ROUND_HALF_UP
    from django.db import IntegrityError, transaction, models
    from apps.finance.models import JournalEntry, JournalEntryLine

    existing = supplier_payment.journal_entries.filter(entry_type=JournalEntry.EntryType.PAYMENTS).first()
    if existing:
        return existing

    if accounts is None:
        accounts = ensure_default_chart_of_accounts(organization)

    po = supplier_payment.purchase_order
    is_foreign_currency = (po.currency_code != 'NIO')

    # 1. Determinar el valor de la deuda que se cancela en NIO (CxP 2101-01)
    if is_foreign_currency:
        hist_rate = po.exchange_rate
        total_hist_nio = (po.net_payable * hist_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if po.balance_due <= Decimal('0.00'):
            # Última liquidación: extinguir exactamente el saldo contable residual
            prev_amortizations_nio = JournalEntryLine.objects.filter(
                entry__organization=organization,
                entry__source_purchase_order=po,
                entry__entry_type=JournalEntry.EntryType.PAYMENTS,
                account=accounts['2101-01'],
            ).exclude(entry__source_supplier_payment=supplier_payment).aggregate(t=models.Sum('debit'))['t'] or Decimal('0.00')
            cxp_nio = (total_hist_nio - prev_amortizations_nio).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            cxp_nio = (supplier_payment.amount_applied_to_order * hist_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        cxp_nio = supplier_payment.amount_applied_to_order.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # 2. Determinar el desembolso real en NIO (Bancos / Caja)
    if supplier_payment.currency_paid != 'NIO':
        if not supplier_payment.exchange_rate_applied or supplier_payment.exchange_rate_applied <= Decimal('0.0000'):
            raise ValidationError(
                f"Se requiere una tasa de cambio oficial válida y estrictamente positiva (> 0) "
                f"para registrar egresos en moneda extranjera ({supplier_payment.currency_paid})."
            )
        settlement_rate = supplier_payment.exchange_rate_applied
        disbursement_nio = (supplier_payment.amount_in_payment_currency * settlement_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        settlement_rate = supplier_payment.exchange_rate_applied or Decimal('1.0')
        disbursement_nio = supplier_payment.amount_in_payment_currency.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # 3. Determinar diferencial cambiario
    fx_diff = disbursement_nio - cxp_nio

    # Determinar cuenta de tesorería contra la que se desembolsó
    if supplier_payment.bank_account_id:
        cash_or_bank_account = accounts.get('1102-01') or accounts.get('1101-01')
        credit_concept = f"Desembolso {supplier_payment.bank_account.bank_name} {supplier_payment.bank_account.account_number}"
    else:
        cash_or_bank_account = accounts.get('1101-01') or accounts.get('1102-01')
        credit_concept = "Desembolso Efectivo en Caja General"

    try:
        with transaction.atomic():
            existing = supplier_payment.journal_entries.filter(entry_type=JournalEntry.EntryType.PAYMENTS).first()
            if existing:
                return existing

            supplier_name = po.supplier.commercial_name if po.supplier else "Proveedor"
            entry = JournalEntry.objects.create(
                organization=organization,
                date=supplier_payment.payment_date or timezone.now().date(),
                entry_type=JournalEntry.EntryType.PAYMENTS,
                concept=f"Pago {supplier_payment.number} a {supplier_name} - OC {po.number}",
                reference=supplier_payment.number,
                source_purchase_order=po,
                source_supplier_invoice=supplier_payment.supplier_invoice,
                source_supplier_payment=supplier_payment,
                created_by=user,
            )

            # 1. DEBE: Proveedores Nacionales (CxP) por el valor histórico cancelado
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry,
                account=accounts['2101-01'],
                concept=f"Amortización CxP {po.number} - {supplier_payment.number}",
                debit=cxp_nio,
                credit=Decimal('0.00'),
            )

            # 2. Diferencial Cambiario
            if fx_diff > Decimal('0.00'):
                # Pérdida cambiaria (DEBE 6201-01)
                JournalEntryLine.objects.create(
                    organization=organization,
                    entry=entry,
                    account=accounts['6201-01'],
                    concept=f"Pérdida por Diferencial Cambiario - {supplier_payment.number}",
                    debit=fx_diff,
                    credit=Decimal('0.00'),
                )
            elif fx_diff < Decimal('0.00'):
                # Ganancia cambiaria (HABER 4201-01)
                gain_amt = abs(fx_diff)
                JournalEntryLine.objects.create(
                    organization=organization,
                    entry=entry,
                    account=accounts['4201-01'],
                    concept=f"Ganancia por Diferencial Cambiario - {supplier_payment.number}",
                    debit=Decimal('0.00'),
                    credit=gain_amt,
                )

            # 3. HABER: Bancos Institucionales o Caja por el desembolso real
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry,
                account=cash_or_bank_account,
                concept=credit_concept,
                debit=Decimal('0.00'),
                credit=disbursement_nio,
            )

            entry.verify_and_save_balance()
            return entry
    except IntegrityError:
        existing = supplier_payment.journal_entries.filter(entry_type=JournalEntry.EntryType.PAYMENTS).first()
        if existing:
            return existing
        raise





@transaction.atomic
def record_client_collection(
    organization,
    invoice,
    amount: Decimal,
    payment_method: str,
    user,
    bank_account=None,
    currency_paid: str = None,
    reference_number: str = '',
    notes: str = '',
    payment_date=None,
    idempotency_key: str = None,
    exchange_rate_applied: Decimal = None,
):
    """
    Orquesta atómicamente el ciclo completo de cobranza:
    1. Valida pertenencia de la factura y estado cobrable.
    2. Valida la cuenta bancaria (misma organización, activa y con moneda compatible).
    3. Maneja Idempotencia estricta vía PaymentIdempotencyRecord (token SHA-256 de parámetros).
    4. Bloquea pesimísticamente la factura y la cuenta bancaria (select_for_update).
    5. Valida importes y previene cobros excedentes al saldo pendiente.
    6. Valida unicidad de referencia bancaria por cuenta y organización.
    7. Emite el Recibo Oficial de Caja (PaymentReceipt).
    8. Si es bancario, incrementa BankAccount.current_balance y asienta FinancialTransaction.
    9. Asegura la póliza de ventas (SALES) y genera la póliza de cobranza cuadrada (COLLECTIONS)
       reconociendo ganancias (4201-01) o pérdidas (6201-01) cambiarias realizadas de forma exacta.
    """
    import hashlib
    from decimal import Decimal, ROUND_HALF_UP
    from django.db import IntegrityError, models
    from django.core.exceptions import ValidationError
    from apps.billing.models import PaymentReceipt, Invoice, PaymentIdempotencyRecord
    from apps.finance.models import BankAccount, FinancialTransaction, JournalEntry, JournalEntryLine

    if invoice.organization_id != organization.id:
        raise ValidationError("La factura no pertenece a la organización actual.")

    amt = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if amt <= Decimal('0.00'):
        raise ValidationError("El monto recaudado debe ser estrictamente mayor a 0.00.")

    clean_ref = (reference_number or '').strip()
    clean_idempotency_key = (idempotency_key or '').strip()
    curr = (currency_paid or invoice.currency_code).strip().upper()

    if exchange_rate_applied is not None:
        rate_to_apply = Decimal(str(exchange_rate_applied))
        if rate_to_apply <= Decimal('0.0000'):
            raise ValidationError("La tasa de cambio aplicada debe ser un valor positivo estrictamente mayor a 0.")
    else:
        rate_to_apply = invoice.exchange_rate if invoice.exchange_rate and invoice.exchange_rate > Decimal('0.0000') else Decimal('1.0')

    if rate_to_apply <= Decimal('0.0000'):
        raise ValidationError("La tasa de cambio aplicada debe ser un valor positivo estrictamente mayor a 0.")

    is_bank_method = payment_method in [
        PaymentReceipt.PaymentMethod.TRANSFER_BAC,
        PaymentReceipt.PaymentMethod.TRANSFER_BANPRO,
        PaymentReceipt.PaymentMethod.TRANSFER_LAFISE,
        PaymentReceipt.PaymentMethod.TRANSFER_FICOHSA,
        PaymentReceipt.PaymentMethod.CHECK,
    ]

    if is_bank_method:
        if not bank_account:
            raise ValidationError("Los cobros mediante transferencia bancaria o cheque requieren una cuenta bancaria de destino.")
        if bank_account.organization_id != organization.id:
            raise ValidationError("La cuenta bancaria no pertenece a la organización de la factura.")
        if not bank_account.is_active:
            raise ValidationError("La cuenta bancaria seleccionada se encuentra inactiva.")
        if bank_account.currency != curr and not (set([bank_account.currency, curr]) <= {'NIO', 'USD'}):
            raise ValidationError(
                f"La moneda de la cuenta bancaria ({bank_account.currency}) es incompatible con la moneda de recaudación ({curr})."
            )

    bank_id_str = str(bank_account.pk if bank_account else '')

    # 1. Comprobación y Replay de Idempotencia de Solicitud
    req_payload_str = f"{organization.id}:{invoice.pk}:{amt}:{curr}:{payment_method}:{bank_id_str}:{clean_ref}:{rate_to_apply}"
    req_hash = hashlib.sha256(req_payload_str.encode('utf-8')).hexdigest()

    if clean_idempotency_key:
        existing_rec = PaymentIdempotencyRecord.objects.select_for_update().filter(
            organization=organization,
            idempotency_key=clean_idempotency_key,
        ).first()
        if existing_rec:
            if existing_rec.request_hash == req_hash:
                rec_receipt = existing_rec.payment_receipt
                rec_trx = FinancialTransaction.objects.filter(
                    organization=organization,
                    reference_code=rec_receipt.reference_number or rec_receipt.number,
                ).first() if rec_receipt else None
                rec_entry = JournalEntry.objects.filter(
                    organization=organization,
                    source_payment_receipt=rec_receipt,
                    entry_type=JournalEntry.EntryType.COLLECTIONS,
                ).first() if rec_receipt else None
                return rec_receipt, rec_trx, rec_entry
            else:
                raise ValidationError("La clave de idempotencia especificada ya fue utilizada con parámetros de solicitud diferentes.")

    # 2. Bloqueo pesimista de la factura
    locked_inv = Invoice.objects.select_for_update().get(pk=invoice.pk, organization=organization)
    if locked_inv.status in [Invoice.Status.PAID, Invoice.Status.VOID]:
        raise ValidationError(f"No se pueden registrar cobros en una factura con estado {locked_inv.get_status_display()}.")

    # 3. Bloqueo pesimista de la cuenta bancaria
    locked_bank = None
    if bank_account:
        locked_bank = BankAccount.objects.select_for_update().get(pk=bank_account.pk, organization=organization)
        if locked_bank.currency != curr and not (set([locked_bank.currency, curr]) <= {'NIO', 'USD'}):
            raise ValidationError(
                f"La moneda de la cuenta bancaria ({locked_bank.currency}) es incompatible con la moneda de recaudación ({curr})."
            )

    # 4. Conversión bimonetaria a la moneda de la factura
    if curr == locked_inv.currency_code:
        applied_to_inv = amt
    elif curr == 'USD' and locked_inv.currency_code == 'NIO':
        applied_to_inv = (amt * rate_to_apply).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    elif curr == 'NIO' and locked_inv.currency_code == 'USD':
        applied_to_inv = (amt / rate_to_apply).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        applied_to_inv = amt

    if applied_to_inv > locked_inv.balance_due:
        raise ValidationError(
            f"El monto recaudado ({locked_inv.currency_symbol} {applied_to_inv:,.2f}) excede el saldo pendiente "
            f"de la factura ({locked_inv.currency_symbol} {locked_inv.balance_due:,.2f})."
        )

    # 5. Verificación de Unicidad de Referencia Bancaria
    if clean_ref:
        if locked_bank:
            existing_ref = PaymentReceipt.objects.filter(
                organization=organization,
                bank_account=locked_bank,
                reference_number=clean_ref,
            ).first()
            if existing_ref:
                if existing_ref.invoice_id == locked_inv.id:
                    raise ValidationError(f"La referencia bancaria '{clean_ref}' ya fue registrada previamente para esta factura.")
                else:
                    raise ValidationError(
                        f"La referencia bancaria '{clean_ref}' ya fue utilizada en esta cuenta bancaria para la factura {existing_ref.invoice.number}."
                    )
        else:
            existing_ref = PaymentReceipt.objects.filter(
                organization=organization,
                reference_number=clean_ref,
            ).first()
            if existing_ref:
                raise ValidationError(f"La referencia '{clean_ref}' ya fue registrada previamente en la organización.")

    # 6. Emisión del Recibo Oficial de Caja (PaymentReceipt)
    try:
        receipt = PaymentReceipt.objects.create(
            organization=organization,
            invoice=locked_inv,
            bank_account=locked_bank,
            payment_date=payment_date or timezone.now().date(),
            payment_method=payment_method,
            reference_number=clean_ref,
            currency_paid=curr,
            amount_in_payment_currency=amt,
            exchange_rate_applied=rate_to_apply,
            amount_applied_to_invoice=applied_to_inv,
            received_by=user,
            notes=notes,
        )
    except IntegrityError:
        raise ValidationError(f"Conflicto de integridad: La referencia bancaria '{clean_ref}' ya fue registrada concurrentemente.")

    # 7. Movimiento de Tesorería (BankAccount y FinancialTransaction)
    trx = None
    if locked_bank:
        if locked_bank.currency == curr:
            bank_amt = amt
        elif locked_bank.currency == 'NIO' and curr == 'USD':
            bank_amt = (amt * rate_to_apply).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        elif locked_bank.currency == 'USD' and curr == 'NIO':
            bank_amt = (amt / rate_to_apply).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            bank_amt = amt

        locked_bank.current_balance += bank_amt
        locked_bank.save(update_fields=['current_balance', 'updated_at'])

        trx = FinancialTransaction.objects.create(
            organization=organization,
            transaction_type=FinancialTransaction.TransactionType.CLIENT_COLLECTION,
            bank_account=locked_bank,
            amount=bank_amt,
            balance_after=locked_bank.current_balance,
            concept=f"Cobro Recibo {receipt.number} - Factura {locked_inv.number}",
            reference_code=receipt.reference_number or receipt.number,
            performed_by=user,
        )

    # 8. Asiento Contable de Cobranza (COLLECTIONS)
    accounts = ensure_default_chart_of_accounts(organization)

    # Garantizar que exista el asiento de venta previo
    record_invoice_sales_journal_entry(organization, locked_inv, user, accounts=accounts)

    is_foreign_currency = (locked_inv.currency_code != 'NIO')
    locked_inv.refresh_from_db()

    # 1. Determinar el valor del derecho que se cancela en NIO (CxC 1103-01)
    if is_foreign_currency:
        hist_rate = locked_inv.exchange_rate
        total_hist_nio = (locked_inv.net_receivable * hist_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if locked_inv.balance_due <= Decimal('0.00'):
            # Última liquidación: extinguir exactamente el saldo contable residual
            prev_credits_nio = JournalEntryLine.objects.filter(
                entry__organization=organization,
                entry__source_invoice=locked_inv,
                entry__entry_type=JournalEntry.EntryType.COLLECTIONS,
                account=accounts['1103-01'],
            ).exclude(entry__source_payment_receipt=receipt).aggregate(t=models.Sum('credit'))['t'] or Decimal('0.00')
            cxc_nio = (total_hist_nio - prev_credits_nio).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            cxc_nio = (applied_to_inv * hist_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        cxc_nio = applied_to_inv.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # 2. Determinar el valor real recaudado en NIO (Bancos / Caja)
    if curr == 'USD':
        received_nio = (amt * rate_to_apply).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    elif curr == 'NIO':
        received_nio = amt.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        received_nio = (amt * rate_to_apply).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # 3. Determinar diferencial cambiario en Cobranza
    fx_diff = received_nio - cxc_nio

    existing_col = JournalEntry.objects.filter(
        organization=organization,
        source_payment_receipt=receipt,
        entry_type=JournalEntry.EntryType.COLLECTIONS,
    ).first()

    if existing_col:
        collection_entry = existing_col
    else:
        try:
            collection_entry = JournalEntry.objects.create(
                organization=organization,
                date=receipt.payment_date,
                entry_type=JournalEntry.EntryType.COLLECTIONS,
                concept=f"Cobro {receipt.number} Factura {locked_inv.number} - {locked_inv.account.commercial_name if locked_inv.account else 'Cliente'}",
                reference=receipt.number,
                source_invoice=locked_inv,
                source_payment_receipt=receipt,
                created_by=user,
            )

            acc_debit = accounts['1102-01'] if locked_bank else accounts['1101-01']

            # 1. DEBE: Bancos o Caja por el valor real recaudado en NIO
            JournalEntryLine.objects.create(
                organization=organization,
                entry=collection_entry,
                account=acc_debit,
                concept=f"Ingreso {receipt.get_payment_method_display()} {receipt.number}",
                debit=received_nio,
                credit=Decimal('0.00'),
            )

            # 2. Diferencial Cambiario
            if fx_diff > Decimal('0.00'):
                # Ganancia cambiaria (HABER 4201-01)
                JournalEntryLine.objects.create(
                    organization=organization,
                    entry=collection_entry,
                    account=accounts['4201-01'],
                    concept=f"Ganancia por Diferencial Cambiario - {receipt.number}",
                    debit=Decimal('0.00'),
                    credit=fx_diff,
                )
            elif fx_diff < Decimal('0.00'):
                # Pérdida cambiaria (DEBE 6201-01)
                loss_amt = abs(fx_diff)
                JournalEntryLine.objects.create(
                    organization=organization,
                    entry=collection_entry,
                    account=accounts['6201-01'],
                    concept=f"Pérdida por Diferencial Cambiario - {receipt.number}",
                    debit=loss_amt,
                    credit=Decimal('0.00'),
                )

            # 3. HABER: Clientes / CxC (1103-01) por el valor histórico cancelado
            JournalEntryLine.objects.create(
                organization=organization,
                entry=collection_entry,
                account=accounts['1103-01'],
                concept=f"Abono CxC Factura {locked_inv.number}",
                debit=Decimal('0.00'),
                credit=cxc_nio,
            )

            collection_entry.verify_and_save_balance()
        except IntegrityError:
            collection_entry = JournalEntry.objects.filter(
                organization=organization,
                source_payment_receipt=receipt,
                entry_type=JournalEntry.EntryType.COLLECTIONS,
            ).first()

    # 9. Asentar Registro de Idempotencia de Solicitud
    if clean_idempotency_key:
        PaymentIdempotencyRecord.objects.create(
            organization=organization,
            idempotency_key=clean_idempotency_key,
            invoice=locked_inv,
            payment_receipt=receipt,
            request_hash=req_hash,
            status='SUCCESS',
            response_payload={
                'receipt_id': str(receipt.pk),
                'receipt_number': receipt.number,
                'amount_paid': str(receipt.amount_in_payment_currency),
                'currency': receipt.currency_paid,
            }
        )

    return receipt, trx, collection_entry


@transaction.atomic
def sync_commercial_and_purchasing_to_ledger(organization, user):
    """
    Puente de Sincronización Contable Automática:
    Asienta pólizas de diario cuadradas (Debe == Haber) para todas las Facturas
    y Órdenes de Compra no sincronizadas previamente.
    Conserva la retrocompatibilidad y no duplica pólizas históricas existentes.
    """
    from apps.billing.models import Invoice
    from apps.purchasing.models import PurchaseOrder
    from apps.finance.models import JournalEntry, JournalEntryLine

    accounts = ensure_default_chart_of_accounts(organization)
    synced_invoices = 0
    synced_purchases = 0

    # 1. Pólizas de Ventas (Facturas Emitidas / Cobradas)
    invoices = Invoice.objects.filter(
        organization=organization,
        status__in=[Invoice.Status.ISSUED, Invoice.Status.PARTIAL, Invoice.Status.PAID],
    ).exclude(journal_entries__entry_type=JournalEntry.EntryType.SALES)

    for inv in invoices:
        record_invoice_sales_journal_entry(organization, inv, user, accounts=accounts)
        synced_invoices += 1

    # 2. Pólizas de Compras (Órdenes Recibidas / En Cartera)
    purchases = PurchaseOrder.objects.filter(
        organization=organization,
        status__in=[PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.PARTIAL_PAID, PurchaseOrder.Status.PAID],
    ).exclude(journal_entries__entry_type=JournalEntry.EntryType.PURCHASES)

    for po in purchases:
        record_purchase_order_journal_entry(organization, po, user, accounts=accounts)
        synced_purchases += 1

    return {
        'invoices_synced': synced_invoices,
        'purchases_synced': synced_purchases,
        'total_entries': synced_invoices + synced_purchases,
    }


def record_card_batch_settlement(
    organization,
    bank_account,
    batch_reference: str,
    gross_amount: Decimal,
    fee_amount: Decimal = Decimal('0.00'),
    tax_retention_amount: Decimal = Decimal('0.00'),
    user=None,
    settlement_date=None,
    notes: str = '',
    idempotency_key: str = None,
):
    """
    Liquidación Atómica de Lotes de Tarjetas de Crédito / Débito:
    1. Valida pertenencia multi-tenant y estado activo de la cuenta bancaria.
    2. Valida importes:
       - gross_amount > 0
       - fee_amount >= 0
       - tax_retention_amount >= 0
       - net_amount = gross_amount - fee_amount - tax_retention_amount > 0
    3. Idempotencia estricta por clave (reintentos idénticos devuelven la liquidación existente,
       parámetros discrepantes lanzan ValidationError).
    4. Unicidad de referencia de lote por cuenta bancaria y organización.
    5. Bloqueo pesimista de cuenta bancaria (select_for_update).
    6. Incrementa BankAccount.current_balance += net_amount.
    7. Emite FinancialTransaction (CARD_SETTLEMENT).
    8. Emite JournalEntry (COLLECTIONS) con partida doble cuadrada:
       - Débito: 1102-01 (Bancos) por net_amount
       - Débito: 6102-01 (Comisiones Bancarias) por fee_amount (si fee_amount > 0)
       - Débito: 1105-01 (Anticipo Retención IR) por tax_retention_amount (si tax_retention_amount > 0)
       - Crédito: 1101-03 (Tarjetas y Vouchers por Liquidar) por gross_amount
    9. Crea y retorna CardBatchSettlement.
    """
    from decimal import Decimal, ROUND_HALF_UP
    from django.db import IntegrityError, transaction
    from django.core.exceptions import ValidationError
    from apps.finance.models import CardBatchSettlement

    clean_idempotency_key = (idempotency_key or '').strip()
    clean_batch_ref = (batch_reference or '').strip()
    gross = Decimal(str(gross_amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    fee = Decimal(str(fee_amount or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    retention = Decimal(str(tax_retention_amount or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    if clean_idempotency_key:
        existing = CardBatchSettlement.objects.filter(
            organization=organization,
            idempotency_key=clean_idempotency_key,
        ).first()
        if existing:
            if (
                existing.bank_account_id == bank_account.id
                and existing.gross_amount == gross
                and existing.fee_amount == fee
                and existing.tax_retention_amount == retention
                and existing.batch_reference == clean_batch_ref
            ):
                return existing
            else:
                raise ValidationError("La clave de idempotencia especificada ya fue utilizada con parámetros de liquidación diferentes.")

    try:
        with transaction.atomic():
            return _record_card_batch_settlement_inner(
                organization=organization,
                bank_account=bank_account,
                batch_reference=batch_reference,
                gross_amount=gross_amount,
                fee_amount=fee_amount,
                tax_retention_amount=tax_retention_amount,
                user=user,
                settlement_date=settlement_date,
                notes=notes,
                clean_idempotency_key=clean_idempotency_key,
                clean_batch_ref=clean_batch_ref,
                gross=gross,
                fee=fee,
                retention=retention,
            )
    except IntegrityError:
        if clean_idempotency_key:
            existing = CardBatchSettlement.objects.filter(
                organization=organization,
                idempotency_key=clean_idempotency_key,
            ).first()
            if existing:
                if (
                    existing.bank_account_id == bank_account.id
                    and existing.gross_amount == gross
                    and existing.fee_amount == fee
                    and existing.tax_retention_amount == retention
                    and existing.batch_reference == clean_batch_ref
                ):
                    return existing
                else:
                    raise ValidationError("La clave de idempotencia especificada ya fue utilizada con parámetros de liquidación diferentes.")
        raise


def _record_card_batch_settlement_inner(
    organization,
    bank_account,
    batch_reference: str,
    gross_amount: Decimal,
    fee_amount: Decimal,
    tax_retention_amount: Decimal,
    user,
    settlement_date,
    notes: str,
    clean_idempotency_key: str,
    clean_batch_ref: str,
    gross: Decimal,
    fee: Decimal,
    retention: Decimal,
):
    from decimal import Decimal, ROUND_HALF_UP
    from django.core.exceptions import ValidationError
    from apps.finance.models import (
        BankAccount,
        CardBatchSettlement,
        FinancialTransaction,
        JournalEntry,
        JournalEntryLine,
    )

    if bank_account.organization_id != organization.id:
        raise ValidationError("La cuenta bancaria no pertenece a la organización de la liquidación.")
    if not bank_account.is_active:
        raise ValidationError("La cuenta bancaria seleccionada se encuentra inactiva.")

    if not clean_batch_ref:
        raise ValidationError("Se requiere una referencia o número de lote del adquirente para liquidar.")

    if gross <= Decimal('0.00'):
        raise ValidationError("El importe bruto del lote a liquidar debe ser estrictamente mayor a 0.00.")
    if fee < Decimal('0.00') or retention < Decimal('0.00'):
        raise ValidationError("Las comisiones y retenciones no pueden ser negativas.")

    net = gross - fee - retention
    if net <= Decimal('0.00'):
        raise ValidationError(
            f"El importe neto a acreditar ({net:.2f}) debe ser mayor a 0.00. Comisiones y retenciones exceden o igualan el bruto."
        )

    # Validar que no se liquide dos veces el mismo lote en la misma cuenta bancaria
    dup_batch = CardBatchSettlement.objects.filter(
        organization=organization,
        bank_account=bank_account,
        batch_reference=clean_batch_ref,
    ).first()
    if dup_batch:
        raise ValidationError(
            f"El lote '{clean_batch_ref}' ya fue liquidado previamente en esta cuenta bancaria con folio {dup_batch.settlement_number}."
        )

    # Bloqueo pesimista de cuenta bancaria
    locked_bank = BankAccount.objects.select_for_update().get(id=bank_account.id, organization=organization)
    locked_bank.current_balance += net
    locked_bank.save(update_fields=['current_balance', 'updated_at'])

    # Transacción de tesorería
    trx = FinancialTransaction.objects.create(
        organization=organization,
        transaction_type=FinancialTransaction.TransactionType.CARD_SETTLEMENT,
        bank_account=locked_bank,
        amount=net,
        balance_after=locked_bank.current_balance,
        concept=f"Liquidación Lote Tarjetas {clean_batch_ref} (Bruto: {gross}, Comis: {fee}, Ret: {retention})",
        reference_code=clean_batch_ref,
        performed_by=user,
    )

    # Asiento contable
    accounts_chart = ensure_default_chart_of_accounts(organization)
    acc_bank = get_required_accounting_account(accounts_chart, '1102-01', organization)
    acc_cards_clearing = get_required_accounting_account(accounts_chart, '1101-03', organization)
    acc_fee = get_required_accounting_account(accounts_chart, '6102-01', organization) if fee > Decimal('0.00') else None
    acc_ret = get_required_accounting_account(accounts_chart, '1105-01', organization) if retention > Decimal('0.00') else None

    entry = JournalEntry.objects.create(
        organization=organization,
        date=settlement_date or timezone.now().date(),
        entry_type=JournalEntry.EntryType.COLLECTIONS,
        concept=f"Liquidación Lote Tarjetas {clean_batch_ref} en {locked_bank.get_bank_name_display()} {locked_bank.account_number}",
        reference=clean_batch_ref,
        created_by=user,
    )

    # Línea Débito a Bancos por el Neto Acreditado
    JournalEntryLine.objects.create(
        organization=organization,
        entry=entry,
        account=acc_bank,
        concept=f"Depósito Neto Lote Tarjetas {clean_batch_ref}",
        debit=net,
        credit=Decimal('0.00'),
    )

    # Línea Débito a Comisiones Bancarias (Gasto) si aplica
    if fee > Decimal('0.00'):
        JournalEntryLine.objects.create(
            organization=organization,
            entry=entry,
            account=acc_fee,
            concept=f"Comisión Adquirente Lote Tarjetas {clean_batch_ref}",
            debit=fee,
            credit=Decimal('0.00'),
        )

    # Línea Débito a Anticipo Retención IR (Activo Fiscal) si aplica
    if retention > Decimal('0.00'):
        JournalEntryLine.objects.create(
            organization=organization,
            entry=entry,
            account=acc_ret,
            concept=f"Retención Fiscal en Tarjeta Lote {clean_batch_ref}",
            debit=retention,
            credit=Decimal('0.00'),
        )

    # Línea Crédito a Tarjetas y Vouchers por Liquidar por el Bruto
    JournalEntryLine.objects.create(
        organization=organization,
        entry=entry,
        account=acc_cards_clearing,
        concept=f"Baja de Vouchers Liquidados Lote {clean_batch_ref}",
        debit=Decimal('0.00'),
        credit=gross,
    )

    settlement = CardBatchSettlement.objects.create(
        organization=organization,
        bank_account=locked_bank,
        batch_reference=clean_batch_ref,
        settlement_date=settlement_date or timezone.now().date(),
        currency=locked_bank.currency,
        gross_amount=gross,
        fee_amount=fee,
        tax_retention_amount=retention,
        net_amount=net,
        status=CardBatchSettlement.Status.SETTLED,
        idempotency_key=clean_idempotency_key,
        notes=notes,
        financial_transaction=trx,
        journal_entry=entry,
        performed_by=user,
    )

    entry.source_card_settlement = settlement
    entry.save(update_fields=['source_card_settlement', 'updated_at'])

    entry.verify_and_save_balance()
    if not entry.is_balanced:
        raise ValidationError("Error contable: la póliza de liquidación de tarjetas no está cuadrada.")

    return settlement


@transaction.atomic
def deposit_check_to_bank(
    organization,
    check_record,
    bank_account,
    deposit_slip_number: str,
    user=None,
):
    """
    Registra el depósito físico de un cheque en remesa bancaria:
    - Valida que el cheque esté en estado RECEIVED.
    - Valida pertenencia multi-tenant y estado activo de la cuenta bancaria.
    - No acredita fondos aún (continúa en compensación).
    - Actualiza el estado a DEPOSITED con la cuenta bancaria y la boleta de depósito.
    """
    from django.core.exceptions import ValidationError
    from apps.finance.models import CheckReceiptRegister

    if check_record.organization_id != organization.id:
        raise ValidationError("El registro de cheque no pertenece a la organización.")
    if bank_account.organization_id != organization.id:
        raise ValidationError("La cuenta bancaria no pertenece a la organización.")
    if not bank_account.is_active:
        raise ValidationError("La cuenta bancaria seleccionada se encuentra inactiva.")

    locked_check = CheckReceiptRegister.objects.select_for_update().get(id=check_record.id, organization=organization)
    if locked_check.status != CheckReceiptRegister.Status.RECEIVED:
        raise ValidationError(f"Solo se pueden depositar cheques en estado Recibido. Estado actual: {locked_check.get_status_display()}.")

    if bank_account.currency != locked_check.currency:
        raise ValidationError(
            f"Incompatibilidad de divisa: No se puede depositar un cheque en {locked_check.currency} en una cuenta bancaria en {bank_account.currency}."
        )

    clean_slip = (deposit_slip_number or '').strip()
    if not clean_slip:
        raise ValidationError("Se requiere el número de boleta o minuta de depósito bancario.")

    locked_check.deposit_bank_account = bank_account
    locked_check.deposit_slip_number = clean_slip
    locked_check.status = CheckReceiptRegister.Status.DEPOSITED
    locked_check.save(update_fields=['deposit_bank_account', 'deposit_slip_number', 'status', 'updated_at'])
    return locked_check


@transaction.atomic
def clear_check_deposit(
    organization,
    check_record,
    user=None,
    bank_account=None,
    clearing_reference: str = '',
):
    """
    Compensación y Acreditación Líquida de Cheque en Cuenta Bancaria:
    1. Valida que el cheque esté en estado DEPOSITED o RECEIVED.
    2. Determina y bloquea pesimísticamente la cuenta bancaria destino.
    3. Incrementa BankAccount.current_balance += check_record.amount.
    4. Emite FinancialTransaction (CHECK_CLEARING).
    5. Emite JournalEntry (COLLECTIONS):
       - Débito: 1102-01 (Bancos Institucionales) por check_record.amount
       - Crédito: 1101-02 (Cheques en Cartera y Valores por Depositar) por check_record.amount
    6. Actualiza estado del cheque a CLEARED con timestamp y enlaces de auditoría.
    """
    from django.core.exceptions import ValidationError
    from apps.finance.models import (
        BankAccount,
        CheckReceiptRegister,
        FinancialTransaction,
        JournalEntry,
        JournalEntryLine,
    )

    if check_record.organization_id != organization.id:
        raise ValidationError("El registro de cheque no pertenece a la organización.")

    locked_check = CheckReceiptRegister.objects.select_for_update().get(id=check_record.id, organization=organization)
    if locked_check.status == CheckReceiptRegister.Status.CLEARED:
        raise ValidationError("Este cheque ya fue compensado y acreditado previamente.")
    if locked_check.status == CheckReceiptRegister.Status.REJECTED:
        raise ValidationError("No se puede compensar un cheque que fue rechazado.")

    target_bank = bank_account or locked_check.deposit_bank_account
    if not target_bank:
        raise ValidationError("Se requiere una cuenta bancaria destino para acreditar la compensación del cheque.")
    if target_bank.organization_id != organization.id:
        raise ValidationError("La cuenta bancaria no pertenece a la organización.")
    if not target_bank.is_active:
        raise ValidationError("La cuenta bancaria se encuentra inactiva.")

    locked_bank = BankAccount.objects.select_for_update().get(id=target_bank.id, organization=organization)
    if locked_bank.currency != locked_check.currency:
        raise ValidationError(
            f"Incompatibilidad de divisa: La moneda del cheque ({locked_check.currency}) no coincide con la moneda de la cuenta bancaria destino ({locked_bank.currency})."
        )
    locked_bank.current_balance += locked_check.amount
    locked_bank.save(update_fields=['current_balance', 'updated_at'])

    ref = (clearing_reference or locked_check.deposit_slip_number or locked_check.check_number).strip()

    trx = FinancialTransaction.objects.create(
        organization=organization,
        transaction_type=FinancialTransaction.TransactionType.CHECK_CLEARING,
        bank_account=locked_bank,
        amount=locked_check.amount,
        balance_after=locked_bank.current_balance,
        concept=f"Compensación y Acreditación de Cheque {locked_check.check_number} ({locked_check.drawer_name})",
        reference_code=ref,
        performed_by=user,
    )

    accounts_chart = ensure_default_chart_of_accounts(organization)
    acc_bank = get_required_accounting_account(accounts_chart, '1102-01', organization)
    acc_checks = get_required_accounting_account(accounts_chart, '1101-02', organization)

    entry = JournalEntry.objects.create(
        organization=organization,
        date=timezone.now().date(),
        entry_type=JournalEntry.EntryType.COLLECTIONS,
        concept=f"Compensación Cheque {locked_check.check_number} en {locked_bank.get_bank_name_display()} {locked_bank.account_number}",
        reference=ref,
        created_by=user,
    )

    JournalEntryLine.objects.create(
        organization=organization,
        entry=entry,
        account=acc_bank,
        concept=f"Acreditación Líquida Cheque {locked_check.check_number}",
        debit=locked_check.amount,
        credit=Decimal('0.00'),
    )
    JournalEntryLine.objects.create(
        organization=organization,
        entry=entry,
        account=acc_checks,
        concept=f"Baja de Cheque en Cartera por Compensación {locked_check.check_number}",
        debit=Decimal('0.00'),
        credit=locked_check.amount,
    )
    entry.verify_and_save_balance()

    locked_check.status = CheckReceiptRegister.Status.CLEARED
    locked_check.cleared_at = timezone.now()
    locked_check.deposit_bank_account = locked_bank
    locked_check.financial_transaction = trx
    locked_check.clearing_journal_entry = entry
    locked_check.save()

    return locked_check


@transaction.atomic
def reject_check(
    organization,
    check_record,
    reason: str,
    user=None,
):
    """
    Rechazo / Devolución de Cheque no Compensado:
    1. Valida que el cheque esté en estado RECEIVED o DEPOSITED (nunca CLEARED).
    2. Reversión contable:
       - Débito: 1103-01 (Clientes por Cobrar CxC) por check_record.amount
       - Crédito: 1101-02 (Cheques en Cartera y Valores por Depositar) por check_record.amount
    3. Si el cheque estaba asociado a una Factura (Invoice):
       - Reactiva el saldo pendiente: invoice.balance_due += check_record.amount.
       - Descuenta el abono: invoice.amount_paid -= check_record.amount.
       - Transiciona el estado de la factura a PARTIAL o ISSUED.
    4. Actualiza estado del cheque a REJECTED con el motivo de rechazo y póliza de reversión.
    """
    from decimal import Decimal
    from django.core.exceptions import ValidationError
    from apps.finance.models import (
        CheckReceiptRegister,
        JournalEntry,
        JournalEntryLine,
    )
    from apps.billing.models import Invoice

    if check_record.organization_id != organization.id:
        raise ValidationError("El registro de cheque no pertenece a la organización.")

    locked_check = CheckReceiptRegister.objects.select_for_update().get(id=check_record.id, organization=organization)
    if locked_check.status == CheckReceiptRegister.Status.CLEARED:
        raise ValidationError("No se puede rechazar directamente un cheque que ya fue acreditado en banco (requiere proceso de contracargo/protesto).")
    if locked_check.status == CheckReceiptRegister.Status.REJECTED:
        raise ValidationError("Este cheque ya se encuentra marcado como rechazado.")

    clean_reason = (reason or '').strip()
    if not clean_reason:
        raise ValidationError("Se requiere especificar el motivo del rechazo del cheque (ej: fondos insuficientes, firma disconforme).")

    accounts_chart = ensure_default_chart_of_accounts(organization)
    acc_cxc = get_required_accounting_account(accounts_chart, '1103-01', organization)
    acc_checks = get_required_accounting_account(accounts_chart, '1101-02', organization)

    entry = JournalEntry.objects.create(
        organization=organization,
        date=timezone.now().date(),
        entry_type=JournalEntry.EntryType.ADJUSTMENT,
        concept=f"Rechazo y Reversión Cheque {locked_check.check_number}: {clean_reason}",
        reference=locked_check.check_number,
        created_by=user,
    )

    # Débito a CxC (Reactivación de la cuenta por cobrar)
    JournalEntryLine.objects.create(
        organization=organization,
        entry=entry,
        account=acc_cxc,
        concept=f"Reactivación CxC por Cheque Rechazado {locked_check.check_number}",
        debit=locked_check.amount,
        credit=Decimal('0.00'),
    )

    # Crédito a Cheques en Cartera (Baja del valor protestado)
    JournalEntryLine.objects.create(
        organization=organization,
        entry=entry,
        account=acc_checks,
        concept=f"Baja por Rechazo de Cheque {locked_check.check_number}",
        debit=Decimal('0.00'),
        credit=locked_check.amount,
    )
    entry.verify_and_save_balance()

    # Reactivar la deuda en la factura si existe
    if locked_check.source_invoice_id:
        locked_inv = Invoice.objects.select_for_update().get(id=locked_check.source_invoice_id, organization=organization)
        locked_inv.amount_paid = max(Decimal('0.00'), locked_inv.amount_paid - locked_check.amount)
        locked_inv.balance_due = max(Decimal('0.00'), locked_inv.net_receivable - locked_inv.amount_paid)
        if locked_inv.balance_due == locked_inv.net_receivable:
            locked_inv.status = Invoice.Status.ISSUED
        else:
            locked_inv.status = Invoice.Status.PARTIAL
        locked_inv.save(update_fields=['amount_paid', 'balance_due', 'status', 'updated_at'])

    locked_check.status = CheckReceiptRegister.Status.REJECTED
    locked_check.rejection_reason = clean_reason
    locked_check.clearing_journal_entry = entry
    locked_check.save(update_fields=['status', 'rejection_reason', 'clearing_journal_entry', 'updated_at'])

    return locked_check

