import threading
import uuid
from decimal import Decimal, ROUND_HALF_UP
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, models, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.accounts_auth.models import User
from apps.billing.models import Invoice, InvoiceItem, PaymentReceipt, PaymentIdempotencyRecord
from apps.crm.models import Account
from apps.core.models import CountryConfig
from apps.finance.models import (
    AccountingAccount,
    BankAccount,
    FinancialTransaction,
    JournalEntry,
    JournalEntryLine,
)
from apps.finance.services import (
    ensure_default_chart_of_accounts,
    record_client_collection,
    record_invoice_sales_journal_entry,
    record_supplier_payment_journal_entry,
)
from apps.inventory.models import InventoryItem, Warehouse
from apps.purchasing.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
    SupplierInvoice,
    SupplierPayment,
)
from apps.purchasing.services import record_supplier_disbursement
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel


class RealizedFXAccountingPhase1C6TestCase(TestCase):
    """
    Suite Integral de Certificación para Fase 1C.6:
    Implementación Segura del Diferencial Cambiario Realizado.
    """

    def setUp(self):
        # 1. Configuración de País
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
            }
        )

        # 2. Organización principal A
        self.org = Organization.objects.create(
            name="Distribuidora FX Test S.A.",
            slug="distribuidora-fx-test",
            country=self.country_ni,
            tax_id="J0310000000001",
        )

        # Organización ajena B para multi-tenancy
        self.org_other = Organization.objects.create(
            name="Empresa Ajena MultiTenant S.A.",
            slug="empresa-ajena-fx-test",
            country=self.country_ni,
            tax_id="J0310000000002",
        )

        # 3. Usuario financiero
        self.user = User.objects.create_user(
            email='financiero.fx@distribuidora.test',
            full_name='Financiero FX',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            organization=self.org,
            user=self.user,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )

        # 4. Plan de cuentas por defecto asegurado
        self.accounts = ensure_default_chart_of_accounts(self.org)
        self.accounts_other = ensure_default_chart_of_accounts(self.org_other)

        # 5. Cuentas bancarias en Org A
        self.bank_usd = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number="BAC-USD-99001",
            currency="USD",
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('50000.00'),
            is_active=True,
        )

        self.bank_nio = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number="BAC-NIO-99002",
            currency="NIO",
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('500000.00'),
            is_active=True,
        )

        # Cuenta bancaria en Org B
        self.bank_other_usd = BankAccount.objects.create(
            organization=self.org_other,
            bank_name=BankAccount.BankName.BANPRO,
            account_number="BANPRO-USD-OTHER-01",
            currency="USD",
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('20000.00'),
            is_active=True,
        )

        # 6. Proveedor y Bodega para compras
        self.supplier = Supplier.objects.create(
            organization=self.org,
            code="PRV-001",
            commercial_name="Importadora de Alimentos Global S.A.",
            legal_name="Importadora de Alimentos Global S.A.",
            tax_id="J0310000009999",
            email="ventas@importadora.test",
        )
        self.warehouse = Warehouse.objects.create(
            organization=self.org,
            name="Bodega Central Managua",
            code="BOD-CEN",
        )
        self.inventory_item = InventoryItem.objects.create(
            organization=self.org,
            sku="MAT-001",
            name="Materia Prima Importada",
        )

        # 7. Cliente para ventas
        self.client_account = Account.objects.create(
            organization=self.org,
            commercial_name="Supermercado Mayorista Norte S.A.",
            tax_id="J0310000008888",
            phone="2222-3333",
            email="contabilidad@mayoristanorte.test",
        )

    # -------------------------------------------------------------------------
    # Helper: Creación de Orden de Compra en USD
    # -------------------------------------------------------------------------
    def _create_approved_purchase_order_usd(self, net_payable_usd=Decimal('1000.00'), hist_rate=Decimal('36.5000')):
        po = PurchaseOrder.objects.create(
            organization=self.org,
            number=f"OC-USD-{uuid.uuid4().hex[:6].upper()}",
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            supplier_invoice_number=f"FAC-PROV-{uuid.uuid4().hex[:4].upper()}",
            status=PurchaseOrder.Status.APPROVED,
            currency_code='USD',
            currency_symbol='$',
            exchange_rate=hist_rate,
            apply_tax=False,
            ir_withholding_rate=Decimal('0.00'),
            imi_withholding_rate=Decimal('0.00'),
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.inventory_item,
            description="Materia Prima Importada",
            quantity=Decimal('1.00'),
            unit_cost=net_payable_usd,
        )
        po.recalculate_totals()
        po.save()
        return po

    # -------------------------------------------------------------------------
    # Helper: Creación de Factura de Venta a Cliente en USD
    # -------------------------------------------------------------------------
    def _create_approved_client_invoice_usd(self, net_receivable_usd=Decimal('1000.00'), hist_rate=Decimal('36.5000')):
        inv = Invoice.objects.create(
            organization=self.org,
            number=f"FAC-CLI-{uuid.uuid4().hex[:6].upper()}",
            account=self.client_account,
            status=Invoice.Status.ISSUED,
            currency_code='USD',
            currency_symbol='$',
            exchange_rate=hist_rate,
            apply_tax=False,
            ir_retention_rate=Decimal('0.00'),
            imi_retention_rate=Decimal('0.00'),
        )
        InvoiceItem.objects.create(
            organization=self.org,
            invoice=inv,
            description="Lote de Mercancía de Exportación",
            quantity=Decimal('1.00'),
            unit_price=net_receivable_usd,
        )
        inv.recalculate_totals()
        inv.save()
        return inv

    # -------------------------------------------------------------------------
    # 1. Pago a proveedor a la tasa histórica
    # -------------------------------------------------------------------------
    def test_01_supplier_disbursement_at_historical_rate(self):
        hist_rate = Decimal('36.5000')
        po = self._create_approved_purchase_order_usd(Decimal('1000.00'), hist_rate)
        init_bank = self.bank_usd.current_balance

        egr, trx, pol = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('1000.00'),
            currency_paid='USD',
            user=self.user,
            bank_account=self.bank_usd,
            reference_number='TRF-FX-01',
            exchange_rate_applied=hist_rate,
        )

        po.refresh_from_db()
        self.bank_usd.refresh_from_db()

        self.assertEqual(po.balance_due, Decimal('0.00'))
        self.assertEqual(po.status, PurchaseOrder.Status.PAID)
        self.assertEqual(self.bank_usd.current_balance, init_bank - Decimal('1000.00'))

        self.assertIsNotNone(pol)
        self.assertTrue(pol.is_balanced)
        self.assertEqual(pol.lines.count(), 2)
        line_cxp = pol.lines.get(account=self.accounts['2101-01'])
        line_bank = pol.lines.get(account=self.accounts['1102-01'])
        self.assertEqual(line_cxp.debit, Decimal('36500.00'))
        self.assertEqual(line_bank.credit, Decimal('36500.00'))
        self.assertFalse(pol.lines.filter(account=self.accounts['6201-01']).exists())
        self.assertFalse(pol.lines.filter(account=self.accounts['4201-01']).exists())

    # -------------------------------------------------------------------------
    # 2. Pago a proveedor con tasa mayor y pérdida cambiaria
    # -------------------------------------------------------------------------
    def test_02_supplier_disbursement_with_higher_rate_realized_loss(self):
        hist_rate = Decimal('36.5000')
        settle_rate = Decimal('37.0000')
        po = self._create_approved_purchase_order_usd(Decimal('1000.00'), hist_rate)

        egr, trx, pol = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('1000.00'),
            currency_paid='USD',
            user=self.user,
            bank_account=self.bank_usd,
            reference_number='TRF-FX-02',
            exchange_rate_applied=settle_rate,
        )

        po.refresh_from_db()
        self.assertEqual(po.balance_due, Decimal('0.00'))

        self.assertTrue(pol.is_balanced)
        line_cxp = pol.lines.get(account=self.accounts['2101-01'])
        line_loss = pol.lines.get(account=self.accounts['6201-01'])
        line_bank = pol.lines.get(account=self.accounts['1102-01'])

        self.assertEqual(line_cxp.debit, Decimal('36500.00'))
        self.assertEqual(line_loss.debit, Decimal('500.00'))
        self.assertEqual(line_loss.credit, Decimal('0.00'))
        self.assertEqual(line_bank.credit, Decimal('37000.00'))

    # -------------------------------------------------------------------------
    # 3. Pago a proveedor con tasa menor y ganancia cambiaria
    # -------------------------------------------------------------------------
    def test_03_supplier_disbursement_with_lower_rate_realized_gain(self):
        hist_rate = Decimal('36.5000')
        settle_rate = Decimal('36.0000')
        po = self._create_approved_purchase_order_usd(Decimal('1000.00'), hist_rate)

        egr, trx, pol = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('1000.00'),
            currency_paid='USD',
            user=self.user,
            bank_account=self.bank_usd,
            reference_number='TRF-FX-03',
            exchange_rate_applied=settle_rate,
        )

        po.refresh_from_db()
        self.assertEqual(po.balance_due, Decimal('0.00'))

        self.assertTrue(pol.is_balanced)
        line_cxp = pol.lines.get(account=self.accounts['2101-01'])
        line_gain = pol.lines.get(account=self.accounts['4201-01'])
        line_bank = pol.lines.get(account=self.accounts['1102-01'])

        self.assertEqual(line_cxp.debit, Decimal('36500.00'))
        self.assertEqual(line_gain.credit, Decimal('500.00'))
        self.assertEqual(line_gain.debit, Decimal('0.00'))
        self.assertEqual(line_bank.credit, Decimal('36000.00'))

    # -------------------------------------------------------------------------
    # 4. Cobro a cliente con tasa mayor y ganancia cambiaria
    # -------------------------------------------------------------------------
    def test_04_client_collection_with_higher_rate_realized_gain(self):
        hist_rate = Decimal('36.5000')
        settle_rate = Decimal('37.0000')
        inv = self._create_approved_client_invoice_usd(Decimal('1000.00'), hist_rate)
        init_bank = self.bank_usd.current_balance

        receipt, trx, pol = record_client_collection(
            organization=self.org,
            invoice=inv,
            amount=Decimal('1000.00'),
            currency_paid='USD',
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            reference_number='REC-FX-04',
            user=self.user,
            exchange_rate_applied=settle_rate,
        )

        inv.refresh_from_db()
        self.bank_usd.refresh_from_db()

        self.assertEqual(inv.balance_due, Decimal('0.00'))
        self.assertEqual(inv.status, Invoice.Status.PAID)
        self.assertEqual(self.bank_usd.current_balance, init_bank + Decimal('1000.00'))

        self.assertTrue(pol.is_balanced)
        line_bank = pol.lines.get(account=self.accounts['1102-01'])
        line_cxc = pol.lines.get(account=self.accounts['1103-01'])
        line_gain = pol.lines.get(account=self.accounts['4201-01'])

        self.assertEqual(line_bank.debit, Decimal('37000.00'))
        self.assertEqual(line_cxc.credit, Decimal('36500.00'))
        self.assertEqual(line_gain.credit, Decimal('500.00'))

    # -------------------------------------------------------------------------
    # 5. Cobro a cliente con tasa menor y pérdida cambiaria
    # -------------------------------------------------------------------------
    def test_05_client_collection_with_lower_rate_realized_loss(self):
        hist_rate = Decimal('36.5000')
        settle_rate = Decimal('36.0000')
        inv = self._create_approved_client_invoice_usd(Decimal('1000.00'), hist_rate)

        receipt, trx, pol = record_client_collection(
            organization=self.org,
            invoice=inv,
            amount=Decimal('1000.00'),
            currency_paid='USD',
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            reference_number='REC-FX-05',
            user=self.user,
            exchange_rate_applied=settle_rate,
        )

        inv.refresh_from_db()
        self.assertEqual(inv.balance_due, Decimal('0.00'))

        self.assertTrue(pol.is_balanced)
        line_bank = pol.lines.get(account=self.accounts['1102-01'])
        line_loss = pol.lines.get(account=self.accounts['6201-01'])
        line_cxc = pol.lines.get(account=self.accounts['1103-01'])

        self.assertEqual(line_bank.debit, Decimal('36000.00'))
        self.assertEqual(line_loss.debit, Decimal('500.00'))
        self.assertEqual(line_cxc.credit, Decimal('36500.00'))

    # -------------------------------------------------------------------------
    # 6. Liquidaciones parciales con tasas fluctuantes
    # -------------------------------------------------------------------------
    def test_06_partial_settlements_with_fluctuating_rates(self):
        hist_rate = Decimal('36.5000')
        po = self._create_approved_purchase_order_usd(Decimal('1000.00'), hist_rate)

        # Pago parcial 1: $400 a tasa 36.8000 (Pérdida: 400 * 0.30 = C$ 120.00)
        egr1, _, pol1 = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('400.00'),
            currency_paid='USD',
            user=self.user,
            bank_account=self.bank_usd,
            reference_number='TRF-PARTIAL-01',
            exchange_rate_applied=Decimal('36.8000'),
        )
        po.refresh_from_db()
        self.assertEqual(po.balance_due, Decimal('600.00'))
        self.assertTrue(pol1.is_balanced)
        self.assertEqual(pol1.lines.get(account=self.accounts['6201-01']).debit, Decimal('120.00'))

        # Pago parcial 2: $300 a tasa 36.2000 (Ganancia: 300 * 0.30 = C$ 90.00)
        egr2, _, pol2 = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('300.00'),
            currency_paid='USD',
            user=self.user,
            bank_account=self.bank_usd,
            reference_number='TRF-PARTIAL-02',
            exchange_rate_applied=Decimal('36.2000'),
        )
        po.refresh_from_db()
        self.assertEqual(po.balance_due, Decimal('300.00'))
        self.assertTrue(pol2.is_balanced)
        self.assertEqual(pol2.lines.get(account=self.accounts['4201-01']).credit, Decimal('90.00'))

        # Pago final 3: $300 a tasa 37.1000 (Pérdida: 300 * 0.60 = C$ 180.00)
        egr3, _, pol3 = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('300.00'),
            currency_paid='USD',
            user=self.user,
            bank_account=self.bank_usd,
            reference_number='TRF-PARTIAL-03',
            exchange_rate_applied=Decimal('37.1000'),
        )
        po.refresh_from_db()
        self.assertEqual(po.balance_due, Decimal('0.00'))
        self.assertEqual(po.status, PurchaseOrder.Status.PAID)
        self.assertTrue(pol3.is_balanced)

    # -------------------------------------------------------------------------
    # 7. Extinción exacta del último saldo residual
    # -------------------------------------------------------------------------
    def test_07_exact_extinction_of_final_residual_balance(self):
        hist_rate = Decimal('36.6243')
        po = self._create_approved_purchase_order_usd(Decimal('1000.00'), hist_rate)

        # Abono 1: 333.33 USD a 36.7000
        record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('333.33'),
            currency_paid='USD',
            user=self.user,
            bank_account=self.bank_usd,
            reference_number='TRF-RESID-01',
            exchange_rate_applied=Decimal('36.7000'),
        )

        # Abono 2: 333.33 USD a 36.5000
        record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('333.33'),
            currency_paid='USD',
            user=self.user,
            bank_account=self.bank_usd,
            reference_number='TRF-RESID-02',
            exchange_rate_applied=Decimal('36.5000'),
        )

        po.refresh_from_db()
        self.assertEqual(po.balance_due, Decimal('333.34'))

        # Abono final 3: 333.34 USD a 36.8500
        _, _, pol_final = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('333.34'),
            currency_paid='USD',
            user=self.user,
            bank_account=self.bank_usd,
            reference_number='TRF-RESID-03',
            exchange_rate_applied=Decimal('36.8500'),
        )

        po.refresh_from_db()
        self.assertEqual(po.balance_due, Decimal('0.00'))

        # Verificar extinción exacta en el Libro Diario para 2101-01
        total_debits_cxp = JournalEntryLine.objects.filter(
            entry__organization=self.org,
            entry__source_purchase_order=po,
            entry__entry_type=JournalEntry.EntryType.PAYMENTS,
            account=self.accounts['2101-01'],
        ).aggregate(total=models.Sum('debit'))['total']

        expected_total_cxp = (Decimal('1000.00') * hist_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        self.assertEqual(total_debits_cxp, expected_total_cxp)
        self.assertTrue(pol_final.is_balanced)

    # -------------------------------------------------------------------------
    # 8. Redondeos en moneda original y moneda funcional
    # -------------------------------------------------------------------------
    def test_08_rounding_precision_in_original_and_functional_currencies(self):
        hist_rate = Decimal('36.6243')
        inv = self._create_approved_client_invoice_usd(Decimal('127.33'), hist_rate)

        receipt, trx, pol = record_client_collection(
            organization=self.org,
            invoice=inv,
            amount=Decimal('127.33'),
            currency_paid='USD',
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            reference_number='REC-ROUNDING-01',
            user=self.user,
            exchange_rate_applied=Decimal('36.7891'),
        )

        inv.refresh_from_db()
        self.assertEqual(inv.balance_due, Decimal('0.00'))
        self.assertTrue(pol.is_balanced)

        line_bank = pol.lines.get(account=self.accounts['1102-01'])
        self.assertEqual(line_bank.debit, Decimal('4684.36'))

        line_cxc = pol.lines.get(account=self.accounts['1103-01'])
        self.assertEqual(line_cxc.credit, Decimal('4663.37'))

        line_gain = pol.lines.get(account=self.accounts['4201-01'])
        self.assertEqual(line_gain.credit, Decimal('20.99'))

    # -------------------------------------------------------------------------
    # 9. Tasas cero, negativas o inválidas
    # -------------------------------------------------------------------------
    def test_09_rejection_of_zero_negative_or_invalid_exchange_rates(self):
        po = self._create_approved_purchase_order_usd(Decimal('100.00'))
        inv = self._create_approved_client_invoice_usd(Decimal('100.00'))

        with self.assertRaises(ValidationError) as ctx1:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('100.00'),
                currency_paid='USD',
                user=self.user,
                bank_account=self.bank_usd,
                reference_number='TRF-INV-01',
                exchange_rate_applied=Decimal('-36.5000'),
            )
        self.assertIn("estrictamente mayor a 0", str(ctx1.exception))

        with self.assertRaises(ValidationError) as ctx2:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('100.00'),
                currency_paid='USD',
                user=self.user,
                bank_account=self.bank_usd,
                reference_number='TRF-INV-02',
                exchange_rate_applied=Decimal('0.0000'),
            )
        self.assertIn("estrictamente mayor a 0", str(ctx2.exception))

        with self.assertRaises(ValidationError) as ctx3:
            record_client_collection(
                organization=self.org,
                invoice=inv,
                amount=Decimal('100.00'),
                currency_paid='USD',
                payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
                bank_account=self.bank_usd,
                reference_number='REC-INV-01',
                user=self.user,
                exchange_rate_applied=Decimal('-1.0'),
            )
        self.assertIn("estrictamente mayor a 0", str(ctx3.exception))

    # -------------------------------------------------------------------------
    # 10. Incompatibilidad de monedas
    # -------------------------------------------------------------------------
    def test_10_rejection_of_currency_mismatches(self):
        po = self._create_approved_purchase_order_usd(Decimal('500.00'))
        inv = self._create_approved_client_invoice_usd(Decimal('500.00'))

        with self.assertRaises(ValidationError) as ctx_pay:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('500.00'),
                currency_paid='USD',
                user=self.user,
                bank_account=self.bank_nio,
                reference_number='TRF-MISMATCH-01',
            )
        self.assertIn("incompatible", str(ctx_pay.exception))

        with self.assertRaises(ValidationError) as ctx_col:
            record_client_collection(
                organization=self.org,
                invoice=inv,
                amount=Decimal('500.00'),
                currency_paid='EUR',
                payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
                bank_account=self.bank_nio,
                reference_number='REC-MISMATCH-01',
                user=self.user,
            )
        self.assertIn("incompatible", str(ctx_col.exception))

    # -------------------------------------------------------------------------
    # 11. Cuenta bancaria en USD para una deuda en USD
    # -------------------------------------------------------------------------
    def test_11_bank_account_in_usd_for_usd_obligation(self):
        hist_rate = Decimal('36.5000')
        settle_rate = Decimal('37.2000')
        po = self._create_approved_purchase_order_usd(Decimal('800.00'), hist_rate)
        init_bank = self.bank_usd.current_balance

        egr, trx, pol = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('800.00'),
            currency_paid='USD',
            user=self.user,
            bank_account=self.bank_usd,
            reference_number='TRF-USD-USD-01',
            exchange_rate_applied=settle_rate,
        )

        self.bank_usd.refresh_from_db()
        self.assertEqual(self.bank_usd.current_balance, init_bank - Decimal('800.00'))

        self.assertIsNotNone(trx)
        self.assertEqual(trx.amount, Decimal('800.00'))

        line_bank = pol.lines.get(account=self.accounts['1102-01'])
        self.assertEqual(line_bank.credit, Decimal('29760.00'))

    # -------------------------------------------------------------------------
    # 12. Rechazo de cuentas bancarias pertenecientes a otra organización
    # -------------------------------------------------------------------------
    def test_12_multi_tenant_bank_account_isolation(self):
        po = self._create_approved_purchase_order_usd(Decimal('500.00'))
        inv = self._create_approved_client_invoice_usd(Decimal('500.00'))

        with self.assertRaises(ValidationError) as ctx_pay:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('500.00'),
                currency_paid='USD',
                user=self.user,
                bank_account=self.bank_other_usd,
                reference_number='TRF-OTHER-01',
            )
        self.assertIn("no pertenece a la organización", str(ctx_pay.exception))

        with self.assertRaises(ValidationError) as ctx_col:
            record_client_collection(
                organization=self.org,
                invoice=inv,
                amount=Decimal('500.00'),
                currency_paid='USD',
                payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
                bank_account=self.bank_other_usd,
                reference_number='REC-OTHER-01',
                user=self.user,
            )
        self.assertIn("no pertenece a la organización", str(ctx_col.exception))

    # -------------------------------------------------------------------------
    # 13. Prevención de duplicación de pólizas por reintentos / Idempotencia
    # -------------------------------------------------------------------------
    def test_13_idempotency_prevents_duplicate_journal_entries_and_replays(self):
        inv = self._create_approved_client_invoice_usd(Decimal('1000.00'), Decimal('36.5000'))
        key = "IDEMP-CLIENT-COL-001"

        r1, trx1, pol1 = record_client_collection(
            organization=self.org,
            invoice=inv,
            amount=Decimal('500.00'),
            currency_paid='USD',
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            reference_number='REC-IDEMP-01',
            user=self.user,
            idempotency_key=key,
            exchange_rate_applied=Decimal('37.0000'),
        )

        r2, trx2, pol2 = record_client_collection(
            organization=self.org,
            invoice=inv,
            amount=Decimal('500.00'),
            currency_paid='USD',
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            reference_number='REC-IDEMP-01',
            user=self.user,
            idempotency_key=key,
            exchange_rate_applied=Decimal('37.0000'),
        )

        self.assertEqual(r1.pk, r2.pk)
        self.assertEqual(pol1.pk, pol2.pk)
        self.assertEqual(PaymentReceipt.objects.filter(reference_number='REC-IDEMP-01').count(), 1)

        with self.assertRaises(ValidationError) as ctx_tamper:
            record_client_collection(
                organization=self.org,
                invoice=inv,
                amount=Decimal('500.00'),
                currency_paid='USD',
                payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
                bank_account=self.bank_usd,
                reference_number='REC-IDEMP-01',
                user=self.user,
                idempotency_key=key,
                exchange_rate_applied=Decimal('38.0000'),
            )
        self.assertIn("ya fue utilizada con parámetros de solicitud diferentes", str(ctx_tamper.exception))

    # -------------------------------------------------------------------------
    # 15. Rollback completo ante fallos intermedios
    # -------------------------------------------------------------------------
    def test_15_atomic_rollback_on_intermediate_failure(self):
        po = self._create_approved_purchase_order_usd(Decimal('600.00'))
        init_bank = self.bank_usd.current_balance

        from unittest.mock import patch

        with patch('apps.purchasing.services.record_supplier_payment_journal_entry', side_effect=RuntimeError("Database I/O Error")):
            with self.assertRaises(RuntimeError):
                record_supplier_disbursement(
                    organization=self.org,
                    purchase_order=po,
                    amount=Decimal('600.00'),
                    currency_paid='USD',
                    user=self.user,
                    bank_account=self.bank_usd,
                    reference_number='TRF-ROLLBACK-01',
                    exchange_rate_applied=Decimal('37.5000'),
                )

        po.refresh_from_db()
        self.bank_usd.refresh_from_db()

        self.assertEqual(po.balance_due, Decimal('600.00'))
        self.assertEqual(po.amount_paid, Decimal('0.00'))
        self.assertEqual(self.bank_usd.current_balance, init_bank)
        self.assertFalse(SupplierPayment.objects.filter(reference_number='TRF-ROLLBACK-01').exists())
        self.assertFalse(FinancialTransaction.objects.filter(reference_code='TRF-ROLLBACK-01').exists())

    # -------------------------------------------------------------------------
    # 16. Balance exacto de débitos y créditos en todos los asientos
    # -------------------------------------------------------------------------
    def test_16_journal_entry_debit_credit_exact_balance(self):
        po = self._create_approved_purchase_order_usd(Decimal('250.00'), Decimal('36.5000'))
        _, _, pol_pay = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('250.00'),
            currency_paid='USD',
            user=self.user,
            bank_account=self.bank_usd,
            reference_number='TRF-BAL-01',
            exchange_rate_applied=Decimal('37.4500'),
        )

        inv = self._create_approved_client_invoice_usd(Decimal('250.00'), Decimal('36.5000'))
        _, _, pol_col = record_client_collection(
            organization=self.org,
            invoice=inv,
            amount=Decimal('250.00'),
            currency_paid='USD',
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            reference_number='REC-BAL-01',
            user=self.user,
            exchange_rate_applied=Decimal('35.8000'),
        )

        for pol in (pol_pay, pol_col):
            tot_debit = sum(line.debit for line in pol.lines.all())
            tot_credit = sum(line.credit for line in pol.lines.all())
            self.assertEqual(tot_debit, tot_credit)
            self.assertTrue(pol.is_balanced)

    # -------------------------------------------------------------------------
    # 17. No regresión de los flujos de cobro, pagos a proveedores, POS y conciliación
    # -------------------------------------------------------------------------
    def test_17_no_regression_existing_flows(self):
        po_nio = PurchaseOrder.objects.create(
            organization=self.org,
            number=f"OC-NIO-{uuid.uuid4().hex[:6].upper()}",
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            supplier_invoice_number=f"FAC-NIO-{uuid.uuid4().hex[:4].upper()}",
            status=PurchaseOrder.Status.APPROVED,
            currency_code='NIO',
            currency_symbol='C$',
            exchange_rate=Decimal('1.0000'),
            apply_tax=False,
            ir_withholding_rate=Decimal('0.00'),
            imi_withholding_rate=Decimal('0.00'),
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po_nio,
            inventory_item=self.inventory_item,
            description="Bienes Locales",
            quantity=Decimal('1.00'),
            unit_cost=Decimal('5000.00'),
        )
        po_nio.recalculate_totals()
        po_nio.save()

        egr_nio, trx_nio, pol_nio = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po_nio,
            amount=Decimal('5000.00'),
            currency_paid='NIO',
            user=self.user,
            bank_account=self.bank_nio,
            reference_number='TRF-NIO-STD-01',
        )

        po_nio.refresh_from_db()
        self.assertEqual(po_nio.balance_due, Decimal('0.00'))
        self.assertEqual(pol_nio.lines.count(), 2)
        self.assertTrue(pol_nio.is_balanced)


class RealizedFXAccountingPhase1C6ConcurrencyTestCase(TransactionTestCase):
    """
    14. Concurrencia al liquidar una misma deuda.
    Verifica que el bloqueo pesimista select_for_update impida el sobrepago.
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
                'tax_id_regex': r'^[A-Z]\d{13}$',
            }
        )
        self.org = Organization.objects.create(
            name="Concurrencia FX Org",
            slug="concurrencia-fx-org",
            country=self.country_ni,
            tax_id="J0310000000999",
        )
        self.user = User.objects.create_user(
            email='concurrency.fx@test.com',
            full_name='Concurrency FX',
            password='Password123!',
        )
        self.accounts = ensure_default_chart_of_accounts(self.org)
        self.bank_usd = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number="BAC-CONCURRENCY-USD",
            currency="USD",
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('20000.00'),
            is_active=True,
        )
        self.supplier = Supplier.objects.create(
            organization=self.org,
            code="PRV-CONC-01",
            commercial_name="Proveedor Concurrente S.A.",
            legal_name="Proveedor Concurrente S.A.",
            tax_id="J0310000000888",
            email="concurrente@proveedor.test",
        )
        self.warehouse = Warehouse.objects.create(
            organization=self.org,
            name="Bodega Concurrente",
            code="BOD-CONC",
        )
        self.inventory_item = InventoryItem.objects.create(
            organization=self.org,
            sku="MAT-CONC-01",
            name="Material Concurrente",
        )

    def test_14_concurrency_control_on_debt_settlement(self):
        po = PurchaseOrder.objects.create(
            organization=self.org,
            number="OC-CONCURRENT-01",
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            supplier_invoice_number="FAC-CONC-01",
            status=PurchaseOrder.Status.APPROVED,
            currency_code='USD',
            currency_symbol='$',
            exchange_rate=Decimal('36.5000'),
            apply_tax=False,
            ir_withholding_rate=Decimal('0.00'),
            imi_withholding_rate=Decimal('0.00'),
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.inventory_item,
            description="Item Concurrente",
            quantity=Decimal('1.00'),
            unit_cost=Decimal('1000.00'),
        )
        po.recalculate_totals()
        po.save()

        results = []
        errors = []

        def attempt_payment(ref_num, rate):
            connection.close()
            try:
                egr, trx, pol = record_supplier_disbursement(
                    organization=self.org,
                    purchase_order=po,
                    amount=Decimal('1000.00'),
                    currency_paid='USD',
                    user=self.user,
                    bank_account=self.bank_usd,
                    reference_number=ref_num,
                    exchange_rate_applied=rate,
                )
                results.append(egr)
            except Exception as e:
                errors.append(e)
            finally:
                connection.close()

        t1 = threading.Thread(target=attempt_payment, args=('TRF-CONC-A', Decimal('37.0000')))
        t2 = threading.Thread(target=attempt_payment, args=('TRF-CONC-B', Decimal('36.2000')))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValidationError)

        po.refresh_from_db()
        self.assertEqual(po.balance_due, Decimal('0.00'))
        self.assertEqual(po.amount_paid, Decimal('1000.00'))
        self.assertEqual(po.status, PurchaseOrder.Status.PAID)
