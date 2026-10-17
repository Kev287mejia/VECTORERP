import uuid
from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.db import models
from django.test import TestCase
from django.utils import timezone

from apps.accounts_auth.models import User
from apps.billing.models import Invoice, InvoiceItem, PaymentReceipt
from apps.core.models import CountryConfig
from apps.crm.models import Account
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
    record_purchase_order_journal_entry,
    record_supplier_payment_journal_entry,
)
from apps.inventory.models import InventoryItem, Warehouse
from apps.purchasing.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
    SupplierPayment,
)
from apps.purchasing.services import record_supplier_disbursement
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel


class FXBankAssetCarryingValueAuditTestCase(TestCase):
    """
    Fase 1C.6.2: Auditoría y Demostración de la Base de Valoración Contable
    de Activos Bancarios en Moneda Extranjera (USD).

    Contrasta el comportamiento del código actual contra los invariantes contables
    requeridos para partidas monetarias de activo en moneda extranjera.
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
            name="Empresa Auditoria FX Activos S.A.",
            slug="audit-fx-activos",
            country=self.country_ni,
            tax_id="J0310000000101",
        )
        self.org_b = Organization.objects.create(
            name="Empresa B Tenant Aislado S.A.",
            slug="audit-fx-tenant-b",
            country=self.country_ni,
            tax_id="J0310000000102",
        )

        self.user = User.objects.create_user(
            email='auditor.fx@activos.test',
            full_name='Auditor FX Activos',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            organization=self.org,
            user=self.user,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )

        self.accounts = ensure_default_chart_of_accounts(self.org)
        self.accounts_b = ensure_default_chart_of_accounts(self.org_b)

        # Cuenta bancaria en USD
        self.bank_usd = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number="BAC-USD-AUDIT-001",
            currency="USD",
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('0.00'),
        )

        # Cuenta bancaria en USD en Org B
        self.bank_usd_b = BankAccount.objects.create(
            organization=self.org_b,
            bank_name=BankAccount.BankName.LAFISE,
            account_number="LAF-USD-TENANT-B",
            currency="USD",
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('0.00'),
        )

        # Proveedor
        self.supplier = Supplier.objects.create(
            organization=self.org,
            code="PRV-USD-01",
            commercial_name="Proveedor Internacional USD",
            tax_id="J0310000000201",
            email="proveedor.usd@internacional.test",
        )

        # Almacén e ítem
        self.warehouse = Warehouse.objects.create(
            organization=self.org,
            name="Bodega Central Dólares",
            code="BOD-USD-01",
        )
        self.item = InventoryItem.objects.create(
            organization=self.org,
            sku="INS-USD-001",
            name="Insumo Importado Especializado",
        )

        # Cliente
        self.client = Account.objects.create(
            organization=self.org,
            commercial_name="Cliente Corporativo Exportación",
            tax_id="J0310000009999",
            email="cliente.export@corporativo.test",
        )

    def _create_po(self, amount_usd, exchange_rate):
        po = PurchaseOrder.objects.create(
            organization=self.org,
            number=f"OC-USD-{uuid.uuid4().hex[:6].upper()}",
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            supplier_invoice_number=f"FAC-PROV-{uuid.uuid4().hex[:4].upper()}",
            status=PurchaseOrder.Status.APPROVED,
            currency_code='USD',
            currency_symbol='$',
            exchange_rate=exchange_rate,
            apply_tax=False,
            ir_withholding_rate=Decimal('0.00'),
            imi_withholding_rate=Decimal('0.00'),
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item,
            description="Materia Prima Importada",
            quantity=Decimal('1.00'),
            unit_cost=amount_usd,
        )
        po.recalculate_totals()
        po.save()
        return po

    def _create_inv(self, amount_usd, exchange_rate):
        inv = Invoice.objects.create(
            organization=self.org,
            number=f"FAC-CLI-{uuid.uuid4().hex[:6].upper()}",
            account=self.client,
            status=Invoice.Status.ISSUED,
            currency_code='USD',
            currency_symbol='$',
            exchange_rate=exchange_rate,
            apply_tax=False,
            ir_retention_rate=Decimal('0.00'),
            imi_retention_rate=Decimal('0.00'),
        )
        InvoiceItem.objects.create(
            organization=self.org,
            invoice=inv,
            description="Servicio Exportación",
            quantity=Decimal('1.00'),
            unit_price=amount_usd,
        )
        inv.recalculate_totals()
        inv.save()
        return inv

    def _get_bank_ledger_balance_nio(self, bank_account=None):
        """Retorna el saldo neto (Debe - Haber) de la cuenta contable 1102-01 en el Libro Diario."""
        bank_acc = self.accounts['1102-01']
        totals = JournalEntryLine.objects.filter(
            entry__organization=self.org,
            account=bank_acc,
        ).aggregate(
            d=models.Sum('debit'),
            c=models.Sum('credit'),
        )
        debit = totals['d'] or Decimal('0.00')
        credit = totals['c'] or Decimal('0.00')
        return debit - credit

    def test_audit_case_a_identical_initial_base_demonstrates_carrying_value_defect(self):
        """
        Caso A: Activo y Pasivo con igual base inicial.
        - Deuda de US$ 100.00 reconocida a tasa 36.50 (CxP contable = C$ 3,650.00).
        - Activo bancario de US$ 100.00 reconocido a tasa 36.50 (Bancos contable = C$ 3,650.00).
        - Se paga la deuda cuando la tasa es 36.48 con los dólares del banco.

        Demostración del comportamiento del código actual:
        1. El código actual acredita Bancos (1102-01) a la tasa de liquidación (36.48 * 100 = C$ 3,648.00).
        2. Como consecuencia, la cuenta contable 1102-01 queda con un remanente huérfano de +C$ 2.00,
           a pesar de que el saldo operativo del banco es US$ 0.00.
        3. El código actual reconoce una Ganancia Cambiaria de C$ 2.00 en 4201-01, sin reconocer
           la Pérdida Cambiaria del activo bancario de C$ 2.00 en 6201-01, distorsionando el resultado neto.
        """
        # 1. Reconocer la deuda de US$ 100 a tasa 36.50
        po = self._create_po(Decimal('100.00'), Decimal('36.5000'))
        po_entry = record_purchase_order_journal_entry(self.org, po, self.user, accounts=self.accounts)
        self.assertTrue(po_entry.is_balanced)
        cxp_line = po_entry.lines.get(account=self.accounts['2101-01'])
        self.assertEqual(cxp_line.credit, Decimal('3650.00'))

        # 2. Reconocer la disponibilidad bancaria de US$ 100 a tasa 36.50
        inv = self._create_inv(Decimal('100.00'), Decimal('36.5000'))
        receipt, _, col_entry = record_client_collection(
            organization=self.org,
            invoice=inv,
            amount=Decimal('100.00'),
            currency_paid="USD",
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            user=self.user,
            reference_number="DEP-USD-CASE-A",
            exchange_rate_applied=Decimal('36.5000'),
        )
        self.bank_usd.refresh_from_db()
        self.assertEqual(self.bank_usd.current_balance, Decimal('100.00'))
        self.assertEqual(self._get_bank_ledger_balance_nio(), Decimal('3650.00'))

        # 3. Pagar la deuda de US$ 100.00 cuando la tasa sea 36.4800 usando bank_usd
        payment, _, pay_entry = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('100.00'),
            currency_paid="USD",
            payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            user=self.user,
            reference_number="PAG-USD-CASE-A",
            exchange_rate_applied=Decimal('36.4800'),
        )
        self.assertTrue(pay_entry.is_balanced)

        # 4. Auditoría de resultados reales en el código actual:
        self.bank_usd.refresh_from_db()
        self.assertEqual(self.bank_usd.current_balance, Decimal('0.00'))

        # A) Proveedores (2101-01) cancela C$ 3,650.00
        line_cxp = pay_entry.lines.get(account=self.accounts['2101-01'])
        self.assertEqual(line_cxp.debit, Decimal('3650.00'))

        # B) Bancos (1102-01) se acredita a la tasa de liquidación (36.48): C$ 3,648.00
        line_bank = pay_entry.lines.get(account=self.accounts['1102-01'])
        self.assertEqual(line_bank.credit, Decimal('3648.00'))

        # C) Ganancia cambiaria reconocida por el código actual: C$ 2.00 en 4201-01
        line_gain = pay_entry.lines.filter(account=self.accounts['4201-01']).first()
        self.assertIsNotNone(line_gain)
        self.assertEqual(line_gain.credit, Decimal('2.00'))

        # D) HALLAZGO / DEFECTO DE VALORACIÓN CONTABLE EN EL CÓDIGO ACTUAL:
        # El saldo en el mayor contable de Bancos (1102-01) NO es 0.00:
        # Debe = 3,650.00, Haber = 3,648.00 -> Quedan C$ 2.00 huérfanos!
        bank_ledger_balance = self._get_bank_ledger_balance_nio()
        self.assertEqual(bank_ledger_balance, Decimal('2.00'))

        # E) Resultado en Ganancia/Pérdida en el código actual:
        total_gains = JournalEntryLine.objects.filter(
            entry__organization=self.org,
            account=self.accounts['4201-01'],
        ).aggregate(t=models.Sum('credit'))['t'] or Decimal('0.00')
        total_losses = JournalEntryLine.objects.filter(
            entry__organization=self.org,
            account=self.accounts['6201-01'],
        ).aggregate(t=models.Sum('debit'))['t'] or Decimal('0.00')

        self.assertEqual(total_gains, Decimal('2.00'))
        self.assertEqual(total_losses, Decimal('0.00'))

    def test_audit_case_b_different_historical_bases(self):
        """
        Caso B: Activo y Pasivo con bases históricas diferentes.
        - Pasivo: US$ 100.00 reconocido a tasa 36.50 (CxP contable = C$ 3,650.00).
        - Activo bancario: US$ 100.00 incorporado a tasa 36.20 (Bancos contable = C$ 3,620.00).
        - Liquidación a tasa 36.48 con esos fondos USD.
        """
        # 1. Pasivo a tasa 36.50
        po = self._create_po(Decimal('100.00'), Decimal('36.5000'))
        record_purchase_order_journal_entry(self.org, po, self.user, accounts=self.accounts)

        # 2. Activo bancario incorporado a tasa 36.20
        inv = self._create_inv(Decimal('100.00'), Decimal('36.2000'))
        record_client_collection(
            organization=self.org,
            invoice=inv,
            amount=Decimal('100.00'),
            currency_paid="USD",
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            user=self.user,
            reference_number="DEP-USD-CASE-B",
            exchange_rate_applied=Decimal('36.2000'),
        )
        self.assertEqual(self._get_bank_ledger_balance_nio(), Decimal('3620.00'))

        # 3. Pagar pasivo a tasa 36.48
        payment, _, pay_entry = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('100.00'),
            currency_paid="USD",
            payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            user=self.user,
            reference_number="PAG-USD-CASE-B",
            exchange_rate_applied=Decimal('36.4800'),
        )
        self.assertTrue(pay_entry.is_balanced)

        self.bank_usd.refresh_from_db()
        self.assertEqual(self.bank_usd.current_balance, Decimal('0.00'))

        # Demostración del comportamiento actual:
        # C$ 3,620.00 (Debe) - C$ 3,648.00 (Haber) = -C$ 28.00 (distorsión de -C$ 28.00)
        bank_ledger_balance = self._get_bank_ledger_balance_nio()
        self.assertEqual(bank_ledger_balance, Decimal('-28.00'))

        # Ganancia en P&L en código actual: solo C$ 2.00
        line_gain = pay_entry.lines.filter(account=self.accounts['4201-01']).first()
        self.assertEqual(line_gain.credit, Decimal('2.00'))

    def test_audit_case_c_partial_operations_and_valuation_policy_analysis(self):
        """
        Caso C: Operaciones parciales y políticas de valoración (FIFO vs Promedio Ponderado).
        - Depósito 1: US$ 100.00 a tasa 36.00 (C$ 3,600.00)
        - Depósito 2: US$ 100.00 a tasa 36.60 (C$ 3,660.00)
        Total saldo USD: US$ 200.00. Total contable Debe en 1102-01: C$ 7,260.00.

        Salida parcial: Disposición de US$ 50.00 a tasa 36.50.
        """
        inv1 = self._create_inv(Decimal('100.00'), Decimal('36.0000'))
        record_client_collection(
            organization=self.org,
            invoice=inv1,
            amount=Decimal('100.00'),
            currency_paid="USD",
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            user=self.user,
            reference_number="DEP-C1",
            exchange_rate_applied=Decimal('36.0000'),
        )

        inv2 = self._create_inv(Decimal('100.00'), Decimal('36.6000'))
        record_client_collection(
            organization=self.org,
            invoice=inv2,
            amount=Decimal('100.00'),
            currency_paid="USD",
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            user=self.user,
            reference_number="DEP-C2",
            exchange_rate_applied=Decimal('36.6000'),
        )

        self.bank_usd.refresh_from_db()
        self.assertEqual(self.bank_usd.current_balance, Decimal('200.00'))
        self.assertEqual(self._get_bank_ledger_balance_nio(), Decimal('7260.00'))

        # Salida parcial de US$ 50.00 a tasa 36.50
        po = self._create_po(Decimal('50.00'), Decimal('36.5000'))
        record_purchase_order_journal_entry(self.org, po, self.user, accounts=self.accounts)

        payment, _, pay_entry = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('50.00'),
            currency_paid="USD",
            payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            user=self.user,
            reference_number="PAG-C-PARTIAL",
            exchange_rate_applied=Decimal('36.5000'),
        )
        self.assertTrue(pay_entry.is_balanced)

        self.bank_usd.refresh_from_db()
        self.assertEqual(self.bank_usd.current_balance, Decimal('150.00'))

        # Comportamiento actual del sistema:
        # Acredita 1102-01 a 50 * 36.50 = C$ 1,825.00
        current_residual_nio = self._get_bank_ledger_balance_nio()
        self.assertEqual(current_residual_nio, Decimal('5435.00'))

    def test_audit_case_d_client_collection_and_subsequent_disposition(self):
        """
        Caso D: Cobro de factura en USD, diferencial de cobro y posterior disposición.
        - Factura emitida: US$ 200.00 a tasa 36.5000 (CxC = C$ 7,300.00).
        - Cobro: US$ 200.00 a tasa 36.7000 en bank_usd.
        """
        inv = self._create_inv(Decimal('200.00'), Decimal('36.5000'))
        receipt, _, col_entry = record_client_collection(
            organization=self.org,
            invoice=inv,
            amount=Decimal('200.00'),
            currency_paid="USD",
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            user=self.user,
            reference_number="DEP-USD-CASE-D",
            exchange_rate_applied=Decimal('36.7000'),
        )
        self.assertTrue(col_entry.is_balanced)

        line_b = col_entry.lines.get(account=self.accounts['1102-01'])
        self.assertEqual(line_b.debit, Decimal('7340.00'))
        line_g = col_entry.lines.get(account=self.accounts['4201-01'])
        self.assertEqual(line_g.credit, Decimal('40.00'))
        self.assertEqual(self._get_bank_ledger_balance_nio(), Decimal('7340.00'))

    def test_audit_case_e_idempotency_isolation_and_cross_tenant_rejection(self):
        """
        Caso E: Idempotencia, aislamiento multiempresa y rechazo de cuentas ajenas.
        """
        inv = self._create_inv(Decimal('50.00'), Decimal('36.5000'))

        with self.assertRaises(ValidationError) as ctx:
            record_client_collection(
                organization=self.org,
                invoice=inv,
                amount=Decimal('50.00'),
                currency_paid="USD",
                payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
                bank_account=self.bank_usd_b,  # Cuenta de Org B!
                user=self.user,
                reference_number="REF-CROSS-TENANT",
            )
        self.assertIn("cuenta bancaria no pertenece", str(ctx.exception).lower())

        r1, _, _ = record_client_collection(
            organization=self.org,
            invoice=inv,
            amount=Decimal('50.00'),
            currency_paid="USD",
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            user=self.user,
            reference_number="REF-IDEMP-01",
            idempotency_key="KEY-IDEMP-01",
            exchange_rate_applied=Decimal('36.5000'),
        )
        r2, _, _ = record_client_collection(
            organization=self.org,
            invoice=inv,
            amount=Decimal('50.00'),
            currency_paid="USD",
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_usd,
            user=self.user,
            reference_number="REF-IDEMP-01",
            idempotency_key="KEY-IDEMP-01",
            exchange_rate_applied=Decimal('36.5000'),
        )
        self.assertEqual(r1.pk, r2.pk)
        self.assertEqual(FinancialTransaction.objects.filter(reference_code="REF-IDEMP-01").count(), 1)
        self.assertEqual(JournalEntry.objects.filter(source_payment_receipt=r1).count(), 1)
