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
        balance_after=caja.current_balance,
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
        balance_after=caja.current_balance,
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
        ('5101-01', 'Costo de Ventas, Compras e Insumos', AccountingAccount.AccountType.EXPENSE),
        ('6101-01', 'Gastos Generales y de Administración', AccountingAccount.AccountType.EXPENSE),
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
    net_receivable_nio = (invoice.net_receivable * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    subtotal_nio = (invoice.subtotal * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
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
    Convierte importes a Córdobas (NIO) si el pago fue aplicado a una orden en divisa extranjera (USD) a la tasa oficial.

    Asiento Contable Cuadrado:
    - DEBE: Proveedores Nacionales (CxP) (Cuenta '2101-01') por el monto aplicado en NIO.
    - HABER: Bancos Institucionales ('1102-01') o Caja General ('1101-01') por el monto desembolsado en NIO.
    """
    from decimal import Decimal, ROUND_HALF_UP
    from django.db import IntegrityError, transaction
    from apps.finance.models import JournalEntry, JournalEntryLine

    existing = supplier_payment.journal_entries.filter(entry_type=JournalEntry.EntryType.PAYMENTS).first()
    if existing:
        return existing

    if accounts is None:
        accounts = ensure_default_chart_of_accounts(organization)

    po = supplier_payment.purchase_order
    if po.currency_code != 'NIO':
        if supplier_payment.exchange_rate_applied != po.exchange_rate:
            raise ValidationError(
                f"Operación cambiaria no soportada: La orden {po.number} fue reconocida contablemente a una tasa de {po.exchange_rate:.4f}. "
                f"El egreso {supplier_payment.number} posee una tasa diferente ({supplier_payment.exchange_rate_applied:.4f}), "
                f"lo cual generaría un descuadre en Cuentas por Pagar sin política de diferencial cambiario."
            )
        rate = supplier_payment.exchange_rate_applied
    else:
        rate = Decimal('1.0')
    applied_nio = (supplier_payment.amount_applied_to_order * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

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

            # 1. DEBE: Proveedores Nacionales (CxP) (Disminución de pasivo)
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry,
                account=accounts['2101-01'],
                concept=f"Amortización CxP {po.number} - {supplier_payment.number}",
                debit=applied_nio,
                credit=Decimal('0.00'),
            )

            # 2. HABER: Bancos Institucionales o Caja (Disminución de activo)
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry,
                account=cash_or_bank_account,
                concept=credit_concept,
                debit=Decimal('0.00'),
                credit=applied_nio,
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
):
    """
    Orquesta atómicamente el ciclo completo de cobranza:
    1. Valida pertenencia de la factura y estado cobrable.
    2. Valida la cuenta bancaria (misma organización y activa si el método es bancario).
    3. Maneja Idempotencia estricta vía PaymentIdempotencyRecord (token SHA-256 de parámetros).
    4. Bloquea pesimísticamente la factura y la cuenta bancaria (select_for_update).
    5. Valida importes y previene cobros excedentes al saldo pendiente.
    6. Valida unicidad de referencia bancaria por cuenta y organización.
    7. Emite el Recibo Oficial de Caja (PaymentReceipt).
    8. Si es bancario, incrementa BankAccount.current_balance y asienta FinancialTransaction.
    9. Asegura la póliza de ventas (SALES) y genera la póliza de cobranza cuadrada (COLLECTIONS).
    """
    import hashlib
    from decimal import Decimal, ROUND_HALF_UP
    from django.db import IntegrityError
    from django.core.exceptions import ValidationError
    from apps.billing.models import PaymentReceipt, Invoice, PaymentIdempotencyRecord
    from apps.finance.models import BankAccount, FinancialTransaction, JournalEntry, JournalEntryLine

    if invoice.organization_id != organization.id:
        raise ValidationError("La factura no pertenece a la organización actual.")

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

    amt = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if amt <= Decimal('0.00'):
        raise ValidationError("El monto recaudado debe ser estrictamente mayor a 0.00.")

    clean_ref = (reference_number or '').strip()
    clean_idempotency_key = (idempotency_key or '').strip()
    curr = (currency_paid or invoice.currency_code).strip().upper()
    bank_id_str = str(bank_account.pk if bank_account else '')

    # 1. Comprobación y Replay de Idempotencia de Solicitud
    req_payload_str = f"{organization.id}:{invoice.pk}:{amt}:{curr}:{payment_method}:{bank_id_str}:{clean_ref}"
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

    # 4. Conversión bimonetaria a la moneda de la factura
    if curr == locked_inv.currency_code:
        applied_to_inv = amt
    elif curr == 'USD' and locked_inv.currency_code == 'NIO':
        applied_to_inv = (amt * locked_inv.exchange_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    elif curr == 'NIO' and locked_inv.currency_code == 'USD':
        applied_to_inv = (amt / locked_inv.exchange_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
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
            exchange_rate_applied=locked_inv.exchange_rate,
            amount_applied_to_invoice=applied_to_inv,
            received_by=user,
            notes=notes,
        )
    except IntegrityError as e:
        raise ValidationError(f"Conflicto de integridad: La referencia bancaria '{clean_ref}' ya fue registrada concurrentemente.")

    # 7. Movimiento de Tesorería (BankAccount y FinancialTransaction)
    trx = None
    if locked_bank:
        if locked_bank.currency == curr:
            bank_amt = amt
        elif locked_bank.currency == 'NIO' and curr == 'USD':
            bank_amt = (amt * locked_inv.exchange_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        elif locked_bank.currency == 'USD' and curr == 'NIO':
            bank_amt = (amt / locked_inv.exchange_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
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

    rate_nio = locked_inv.exchange_rate if locked_inv.currency_code == 'USD' else Decimal('1.0')
    amt_nio = (applied_to_inv * rate_nio).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

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
            JournalEntryLine.objects.create(
                organization=organization,
                entry=collection_entry,
                account=acc_debit,
                concept=f"Ingreso {receipt.get_payment_method_display()} {receipt.number}",
                debit=amt_nio,
                credit=Decimal('0.00'),
            )
            JournalEntryLine.objects.create(
                organization=organization,
                entry=collection_entry,
                account=accounts['1103-01'],
                concept=f"Abono CxC Factura {locked_inv.number}",
                debit=Decimal('0.00'),
                credit=amt_nio,
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

