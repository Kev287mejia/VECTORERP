from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.core.models import CountryConfig
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel
from apps.finance.models import (
    AccountingAccount,
    BankAccount,
    FinancialTransaction,
    JournalEntry,
    JournalEntryLine,
    PettyCashFund,
)
from apps.finance.services import (
    allocate_petty_cash_fund,
    ensure_default_chart_of_accounts,
    record_supplier_payment_journal_entry,
)
from apps.finance.views import PettyCashFundCreateView
from apps.purchasing.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
    SupplierInvoice,
    SupplierPayment,
)
from apps.purchasing.services import record_supplier_disbursement
from apps.inventory.models import InventoryItem, Warehouse

User = get_user_model()


class Phase1C83AIntegrityTestCase(TestCase):
    """
    VECTOR ERP — Fase 1C.8.3A: Correcciones Controladas de Integridad Contable
    Suite TDD de Verificación de Integridad:
    - H-08: Inmutabilidad de JournalEntry y JournalEntryLine.
    - H-07: Tasa Cambiaria Obligatoria y eliminación de fallback silencioso a 1.0.
    - H-09: Corrección de Saldo Inicial en Caja Chica y prevención de doble dotación.
    """

    def setUp(self):
        self.factory = RequestFactory()

        # 1. Configuración Regional
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
                'tax_id_regex': r'^[A-Z]\d{13}$',
                'is_active': True,
                'default_tax_rate': Decimal('15.00'),
                'default_tax_name': 'IVA',
            }
        )

        # 2. Organización
        self.org = Organization.objects.create(
            name="VECTOR CONSTRUCCIONES S.A.",
            slug="vector-construcciones-1c83a",
            country=self.country_ni,
            currency="NIO",
            is_active=True,
            tax_id="J0310000000099",
            enabled_modules=['finance', 'purchasing', 'inventory', 'billing'],
        )

        # 3. Usuario Financiero (Rol Owner Admin)
        self.user = User.objects.create_user(
            email='contralor.test@vector.ni',
            full_name='Lic. Auditor Contable',
            password='Password123!',
        )
        self.membership = OrganizationMembership.objects.create(
            organization=self.org,
            user=self.user,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )

        # 4. Catálogo Contable Estándar
        self.accounts = ensure_default_chart_of_accounts(self.org)
        self.acc_caja = self.accounts['1101-01']
        self.acc_bancos = self.accounts['1102-01']
        self.acc_cxp = self.accounts['2101-01']
        self.acc_fx_loss = self.accounts['6201-01']
        self.acc_fx_gain = self.accounts['4201-01']

        # 5. Cuentas Bancarias
        self.bank_nio = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number="BAC-NIO-1C83A-01",
            currency="NIO",
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('200000.00'),
            is_active=True,
        )

        self.bank_usd = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number="BAC-USD-1C83A-02",
            currency="USD",
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('10000.00'),
            is_active=True,
        )

        # 6. Proveedor y Bodega
        self.supplier = Supplier.objects.create(
            organization=self.org,
            legal_name="Distribuidora de Cemento S.A.",
            commercial_name="Cemento Canal",
            tax_id="J0310000000888",
        )

        self.warehouse = Warehouse.objects.create(
            organization=self.org,
            name="Bodega Central Managua",
            code="BOD-01",
        )

        self.item = InventoryItem.objects.create(
            organization=self.org,
            name="Bolsa Cemento Gris 42.5kg",
            sku="CEM-001",
            unit="BOL",
            unit_cost=Decimal('10.00'),
        )

    def _create_sample_purchase_order(self, currency='USD', total=Decimal('1000.00'), rate=Decimal('36.6243')):
        po = PurchaseOrder.objects.create(
            organization=self.org,
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            currency_code=currency,
            subtotal=total,
            tax_total=Decimal('0.00'),
            gross_total=total,
            net_payable=total,
            balance_due=total,
            amount_paid=Decimal('0.00'),
            exchange_rate=rate,
            stock_received=True,
            status=PurchaseOrder.Status.APPROVED,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item,
            quantity=Decimal('100.00'),
            unit_cost=Decimal('10.00'),
            line_total=total,
        )
        return po

    # =========================================================================
    # BLOQUE H-08: INTEGRIDAD DE PÓLIZAS CONTABLES (JOURNALENTRY Y LÍNEAS)
    # =========================================================================

    def test_h08_01_cannot_delete_journal_entry_instance(self):
        """H-08: JournalEntry.delete() debe lanzar ValidationError incondicionalmente."""
        entry = JournalEntry.objects.create(
            organization=self.org,
            concept="Póliza de Prueba Inmutabilidad",
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acc_caja,
            debit=Decimal('100.00'),
            credit=Decimal('0.00'),
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acc_bancos,
            debit=Decimal('0.00'),
            credit=Decimal('100.00'),
        )
        entry.verify_and_save_balance()
        self.assertTrue(entry.is_balanced)

        with self.assertRaises(ValidationError) as ctx:
            entry.delete()

        self.assertIn("inmutable", str(ctx.exception).lower())
        self.assertTrue(JournalEntry.objects.filter(pk=entry.pk).exists())
        self.assertEqual(entry.lines.count(), 2)

    def test_h08_02_cannot_delete_journal_entry_line_instance(self):
        """H-08: JournalEntryLine.delete() debe lanzar ValidationError incondicionalmente."""
        entry = JournalEntry.objects.create(
            organization=self.org,
            concept="Póliza de Prueba Inmutabilidad Línea",
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
        )
        line1 = JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acc_caja,
            debit=Decimal('100.00'),
            credit=Decimal('0.00'),
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acc_bancos,
            debit=Decimal('0.00'),
            credit=Decimal('100.00'),
        )
        entry.verify_and_save_balance()

        with self.assertRaises(ValidationError) as ctx:
            line1.delete()

        self.assertIn("inmutable", str(ctx.exception).lower())
        self.assertTrue(JournalEntryLine.objects.filter(pk=line1.pk).exists())

    def test_h08_03_cannot_modify_balanced_journal_entry_save(self):
        """H-08: Modificar y guardar una póliza ya balanceada vía instance.save() debe lanzar ValidationError."""
        entry = JournalEntry.objects.create(
            organization=self.org,
            concept="Póliza Original Cuadrada",
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acc_caja,
            debit=Decimal('500.00'),
            credit=Decimal('0.00'),
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acc_bancos,
            debit=Decimal('0.00'),
            credit=Decimal('500.00'),
        )
        entry.verify_and_save_balance()
        self.assertTrue(entry.is_balanced)

        # Intento de alterar concepto o póliza
        entry.concept = "CONCEPTO FRAUDULENTO POST-CIERRE"
        with self.assertRaises(ValidationError) as ctx:
            entry.save()

        self.assertIn("inmutable", str(ctx.exception).lower())
        entry.refresh_from_db()
        self.assertEqual(entry.concept, "Póliza Original Cuadrada")

    def test_h08_04_cannot_modify_journal_entry_line_save(self):
        """H-08: Modificar una línea de asiento contable asentada vía instance.save() debe lanzar ValidationError."""
        entry = JournalEntry.objects.create(
            organization=self.org,
            concept="Póliza Original para Test Línea Save",
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
        )
        line1 = JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acc_caja,
            debit=Decimal('250.00'),
            credit=Decimal('0.00'),
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acc_bancos,
            debit=Decimal('0.00'),
            credit=Decimal('250.00'),
        )
        entry.verify_and_save_balance()

        line1.debit = Decimal('99999.00')
        with self.assertRaises(ValidationError) as ctx:
            line1.save()

        self.assertIn("inmutable", str(ctx.exception).lower())
        line1.refresh_from_db()
        self.assertEqual(line1.debit, Decimal('250.00'))

    def test_h08_05_cannot_add_line_to_already_balanced_entry(self):
        """H-08: Intentar inyectar nuevas líneas a una póliza ya balanceada debe ser rechazado."""
        entry = JournalEntry.objects.create(
            organization=self.org,
            concept="Póliza Cerrada",
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acc_caja,
            debit=Decimal('100.00'),
            credit=Decimal('0.00'),
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acc_bancos,
            debit=Decimal('0.00'),
            credit=Decimal('100.00'),
        )
        entry.verify_and_save_balance()
        self.assertTrue(entry.is_balanced)

        with self.assertRaises(ValidationError) as ctx:
            JournalEntryLine.objects.create(
                organization=self.org,
                entry=entry,
                account=self.acc_caja,
                debit=Decimal('50.00'),
                credit=Decimal('0.00'),
            )

        self.assertIn("asentada", str(ctx.exception).lower())
        self.assertEqual(entry.lines.count(), 2)

    def test_h08_06_entry_protected_on_delete_cascade_prevention(self):
        """H-08: JournalEntryLine debe proteger contra borrado en cascada (models.PROTECT)."""
        field = JournalEntryLine._meta.get_field('entry')
        self.assertEqual(field.remote_field.on_delete, models.PROTECT)

    def test_h08_07_legitimate_reversal_is_permitted(self):
        """H-08: La reversión contable legítima mediante un nuevo asiento de ajuste está permitida."""
        entry = JournalEntry.objects.create(
            organization=self.org,
            concept="Asiento Original a Revertir",
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acc_caja,
            debit=Decimal('1000.00'),
            credit=Decimal('0.00'),
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acc_bancos,
            debit=Decimal('0.00'),
            credit=Decimal('1000.00'),
        )
        entry.verify_and_save_balance()

        # Reversión legítima
        reversal = JournalEntry.objects.create(
            organization=self.org,
            concept=f"REVERSIÓN - {entry.concept}",
            reference=entry.number,
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=reversal,
            account=self.acc_caja,
            debit=Decimal('0.00'),
            credit=Decimal('1000.00'),
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=reversal,
            account=self.acc_bancos,
            debit=Decimal('1000.00'),
            credit=Decimal('0.00'),
        )
        balanced = reversal.verify_and_save_balance()
        self.assertTrue(balanced)
        self.assertTrue(reversal.is_balanced)

    # =========================================================================
    # BLOQUE H-07: TASA CAMBIARIA OBLIGATORIA (ELIMINACIÓN DE FALLBACK A 1.0)
    # =========================================================================

    def test_h07_01_rejects_missing_exchange_rate_on_usd_disbursement(self):
        """H-07: Debe rechazar desembolsos con conversión de divisa si la tasa es None o no es enviada."""
        po_nio = self._create_sample_purchase_order(currency='NIO', total=Decimal('3660.00'), rate=Decimal('1.0000'))

        with self.assertRaises(ValidationError) as ctx:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po_nio,
                amount=Decimal('100.00'),
                currency_paid='USD',
                payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
                bank_account=self.bank_usd,
                exchange_rate_applied=None,
                user=self.user,
            )

        self.assertIn("tasa de cambio", str(ctx.exception).lower())

    def test_h07_02_rejects_zero_or_negative_exchange_rate_on_usd_disbursement(self):
        """H-07: Debe rechazar tasas cero o negativas en desembolsos en moneda extranjera."""
        po = self._create_sample_purchase_order(currency='USD', total=Decimal('100.00'), rate=Decimal('36.60'))

        with self.assertRaises(ValidationError):
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('100.00'),
                currency_paid='USD',
                payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
                bank_account=self.bank_usd,
                exchange_rate_applied=Decimal('0.0000'),
                user=self.user,
            )

        with self.assertRaises(ValidationError):
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('100.00'),
                currency_paid='USD',
                payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
                bank_account=self.bank_usd,
                exchange_rate_applied=Decimal('-36.6000'),
                user=self.user,
            )

    def test_h07_03_accepts_valid_exchange_rate_on_usd_disbursement(self):
        """H-07: Desembolso en USD con tasa válida mayor a cero debe procesarse exitosamente."""
        po = self._create_sample_purchase_order(currency='USD', total=Decimal('100.00'), rate=Decimal('36.60'))

        egr, trx, pol = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('100.00'),
            currency_paid='USD',
            payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            exchange_rate_applied=Decimal('36.7500'),
            user=self.user,
            reference_number="TRX-USD-VALID",
        )

        self.assertEqual(egr.currency_paid, 'USD')
        self.assertEqual(egr.exchange_rate_applied, Decimal('36.7500'))
        self.bank_usd.refresh_from_db()
        self.assertEqual(self.bank_usd.current_balance, Decimal('9900.00'))

        # Verificar póliza contable generada
        self.assertIsNotNone(pol)
        self.assertTrue(pol.is_balanced)
        # Desembolso en NIO = 100 * 36.75 = 3,675.00
        self.assertEqual(pol.total_credit, Decimal('3675.00'))

    def test_h07_04_nio_disbursement_without_conversion_succeeds(self):
        """H-07: Operaciones en NIO (moneda funcional) para órdenes en NIO no requieren tasa de cambio."""
        po_nio = PurchaseOrder.objects.create(
            organization=self.org,
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            currency_code="NIO",
            subtotal=Decimal('3600.00'),
            tax_total=Decimal('0.00'),
            gross_total=Decimal('3600.00'),
            net_payable=Decimal('3600.00'),
            balance_due=Decimal('3600.00'),
            amount_paid=Decimal('0.00'),
            exchange_rate=Decimal('1.0000'),
            stock_received=True,
            status=PurchaseOrder.Status.APPROVED,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po_nio,
            inventory_item=self.item,
            quantity=Decimal('10.00'),
            unit_cost=Decimal('360.00'),
            line_total=Decimal('3600.00'),
        )

        egr, trx, pol = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po_nio,
            amount=Decimal('3600.00'),
            currency_paid='NIO',
            payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_nio,
            exchange_rate_applied=None,  # No requerido para NIO -> NIO
            user=self.user,
            reference_number="TRX-NIO-LOCAL",
        )

        self.assertEqual(egr.currency_paid, 'NIO')
        self.bank_nio.refresh_from_db()
        self.assertEqual(self.bank_nio.current_balance, Decimal('196400.00'))
        self.assertIsNotNone(pol)
        self.assertEqual(pol.total_credit, Decimal('3600.00'))

    def test_h07_05_rejected_disbursement_rolls_back_cleanly(self):
        """H-07: Una operación rechazada por tasa inválida no debe dejar escrituras parciales."""
        po = self._create_sample_purchase_order(currency='USD', total=Decimal('500.00'), rate=Decimal('36.60'))
        initial_bank_balance = self.bank_usd.current_balance

        with self.assertRaises(ValidationError):
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('500.00'),
                currency_paid='USD',
                payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
                bank_account=self.bank_usd,
                exchange_rate_applied=Decimal('-36.6000'),  # Tasa negativa rechazada
                user=self.user,
            )

        self.bank_usd.refresh_from_db()
        self.assertEqual(self.bank_usd.current_balance, initial_bank_balance)
        self.assertEqual(SupplierPayment.objects.filter(purchase_order=po).count(), 0)
        self.assertEqual(FinancialTransaction.objects.filter(bank_account=self.bank_usd, amount=Decimal('500.00')).count(), 0)
        self.assertEqual(JournalEntry.objects.filter(source_purchase_order=po, entry_type=JournalEntry.EntryType.PAYMENTS).count(), 0)

    def test_h07_06_record_supplier_payment_journal_entry_rejects_missing_rate_directly(self):
        """H-07: record_supplier_payment_journal_entry debe lanzar ValidationError si exchange_rate_applied es None/cero en USD."""
        po = self._create_sample_purchase_order(currency='USD', total=Decimal('100.00'), rate=Decimal('36.60'))
        payment = SupplierPayment.objects.create(
            organization=self.org,
            purchase_order=po,
            currency_paid='USD',
            amount_in_payment_currency=Decimal('100.00'),
            exchange_rate_applied=Decimal('36.6000'),
            amount_applied_to_order=Decimal('100.00'),
            payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
        )
        payment.exchange_rate_applied = None  # Simular omisión de tasa para probar el fallback a 1.0

        with self.assertRaises(ValidationError) as ctx:
            record_supplier_payment_journal_entry(
                organization=self.org,
                supplier_payment=payment,
                user=self.user,
            )

        self.assertIn("tasa de cambio", str(ctx.exception).lower())

    # =========================================================================
    # BLOQUE H-09: SALDO INICIAL Y CONTROL DE DOTACIÓN DE CAJA CHICA
    # =========================================================================

    def test_h09_01_petty_cash_fund_created_with_zero_current_balance(self):
        """H-09: PettyCashFundCreateView debe inicializar current_balance en 0.00, no en allocated_amount."""
        request = self.factory.post('/finance/petty-cash/create/', {
            'code': 'CAJ-TEST-01',
            'name': 'Caja Chica Frente Norte',
            'custodian': str(self.user.pk),
            'allocated_amount': '15000.00',
            'is_active': 'on',
        })
        request.user = self.user
        request.organization = self.org
        request.membership = self.membership
        request.role_level = RoleLevel.OWNER_ADMIN

        from django.contrib.sessions.middleware import SessionMiddleware
        from django.contrib.messages.middleware import MessageMiddleware

        SessionMiddleware(lambda req: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda req: None).process_request(request)

        view = PettyCashFundCreateView.as_view()
        view(request)

        fund = PettyCashFund.objects.filter(organization=self.org, code='CAJ-TEST-01').first()
        self.assertIsNotNone(fund)
        self.assertEqual(fund.allocated_amount, Decimal('15000.00'))
        # SALDO REAL FÍSICO DEBE SER 0.00, NO 15000.00
        self.assertEqual(fund.current_balance, Decimal('0.00'))

    def test_h09_02_single_allocation_updates_bank_and_fund(self):
        """H-09: allocate_petty_cash_fund debe descontar banco e incrementar caja una sola vez."""
        fund = PettyCashFund.objects.create(
            organization=self.org,
            code="CAJ-02",
            name="Caja Chica Cuadrilla Beta",
            custodian=self.user,
            allocated_amount=Decimal('10000.00'),
            current_balance=Decimal('0.00'),
        )
        initial_bank = self.bank_nio.current_balance

        trx = allocate_petty_cash_fund(
            organization=self.org,
            fund=fund,
            bank_account=self.bank_nio,
            amount=Decimal('10000.00'),
            user=self.user,
        )

        fund.refresh_from_db()
        self.bank_nio.refresh_from_db()

        self.assertEqual(fund.current_balance, Decimal('10000.00'))
        self.assertEqual(self.bank_nio.current_balance, initial_bank - Decimal('10000.00'))
        self.assertEqual(trx.balance_after, self.bank_nio.current_balance)
        self.assertEqual(trx.transaction_type, FinancialTransaction.TransactionType.PETTY_CASH_ALLOCATION)

    def test_h09_03_prevents_duplicate_or_excess_allocation(self):
        """H-09: Una segunda dotación accidental o un monto que supere allocated_amount debe ser rechazado."""
        fund = PettyCashFund.objects.create(
            organization=self.org,
            code="CAJ-03",
            name="Caja Chica Cuadrilla Gamma",
            custodian=self.user,
            allocated_amount=Decimal('10000.00'),
            current_balance=Decimal('0.00'),
        )

        # Primera dotación completa
        allocate_petty_cash_fund(
            organization=self.org,
            fund=fund,
            bank_account=self.bank_nio,
            amount=Decimal('10000.00'),
            user=self.user,
        )
        fund.refresh_from_db()
        self.assertEqual(fund.current_balance, Decimal('10000.00'))

        # Intento de segunda dotación accidental
        with self.assertRaises(ValidationError) as ctx:
            allocate_petty_cash_fund(
                organization=self.org,
                fund=fund,
                bank_account=self.bank_nio,
                amount=Decimal('10000.00'),
                user=self.user,
            )

        self.assertIn("excede el monto máximo autorizado", str(ctx.exception).lower())
        fund.refresh_from_db()
        self.assertEqual(fund.current_balance, Decimal('10000.00'))

    def test_h09_04_insufficient_bank_balance_rolls_back_allocation(self):
        """H-09: Si el banco no tiene fondos suficientes, se debe lanzar ValidationError y hacer rollback."""
        poor_bank = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number="BAC-POOR-01",
            currency="NIO",
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('500.00'),
            is_active=True,
        )
        fund = PettyCashFund.objects.create(
            organization=self.org,
            code="CAJ-04",
            name="Caja Chica Cuadrilla Delta",
            custodian=self.user,
            allocated_amount=Decimal('5000.00'),
            current_balance=Decimal('0.00'),
        )

        with self.assertRaises(ValidationError) as ctx:
            allocate_petty_cash_fund(
                organization=self.org,
                fund=fund,
                bank_account=poor_bank,
                amount=Decimal('5000.00'),
                user=self.user,
            )

        self.assertIn("saldo bancario insuficiente", str(ctx.exception).lower())
        poor_bank.refresh_from_db()
        fund.refresh_from_db()
        self.assertEqual(poor_bank.current_balance, Decimal('500.00'))
        self.assertEqual(fund.current_balance, Decimal('0.00'))
        self.assertEqual(FinancialTransaction.objects.filter(bank_account=poor_bank).count(), 0)

    def test_h09_05_no_incomplete_journal_entries_created(self):
        """H-09: Confirmar que la dotación no genera pólizas incompletas ni ad-hoc antes de aprobación contable DEC-04."""
        initial_entries_count = JournalEntry.objects.filter(organization=self.org).count()
        fund = PettyCashFund.objects.create(
            organization=self.org,
            code="CAJ-05",
            name="Caja Chica Cuadrilla Epsilon",
            custodian=self.user,
            allocated_amount=Decimal('8000.00'),
            current_balance=Decimal('0.00'),
        )
        allocate_petty_cash_fund(
            organization=self.org,
            fund=fund,
            bank_account=self.bank_nio,
            amount=Decimal('8000.00'),
            user=self.user,
        )
        final_entries_count = JournalEntry.objects.filter(organization=self.org).count()
        # En esta fase 1C.8.3A, no se generan pólizas desbalanceadas o sin catálogo aprobado
        self.assertEqual(final_entries_count, initial_entries_count)
