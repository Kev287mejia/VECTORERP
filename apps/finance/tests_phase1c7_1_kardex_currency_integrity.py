from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.core.models import CountryConfig
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel
from apps.crm.models import Account
from apps.operations.models import Crew, WorkOrder
from apps.inventory.models import Warehouse, InventoryItem, StockBalance
from apps.finance.models import (
    BankAccount,
    PettyCashFund,
    PettyCashExpense,
    PettyCashReplenishment,
    FinancialTransaction,
    JournalEntry,
    JournalEntryLine,
    CheckReceiptRegister,
)
from apps.finance.services import (
    allocate_petty_cash_fund,
    record_petty_cash_expense,
    submit_petty_cash_replenishment,
    disburse_petty_cash_replenishment,
    deposit_check_to_bank,
    clear_check_deposit,
    ensure_default_chart_of_accounts,
)
from apps.billing.models import PosRegisterShift, CashBankDeposit, Invoice, PaymentReceipt
from apps.billing.services_pos_shift import open_pos_shift
from apps.billing.services_cash import (
    prepare_cash_bank_deposit,
    dispatch_cash_for_deposit,
    record_bank_deposit_slip,
    confirm_bank_deposit,
)
from apps.billing.services import execute_pos_sale

User = get_user_model()


class KardexAndCurrencyIntegrityPhase1C71TestCase(TestCase):
    """
    Fase 1C.7.1 — Suite de Certificación de Integridad del Kardex Bancario y Validación de Divisas:
    - H-01: Corrección de FinancialTransaction.balance_after en Dotación y Reposición de Fondos de Caja Chica.
    - H-05: Validación de Compatibilidad Monetaria en Depósitos Bancarios de Efectivo y Compensación de Cheques.
    """

    def setUp(self):
        self.country_ni, _ = CountryConfig.objects.get_or_create(
            code='NI',
            defaults={
                'name': 'Nicaragua',
                'flag_emoji': '🇳🇮',
                'primary_currency_code': 'NIO',
                'primary_currency_symbol': 'C$',
                'secondary_currency_code': 'USD',
                'secondary_currency_symbol': '$',
                'default_tax_name': 'IVA',
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )

        self.org = Organization.objects.create(
            name="VECTOR Integración 1C.7.1 S.A.",
            slug="vector-integ-1c71",
            country=self.country_ni,
            currency="NIO",
            is_active=True,
        )

        self.user_admin = User.objects.create_user(
            email='admin1c71@vector.erp',
            password='Password123!',
            full_name='Admin 1C71',
        )
        self.user_cashier = User.objects.create_user(
            email='cajero1c71@vector.erp',
            password='Password123!',
            full_name='Cajero POS 1C71',
        )

        OrganizationMembership.objects.create(
            organization=self.org,
            user=self.user_admin,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            organization=self.org,
            user=self.user_cashier,
            role_level=RoleLevel.TECHNICIAN,
            is_active=True,
        )

        ensure_default_chart_of_accounts(self.org)

        # Cuentas bancarias
        self.bank_nio = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number='001-NIO-1C71',
            currency='NIO',
            current_balance=Decimal('200000.00'),
            is_active=True,
        )
        self.bank_usd = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BANPRO,
            account_number='002-USD-1C71',
            currency='USD',
            current_balance=Decimal('5000.00'),
            is_active=True,
        )

        # Fondo de caja chica
        self.fund_nio = PettyCashFund.objects.create(
            organization=self.org,
            name="Caja Chica Operativa Central",
            code="CC-CENTRAL",
            currency="NIO",
            allocated_amount=Decimal('10000.00'),
            current_balance=Decimal('0.00'),
            custodian=self.user_cashier,
            is_active=True,
        )

        self.client_account = Account.objects.create(
            organization=self.org,
            commercial_name='Cliente Mayorista 1C71 S.A.',
            tax_id='J0310000099999',
            account_type=Account.AccountType.CORPORATE,
        )

        # Operaciones: cuadrilla y OT para gastos de caja chica
        self.crew = Crew.objects.create(
            organization=self.org,
            name="Cuadrilla 1C71",
            leader=self.user_cashier,
        )
        self.work_order = WorkOrder.objects.create(
            organization=self.org,
            title="Mantenimiento Preventivo 1C71",
            account=self.client_account,
            assigned_crew=self.crew,
        )

        # Inventario y Almacén para POS
        self.warehouse = Warehouse.objects.create(
            organization=self.org,
            name='Almacén Central POS',
            code='ALM-01',
            is_active=True,
        )
        self.item = InventoryItem.objects.create(
            organization=self.org,
            name='Producto Prueba 1C71',
            sku='PRD-1C71',
            unit='UND',
            unit_cost=Decimal('50.00'),
            price_retail=Decimal('100.00'),
            is_active=True,
        )
        StockBalance.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            item=self.item,
            quantity=Decimal('500.00'),
        )

    # =========================================================================
    # SECCIÓN 1: H-01 — INTEGRIDAD DE balance_after EN CAJA CHICA Y BANCOS
    # =========================================================================

    def test_h01_01_allocate_petty_cash_records_bank_balance_after(self):
        """
        H-01: Dotación de fondo fijo:
        - El saldo posterior registrado en FinancialTransaction (asociada a bank)
          coincide con el saldo bancario real posterior a la operación.
        - El saldo de caja chica no se confunde con el saldo bancario.
        """
        bank_initial = self.bank_nio.current_balance  # 200,000.00
        fund_initial = self.fund_nio.current_balance  # 0.00
        dotation_amt = Decimal('10000.00')

        trx = allocate_petty_cash_fund(
            organization=self.org,
            fund=self.fund_nio,
            bank_account=self.bank_nio,
            amount=dotation_amt,
            user=self.user_admin,
        )

        self.bank_nio.refresh_from_db()
        self.fund_nio.refresh_from_db()

        expected_bank_balance = bank_initial - dotation_amt  # 190,000.00
        expected_fund_balance = fund_initial + dotation_amt  # 10,000.00

        self.assertEqual(self.bank_nio.current_balance, expected_bank_balance)
        self.assertEqual(self.fund_nio.current_balance, expected_fund_balance)

        # Validación crítica H-01:
        self.assertEqual(trx.bank_account, self.bank_nio)
        self.assertEqual(trx.balance_after, expected_bank_balance)
        self.assertEqual(trx.balance_after, self.bank_nio.current_balance)
        self.assertNotEqual(trx.balance_after, self.fund_nio.current_balance)

    def test_h01_02_disburse_petty_cash_replenishment_records_bank_balance_after(self):
        """
        H-01: Reposición de caja chica:
        - El saldo posterior registrado en FinancialTransaction (asociada a bank)
          coincide con el saldo bancario real posterior a la operación.
        - El saldo de caja chica no se confunde con el saldo bancario.
        """
        # 1. Dotar primero el fondo
        allocate_petty_cash_fund(
            organization=self.org,
            fund=self.fund_nio,
            bank_account=self.bank_nio,
            amount=Decimal('10000.00'),
            user=self.user_admin,
        )
        self.bank_nio.refresh_from_db()
        self.fund_nio.refresh_from_db()

        bank_before_replenish = self.bank_nio.current_balance  # 190,000.00

        # 2. Registrar gastos por C$ 3,500.00
        record_petty_cash_expense(
            organization=self.org,
            fund=self.fund_nio,
            expense_category=PettyCashExpense.Category.FUEL,
            vendor_name="Gasolinera Central",
            amount=Decimal('3500.00'),
            description="Combustible para labores de campo",
            user=self.user_cashier,
            work_order=self.work_order,
        )
        self.fund_nio.refresh_from_db()
        self.assertEqual(self.fund_nio.current_balance, Decimal('6500.00'))

        # 3. Solicitar reposición
        rep = submit_petty_cash_replenishment(
            organization=self.org,
            fund=self.fund_nio,
            notes="Reposición lote 1C71",
            user=self.user_cashier,
        )

        # 4. Desembolsar reposición desde la cuenta bancaria
        trx_rep = disburse_petty_cash_replenishment(
            organization=self.org,
            replenishment=rep,
            bank_account=self.bank_nio,
            user=self.user_admin,
        )

        self.bank_nio.refresh_from_db()
        self.fund_nio.refresh_from_db()

        expected_bank_after = bank_before_replenish - Decimal('3500.00')  # 186,500.00
        expected_fund_after = Decimal('10000.00')

        self.assertEqual(self.bank_nio.current_balance, expected_bank_after)
        self.assertEqual(self.fund_nio.current_balance, expected_fund_after)

        # Validación crítica H-01:
        self.assertEqual(trx_rep.bank_account, self.bank_nio)
        self.assertEqual(trx_rep.balance_after, expected_bank_after)
        self.assertEqual(trx_rep.balance_after, self.bank_nio.current_balance)
        self.assertNotEqual(trx_rep.balance_after, self.fund_nio.current_balance)

    def test_h01_03_atomic_rollback_prevents_unlinked_bank_mutation(self):
        """
        H-01: Atomicidad y Rollback:
        - Una excepción simulada durante la operación no deja un saldo bancario
          modificado sin la transacción correspondiente en la base de datos.
        """
        bank_initial = self.bank_nio.current_balance
        fund_initial = self.fund_nio.current_balance

        with patch('apps.finance.models.FinancialTransaction.objects.create', side_effect=RuntimeError("Fallo inducido en ledger")):
            with self.assertRaises(RuntimeError):
                allocate_petty_cash_fund(
                    organization=self.org,
                    fund=self.fund_nio,
                    bank_account=self.bank_nio,
                    amount=Decimal('5000.00'),
                    user=self.user_admin,
                )

        self.bank_nio.refresh_from_db()
        self.fund_nio.refresh_from_db()

        # Los saldos no deben haberse alterado debido al rollback atómico
        self.assertEqual(self.bank_nio.current_balance, bank_initial)
        self.assertEqual(self.fund_nio.current_balance, fund_initial)
        self.assertFalse(
            FinancialTransaction.objects.filter(
                organization=self.org,
                transaction_type=FinancialTransaction.TransactionType.PETTY_CASH_ALLOCATION,
            ).exists()
        )

    # =========================================================================
    # SECCIÓN 2: H-05 — VALIDACIÓN DE DIVISAS EN DEPÓSITOS DE EFECTIVO
    # =========================================================================

    def test_h05_01_cash_deposit_nio_to_nio_bank_allowed(self):
        """
        H-05: Depósito de efectivo en Córdobas (NIO) hacia cuenta bancaria en Córdobas (NIO):
        - Permitido y completa satisfactoriamente el ciclo operativo de depósito y acreditación.
        """
        shift = open_pos_shift(
            organization=self.org,
            cashier=self.user_cashier,
            register_name='CAJA-NIO-01',
            opening_cash_nio=Decimal('5000.00'),
        )

        deposit = prepare_cash_bank_deposit(
            shift=shift,
            destination_bank_account=self.bank_nio,
            amount=Decimal('2000.00'),
            user=self.user_cashier,
            notes='Remesa normal en NIO',
        )
        self.assertEqual(deposit.status, CashBankDeposit.Status.PREPARED)

        # Despachar al custodio
        dispatch_cash_for_deposit(
            deposit=deposit,
            user=self.user_cashier,
            carrier_name='Juan Custodio',
            carrier_id='001-000000-0000A',
        )

        # Asentar boleta
        record_bank_deposit_slip(
            deposit=deposit,
            deposit_slip_number='BOL-BAC-NIO-001',
            user=self.user_cashier,
        )

        bank_bal_before = self.bank_nio.current_balance
        confirm_bank_deposit(
            deposit=deposit,
            user=self.user_admin,
        )

        self.bank_nio.refresh_from_db()
        deposit.refresh_from_db()

        self.assertEqual(deposit.status, CashBankDeposit.Status.CONFIRMED)
        self.assertEqual(self.bank_nio.current_balance, bank_bal_before + Decimal('2000.00'))

        trx = FinancialTransaction.objects.filter(reference_code='BOL-BAC-NIO-001').first()
        self.assertIsNotNone(trx)
        self.assertEqual(trx.balance_after, self.bank_nio.current_balance)

    def test_h05_02_cash_deposit_nio_to_usd_bank_rejected_at_prepare(self):
        """
        H-05: Depósito de efectivo en Córdobas (NIO) hacia cuenta bancaria en Dólares (USD):
        - Rechazado en el límite de servicio al preparar el depósito sin conversión explícita.
        - No crea registro de depósito ni altera el efectivo del turno.
        """
        shift = open_pos_shift(
            organization=self.org,
            cashier=self.user_cashier,
            register_name='CAJA-NIO-02',
            opening_cash_nio=Decimal('5000.00'),
        )

        with self.assertRaises(ValidationError) as ctx:
            prepare_cash_bank_deposit(
                shift=shift,
                destination_bank_account=self.bank_usd,
                amount=Decimal('1000.00'),
                user=self.user_cashier,
            )

        self.assertIn("Incompatibilidad de divisa", str(ctx.exception))
        self.assertIn("NIO", str(ctx.exception))
        self.assertIn("USD", str(ctx.exception))

        shift.refresh_from_db()
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('5000.00'))
        self.assertFalse(CashBankDeposit.objects.filter(shift=shift).exists())

    def test_h05_03_cash_deposit_nio_to_usd_bank_rejected_at_confirm_boundary(self):
        """
        H-05: Defensa en profundidad:
        - Si se invoca directamente confirm_bank_deposit hacia una cuenta bancaria en USD,
          se rechaza categóricamente antes de mutar saldos o crear pólizas contables.
        """
        shift = open_pos_shift(
            organization=self.org,
            cashier=self.user_cashier,
            register_name='CAJA-NIO-03',
            opening_cash_nio=Decimal('5000.00'),
        )

        # Depósito creado directamente con cuenta bancaria destino en USD (bypass de prepare)
        deposit_invalid = CashBankDeposit.objects.create(
            organization=self.org,
            shift=shift,
            destination_bank_account=self.bank_usd,
            amount=Decimal('1500.00'),
            status=CashBankDeposit.Status.BANK_DEPOSIT_RECORDED,
            prepared_by=self.user_cashier,
            deposit_slip_number='BOL-USD-BYPASS-01',
        )

        usd_bal_before = self.bank_usd.current_balance

        with self.assertRaises(ValidationError) as ctx:
            confirm_bank_deposit(
                deposit=deposit_invalid,
                user=self.user_admin,
            )

        self.assertIn("Incompatibilidad de divisa", str(ctx.exception))
        self.bank_usd.refresh_from_db()
        deposit_invalid.refresh_from_db()

        # El saldo en USD no fue mutado
        self.assertEqual(self.bank_usd.current_balance, usd_bal_before)
        self.assertEqual(deposit_invalid.status, CashBankDeposit.Status.BANK_DEPOSIT_RECORDED)
        self.assertFalse(
            FinancialTransaction.objects.filter(reference_code='BOL-USD-BYPASS-01').exists()
        )
        self.assertFalse(
            JournalEntry.objects.filter(reference='BOL-USD-BYPASS-01').exists()
        )

    # =========================================================================
    # SECCIÓN 3: H-05 — VALIDACIÓN DE DIVISAS EN COMPENSACIÓN DE CHEQUES
    # =========================================================================

    def _create_check_register(self, check_number: str, amount: Decimal, currency: str = 'NIO', status=CheckReceiptRegister.Status.RECEIVED, deposit_bank_account=None):
        invoice = Invoice.objects.create(
            organization=self.org,
            account=self.client_account,
            subtotal=amount,
            gross_total=amount,
            currency_code=currency,
            exchange_rate=Decimal('36.6241') if currency == 'USD' else Decimal('1.0000'),
            net_receivable=amount,
            balance_due=amount,
            status=Invoice.Status.ISSUED,
        )
        receipt = PaymentReceipt.objects.create(
            organization=self.org,
            invoice=invoice,
            payment_date=timezone.now().date(),
            payment_method=PaymentReceipt.PaymentMethod.CHECK,
            reference_number=check_number,
            currency_paid=currency,
            amount_in_payment_currency=amount,
            amount_applied_to_invoice=amount,
            exchange_rate_applied=Decimal('36.6241') if currency == 'USD' else Decimal('1.0000'),
            received_by=self.user_cashier,
        )
        check = CheckReceiptRegister.objects.create(
            organization=self.org,
            check_number=check_number,
            amount=amount,
            currency=currency,
            status=status,
            deposit_bank_account=deposit_bank_account,
            payment_receipt=receipt,
            source_invoice=invoice,
            received_by=self.user_cashier,
        )
        return check

    def test_h05_04_check_nio_to_nio_bank_allowed(self):
        """
        H-05: Cheque en Córdobas (NIO) acreditado en cuenta en Córdobas (NIO):
        - Permitido; el saldo bancario se incrementa por el monto exacto del cheque.
        """
        check = self._create_check_register('CHK-NIO-OK', Decimal('8000.00'), currency='NIO')

        deposit_check_to_bank(
            organization=self.org,
            check_record=check,
            bank_account=self.bank_nio,
            deposit_slip_number='DEP-NIO-01',
        )
        check.refresh_from_db()
        self.assertEqual(check.status, CheckReceiptRegister.Status.DEPOSITED)

        bank_bal_before = self.bank_nio.current_balance
        clear_check_deposit(
            organization=self.org,
            check_record=check,
            user=self.user_admin,
            clearing_reference='CLR-NIO-01',
        )

        self.bank_nio.refresh_from_db()
        check.refresh_from_db()

        self.assertEqual(check.status, CheckReceiptRegister.Status.CLEARED)
        self.assertEqual(self.bank_nio.current_balance, bank_bal_before + Decimal('8000.00'))

        trx = FinancialTransaction.objects.filter(reference_code='CLR-NIO-01').first()
        self.assertIsNotNone(trx)
        self.assertEqual(trx.balance_after, self.bank_nio.current_balance)

    def test_h05_05_check_usd_to_nio_bank_rejected_at_deposit(self):
        """
        H-05: Cheque en Dólares (USD) intentado depositar en cuenta en Córdobas (NIO) sin conversión:
        - Rechazado en deposit_check_to_bank.
        - Estado del cheque permanece RECEIVED.
        """
        check_usd = self._create_check_register('CHK-USD-REJ-DEP', Decimal('500.00'), currency='USD')

        with self.assertRaises(ValidationError) as ctx:
            deposit_check_to_bank(
                organization=self.org,
                check_record=check_usd,
                bank_account=self.bank_nio,
                deposit_slip_number='SLIP-MISMATCH',
            )

        self.assertIn("Incompatibilidad de divisa", str(ctx.exception))
        self.assertIn("USD", str(ctx.exception))
        self.assertIn("NIO", str(ctx.exception))

        check_usd.refresh_from_db()
        self.assertEqual(check_usd.status, CheckReceiptRegister.Status.RECEIVED)
        self.assertIsNone(check_usd.deposit_bank_account)

    def test_h05_06_check_usd_to_nio_bank_rejected_at_clear_boundary(self):
        """
        H-05: Cheque en Dólares (USD) intentado compensar en cuenta en Córdobas (NIO):
        - Rechazado en clear_check_deposit antes de mutar saldos bancarios o emitir pólizas.
        """
        check_usd = self._create_check_register(
            'CHK-USD-REJ-CLR',
            Decimal('300.00'),
            currency='USD',
            status=CheckReceiptRegister.Status.DEPOSITED,
            deposit_bank_account=self.bank_nio,  # Cuenta inconsistente preexistente
        )

        nio_bal_before = self.bank_nio.current_balance

        with self.assertRaises(ValidationError) as ctx:
            clear_check_deposit(
                organization=self.org,
                check_record=check_usd,
                user=self.user_admin,
                bank_account=self.bank_nio,
                clearing_reference='CLR-USD-FAIL',
            )

        self.assertIn("Incompatibilidad de divisa", str(ctx.exception))

        self.bank_nio.refresh_from_db()
        check_usd.refresh_from_db()

        self.assertEqual(self.bank_nio.current_balance, nio_bal_before)
        self.assertEqual(check_usd.status, CheckReceiptRegister.Status.DEPOSITED)
        self.assertFalse(
            FinancialTransaction.objects.filter(reference_code='CLR-USD-FAIL').exists()
        )
        self.assertFalse(
            JournalEntry.objects.filter(reference='CLR-USD-FAIL').exists()
        )

    def test_h05_07_check_nio_to_usd_bank_rejected_at_deposit_and_clear(self):
        """
        H-05: Cheque en Córdobas (NIO) intentado depositar o compensar en cuenta en Dólares (USD):
        - Rechazado en deposit_check_to_bank y en clear_check_deposit.
        """
        check_nio = self._create_check_register('CHK-NIO-TO-USD', Decimal('4000.00'), currency='NIO')

        with self.assertRaises(ValidationError) as ctx_dep:
            deposit_check_to_bank(
                organization=self.org,
                check_record=check_nio,
                bank_account=self.bank_usd,
                deposit_slip_number='SLIP-NIO-TO-USD',
            )
        self.assertIn("Incompatibilidad de divisa", str(ctx_dep.exception))

        with self.assertRaises(ValidationError) as ctx_clr:
            clear_check_deposit(
                organization=self.org,
                check_record=check_nio,
                user=self.user_admin,
                bank_account=self.bank_usd,
            )
        self.assertIn("Incompatibilidad de divisa", str(ctx_clr.exception))

        self.bank_usd.refresh_from_db()
        self.assertEqual(self.bank_usd.current_balance, Decimal('5000.00'))

    def test_h05_08_check_usd_to_usd_bank_allowed(self):
        """
        H-05: Cheque en Dólares (USD) depositado y compensado en cuenta bancaria en Dólares (USD):
        - Flujo legítimo en la misma divisa permitido sin distorsión monetaria.
        """
        check_usd = self._create_check_register('CHK-USD-TO-USD', Decimal('750.00'), currency='USD')

        deposit_check_to_bank(
            organization=self.org,
            check_record=check_usd,
            bank_account=self.bank_usd,
            deposit_slip_number='DEP-USD-OK',
        )
        check_usd.refresh_from_db()
        self.assertEqual(check_usd.status, CheckReceiptRegister.Status.DEPOSITED)

        usd_bal_before = self.bank_usd.current_balance
        clear_check_deposit(
            organization=self.org,
            check_record=check_usd,
            user=self.user_admin,
            clearing_reference='CLR-USD-OK',
        )

        self.bank_usd.refresh_from_db()
        check_usd.refresh_from_db()

        self.assertEqual(check_usd.status, CheckReceiptRegister.Status.CLEARED)
        self.assertEqual(self.bank_usd.current_balance, usd_bal_before + Decimal('750.00'))

        trx = FinancialTransaction.objects.filter(reference_code='CLR-USD-OK').first()
        self.assertIsNotNone(trx)
        self.assertEqual(trx.balance_after, self.bank_usd.current_balance)

    def test_h05_09_explicit_pos_transfer_conversion_remains_functional(self):
        """
        H-05: Regresión controlada de conversiones existentes soportadas:
        - Las transferencias bancarias directas en cobros POS donde la moneda del cobro difiere
          de la cuenta bancaria pero cuentan con tipo de cambio explícito conservan su funcionamiento.
        """
        # Venta POS con pago en USD hacia cuenta bancaria en NIO
        inv = execute_pos_sale(
            organization=self.org,
            user=self.user_cashier,
            warehouse=self.warehouse,
            cart_items=[{'item_id': str(self.item.id), 'quantity': Decimal('2.00')}],
            account=self.client_account,
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            reference_number='TRF-FX-LEGIT-01',
            currency_paid='USD',
            bank_account=self.bank_nio,
        )

        self.assertEqual(inv.status, Invoice.Status.PAID)
        self.bank_nio.refresh_from_db()

        # El cobro se convirtió legítimamente a la cuenta bancaria NIO
        trx = FinancialTransaction.objects.filter(
            bank_account=self.bank_nio,
            transaction_type=FinancialTransaction.TransactionType.CLIENT_COLLECTION,
            reference_code='TRF-FX-LEGIT-01',
        ).first()
        self.assertIsNotNone(trx)
        self.assertEqual(trx.balance_after, self.bank_nio.current_balance)

