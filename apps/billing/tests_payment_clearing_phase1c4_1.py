import datetime
from decimal import Decimal
import threading
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from apps.billing.models import (
    Invoice,
    InvoiceItem,
    PaymentReceipt,
    PaymentIdempotencyRecord,
    PosRegisterShift,
)
from apps.billing.services import execute_pos_sale
from apps.billing.services_pos_shift import open_pos_shift
from apps.core.models import CountryConfig
from apps.crm.models import Account
from apps.finance.models import (
    AccountingAccount,
    BankAccount,
    CardBatchSettlement,
    CheckReceiptRegister,
    FinancialTransaction,
    JournalEntry,
    JournalEntryLine,
)
from apps.finance.services import (
    clear_check_deposit,
    deposit_check_to_bank,
    ensure_default_chart_of_accounts,
    record_card_batch_settlement,
    record_client_collection,
    reject_check,
)
from apps.inventory.models import (
    InventoryBatch,
    InventoryItem,
    StockBalance,
    StockMovement,
    Warehouse,
)
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel

User = get_user_model()


class PosPaymentClearingTestCase(TestCase):
    """
    Suite de Certificación Contable de Medios de Pago y Liquidación en POS (Fase 1C.4.1):
    1. Venta en efectivo sin incremento bancario.
    2. Transferencia confirmada que afecta únicamente la cuenta explícitamente seleccionada.
    3. Transferencia sin cuenta explícita rechazada (cero fallback .first()).
    4. Cuenta bancaria de otra organización rechazada.
    5. Tarjeta no liquidada registrada en su cuenta transitoria (1101-03) sin incremento bancario.
    6. Cheque recibido registrado como valor en cartera (1101-02) sin incremento bancario.
    7. Liquidación de lote de tarjeta contabilizada una sola vez, con comisión (6102-01),
       retenciones (1105-01) y depósito neto en banco (1102-01).
    8. Duplicación de lote o liquidación rechazada mediante idempotencia y unicidad.
    9. Ciclo completo de cheques: depósito, acreditación líquida y rechazo consistente.
    10. Fallo intermedio que provoque rollback de toda la operación atómica.
    11. Ausencia de asientos duplicados y coherencia contable (Debe == Haber).
    12. Aislamiento multi-tenant en liquidaciones y cheques.
    13. No regresión en record_client_collection.
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
                'tax_id_label': 'RUC',
                'default_tax_name': 'IVA',
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )

        # Organización A (Ferretería / Comercio General)
        self.org_a = Organization.objects.create(
            name="Ferretería Industrial del Norte S.A.",
            slug="ferreteria-norte-pos",
            country=self.country_ni,
            currency='NIO',
            enabled_modules=['crm', 'inventory', 'finance', 'billing'],
            is_active=True,
        )

        # Organización B (Aislamiento Tenant)
        self.org_b = Organization.objects.create(
            name="Farmacia Central Managua S.A.",
            slug="farmacia-central-pos",
            country=self.country_ni,
            currency='NIO',
            enabled_modules=['crm', 'inventory', 'finance', 'billing'],
            is_active=True,
        )

        # Usuarios
        self.user_a = User.objects.create_user(
            email='cajero1@ferreterianorte.ni',
            password='TestPassword2026*',
            full_name='Cajero Norte 1',
        )
        self.user_b = User.objects.create_user(
            email='cajero1@farmaciacentral.ni',
            password='TestPassword2026*',
            full_name='Cajero Central 1',
        )

        OrganizationMembership.objects.create(
            organization=self.org_a,
            user=self.user_a,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            organization=self.org_b,
            user=self.user_b,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )

        # Bodegas
        self.warehouse_a = Warehouse.objects.create(
            organization=self.org_a,
            name='Bodega Principal Norte',
            is_active=True,
        )
        self.warehouse_b = Warehouse.objects.create(
            organization=self.org_b,
            name='Bodega Central B',
            is_active=True,
        )

        # Clientes
        self.client_a = Account.objects.create(
            organization=self.org_a,
            commercial_name='Constructora El Bosque S.A.',
            tax_id='J0310000005555',
            account_type=Account.AccountType.CORPORATE,
        )
        self.client_b = Account.objects.create(
            organization=self.org_b,
            commercial_name='Cliente Farmacia B',
            tax_id='J0310000008888',
            account_type=Account.AccountType.INDIVIDUAL,
        )

        # Cuentas bancarias Org A
        self.bank_a1 = BankAccount.objects.create(
            organization=self.org_a,
            bank_name=BankAccount.BankName.BAC,
            account_number='001-998877-01',
            currency='NIO',
            current_balance=Decimal('50000.00'),
            is_active=True,
        )
        self.bank_a2 = BankAccount.objects.create(
            organization=self.org_a,
            bank_name=BankAccount.BankName.BANPRO,
            account_number='002-334455-02',
            currency='NIO',
            current_balance=Decimal('20000.00'),
            is_active=True,
        )
        self.bank_a_inactive = BankAccount.objects.create(
            organization=self.org_a,
            bank_name=BankAccount.BankName.LAFISE,
            account_number='003-112233-03',
            currency='NIO',
            current_balance=Decimal('1000.00'),
            is_active=False,
        )

        # Cuenta bancaria Org B (Tenant B)
        self.bank_b = BankAccount.objects.create(
            organization=self.org_b,
            bank_name=BankAccount.BankName.BAC,
            account_number='009-887766-09',
            currency='NIO',
            current_balance=Decimal('15000.00'),
            is_active=True,
        )

        # Productos Org A
        self.item_cement = InventoryItem.objects.create(
            organization=self.org_a,
            name='Bolsa de Cemento Canal 42.5kg',
            sku='MAT-CEM-001',
            unit='BLS',
            unit_cost=Decimal('300.00'),
            price_retail=Decimal('400.00'),
            is_active=True,
        )
        StockBalance.objects.create(
            organization=self.org_a,
            warehouse=self.warehouse_a,
            item=self.item_cement,
            quantity=Decimal('100.00'),
        )

        # Apertura de turno POS
        self.shift_a = open_pos_shift(
            organization=self.org_a,
            cashier=self.user_a,
            register_name='CAJA-01',
            opening_cash_nio=Decimal('2000.00'),
        )

        # Catálogo de cuentas contables
        ensure_default_chart_of_accounts(self.org_a)
        ensure_default_chart_of_accounts(self.org_b)

    def test_01_cash_sale_does_not_modify_bank_balance(self):
        """
        1. Venta POS en efectivo:
        - No modifica BankAccount.current_balance de ninguna cuenta bancaria.
        - No genera FinancialTransaction.
        - Póliza COLLECTIONS debita Caja General (1101-01) y acredita Clientes CxC (1103-01).
        """
        initial_a1 = self.bank_a1.current_balance
        initial_a2 = self.bank_a2.current_balance

        inv = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{
                'item_id': str(self.item_cement.id),
                'quantity': Decimal('2.00'),
                'price_tier': 'RETAIL',
            }],
            account=self.client_a,
            payment_method=PaymentReceipt.PaymentMethod.CASH,
            notes='Venta mostrador de 2 bolsas de cemento en efectivo',
        )

        # Total facturado: 2 * 400 + 15% IVA = 800 + 120 = 920.00 NIO
        self.assertEqual(inv.net_receivable, Decimal('920.00'))
        self.assertEqual(inv.status, Invoice.Status.PAID)

        # Saldos bancarios intactos
        self.bank_a1.refresh_from_db()
        self.bank_a2.refresh_from_db()
        self.assertEqual(self.bank_a1.current_balance, initial_a1)
        self.assertEqual(self.bank_a2.current_balance, initial_a2)

        # Cero transacciones bancarias
        self.assertEqual(FinancialTransaction.objects.filter(organization=self.org_a).count(), 0)

        # Asiento de cobro (COLLECTIONS) debita 1101-01 Caja
        receipt = inv.payments.first()
        self.assertEqual(receipt.payment_method, PaymentReceipt.PaymentMethod.CASH)
        self.assertIsNone(receipt.bank_account)

        col_entry = JournalEntry.objects.filter(source_payment_receipt=receipt).first()
        self.assertIsNotNone(col_entry)
        self.assertTrue(col_entry.is_balanced)
        lines = {l.account.code: (l.debit, l.credit) for l in col_entry.lines.all()}
        self.assertEqual(lines['1101-01'], (Decimal('920.00'), Decimal('0.00')))
        self.assertEqual(lines['1103-01'], (Decimal('0.00'), Decimal('920.00')))

    def test_02_confirmed_transfer_affects_only_selected_bank_account(self):
        """
        2. Venta POS con transferencia bancaria confirmada:
        - Afecta ÚNICAMENTE la cuenta bancaria seleccionada explícitamente.
        - Las demás cuentas bancarias de la empresa permanecen inalteradas.
        - Emite FinancialTransaction y debita Bancos (1102-01).
        """
        bal_a1 = self.bank_a1.current_balance
        bal_a2 = self.bank_a2.current_balance

        ref = 'BAC-TRF-2026-X1'
        inv = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{
                'item_id': str(self.item_cement.id),
                'quantity': Decimal('1.00'),
                'price_tier': 'RETAIL',
            }],
            account=self.client_a,
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_a1,
            reference_number=ref,
        )

        expected = Decimal('460.00')
        self.assertEqual(inv.net_receivable, expected)

        # Cuenta seleccionada A1 incrementada
        self.bank_a1.refresh_from_db()
        self.assertEqual(self.bank_a1.current_balance, bal_a1 + expected)

        # Cuenta A2 intacta
        self.bank_a2.refresh_from_db()
        self.assertEqual(self.bank_a2.current_balance, bal_a2)

        # Transacción de tesorería asociada a A1
        trx = FinancialTransaction.objects.filter(reference_code=ref).first()
        self.assertIsNotNone(trx)
        self.assertEqual(trx.bank_account, self.bank_a1)
        self.assertEqual(trx.amount, expected)

        # Póliza debita 1102-01
        col_entry = JournalEntry.objects.filter(source_payment_receipt=inv.payments.first()).first()
        lines = {l.account.code: (l.debit, l.credit) for l in col_entry.lines.all()}
        self.assertEqual(lines['1102-01'], (expected, Decimal('0.00')))
        self.assertEqual(lines['1103-01'], (Decimal('0.00'), expected))

    def test_03_transfer_without_explicit_bank_account_rejected_no_fallback(self):
        """
        3. Transferencia sin cuenta bancaria explícita es rechazada con ValidationError:
        - Confirma la eliminación definitiva del fallback ciego .first().
        """
        with self.assertRaises(ValidationError) as ctx:
            execute_pos_sale(
                organization=self.org_a,
                user=self.user_a,
                warehouse=self.warehouse_a,
                cart_items=[{
                    'item_id': str(self.item_cement.id),
                    'quantity': Decimal('1.00'),
                }],
                account=self.client_a,
                payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
                bank_account=None,  # Omisión deliberada
                reference_number='BAC-BLIND-FALLBACK',
            )

        self.assertIn("requieren una cuenta bancaria de destino explícita", str(ctx.exception))
        # Saldo bancario sin alteraciones
        self.bank_a1.refresh_from_db()
        self.assertEqual(self.bank_a1.current_balance, Decimal('50000.00'))

    def test_04_cross_tenant_or_inactive_bank_account_rejected(self):
        """
        4. Cuenta bancaria de otra organización o inactiva es rechazada estrictamente.
        """
        # Cross-tenant (cuenta de Org B en venta de Org A)
        with self.assertRaises(ValidationError) as ctx:
            execute_pos_sale(
                organization=self.org_a,
                user=self.user_a,
                warehouse=self.warehouse_a,
                cart_items=[{'item_id': str(self.item_cement.id), 'quantity': Decimal('1.00')}],
                account=self.client_a,
                payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
                bank_account=self.bank_b,
                reference_number='TRF-HACK-TENANT',
            )
        self.assertIn("La cuenta bancaria no pertenece a la organización", str(ctx.exception))

        # Cuenta inactiva de la misma empresa
        with self.assertRaises(ValidationError) as ctx2:
            execute_pos_sale(
                organization=self.org_a,
                user=self.user_a,
                warehouse=self.warehouse_a,
                cart_items=[{'item_id': str(self.item_cement.id), 'quantity': Decimal('1.00')}],
                account=self.client_a,
                payment_method=PaymentReceipt.PaymentMethod.TRANSFER_LAFISE,
                bank_account=self.bank_a_inactive,
                reference_number='TRF-INACTIVE',
            )
        self.assertIn("se encuentra inactiva", str(ctx2.exception))

    def test_05_unsettled_card_sale_in_transient_account_no_bank_inflation(self):
        """
        5. Cobro con tarjeta de crédito/débito no liquidado en mostrador:
        - No incrementa BankAccount.current_balance (no asume fondos bancarios líquidos).
        - No crea FinancialTransaction bancaria prematura.
        - Asienta el cobro en la cuenta transitoria '1101-03' (Tarjetas y Vouchers por Liquidar).
        - Póliza COLLECTIONS: Débito 1101-03 vs Crédito 1103-01.
        """
        bal_a1 = self.bank_a1.current_balance

        inv = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{
                'item_id': str(self.item_cement.id),
                'quantity': Decimal('2.00'),
            }],
            account=self.client_a,
            payment_method='CARD',
            reference_number='AUTH-BAC-VISA-9912',
            notes='Cobro voucher POS con tarjeta Visa',
        )

        expected = Decimal('920.00')
        self.assertEqual(inv.net_receivable, expected)

        # Banco NO incrementado
        self.bank_a1.refresh_from_db()
        self.assertEqual(self.bank_a1.current_balance, bal_a1)

        # Cero FinancialTransaction
        self.assertEqual(FinancialTransaction.objects.filter(organization=self.org_a).count(), 0)

        # Recibo Oficial de Caja emitido con método CARD
        receipt = inv.payments.first()
        self.assertEqual(receipt.payment_method, PaymentReceipt.PaymentMethod.CARD)
        self.assertIsNone(receipt.bank_account)

        # Asiento contable de cobro debita la cuenta transitoria 1101-03
        col_entry = JournalEntry.objects.filter(source_payment_receipt=receipt).first()
        self.assertIsNotNone(col_entry)
        self.assertTrue(col_entry.is_balanced)
        lines = {l.account.code: (l.debit, l.credit) for l in col_entry.lines.all()}
        self.assertEqual(lines['1101-03'], (expected, Decimal('0.00')))
        self.assertEqual(lines['1103-01'], (Decimal('0.00'), expected))

    def test_06_received_check_in_portfolio_no_bank_inflation(self):
        """
        6. Cheque recibido en mostrador:
        - No incrementa BankAccount.current_balance (el cheque no está compensado ni depositado).
        - No crea FinancialTransaction bancaria.
        - Crea registro de custodia CheckReceiptRegister en estado RECEIVED.
        - Asienta el cobro en la cuenta de cheques en cartera '1101-02'.
        - Póliza COLLECTIONS: Débito 1101-02 vs Crédito 1103-01.
        """
        bal_a1 = self.bank_a1.current_balance

        inv = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{
                'item_id': str(self.item_cement.id),
                'quantity': Decimal('5.00'),
            }],
            account=self.client_a,
            payment_method=PaymentReceipt.PaymentMethod.CHECK,
            reference_number='CHK-BAC-774411',
            notes='Cheque certificado recibido en mostrador',
        )

        # 5 * 400 * 1.15 = 2,300.00 NIO
        expected = Decimal('2300.00')
        self.assertEqual(inv.net_receivable, expected)

        # Banco NO incrementado
        self.bank_a1.refresh_from_db()
        self.assertEqual(self.bank_a1.current_balance, bal_a1)

        # Cero transacciones bancarias
        self.assertEqual(FinancialTransaction.objects.filter(organization=self.org_a).count(), 0)

        # Registro de custodia de cheque
        chk_record = CheckReceiptRegister.objects.filter(check_number='CHK-BAC-774411').first()
        self.assertIsNotNone(chk_record)
        self.assertEqual(chk_record.status, CheckReceiptRegister.Status.RECEIVED)
        self.assertEqual(chk_record.amount, expected)
        self.assertEqual(chk_record.drawer_name, self.client_a.commercial_name)
        self.assertEqual(chk_record.source_invoice, inv)

        # Asiento contable de cobro debita la cuenta de cheques 1101-02
        col_entry = JournalEntry.objects.filter(source_payment_receipt=inv.payments.first()).first()
        lines = {l.account.code: (l.debit, l.credit) for l in col_entry.lines.all()}
        self.assertEqual(lines['1101-02'], (expected, Decimal('0.00')))
        self.assertEqual(lines['1103-01'], (Decimal('0.00'), expected))

    def test_07_card_batch_settlement_single_accounting_with_fee_and_retention(self):
        """
        7. Liquidación de lote de tarjeta de crédito / débito (Adquirente):
        - Concilia el importe bruto pendiente de la cuenta transitoria (1101-03).
        - Reconoce la comisión bancaria del adquirente (6102-01).
        - Reconoce las retenciones aplicables (1105-01 Anticipo IR Sufrido).
        - Acredita ÚNICAMENTE el importe neto en BankAccount.current_balance.
        - Emite FinancialTransaction bancaria con tipo CARD_SETTLEMENT.
        - Asiento contable perfectamente cuadrado (Debe == Haber).
        """
        # Paso previo: registrar 2 ventas con tarjeta por un total bruto de 3,000.00 NIO
        execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{'item_id': str(self.item_cement.id), 'quantity': Decimal('2.00'), 'unit_price': Decimal('1000.00')}],
            payment_method='CARD',
            reference_number='VOUCHER-001',
        )
        execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{'item_id': str(self.item_cement.id), 'quantity': Decimal('1.00'), 'unit_price': Decimal('700.00')}],
            payment_method='CARD',
            reference_number='VOUCHER-002',
        )

        bal_initial = self.bank_a1.current_balance
        gross = Decimal('3000.00')
        fee = Decimal('90.00')        # 3.0% comisión adquirente
        retention = Decimal('45.00')  # 1.5% retención fiscal en la fuente
        net_expected = gross - fee - retention  # 2,865.00 NIO

        batch_ref = 'LOTE-BAC-2026-OCT16'
        settlement = record_card_batch_settlement(
            organization=self.org_a,
            bank_account=self.bank_a1,
            batch_reference=batch_ref,
            gross_amount=gross,
            fee_amount=fee,
            tax_retention_amount=retention,
            user=self.user_a,
            notes='Cierre de lote POS Credomatic Managua',
            idempotency_key='IDEMP-SETTLEMENT-001',
        )

        self.assertIsNotNone(settlement)
        self.assertEqual(settlement.gross_amount, gross)
        self.assertEqual(settlement.fee_amount, fee)
        self.assertEqual(settlement.tax_retention_amount, retention)
        self.assertEqual(settlement.net_amount, net_expected)

        # Saldo bancario incrementado ÚNICAMENTE por el neto
        self.bank_a1.refresh_from_db()
        self.assertEqual(self.bank_a1.current_balance, bal_initial + net_expected)

        # Transacción financiera asentada
        trx = FinancialTransaction.objects.filter(reference_code=batch_ref).first()
        self.assertIsNotNone(trx)
        self.assertEqual(trx.transaction_type, FinancialTransaction.TransactionType.CARD_SETTLEMENT)
        self.assertEqual(trx.amount, net_expected)
        self.assertEqual(trx.balance_after, self.bank_a1.current_balance)

        # Póliza contable cuadrada
        entry = settlement.journal_entry
        self.assertIsNotNone(entry)
        self.assertTrue(entry.is_balanced)
        lines = {l.account.code: (l.debit, l.credit) for l in entry.lines.all()}
        self.assertEqual(lines['1102-01'], (net_expected, Decimal('0.00')))   # Débito Bancos
        self.assertEqual(lines['6102-01'], (fee, Decimal('0.00')))            # Débito Gasto Comisión
        self.assertEqual(lines['1105-01'], (retention, Decimal('0.00')))      # Débito Anticipo IR
        self.assertEqual(lines['1101-03'], (Decimal('0.00'), gross))          # Crédito Baja Transitoria

    def test_08_duplicate_batch_or_divergent_idempotency_rejected(self):
        """
        8. Idempotencia y prevención de duplicidad en liquidación de tarjetas:
        - Replay idéntico devuelve la liquidación existente sin alterar saldos.
        - Reutilización con parámetros discrepantes es rechazada con ValidationError.
        - Intento de liquidar el mismo lote en la misma cuenta bancaria es rechazado.
        """
        gross = Decimal('1000.00')
        fee = Decimal('30.00')
        idemp_key = 'IDEMP-SETTLEMENT-DUP'
        batch_ref = 'LOTE-TEST-DUP-01'

        s1 = record_card_batch_settlement(
            organization=self.org_a,
            bank_account=self.bank_a1,
            batch_reference=batch_ref,
            gross_amount=gross,
            fee_amount=fee,
            idempotency_key=idemp_key,
        )

        self.bank_a1.refresh_from_db()
        bal_after_s1 = self.bank_a1.current_balance

        # Replay idéntico
        s2 = record_card_batch_settlement(
            organization=self.org_a,
            bank_account=self.bank_a1,
            batch_reference=batch_ref,
            gross_amount=gross,
            fee_amount=fee,
            idempotency_key=idemp_key,
        )
        self.assertEqual(s1.id, s2.id)
        self.bank_a1.refresh_from_db()
        self.assertEqual(self.bank_a1.current_balance, bal_after_s1)

        # Misma clave con parámetros divergentes
        with self.assertRaises(ValidationError) as ctx:
            record_card_batch_settlement(
                organization=self.org_a,
                bank_account=self.bank_a1,
                batch_reference=batch_ref,
                gross_amount=Decimal('2000.00'),  # Divergente
                fee_amount=fee,
                idempotency_key=idemp_key,
            )
        self.assertIn("utilizada con parámetros de liquidación diferentes", str(ctx.exception))

        # Intento de duplicar lote con clave diferente
        with self.assertRaises(ValidationError) as ctx2:
            record_card_batch_settlement(
                organization=self.org_a,
                bank_account=self.bank_a1,
                batch_reference=batch_ref,  # Mismo lote
                gross_amount=gross,
                fee_amount=fee,
                idempotency_key='ANOTHER-KEY',
            )
        self.assertIn("ya fue liquidado previamente", str(ctx2.exception))

    def test_09_check_lifecycle_deposit_clearing_and_rejection(self):
        """
        9. Ciclo completo de cheques:
        - Recepción en mostrador -> DEPOSITED (remesa sin fondos) -> CLEARED (fondos bancarios líquidos).
        - En otro cheque: Recepción -> REJECTED (reversión contable y reactivación de deuda en factura).
        """
        # Cheque 1: Flujo exitoso de compensación
        inv1 = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{'item_id': str(self.item_cement.id), 'quantity': Decimal('1.00')}],
            account=self.client_a,
            payment_method=PaymentReceipt.PaymentMethod.CHECK,
            reference_number='CHK-GOOD-101',
        )
        chk1 = CheckReceiptRegister.objects.get(check_number='CHK-GOOD-101')
        self.assertEqual(chk1.status, CheckReceiptRegister.Status.RECEIVED)

        # Depósito en banco (sin acreditar fondos)
        bal_before_dep = self.bank_a1.current_balance
        deposit_check_to_bank(
            organization=self.org_a,
            check_record=chk1,
            bank_account=self.bank_a1,
            deposit_slip_number='DEP-SLIP-001',
        )
        chk1.refresh_from_db()
        self.assertEqual(chk1.status, CheckReceiptRegister.Status.DEPOSITED)
        self.bank_a1.refresh_from_db()
        self.assertEqual(self.bank_a1.current_balance, bal_before_dep)  # Cero incremento en depósito

        # Compensación bancaria (acreditación líquida)
        clear_check_deposit(
            organization=self.org_a,
            check_record=chk1,
            user=self.user_a,
            clearing_reference='CLEAR-ACH-999',
        )
        chk1.refresh_from_db()
        self.assertEqual(chk1.status, CheckReceiptRegister.Status.CLEARED)
        self.assertIsNotNone(chk1.cleared_at)

        self.bank_a1.refresh_from_db()
        self.assertEqual(self.bank_a1.current_balance, bal_before_dep + chk1.amount)

        # Transacción de compensación
        trx = FinancialTransaction.objects.filter(reference_code='CLEAR-ACH-999').first()
        self.assertIsNotNone(trx)
        self.assertEqual(trx.transaction_type, FinancialTransaction.TransactionType.CHECK_CLEARING)

        # Póliza de compensación: Débito Bancos 1102-01 vs Crédito Cheques 1101-02
        clearing_entry = chk1.clearing_journal_entry
        self.assertTrue(clearing_entry.is_balanced)
        lines1 = {l.account.code: (l.debit, l.credit) for l in clearing_entry.lines.all()}
        self.assertEqual(lines1['1102-01'], (chk1.amount, Decimal('0.00')))
        self.assertEqual(lines1['1101-02'], (Decimal('0.00'), chk1.amount))

        # Cheque 2: Flujo de rechazo / rebote
        inv2 = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{'item_id': str(self.item_cement.id), 'quantity': Decimal('2.00')}],
            account=self.client_a,
            payment_method=PaymentReceipt.PaymentMethod.CHECK,
            reference_number='CHK-BAD-202',
        )
        chk2 = CheckReceiptRegister.objects.get(check_number='CHK-BAD-202')
        self.assertEqual(inv2.status, Invoice.Status.PAID)
        self.assertEqual(inv2.balance_due, Decimal('0.00'))

        # Rechazo del cheque por fondos insuficientes
        reject_check(
            organization=self.org_a,
            check_record=chk2,
            reason='Fondos insuficientes reportados por banco librador BDF',
            user=self.user_a,
        )
        chk2.refresh_from_db()
        self.assertEqual(chk2.status, CheckReceiptRegister.Status.REJECTED)

        # Factura reactivada con saldo pendiente
        inv2.refresh_from_db()
        self.assertEqual(inv2.status, Invoice.Status.ISSUED)
        self.assertEqual(inv2.balance_due, chk2.amount)
        self.assertEqual(inv2.amount_paid, Decimal('0.00'))

        # Póliza de reversión: Débito Clientes CxC 1103-01 vs Crédito Cheques 1101-02
        rej_entry = chk2.clearing_journal_entry
        self.assertTrue(rej_entry.is_balanced)
        lines2 = {l.account.code: (l.debit, l.credit) for l in rej_entry.lines.all()}
        self.assertEqual(lines2['1103-01'], (chk2.amount, Decimal('0.00')))
        self.assertEqual(lines2['1101-02'], (Decimal('0.00'), chk2.amount))

    def test_10_intermediate_failure_causes_full_atomic_rollback(self):
        """
        10. Fallo intermedio provoca rollback atómico total:
        - Si falla la validación bancaria o contable, no se crea factura, recibo ni póliza.
        """
        count_inv = Invoice.objects.filter(organization=self.org_a).count()
        count_rec = PaymentReceipt.objects.filter(organization=self.org_a).count()
        count_pol = JournalEntry.objects.filter(organization=self.org_a).count()

        with self.assertRaises(ValidationError):
            execute_pos_sale(
                organization=self.org_a,
                user=self.user_a,
                warehouse=self.warehouse_a,
                cart_items=[{'item_id': str(self.item_cement.id), 'quantity': Decimal('1.00')}],
                account=self.client_a,
                payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
                bank_account=self.bank_b,  # Falla por aislamiento tenant
            )

        self.assertEqual(Invoice.objects.filter(organization=self.org_a).count(), count_inv)
        self.assertEqual(PaymentReceipt.objects.filter(organization=self.org_a).count(), count_rec)
        self.assertEqual(JournalEntry.objects.filter(organization=self.org_a).count(), count_pol)

    def test_11_no_duplicate_journal_entries_and_ledger_consistency(self):
        """
        11. Coherencia contable estricta en el Libro Diario:
        - Todas las pólizas generadas en ventas, cobros y liquidaciones tienen Debe == Haber.
        - No existen pólizas desbalanceadas en la base de datos.
        """
        execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{'item_id': str(self.item_cement.id), 'quantity': Decimal('1.00')}],
            payment_method=PaymentReceipt.PaymentMethod.CASH,
        )
        execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{'item_id': str(self.item_cement.id), 'quantity': Decimal('1.00')}],
            payment_method='CARD',
        )

        entries = JournalEntry.objects.filter(organization=self.org_a)
        self.assertGreater(entries.count(), 0)
        for e in entries:
            self.assertTrue(e.is_balanced, f"Póliza {e.number} no está cuadrada: D={e.total_debit}, C={e.total_credit}")

    def test_12_multi_tenant_isolation_in_settlements_and_checks(self):
        """
        12. Aislamiento multi-tenant estricto:
        - Org B no puede liquidar cuentas de Org A.
        - Org B no puede compensar cheques de Org A.
        """
        # Cheque de Org A
        inv = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{'item_id': str(self.item_cement.id), 'quantity': Decimal('1.00')}],
            account=self.client_a,
            payment_method=PaymentReceipt.PaymentMethod.CHECK,
            reference_number='CHK-ISO-01',
        )
        chk = CheckReceiptRegister.objects.get(check_number='CHK-ISO-01')

        # Intento de Org B de compensar el cheque de Org A
        with self.assertRaises(ValidationError) as ctx:
            clear_check_deposit(
                organization=self.org_b,
                check_record=chk,
                user=self.user_b,
                bank_account=self.bank_b,
            )
        self.assertIn("no pertenece a la organización", str(ctx.exception))

        # Intento de Org B de liquidar tarjeta acreditando cuenta de Org A
        with self.assertRaises(ValidationError) as ctx2:
            record_card_batch_settlement(
                organization=self.org_b,
                bank_account=self.bank_a1,  # Cuenta de Org A
                batch_reference='LOTE-HACK',
                gross_amount=Decimal('1000.00'),
            )
        self.assertIn("no pertenece a la organización", str(ctx2.exception))

    def test_13_no_regression_record_client_collection(self):
        """
        13. Cero regresión en record_client_collection (Fase 1B.1):
        - Verifica que los cobros estándar de cuentas por cobrar siguen operativos al 100%.
        """
        inv = Invoice.objects.create(
            organization=self.org_a,
            account=self.client_a,
            status=Invoice.Status.ISSUED,
            currency_code='NIO',
            currency_symbol='C$',
            apply_tax=False,
            created_by=self.user_a,
        )
        InvoiceItem.objects.create(
            invoice=inv,
            item_type='PRODUCT',
            code='CEM-01',
            description='Cemento',
            unit='BLS',
            quantity=Decimal('1.00'),
            unit_price=Decimal('1000.00'),
            discount_percent=Decimal('0.00'),
        )
        inv.recalculate_totals()
        inv.save()

        receipt, trx, entry = record_client_collection(
            organization=self.org_a,
            invoice=inv,
            amount=Decimal('500.00'),
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            user=self.user_a,
            bank_account=self.bank_a1,
            reference_number='REC-COLL-REGRESSION-1',
        )

        self.assertIsNotNone(receipt)
        self.assertIsNotNone(trx)
        self.assertIsNotNone(entry)
        self.assertTrue(entry.is_balanced)

        inv.refresh_from_db()
        self.assertEqual(inv.amount_paid, Decimal('500.00'))
        self.assertEqual(inv.balance_due, Decimal('500.00'))
        self.assertEqual(inv.status, Invoice.Status.PARTIAL)

    def test_15_sales_and_collection_dual_journal_audit_with_explicit_vat(self):
        """
        15. Auditoría del Ciclo Contable de Ventas con IVA 15% Explícito:
        - Verifica por separado la póliza SALES (CxC 1103-01, Ingresos 4101-01, IVA Débito 2102-01).
        - Verifica por separado la póliza COLLECTIONS (Caja 1101-01 cancelando CxC 1103-01).
        - Constata que la posición neta de CxC queda en 0.00.
        - Constata la ausencia de omisiones y duplicaciones en Kardex y facturación.
        """
        stock_before = StockBalance.objects.get(
            organization=self.org_a,
            warehouse=self.warehouse_a,
            item=self.item_cement,
        ).quantity

        inv = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{
                'item_id': str(self.item_cement.id),
                'quantity': Decimal('2.00'),
                'price_tier': 'RETAIL',
            }],
            account=self.client_a,
            payment_method=PaymentReceipt.PaymentMethod.CASH,
            notes='Venta para auditoría contable dual con IVA',
        )

        # Totales esperados: Base = 800.00, IVA 15% = 120.00, Total Neto = 920.00
        self.assertEqual(inv.subtotal, Decimal('800.00'))
        self.assertEqual(inv.tax_total, Decimal('120.00'))
        self.assertEqual(inv.net_receivable, Decimal('920.00'))
        self.assertEqual(inv.amount_paid, Decimal('920.00'))
        self.assertEqual(inv.balance_due, Decimal('0.00'))
        self.assertEqual(inv.status, Invoice.Status.PAID)

        # 1. Póliza de Ventas (SALES)
        sales_entry = inv.journal_entries.filter(entry_type=JournalEntry.EntryType.SALES).first()
        self.assertIsNotNone(sales_entry, "Debe existir la póliza de ventas SALES.")
        self.assertTrue(sales_entry.is_balanced)
        self.assertEqual(sales_entry.total_debit, Decimal('920.00'))
        self.assertEqual(sales_entry.total_credit, Decimal('920.00'))

        sales_lines = {l.account.code: (l.debit, l.credit) for l in sales_entry.lines.all()}
        # DEBE: CxC Clientes 1103-01 por el total neto a cobrar
        self.assertEqual(sales_lines['1103-01'], (Decimal('920.00'), Decimal('0.00')))
        # HABER: Ingresos Gravados 4101-01 por la base imponible
        self.assertEqual(sales_lines['4101-01'], (Decimal('0.00'), Decimal('800.00')))
        # HABER: IVA 15% Débito Fiscal 2102-01
        self.assertEqual(sales_lines['2102-01'], (Decimal('0.00'), Decimal('120.00')))

        # 2. Póliza de Cobro (COLLECTIONS)
        receipt = inv.payments.first()
        self.assertIsNotNone(receipt)
        col_entry = JournalEntry.objects.filter(source_payment_receipt=receipt).first()
        self.assertIsNotNone(col_entry, "Debe existir la póliza de cobro COLLECTIONS.")
        self.assertTrue(col_entry.is_balanced)
        self.assertEqual(col_entry.total_debit, Decimal('920.00'))
        self.assertEqual(col_entry.total_credit, Decimal('920.00'))

        col_lines = {l.account.code: (l.debit, l.credit) for l in col_entry.lines.all()}
        # DEBE: Caja General 1101-01
        self.assertEqual(col_lines['1101-01'], (Decimal('920.00'), Decimal('0.00')))
        # HABER: Cancelación de CxC Clientes 1103-01
        self.assertEqual(col_lines['1103-01'], (Decimal('0.00'), Decimal('920.00')))

        # 3. Conciliación de CxC neta
        total_cxc_debit = sales_lines['1103-01'][0] + col_lines.get('1103-01', (Decimal('0.00'), Decimal('0.00')))[0]
        total_cxc_credit = sales_lines['1103-01'][1] + col_lines.get('1103-01', (Decimal('0.00'), Decimal('0.00')))[1]
        self.assertEqual(total_cxc_debit, total_cxc_credit, "El saldo residual de CxC para la venta POS debe ser exactamente 0.00.")

        # 4. Kardex e Inventario
        stock_after = StockBalance.objects.get(
            organization=self.org_a,
            warehouse=self.warehouse_a,
            item=self.item_cement,
        ).quantity
        self.assertEqual(stock_after, stock_before - Decimal('2.00'))
        kardex = StockMovement.objects.filter(invoice=inv)
        self.assertEqual(kardex.count(), 1)
        self.assertEqual(kardex.first().movement_type, StockMovement.MovementType.OUT_SALE_POS)
        self.assertEqual(kardex.first().quantity, Decimal('2.00'))

        # 5. Cero duplicación
        self.assertEqual(inv.journal_entries.count(), 2)
        self.assertEqual(inv.payments.count(), 1)

    def test_16_check_lifecycle_duplicate_transitions_and_cross_tenant_rejected(self):
        """
        16. Auditoría Exhaustiva del Ciclo de Cheques:
        - Bloqueo de transiciones duplicadas o inválidas (segundo depósito, segunda compensación, segundo rechazo).
        - Bloqueo de operaciones cross-tenant (cheque de Org A depositado o compensado por Org B).
        - Validación estricta de campos obligatorios (motivo de rechazo, boleta de depósito).
        """
        inv = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{'item_id': str(self.item_cement.id), 'quantity': Decimal('1.00')}],
            account=self.client_a,
            payment_method=PaymentReceipt.PaymentMethod.CHECK,
            reference_number='CHK-LIFECYCLE-01',
        )
        chk = CheckReceiptRegister.objects.get(check_number='CHK-LIFECYCLE-01')
        self.assertEqual(chk.status, CheckReceiptRegister.Status.RECEIVED)

        # 1. Depósito inválido por falta de boleta
        with self.assertRaises(ValidationError) as ctx_slip:
            deposit_check_to_bank(self.org_a, chk, self.bank_a1, deposit_slip_number='   ')
        self.assertIn("boleta", str(ctx_slip.exception).lower())

        # 2. Depósito inválido en banco inactivo
        with self.assertRaises(ValidationError) as ctx_inact:
            deposit_check_to_bank(self.org_a, chk, self.bank_a_inactive, deposit_slip_number='SLIP-01')
        self.assertIn("inactiva", str(ctx_inact.exception).lower())

        # 3. Depósito cross-tenant rechazado (Banco de Org B)
        with self.assertRaises(ValidationError) as ctx_xbank:
            deposit_check_to_bank(self.org_a, chk, self.bank_b, deposit_slip_number='SLIP-01')
        self.assertIn("no pertenece a la organización", str(ctx_xbank.exception).lower())

        # 4. Depósito ejecutado bajo Org B rechazado
        with self.assertRaises(ValidationError) as ctx_xorg:
            deposit_check_to_bank(self.org_b, chk, self.bank_b, deposit_slip_number='SLIP-01')
        self.assertIn("no pertenece a la organización", str(ctx_xorg.exception).lower())

        # 5. Depósito válido a DEPOSITED
        deposit_check_to_bank(self.org_a, chk, self.bank_a1, deposit_slip_number='SLIP-VALID-01')
        chk.refresh_from_db()
        self.assertEqual(chk.status, CheckReceiptRegister.Status.DEPOSITED)

        # 6. Segundo depósito en cheque ya depositado RECHAZADO
        with self.assertRaises(ValidationError) as ctx_dup_dep:
            deposit_check_to_bank(self.org_a, chk, self.bank_a1, deposit_slip_number='SLIP-VALID-02')
        self.assertIn("solo se pueden depositar cheques en estado recibido", str(ctx_dup_dep.exception).lower())

        # 7. Compensación ejecutada bajo Org B RECHAZADA
        with self.assertRaises(ValidationError):
            clear_check_deposit(self.org_b, chk, user=self.user_b, bank_account=self.bank_b)

        # 8. Compensación exitosa a CLEARED
        bal_before = self.bank_a1.current_balance
        clear_check_deposit(self.org_a, chk, user=self.user_a, bank_account=self.bank_a1)
        chk.refresh_from_db()
        self.assertEqual(chk.status, CheckReceiptRegister.Status.CLEARED)
        self.bank_a1.refresh_from_db()
        self.assertEqual(self.bank_a1.current_balance, bal_before + chk.amount)

        # 9. Segunda compensación en cheque ya acreditado RECHAZADA
        with self.assertRaises(ValidationError) as ctx_dup_clear:
            clear_check_deposit(self.org_a, chk, user=self.user_a, bank_account=self.bank_a1)
        self.assertIn("ya fue compensado", str(ctx_dup_clear.exception).lower())

        # 10. Intento de rechazar un cheque ya compensado RECHAZADO
        with self.assertRaises(ValidationError) as ctx_rej_cleared:
            reject_check(self.org_a, chk, reason='Intento indebido de rebote posterior', user=self.user_a)
        self.assertIn("no se puede rechazar directamente un cheque que ya fue acreditado", str(ctx_rej_cleared.exception).lower())

        # 11. Flujo de cheque rechazado independiente
        inv2 = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{'item_id': str(self.item_cement.id), 'quantity': Decimal('1.00')}],
            account=self.client_a,
            payment_method=PaymentReceipt.PaymentMethod.CHECK,
            reference_number='CHK-LIFECYCLE-02',
        )
        chk2 = CheckReceiptRegister.objects.get(check_number='CHK-LIFECYCLE-02')

        # Rechazo sin motivo RECHAZADO
        with self.assertRaises(ValidationError):
            reject_check(self.org_a, chk2, reason='   ', user=self.user_a)

        # Rechazo cross-tenant RECHAZADO
        with self.assertRaises(ValidationError):
            reject_check(self.org_b, chk2, reason='Motivo válido', user=self.user_b)

        # Rechazo válido
        reject_check(self.org_a, chk2, reason='Cheque devuelto por orden de no pago', user=self.user_a)
        chk2.refresh_from_db()
        self.assertEqual(chk2.status, CheckReceiptRegister.Status.REJECTED)

        # Segundo intento de rechazo RECHAZADO
        with self.assertRaises(ValidationError) as ctx_dup_rej:
            reject_check(self.org_a, chk2, reason='Segundo intento', user=self.user_a)
        self.assertIn("ya se encuentra marcado como rechazado", str(ctx_dup_rej.exception).lower())

        # Intento de compensar cheque rechazado RECHAZADO
        with self.assertRaises(ValidationError) as ctx_clear_rej:
            clear_check_deposit(self.org_a, chk2, user=self.user_a, bank_account=self.bank_a1)
        self.assertIn("no se puede compensar un cheque que fue rechazado", str(ctx_clear_rej.exception).lower())

    def test_17_card_batch_settlement_arithmetic_and_negative_validations(self):
        """
        17. Auditoría de Liquidaciones de Tarjetas y Cuenta Transitoria 1105-01:
        - Bloqueo de importes negativos, ceros e inconsistencias aritméticas.
        - Validación de la cuenta 1105-01 como ACTIVO circulante (Anticipo de Impuestos Retenidos).
        - Conciliación estricta de Debe == Haber y cancelación de la cuenta 1101-03 por el bruto.
        """
        # 1. Bruto <= 0
        with self.assertRaises(ValidationError) as ctx1:
            record_card_batch_settlement(
                organization=self.org_a,
                bank_account=self.bank_a1,
                batch_reference='LOTE-ERR-01',
                gross_amount=Decimal('0.00'),
            )
        self.assertIn("mayor a 0.00", str(ctx1.exception).lower())

        # 2. Comisiones o retenciones negativas
        with self.assertRaises(ValidationError) as ctx2:
            record_card_batch_settlement(
                organization=self.org_a,
                bank_account=self.bank_a1,
                batch_reference='LOTE-ERR-02',
                gross_amount=Decimal('100.00'),
                fee_amount=Decimal('-5.00'),
            )
        self.assertIn("no pueden ser negativas", str(ctx2.exception).lower())

        # 3. Neto <= 0 (Comisiones + Retenciones >= Bruto)
        with self.assertRaises(ValidationError) as ctx3:
            record_card_batch_settlement(
                organization=self.org_a,
                bank_account=self.bank_a1,
                batch_reference='LOTE-ERR-03',
                gross_amount=Decimal('100.00'),
                fee_amount=Decimal('60.00'),
                tax_retention_amount=Decimal('50.00'),
            )
        self.assertIn("debe ser mayor a 0.00", str(ctx3.exception).lower())

        # 4. Banco inactivo rechazado
        with self.assertRaises(ValidationError) as ctx4:
            record_card_batch_settlement(
                organization=self.org_a,
                bank_account=self.bank_a_inactive,
                batch_reference='LOTE-ERR-04',
                gross_amount=Decimal('500.00'),
            )
        self.assertIn("inactiva", str(ctx4.exception).lower())

        # 5. Referencia de lote vacía rechazada
        with self.assertRaises(ValidationError) as ctx5:
            record_card_batch_settlement(
                organization=self.org_a,
                bank_account=self.bank_a1,
                batch_reference='   ',
                gross_amount=Decimal('500.00'),
            )
        self.assertIn("referencia o número de lote", str(ctx5.exception).lower())

        # 6. Inspección de cuenta 1105-01 en el catálogo contable de la organización
        chart = ensure_default_chart_of_accounts(self.org_a)
        acc_1105_01 = chart['1105-01']
        self.assertEqual(acc_1105_01.account_type, AccountingAccount.AccountType.ASSET)
        self.assertIn("anticipo retención ir", acc_1105_01.name.lower())

        # 7. Liquidación válida con verificación de asientos
        settlement = record_card_batch_settlement(
            organization=self.org_a,
            bank_account=self.bank_a1,
            batch_reference='LOTE-AUDIT-FINAL-77',
            gross_amount=Decimal('2000.00'),
            fee_amount=Decimal('60.00'),
            tax_retention_amount=Decimal('40.00'),
            user=self.user_a,
        )
        self.assertEqual(settlement.net_amount, Decimal('1900.00'))
        self.assertEqual(settlement.gross_amount, settlement.net_amount + settlement.fee_amount + settlement.tax_retention_amount)

        pol = JournalEntry.objects.filter(reference='LOTE-AUDIT-FINAL-77').first()
        self.assertIsNotNone(pol)
        self.assertTrue(pol.is_balanced)
        lines = {l.account.code: (l.debit, l.credit) for l in pol.lines.all()}
        # DEBE Banco 1102-01: 1900.00
        self.assertEqual(lines['1102-01'], (Decimal('1900.00'), Decimal('0.00')))
        # DEBE Gastos Bancarios 6102-01: 60.00
        self.assertEqual(lines['6102-01'], (Decimal('60.00'), Decimal('0.00')))
        # DEBE Anticipo Retención IR 1105-01: 40.00
        self.assertEqual(lines['1105-01'], (Decimal('40.00'), Decimal('0.00')))
        # HABER Tarjetas por Liquidar 1101-03: 2000.00 (Bruto cancelado)
        self.assertEqual(lines['1101-03'], (Decimal('0.00'), Decimal('2000.00')))


class PosConcurrencyClearingTestCase(TransactionTestCase):
    """
    14. Pruebas de concurrencia y reintentos sin duplicación:
    - Múltiples hilos concurrentes intentando liquidar el mismo lote con la misma clave de idempotencia.
    - Garantiza que solo se acredita una sola vez y no se duplican saldos ni pólizas contables.
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
                'tax_id_label': 'RUC',
                'default_tax_name': 'IVA',
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )

        self.org = Organization.objects.create(
            name="Comercio Concurrente S.A.",
            slug="comercio-concurrente-pos",
            country=self.country_ni,
            currency='NIO',
            is_active=True,
        )

        self.user = User.objects.create_user(
            email='cajero_thread@concurrente.ni',
            password='TestPassword2026*',
            full_name='Cajero Thread',
        )

        self.bank = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number='001-CONCURRENT-01',
            currency='NIO',
            current_balance=Decimal('10000.00'),
            is_active=True,
        )

        ensure_default_chart_of_accounts(self.org)

    def test_14_concurrency_and_retries_without_duplication(self):
        """
        Dos hilos concurrentes ejecutando la misma liquidación de lote con idéntica clave de idempotencia:
        - Ambos deben retornar la misma instancia o completar sin colisiones corruptas.
        - El saldo bancario debe incrementarse EXACTAMENTE una sola vez.
        - Debe existir exactamente una póliza contable de liquidación y una transacción financiera.
        """
        initial_balance = self.bank.current_balance
        gross = Decimal('5000.00')
        fee = Decimal('150.00')
        net_expected = gross - fee  # 4,850.00 NIO
        batch_ref = 'BATCH-CONCURRENT-99'
        idemp_key = 'IDEMP-THREAD-CONCURRENT'

        results = []
        errors = []

        def worker():
            try:
                settlement = record_card_batch_settlement(
                    organization=self.org,
                    bank_account=self.bank,
                    batch_reference=batch_ref,
                    gross_amount=gross,
                    fee_amount=fee,
                    idempotency_key=idemp_key,
                )
                results.append(settlement.id)
            except Exception as e:
                errors.append(str(e))
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Errores concurrentes inesperados: {errors}")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1], "Ambos hilos debieron resolver la misma liquidación.")

        self.bank.refresh_from_db()
        self.assertEqual(self.bank.current_balance, initial_balance + net_expected)

        # Exactamente 1 FinancialTransaction
        self.assertEqual(
            FinancialTransaction.objects.filter(organization=self.org, reference_code=batch_ref).count(),
            1
        )

        # Exactamente 1 CardBatchSettlement
        self.assertEqual(
            CardBatchSettlement.objects.filter(organization=self.org, batch_reference=batch_ref).count(),
            1
        )

