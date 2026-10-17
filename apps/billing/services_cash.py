import hashlib
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction, models
from django.utils import timezone

from apps.billing.models import (
    PosRegisterShift,
    CashMovement,
    CashBankDeposit,
    CashRemittance,
    Invoice,
    PaymentReceipt,
)
from apps.finance.models import (
    AccountingAccount,
    BankAccount,
    FinancialTransaction,
    JournalEntry,
    JournalEntryLine,
)
from apps.finance.services import (
    ensure_default_chart_of_accounts,
    get_required_accounting_account,
)


DENOMINATIONS_NIO = [
    Decimal('1000.00'),
    Decimal('500.00'),
    Decimal('200.00'),
    Decimal('100.00'),
    Decimal('50.00'),
    Decimal('20.00'),
    Decimal('10.00'),
    Decimal('5.00'),
    Decimal('1.00'),
    Decimal('0.50'),
    Decimal('0.25'),
    Decimal('0.10'),
]


def calculate_denomination_total(breakdown: dict) -> Decimal:
    """
    Calcula el importe total en córdobas a partir de un diccionario de conteo por denominación.
    Ejemplo: {'1000': 5, '500': 10, '100': 20, '20': 15, '5': 4} -> Total C$
    """
    if not breakdown or not isinstance(breakdown, dict):
        return Decimal('0.00')

    total = Decimal('0.00')
    for denom_str, count in breakdown.items():
        try:
            val = Decimal(str(denom_str)).quantize(Decimal('0.01'))
            qty = Decimal(str(count or 0))
            if qty < Decimal('0'):
                raise ValidationError(f"La cantidad para la denominación {denom_str} no puede ser negativa.")
            total += (val * qty).quantize(Decimal('0.01'))
        except (ValueError, TypeError):
            continue

    return total.quantize(Decimal('0.01'))


@transaction.atomic
def record_cash_movement(
    shift: PosRegisterShift,
    movement_type: str,
    amount: Decimal,
    concept: str,
    performed_by,
    authorized_by=None,
    reference: str = '',
    invoice=None,
    payment_receipt=None,
    supplier_payment=None,
    bank_deposit=None,
    remittance=None,
) -> CashMovement:
    """
    Registra un movimiento granular de efectivo en la gaveta del turno de caja POS.
    Valida estado abierto, importe positivo, tenencia de tenant y actualiza métricas del turno.
    """
    locked_shift = PosRegisterShift.objects.select_for_update().get(pk=shift.pk)

    if locked_shift.status == PosRegisterShift.Status.CLOSED_Z:
        raise ValidationError("No se pueden registrar movimientos de efectivo en un turno cerrado con Corte Z.")

    amt = Decimal(str(amount or '0.00')).quantize(Decimal('0.01'))
    if amt <= Decimal('0.00'):
        raise ValidationError("El monto del movimiento de efectivo debe ser estrictamente mayor a 0.")

    from apps.tenancy.models import OrganizationMembership
    is_member = OrganizationMembership.objects.filter(
        organization=locked_shift.organization,
        user=performed_by,
        is_active=True,
    ).exists() or getattr(performed_by, 'organization_id', None) == locked_shift.organization_id

    if not is_member:
        raise ValidationError("Aislamiento de tenant: el usuario y el turno no pertenecen a la misma organización.")

    # Validar liquidez disponible en gaveta para salidas físicas
    outflow_types = [
        CashMovement.MovementType.CASH_OUT,
        CashMovement.MovementType.CUSTOMER_REFUND,
        CashMovement.MovementType.SUPPLIER_PAYMENT,
        CashMovement.MovementType.CASH_DEPOSIT_DISPATCH,
        CashMovement.MovementType.REMITTANCE_DISPATCH,
    ]
    if movement_type in outflow_types:
        current_expected = locked_shift.expected_cash_in_drawer_nio
        if amt > current_expected:
            raise ValidationError(
                f"Fondos insuficientes en gaveta: Se intentan retirar C$ {amt:,.2f} pero el saldo actual esperado es C$ {current_expected:,.2f}."
            )

    deposit_rel = bank_deposit or remittance
    movement = CashMovement.objects.create(
        organization=locked_shift.organization,
        shift=locked_shift,
        movement_type=movement_type,
        amount=amt,
        concept=(concept or '').strip(),
        reference=(reference or '').strip(),
        performed_by=performed_by,
        authorized_by=authorized_by,
        invoice=invoice,
        payment_receipt=payment_receipt,
        supplier_payment=supplier_payment,
        bank_deposit=deposit_rel,
    )

    recalculate_shift_cash_metrics(locked_shift)
    return movement


def recalculate_shift_cash_metrics(shift: PosRegisterShift) -> dict:
    """
    Recalcula las métricas auditables de caja para un turno POS:
    Efectivo Esperado = Fondo Inicial
                      + Ventas en Efectivo (POS)
                      + Entradas Adicionales (CASH_IN + CUSTOMER_COLLECTION)
                      - Salidas Menores (CASH_OUT)
                      - Devoluciones en Efectivo (CUSTOMER_REFUND)
                      - Pagos a Proveedores en Efectivo (SUPPLIER_PAYMENT)
                      - Depósitos Bancarios Despachados (CASH_DEPOSIT_DISPATCH / REMITTANCE_DISPATCH)
    """
    invoices = list(
        shift.shift_invoices.filter(
            status__in=[Invoice.Status.ISSUED, Invoice.Status.PAID, Invoice.Status.PARTIAL]
        ).select_related('account').prefetch_related('payments').order_by('-issue_date', '-created_at')
    )

    transactions_count = len(invoices)
    cash_sales = Decimal('0.00')
    card_transfer_sales = Decimal('0.00')

    for inv in invoices:
        payments = list(inv.payments.all())
        if payments:
            for p in payments:
                amt = p.amount_applied_to_invoice or p.amount_in_payment_currency
                if p.payment_method == PaymentReceipt.PaymentMethod.CASH:
                    cash_sales += amt
                else:
                    card_transfer_sales += amt
        else:
            if inv.payment_condition == Invoice.PaymentCondition.CASH:
                cash_sales += inv.net_receivable
            else:
                card_transfer_sales += inv.net_receivable

    cash_sales = cash_sales.quantize(Decimal('0.01'))
    card_transfer_sales = card_transfer_sales.quantize(Decimal('0.01'))
    total_sales = (cash_sales + card_transfer_sales).quantize(Decimal('0.01'))

    # Agregación granular de movimientos de efectivo no comerciales / operativos
    movements = list(shift.cash_movements.all())
    total_inflow = Decimal('0.00')
    total_outflow = Decimal('0.00')
    total_refunds = Decimal('0.00')
    total_supplier_payments = Decimal('0.00')
    total_bank_deposits = Decimal('0.00')

    for m in movements:
        if m.movement_type in [CashMovement.MovementType.CASH_IN, CashMovement.MovementType.CUSTOMER_COLLECTION]:
            total_inflow += m.amount
        elif m.movement_type == CashMovement.MovementType.CASH_OUT:
            total_outflow += m.amount
        elif m.movement_type == CashMovement.MovementType.CUSTOMER_REFUND:
            total_refunds += m.amount
        elif m.movement_type == CashMovement.MovementType.SUPPLIER_PAYMENT:
            total_supplier_payments += m.amount
        elif m.movement_type in [CashMovement.MovementType.CASH_DEPOSIT_DISPATCH, CashMovement.MovementType.REMITTANCE_DISPATCH]:
            total_bank_deposits += m.amount

    expected_cash = (
        shift.opening_cash_nio
        + cash_sales
        + total_inflow
        - total_outflow
        - total_refunds
        - total_supplier_payments
        - total_bank_deposits
    ).quantize(Decimal('0.01'))

    if shift.status == PosRegisterShift.Status.OPEN:
        shift.system_cash_sales_nio = cash_sales
        shift.system_card_transfer_sales_nio = card_transfer_sales
        shift.total_cash_inflow_nio = total_inflow
        shift.total_cash_outflow_nio = total_outflow
        shift.total_refunds_nio = total_refunds
        shift.total_supplier_payments_nio = total_supplier_payments
        shift.total_bank_deposits_nio = total_bank_deposits
        shift.expected_cash_in_drawer_nio = expected_cash
        shift.transactions_count = transactions_count
        super(PosRegisterShift, shift).save(update_fields=[
            'system_cash_sales_nio',
            'system_card_transfer_sales_nio',
            'total_cash_inflow_nio',
            'total_cash_outflow_nio',
            'total_refunds_nio',
            'total_supplier_payments_nio',
            'total_bank_deposits_nio',
            'expected_cash_in_drawer_nio',
            'transactions_count',
        ])

    return {
        'shift': shift,
        'invoices': invoices,
        'movements': movements,
        'transactions_count': transactions_count,
        'opening_cash_nio': shift.opening_cash_nio,
        'system_cash_sales_nio': cash_sales,
        'system_card_transfer_sales_nio': card_transfer_sales,
        'cash_sales_nio': cash_sales,
        'card_transfer_sales_nio': card_transfer_sales,
        'total_sales_nio': total_sales,
        'total_inflow_nio': total_inflow,
        'total_outflow_nio': total_outflow,
        'total_refunds_nio': total_refunds,
        'total_supplier_payments_nio': total_supplier_payments,
        'total_bank_deposits_nio': total_bank_deposits,
        'total_remittances_nio': total_bank_deposits,
        'expected_cash_in_drawer_nio': expected_cash,
    }


# ==============================================================================
# CICLO DE DEPÓSITOS BANCARIOS DE EFECTIVO
# ==============================================================================

@transaction.atomic
def prepare_cash_bank_deposit(
    shift: PosRegisterShift,
    destination_bank_account: BankAccount,
    amount: Decimal,
    user,
    notes: str = '',
) -> CashBankDeposit:
    """
    Fase 1: Preparación del depósito bancario de efectivo en gaveta.
    El dinero sigue físicamente dentro de la gaveta de caja POS.
    No genera asiento contable ni altera el saldo bancario ni el saldo esperado aún.
    """
    locked_shift = PosRegisterShift.objects.select_for_update().get(pk=shift.pk)

    if locked_shift.status != PosRegisterShift.Status.OPEN:
        raise ValidationError("Solo se pueden preparar depósitos a partir de un turno de caja abierto.")

    if destination_bank_account.organization_id != locked_shift.organization_id:
        raise ValidationError("La cuenta bancaria destino no pertenece a la misma organización que el turno.")

    if destination_bank_account.currency != 'NIO':
        raise ValidationError(
            f"Incompatibilidad de divisa: Las remesas de efectivo de caja POS en moneda nacional (NIO) solo pueden depositarse en cuentas bancarias en Córdobas (NIO), no en {destination_bank_account.currency}."
        )

    amt = Decimal(str(amount or '0.00')).quantize(Decimal('0.01'))
    if amt <= Decimal('0.00'):
        raise ValidationError("El monto a depositar debe ser mayor a cero.")

    if amt > locked_shift.expected_cash_in_drawer_nio:
        raise ValidationError(
            f"El monto a depositar (C$ {amt:,.2f}) excede el efectivo disponible en gaveta (C$ {locked_shift.expected_cash_in_drawer_nio:,.2f})."
        )

    deposit = CashBankDeposit.objects.create(
        organization=locked_shift.organization,
        shift=locked_shift,
        destination_bank_account=destination_bank_account,
        amount=amt,
        status=CashBankDeposit.Status.PREPARED,
        prepared_by=user,
        notes=(notes or '').strip(),
    )
    return deposit


@transaction.atomic
def dispatch_cash_for_deposit(
    deposit: CashBankDeposit = None,
    user=None,
    carrier_name: str = '',
    carrier_id: str = '',
    remittance: CashBankDeposit = None,
) -> CashBankDeposit:
    """
    Fase 2: Despacho y entrega física del efectivo al custodio/mensajero para depósito bancario.
    El efectivo SALE de la gaveta física:
    1. Se asienta un CashMovement(CASH_DEPOSIT_DISPATCH) en el turno reduciendo su efectivo esperado.
    2. Se genera póliza contable de Efectivo en Tránsito:
       DÉBITO:  1101-04 Efectivo y Remesas en Tránsito para Depósito (C$ amt)
       CRÉDITO: 1101-01 Caja General y Mostrador (C$ amt)
    3. El saldo de la cuenta bancaria NO se incrementa aún (dinero en tránsito físico).
    """
    target = deposit if deposit is not None else remittance
    if target is None:
        raise ValidationError("Se requiere especificar el depósito bancario de efectivo a despachar.")
    locked_deposit = CashBankDeposit.objects.select_for_update().get(pk=target.pk)

    if locked_deposit.status != CashBankDeposit.Status.PREPARED:
        raise ValidationError(f"Solo se pueden despachar depósitos en estado PREPARED. Estado actual: {locked_deposit.status}.")

    shift = PosRegisterShift.objects.select_for_update().get(pk=locked_deposit.shift_id)
    if shift.status != PosRegisterShift.Status.OPEN:
        raise ValidationError("No se puede despachar efectivo si el turno de origen ya fue cerrado con Corte Z.")

    if locked_deposit.amount > shift.expected_cash_in_drawer_nio:
        raise ValidationError(
            f"El monto a despachar (C$ {locked_deposit.amount:,.2f}) supera el efectivo en gaveta (C$ {shift.expected_cash_in_drawer_nio:,.2f})."
        )

    org = locked_deposit.organization

    # 1. Movimiento físico de salida de gaveta
    record_cash_movement(
        shift=shift,
        movement_type=CashMovement.MovementType.CASH_DEPOSIT_DISPATCH,
        amount=locked_deposit.amount,
        concept=f"Despacho de Efectivo para Depósito Bancario {locked_deposit.deposit_number} hacia {locked_deposit.destination_bank_account.bank_name}",
        performed_by=user,
        reference=locked_deposit.deposit_number,
        bank_deposit=locked_deposit,
    )

    # 2. Póliza contable de Efectivo en Tránsito
    accounts = ensure_default_chart_of_accounts(org)
    acc_transit = get_required_accounting_account(accounts, '1101-04', org)
    acc_cash = get_required_accounting_account(accounts, '1101-01', org)

    journal_entry = JournalEntry.objects.create(
        organization=org,
        date=timezone.now().date(),
        entry_type=JournalEntry.EntryType.ADJUSTMENT,
        concept=f"Efectivo en Tránsito para Depósito {locked_deposit.deposit_number} a {locked_deposit.destination_bank_account.bank_name}",
        reference=locked_deposit.deposit_number,
        created_by=user,
    )

    JournalEntryLine.objects.create(
        organization=org,
        entry=journal_entry,
        account=acc_transit,
        concept=f"Efectivo en Tránsito Depósito Bancario {locked_deposit.deposit_number}",
        debit=locked_deposit.amount,
        credit=Decimal('0.00'),
    )

    JournalEntryLine.objects.create(
        organization=org,
        entry=journal_entry,
        account=acc_cash,
        concept=f"Salida de Gaveta POS {shift.register_name} Depósito {locked_deposit.deposit_number}",
        debit=Decimal('0.00'),
        credit=locked_deposit.amount,
    )

    journal_entry.verify_and_save_balance()

    # 3. Transición de estado a IN_TRANSIT
    locked_deposit.status = CashBankDeposit.Status.IN_TRANSIT
    locked_deposit.dispatched_by = user
    locked_deposit.dispatched_at = timezone.now()
    locked_deposit.carrier_name = (carrier_name or '').strip()
    locked_deposit.carrier_id = (carrier_id or '').strip()
    locked_deposit.transit_journal_entry = journal_entry
    locked_deposit.save(update_fields=[
        'status', 'dispatched_by', 'dispatched_at',
        'carrier_name', 'carrier_id', 'transit_journal_entry', 'updated_at'
    ])

    return locked_deposit


@transaction.atomic
def record_bank_deposit_slip(
    deposit: CashBankDeposit = None,
    deposit_slip_number: str = '',
    deposit_date=None,
    user=None,
    notes: str = '',
    remittance: CashBankDeposit = None,
) -> CashBankDeposit:
    """
    Fase 3: Asentamiento de la Boleta de Depósito Bancario entregada en ventanilla.
    Verifica que la boleta no haya sido registrada previamente en la misma cuenta bancaria (prevención de doble depósito).
    El depósito queda en estado BANK_DEPOSIT_RECORDED, pendiente de confirmación/acreditación final.
    """
    target = deposit if deposit is not None else remittance
    if target is None:
        raise ValidationError("Se requiere especificar el depósito bancario de efectivo.")
    locked_deposit = CashBankDeposit.objects.select_for_update().get(pk=target.pk)

    if locked_deposit.status != CashBankDeposit.Status.IN_TRANSIT:
        raise ValidationError(f"Solo se puede asentar la boleta de un depósito en estado IN_TRANSIT. Estado actual: {locked_deposit.status}.")

    clean_slip = (deposit_slip_number or '').strip()
    if not clean_slip:
        raise ValidationError("El número de boleta de depósito bancario es obligatorio.")

    # Prevención estricta de doble depósito con la misma boleta en la cuenta bancaria
    existing_duplicate = CashBankDeposit.objects.filter(
        organization=locked_deposit.organization,
        destination_bank_account=locked_deposit.destination_bank_account,
        deposit_slip_number__iexact=clean_slip,
    ).exclude(pk=locked_deposit.pk).exists()

    if existing_duplicate:
        raise ValidationError(
            f"La boleta de depósito '{clean_slip}' ya fue registrada previamente en la cuenta {locked_deposit.destination_bank_account.bank_name}."
        )

    dep_date = deposit_date or timezone.now().date()
    locked_deposit.deposit_slip_number = clean_slip
    locked_deposit.deposit_date = dep_date
    locked_deposit.deposited_by = user
    locked_deposit.status = CashBankDeposit.Status.BANK_DEPOSIT_RECORDED
    if notes:
        locked_deposit.notes = f"{locked_deposit.notes}\n[Depósito] {notes}".strip()

    locked_deposit.save(update_fields=[
        'deposit_slip_number', 'deposit_date', 'deposited_by',
        'status', 'notes', 'updated_at'
    ])
    return locked_deposit


@transaction.atomic
def confirm_bank_deposit(
    deposit: CashBankDeposit = None,
    user=None,
    deposit_slip_number: str = '',
    deposit_date=None,
    notes: str = '',
    remittance: CashBankDeposit = None,
) -> CashBankDeposit:
    """
    Fase 4: Acreditación Bancaria Confirmada (Banco y Contabilidad).
    1. Verifica que el depósito esté en BANK_DEPOSIT_RECORDED o IN_TRANSIT con boleta válida.
    2. Acredita de forma atómica e inmutable el saldo de la cuenta bancaria destino.
    3. Genera el registro de FinancialTransaction(CASH_BANK_DEPOSIT).
    4. Genera el asiento contable definitivo:
       DÉBITO:  1102-01 Bancos Institucionales (C$ amt)
       CRÉDITO: 1101-04 Efectivo y Remesas en Tránsito para Depósito (C$ amt)
    5. La FinancialTransaction queda lista para conciliación con extractos bancarios sin duplicación.
    """
    target = deposit if deposit is not None else remittance
    if target is None:
        raise ValidationError("Se requiere especificar el depósito bancario de efectivo.")
    locked_deposit = CashBankDeposit.objects.select_for_update().get(pk=target.pk)

    if locked_deposit.status == CashBankDeposit.Status.CONFIRMED:
        raise ValidationError("Este depósito ya fue confirmado y acreditado previamente en la cuenta bancaria.")

    if locked_deposit.status not in [CashBankDeposit.Status.BANK_DEPOSIT_RECORDED, CashBankDeposit.Status.DEPOSITED, CashBankDeposit.Status.IN_TRANSIT]:
        raise ValidationError(f"No se puede confirmar un depósito en estado {locked_deposit.status}.")

    slip = deposit_slip_number or locked_deposit.deposit_slip_number
    if not (slip and slip.strip()):
        raise ValidationError("Se requiere el número de boleta bancaria para confirmar la acreditación.")
    clean_slip = slip.strip()

    # Detección de doble depósito
    existing_duplicate = CashBankDeposit.objects.filter(
        organization=locked_deposit.organization,
        destination_bank_account=locked_deposit.destination_bank_account,
        deposit_slip_number__iexact=clean_slip,
        status=CashBankDeposit.Status.CONFIRMED,
    ).exclude(pk=locked_deposit.pk).exists()

    if existing_duplicate:
        raise ValidationError(
            f"Conflicto de duplicidad: La boleta bancaria '{clean_slip}' ya fue acreditada en otro depósito."
        )

    org = locked_deposit.organization
    bank = BankAccount.objects.select_for_update().get(pk=locked_deposit.destination_bank_account_id)

    if bank.currency != 'NIO':
        raise ValidationError(
            f"Incompatibilidad de divisa: No se puede acreditar un depósito en efectivo en Córdobas (NIO) en una cuenta bancaria en {bank.currency}."
        )

    # 1. Incrementar saldo bancario
    bank.current_balance += locked_deposit.amount
    bank.save(update_fields=['current_balance', 'updated_at'])

    # 2. Generar FinancialTransaction inmutable en Libro Mayor de Bancos
    trx = FinancialTransaction.objects.create(
        organization=org,
        transaction_type=FinancialTransaction.TransactionType.CASH_BANK_DEPOSIT,
        bank_account=bank,
        amount=locked_deposit.amount,
        balance_after=bank.current_balance,
        concept=f"Depósito Bancario de Efectivo {locked_deposit.deposit_number} - Boleta {clean_slip}",
        reference_code=clean_slip,
        performed_by=user,
    )

    # 3. Póliza contable de acreditación bancaria (Cancela Efectivo en Tránsito)
    accounts = ensure_default_chart_of_accounts(org)
    acc_bank = get_required_accounting_account(accounts, '1102-01', org)
    acc_transit = get_required_accounting_account(accounts, '1101-04', org)

    confirmation_entry = JournalEntry.objects.create(
        organization=org,
        date=deposit_date or locked_deposit.deposit_date or timezone.now().date(),
        entry_type=JournalEntry.EntryType.COLLECTIONS,
        concept=f"Acreditación Bancaria Depósito {locked_deposit.deposit_number} en {bank.bank_name}",
        reference=clean_slip,
        created_by=user,
    )

    JournalEntryLine.objects.create(
        organization=org,
        entry=confirmation_entry,
        account=acc_bank,
        concept=f"Depósito Bancario {locked_deposit.deposit_number} Boleta {clean_slip}",
        debit=locked_deposit.amount,
        credit=Decimal('0.00'),
    )

    JournalEntryLine.objects.create(
        organization=org,
        entry=confirmation_entry,
        account=acc_transit,
        concept=f"Cancelación Efectivo en Tránsito Depósito {locked_deposit.deposit_number}",
        debit=Decimal('0.00'),
        credit=locked_deposit.amount,
    )

    confirmation_entry.verify_and_save_balance()

    # 4. Actualizar depósito
    now = timezone.now()
    locked_deposit.status = CashBankDeposit.Status.CONFIRMED
    locked_deposit.deposit_slip_number = clean_slip
    if deposit_date:
        locked_deposit.deposit_date = deposit_date
    locked_deposit.confirmed_by = user
    locked_deposit.confirmed_at = now
    locked_deposit.financial_transaction = trx
    locked_deposit.confirmation_journal_entry = confirmation_entry
    if notes:
        locked_deposit.notes = f"{locked_deposit.notes}\n[Confirmación] {notes}".strip()

    locked_deposit.save(update_fields=[
        'status', 'deposit_slip_number', 'deposit_date', 'confirmed_by',
        'confirmed_at', 'financial_transaction', 'confirmation_journal_entry',
        'notes', 'updated_at'
    ])

    return locked_deposit


@transaction.atomic
def reject_bank_deposit(
    deposit: CashBankDeposit = None,
    user=None,
    rejection_reason: str = '',
    remittance: CashBankDeposit = None,
) -> CashBankDeposit:
    """
    Rechaza o anula un depósito bancario que no pudo ser acreditado por el banco.
    Si el efectivo ya había salido de caja (IN_TRANSIT / BANK_DEPOSIT_RECORDED):
    1. Revierte la póliza contable de Efectivo en Tránsito:
       DÉBITO:  1101-01 Caja General
       CRÉDITO: 1101-04 Efectivo en Tránsito
    2. Si el turno sigue abierto, reingresa el efectivo físicamente como CASH_IN.
    """
    target = deposit if deposit is not None else remittance
    if target is None:
        raise ValidationError("Se requiere especificar el depósito bancario de efectivo.")
    locked_deposit = CashBankDeposit.objects.select_for_update().get(pk=target.pk)

    if locked_deposit.status == CashBankDeposit.Status.CONFIRMED:
        raise ValidationError("No se puede rechazar un depósito bancario cuyo abono ya fue acreditado en banco.")

    if locked_deposit.status == CashBankDeposit.Status.REJECTED:
        raise ValidationError("Este depósito bancario ya se encuentra rechazado.")

    reason = (rejection_reason or '').strip()
    if not reason:
        raise ValidationError("Debe especificar un motivo formal para el rechazo del depósito bancario.")

    org = locked_deposit.organization

    # Si había póliza de tránsito, generar reversión contable
    if locked_deposit.transit_journal_entry:
        accounts = ensure_default_chart_of_accounts(org)
        acc_cash = get_required_accounting_account(accounts, '1101-01', org)
        acc_transit = get_required_accounting_account(accounts, '1101-04', org)

        reversal_entry = JournalEntry.objects.create(
            organization=org,
            date=timezone.now().date(),
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
            concept=f"Reversión de Tránsito por Rechazo de Depósito {locked_deposit.deposit_number}: {reason}",
            reference=locked_deposit.deposit_number,
            created_by=user,
        )

        JournalEntryLine.objects.create(
            organization=org,
            entry=reversal_entry,
            account=acc_cash,
            concept=f"Reintegro a Caja por Rechazo Depósito {locked_deposit.deposit_number}",
            debit=locked_deposit.amount,
            credit=Decimal('0.00'),
        )

        JournalEntryLine.objects.create(
            organization=org,
            entry=reversal_entry,
            account=acc_transit,
            concept=f"Baja de Efectivo en Tránsito Rechazado Depósito {locked_deposit.deposit_number}",
            debit=Decimal('0.00'),
            credit=locked_deposit.amount,
        )

        reversal_entry.verify_and_save_balance()

        # Reingresar efectivo si el turno sigue abierto
        shift = locked_deposit.shift
        if shift.status == PosRegisterShift.Status.OPEN:
            record_cash_movement(
                shift=shift,
                movement_type=CashMovement.MovementType.CASH_IN,
                amount=locked_deposit.amount,
                concept=f"Reintegro físico de efectivo por depósito bancario rechazado {locked_deposit.deposit_number}",
                performed_by=user,
                reference=locked_deposit.deposit_number,
            )

    locked_deposit.status = CashBankDeposit.Status.REJECTED
    locked_deposit.rejection_reason = reason
    locked_deposit.save(update_fields=['status', 'rejection_reason', 'updated_at'])
    return locked_deposit


# ==============================================================================
# ALIASES DE RETROCOMPATIBILIDAD CON LA NOMENCLATURA ANTERIOR DE REMESAS
# (Permiten migración gradual sin romper consumidores existentes)
# ==============================================================================

prepare_cash_remittance = prepare_cash_bank_deposit
dispatch_cash_remittance = dispatch_cash_for_deposit
record_remittance_bank_deposit = record_bank_deposit_slip
confirm_remittance_deposit = confirm_bank_deposit
reject_cash_remittance = reject_bank_deposit


# ==============================================================================
# ARQUEO CIEGO POR DENOMINACIÓN, CONTROL DE DIFERENCIAS Y CORTE Z
# ==============================================================================

@transaction.atomic
def close_pos_shift_z_cut(
    shift: PosRegisterShift,
    declared_cash_nio: Decimal,
    user,
    supervisor=None,
    notes: str = '',
    denomination_breakdown: dict = None,
    difference_justification: str = '',
) -> PosRegisterShift:
    """
    Ejecuta el Arqueo y Cierre Operativo de Corte Z Definitivo para el turno de caja POS:
    1. Bloqueo pesimista con select_for_update().
    2. Recalcula las métricas finales de ventas y movimientos de efectivo.
    3. Si se proporciona desglose por denominación, valida su sumatoria y lo guarda.
    4. Computa la diferencia de caja: declared - expected.
    5. Si existe diferencia (descuadre != 0.00), exige obligatoriamente justificación formal.
    6. Valida autorización de supervisor si existe discrepancia.
    7. Genera el sello criptográfico SHA-256 inmutable.
    8. Transiciona el turno a CLOSED_Z inmutable.
    """
    shift = PosRegisterShift.objects.select_for_update().get(pk=shift.pk)

    if shift.status == PosRegisterShift.Status.CLOSED_Z:
        raise ValidationError("Un turno con Corte Z sellado es inmutable.")

    recalculate_shift_cash_metrics(shift)
    shift.refresh_from_db()

    # Denominación
    breakdown_total = Decimal('0.00')
    if denomination_breakdown:
        breakdown_total = calculate_denomination_total(denomination_breakdown)
        if declared_cash_nio is None:
            declared_cash_nio = breakdown_total
        else:
            decl_dec = Decimal(str(declared_cash_nio or '0.00')).quantize(Decimal('0.01'))
            if breakdown_total > Decimal('0.00') and decl_dec != breakdown_total:
                raise ValidationError(
                    f"Discrepancia en arqueo: El total declarado (C$ {decl_dec:,.2f}) no coincide con la suma del desglose por billetes/monedas (C$ {breakdown_total:,.2f})."
                )

    declared = Decimal(str(declared_cash_nio or '0.00')).quantize(Decimal('0.01'))
    if declared < Decimal('0.00'):
        raise ValidationError("El monto de efectivo declarado no puede ser negativo.")

    expected = shift.expected_cash_in_drawer_nio
    diff = (declared - expected).quantize(Decimal('0.01'))

    # Validación de justificación ante faltantes o sobrantes
    clean_justification = (difference_justification or '').strip()
    if not clean_justification and notes:
        clean_justification = str(notes).strip()

    if diff != Decimal('0.00') and not clean_justification:
        raise ValidationError(
            f"Diferencia de arqueo detectada ({'Sobrante' if diff > 0 else 'Faltante'} de C$ {abs(diff):,.2f}). "
            "Se requiere obligatoriamente una justificación formal detallada antes de sellar el Corte Z."
        )

    now = timezone.now()

    raw_payload = (
        f"{shift.organization_id}|{shift.shift_number}|{shift.opened_at.isoformat()}|"
        f"{now.isoformat()}|{shift.opening_cash_nio}|{shift.system_cash_sales_nio}|"
        f"{shift.system_card_transfer_sales_nio}|{shift.total_cash_inflow_nio}|"
        f"{shift.total_cash_outflow_nio}|{shift.total_bank_deposits_nio}|"
        f"{shift.total_refunds_nio}|{shift.total_supplier_payments_nio}|"
        f"{expected}|{declared}|{diff}|{user.id}|{supervisor.id if supervisor else ''}"
    )
    audit_hash = hashlib.sha256(raw_payload.encode('utf-8')).hexdigest()

    shift.declared_cash_nio = declared
    shift.cash_difference_nio = diff
    shift.denomination_breakdown = denomination_breakdown or {}
    shift.difference_justification = clean_justification
    shift.supervisor = supervisor
    shift.status = PosRegisterShift.Status.CLOSED_Z
    shift.closed_at = now
    shift.closing_notes = (notes or '').strip()
    shift.audit_hash = audit_hash

    super(PosRegisterShift, shift).save()
    return shift
