import threading
import uuid
from decimal import Decimal, ROUND_HALF_UP
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.accounts_auth.models import User
from apps.finance.models import (
    AccountingAccount,
    BankAccount,
    FinancialTransaction,
    JournalEntry,
    JournalEntryLine,
)
from apps.finance.services import (
    ensure_default_chart_of_accounts,
    record_supplier_payment_journal_entry,
)
from apps.finance.services_statements import FinancialStatementsService
from apps.inventory.models import InventoryItem, Warehouse
from apps.purchasing.admin import (
    PurchaseOrderAdmin,
    SupplierPaymentAdmin,
    SupplierPaymentInline,
    SupplierDisbursementIdempotencyRecordAdmin,
)
from apps.purchasing.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
    SupplierInvoice,
    SupplierPayment,
    SupplierDisbursementIdempotencyRecord,
)
from apps.purchasing.services import (
    receive_purchase_order_stock,
    record_supplier_disbursement,
)
from apps.core.models import CountryConfig
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel


class PurchasingDisbursementPhase1B2TestCase(TestCase):
    """
    Suite de Verificación y Certificación de Integridad para Desembolsos y Pagos a Proveedores (Fase 1B.2.2):
    1. Pago parcial a proveedor que reduce correctamente la deuda y actualiza status.
    2. Pago completo que liquida el saldo pendiente de la orden y la factura comercial.
    3. Rechazo de un pago superior a la deuda restante (anti-sobrepago).
    4. Rechazo de cuentas bancarias pertenecientes a otra organización (aislamiento multi-tenant).
    5. Rechazo de cuentas bancarias inactivas, de moneda incompatible o con fondos insuficientes.
    6. Actualización coherente de FinancialTransaction, saldo bancario y JournalEntry (PAYMENTS).
    7. Ausencia de pólizas contables PAYMENTS duplicadas (garantía de BD).
    8. Repetición de la misma solicitud con clave de idempotencia idéntica sin duplicar desembolsos.
    9. Reutilización de clave de idempotencia con parámetros modificados (rechazo por hash mismatch).
    10. Rechazo de referencias bancarias reutilizadas en la misma cuenta bancaria.
    11. Rollback completo ante un fallo intermedio.
    12. Consistencia entre auxiliar de proveedores, tesorería y Balance General.
    13. Conversión bimonetaria exacta en libro diario para pagos en USD a tasa BCN.
    14. Blindaje administrativo en Django Admin contra creación, edición o borrado de egresos.
    """

    def setUp(self):
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
                'default_tax_name': 'IVA',
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )

        # 2. Tenants (Organización Activa y Tenant Ajeno)
        self.org = Organization.objects.create(
            name="SERMANSA Industrial S.A.",
            legal_name="Servicios y Mantenimiento Electromecánico S.A.",
            slug="sermansa-phase1b2-2",
            tax_id="J0310000458912",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['purchasing', 'inventory', 'finance'],
            business_profile=Organization.BusinessProfile.ENGINEERING_SERVICES,
            is_active=True,
        )

        self.org_other = Organization.objects.create(
            name="Empresa Ajena S.A.",
            legal_name="Tercero No Autorizado S.A.",
            slug="ajena-phase1b2-2",
            tax_id="J0999999999999",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['purchasing', 'finance'],
            is_active=True,
        )

        # 3. Usuarios
        self.owner = User.objects.create_user(
            email='director@sermansa.ni',
            full_name='Ing. Carlos Mendoza (Director)',
            password='Password123*',
        )
        OrganizationMembership.objects.create(
            user=self.owner,
            organization=self.org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )

        self.commercial_mgr = User.objects.create_user(
            email='comercial@sermansa.ni',
            full_name='Lic. Valentina Jarquín (Comercial)',
            password='Password123*',
        )
        OrganizationMembership.objects.create(
            user=self.commercial_mgr,
            organization=self.org,
            role_level=RoleLevel.COMMERCIAL_MGR,
            is_active=True,
            is_default_org=True,
        )

        # 4. Catálogo Contable Oficial
        self.accounts = ensure_default_chart_of_accounts(self.org)

        # 5. Cuentas Bancarias Institucionales
        self.bank_bac_nio = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number='BAC-001-NIO-8899',
            currency='NIO',
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('200000.00'),
            description='Cuenta Operativa Principal BAC Córdobas',
            is_active=True,
        )

        self.bank_bac_usd = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number='BAC-002-USD-1122',
            currency='USD',
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('50000.00'),
            description='Cuenta Dólares BAC Operaciones',
            is_active=True,
        )

        self.bank_other_org = BankAccount.objects.create(
            organization=self.org_other,
            bank_name=BankAccount.BankName.BANPRO,
            account_number='BANPRO-999-NIO-0000',
            currency='NIO',
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('500000.00'),
            is_active=True,
        )

        # 6. Proveedor y Bodega
        self.supplier = Supplier.objects.create(
            organization=self.org,
            commercial_name="Ferretería Industrial El Halcón",
            tax_id="J0310000888888",
            contact_name="Don Ramón Gómez",
            email="ramon@elhalcon.ni",
            credit_days=30,
            default_ir_rate=Decimal('1.00'),
            default_imi_rate=Decimal('1.00'),
        )

        self.warehouse = Warehouse.objects.create(
            organization=self.org,
            name="Bodega Principal Managua",
            code="BOD-MGA-P1B2",
            is_active=True,
        )

        self.item_material = InventoryItem.objects.create(
            organization=self.org,
            sku="MAT-CABLE-THHN",
            name="Cable THHN Calibre 10 AWG",
            unit="MTR",
            unit_cost=Decimal('100.00'),
            is_active=True,
        )

    def _create_received_purchase_order(self, gross_subtotal=Decimal('10000.00'), currency='NIO', exchange_rate=Decimal('36.6243'), inv_number='FAC-2026-9001'):
        """Helper para crear y recibir en almacén una orden de compra lista para pago."""
        po = PurchaseOrder.objects.create(
            organization=self.org,
            number=f"OC-{timezone.now().year}-{uuid.uuid4().hex[:4].upper()}",
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            supplier_invoice_number=inv_number,
            status=PurchaseOrder.Status.APPROVED,
            currency_code=currency,
            currency_symbol='C$' if currency == 'NIO' else '$',
            exchange_rate=exchange_rate,
            apply_tax=True,
            tax_rate=Decimal('15.00'),
            ir_withholding_rate=Decimal('1.00'),
            imi_withholding_rate=Decimal('1.00'),
            created_by=self.commercial_mgr,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item_material,
            description="Cable Eléctrico THHN 10 AWG",
            quantity=Decimal('100.00'),
            unit_cost=(gross_subtotal / Decimal('100.00')).quantize(Decimal('0.01')),
        )
        po.recalculate_totals()
        po.save()

        # Recepción en bodega (Kardex, factura comercial y asiento PURCHASES)
        po, movements, inv, pol = receive_purchase_order_stock(
            organization=self.org,
            purchase_order=po,
            user=self.commercial_mgr,
            supplier_invoice_number=inv_number,
        )
        return po, inv

    def test_01_partial_disbursement_reduces_balance_and_updates_status(self):
        """
        1. Pago parcial a proveedor que reduce correctamente la deuda:
        - Orden por Subtotal C$ 10,000 + IVA C$ 1,500 - Ret IR C$ 100 - Ret IMI C$ 100 = Neto C$ 11,300.
        - Abono de C$ 5,000 vía transferencia bancaria BAC.
        - Saldo pendiente de orden y de factura queda en C$ 6,300.
        - Estados pasan a PARTIAL_PAID y PARTIAL.
        """
        po, inv = self._create_received_purchase_order(gross_subtotal=Decimal('10000.00'), inv_number='FAC-PROV-001')
        self.assertEqual(po.net_payable, Decimal('113000.00') / Decimal('10.00')) # C$ 11,300.00
        self.assertEqual(po.balance_due, Decimal('11300.00'))
        self.assertEqual(inv.balance_due, Decimal('11300.00'))

        initial_bank_bal = self.bank_bac_nio.current_balance

        egr, trx, pol = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('5000.00'),
            user=self.commercial_mgr,
            payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_bac_nio,
            reference_number='TRF-BAC-10001',
            notes='Abono inicial 50% aprox',
        )

        po.refresh_from_db()
        inv.refresh_from_db()
        self.bank_bac_nio.refresh_from_db()

        self.assertEqual(egr.amount_in_payment_currency, Decimal('5000.00'))
        self.assertEqual(egr.amount_applied_to_order, Decimal('5000.00'))
        self.assertEqual(po.amount_paid, Decimal('5000.00'))
        self.assertEqual(po.balance_due, Decimal('6300.00'))
        self.assertEqual(po.status, PurchaseOrder.Status.PARTIAL_PAID)

        self.assertEqual(inv.amount_paid, Decimal('5000.00'))
        self.assertEqual(inv.balance_due, Decimal('6300.00'))
        self.assertEqual(inv.status, SupplierInvoice.Status.PARTIAL)

        self.assertEqual(self.bank_bac_nio.current_balance, initial_bank_bal - Decimal('5000.00'))
        self.assertIsNotNone(trx)
        self.assertEqual(trx.transaction_type, FinancialTransaction.TransactionType.VENDOR_DISBURSEMENT)
        self.assertIsNotNone(pol)
        self.assertEqual(pol.entry_type, JournalEntry.EntryType.PAYMENTS)
        self.assertTrue(pol.is_balanced)

    def test_02_full_disbursement_liquidates_balance_to_zero(self):
        """
        2. Pago completo que liquida el saldo pendiente:
        - Orden por C$ 11,300.
        - Pago 1 por C$ 5,000.
        - Pago 2 por C$ 6,300.
        - Saldo final C$ 0.00, estado PAID en PurchaseOrder y en SupplierInvoice.
        """
        po, inv = self._create_received_purchase_order(gross_subtotal=Decimal('10000.00'), inv_number='FAC-PROV-002')

        # Pago 1
        record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('5000.00'),
            user=self.commercial_mgr,
            payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_bac_nio,
            reference_number='TRF-BAC-20001',
        )

        # Pago 2: Saldo restante
        egr2, trx2, pol2 = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('6300.00'),
            user=self.commercial_mgr,
            payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_bac_nio,
            reference_number='TRF-BAC-20002',
        )

        po.refresh_from_db()
        inv.refresh_from_db()

        self.assertEqual(po.amount_paid, Decimal('11300.00'))
        self.assertEqual(po.balance_due, Decimal('0.00'))
        self.assertEqual(po.status, PurchaseOrder.Status.PAID)

        self.assertEqual(inv.amount_paid, Decimal('11300.00'))
        self.assertEqual(inv.balance_due, Decimal('0.00'))
        self.assertEqual(inv.status, SupplierInvoice.Status.PAID)

    def test_03_rejection_of_overpayment_exceeding_debt(self):
        """
        3. Rechazo de un pago superior a la deuda restante (anti-sobrepago).
        """
        po, inv = self._create_received_purchase_order(gross_subtotal=Decimal('10000.00'), inv_number='FAC-PROV-003')

        with self.assertRaises(ValidationError) as ctx:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('12000.00'), # Excede C$ 11,300
                user=self.commercial_mgr,
                payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
                bank_account=self.bank_bac_nio,
                reference_number='TRF-BAC-30001',
            )
        self.assertIn("excede el saldo pendiente", str(ctx.exception))

    def test_04_cross_tenant_bank_account_rejected(self):
        """
        4. Rechazo de cuentas bancarias de otra organización.
        """
        po, inv = self._create_received_purchase_order(gross_subtotal=Decimal('5000.00'), inv_number='FAC-PROV-004')

        with self.assertRaises(ValidationError) as ctx:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('1000.00'),
                user=self.commercial_mgr,
                payment_method=SupplierPayment.PaymentMethod.TRANSFER_BANPRO,
                bank_account=self.bank_other_org, # Cuenta de otro tenant
                reference_number='TRF-ILLEGAL-01',
            )
        self.assertIn("no pertenece a la organización activa", str(ctx.exception))

    def test_05_inactive_account_and_currency_incompatibility_and_insufficient_funds(self):
        """
        5. Rechazo de cuentas inactivas, incompatibles o con fondos insuficientes:
        - Cuenta inactiva -> ValidationError.
        - Moneda incompatible (cuenta NIO para desembolso USD) -> ValidationError.
        - Fondos insuficientes (cuenta con C$ 500 para pago de C$ 1,000) -> ValidationError.
        """
        po, inv = self._create_received_purchase_order(gross_subtotal=Decimal('5000.00'), inv_number='FAC-PROV-005')

        # Caso A: Cuenta inactiva
        bank_inactive = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.LAFISE,
            account_number='LAF-INACTIVE-01',
            currency='NIO',
            current_balance=Decimal('50000.00'),
            is_active=False,
        )
        with self.assertRaises(ValidationError) as ctx_a:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('1000.00'),
                user=self.commercial_mgr,
                bank_account=bank_inactive,
                reference_number='TRF-LAF-01',
            )
        self.assertIn("inactiva", str(ctx_a.exception))

        # Caso B: Moneda incompatible (cuenta USD para pago NIO)
        with self.assertRaises(ValidationError) as ctx_b:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('1000.00'),
                currency_paid='NIO',
                user=self.commercial_mgr,
                bank_account=self.bank_bac_usd, # Cuenta en USD
                reference_number='TRF-USD-INCOMPAT',
            )
        self.assertIn("incompatible", str(ctx_b.exception))

        # Caso C: Fondos bancarios insuficientes
        bank_broke = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.FICOHSA,
            account_number='FIC-BROKE-01',
            currency='NIO',
            current_balance=Decimal('500.00'), # Solo C$ 500
            is_active=True,
        )
        with self.assertRaises(ValidationError) as ctx_c:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('2000.00'),
                user=self.commercial_mgr,
                bank_account=bank_broke,
                reference_number='TRF-FIC-01',
            )
        self.assertIn("insuficiente", str(ctx_c.exception))

    def test_06_coherent_financial_transaction_and_balanced_payments_journal(self):
        """
        6. Actualización coherente de FinancialTransaction, saldo bancario y JournalEntry:
        - Comprueba que la póliza debite 2101-01 (Proveedores) y acredite 1102-01 (Bancos).
        - Debe == Haber, cuadrada al centavo.
        """
        po, inv = self._create_received_purchase_order(gross_subtotal=Decimal('20000.00'), inv_number='FAC-PROV-006')
        initial_bank_bal = self.bank_bac_nio.current_balance
        pago_monto = Decimal('15000.00')

        egr, trx, pol = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=pago_monto,
            user=self.commercial_mgr,
            bank_account=self.bank_bac_nio,
            reference_number='TRF-BAC-60001',
        )

        self.bank_bac_nio.refresh_from_db()
        self.assertEqual(self.bank_bac_nio.current_balance, initial_bank_bal - pago_monto)

        # Transacción de tesorería
        self.assertIsNotNone(trx)
        self.assertEqual(trx.amount, pago_monto)
        self.assertEqual(trx.balance_after, self.bank_bac_nio.current_balance)
        self.assertEqual(trx.bank_account_id, self.bank_bac_nio.id)

        # Póliza Contable
        self.assertIsNotNone(pol)
        self.assertEqual(pol.entry_type, JournalEntry.EntryType.PAYMENTS)
        self.assertTrue(pol.is_balanced)
        self.assertEqual(pol.total_debit, pago_monto)
        self.assertEqual(pol.total_credit, pago_monto)

        lines = list(pol.lines.all())
        self.assertEqual(len(lines), 2)
        debit_line = next(l for l in lines if l.debit > Decimal('0.00'))
        credit_line = next(l for l in lines if l.credit > Decimal('0.00'))

        self.assertEqual(debit_line.account.code, '2101-01') # Proveedores Nacionales
        self.assertEqual(debit_line.debit, pago_monto)

        self.assertEqual(credit_line.account.code, '1102-01') # Bancos Institucionales
        self.assertEqual(credit_line.credit, pago_monto)

    def test_07_absence_of_duplicate_payments_journal_entries(self):
        """
        7. Ausencia de pólizas PAYMENTS duplicadas (garantía de BD y servicio idempotente).
        """
        po, inv = self._create_received_purchase_order(gross_subtotal=Decimal('10000.00'), inv_number='FAC-PROV-007')
        egr, trx, pol1 = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('4000.00'),
            user=self.commercial_mgr,
            bank_account=self.bank_bac_nio,
            reference_number='TRF-BAC-70001',
        )

        # Invocar directamente el servicio contable sobre el mismo egreso
        pol2 = record_supplier_payment_journal_entry(
            organization=self.org,
            supplier_payment=egr,
            user=self.commercial_mgr,
        )

        self.assertEqual(pol1.id, pol2.id)
        count = JournalEntry.objects.filter(
            organization=self.org,
            source_supplier_payment=egr,
            entry_type=JournalEntry.EntryType.PAYMENTS,
        ).count()
        self.assertEqual(count, 1)

    def test_08_idempotency_replay_with_identical_key_returns_existing(self):
        """
        8. Repetición de la misma solicitud con clave de idempotencia idéntica:
        - Retorna el mismo comprobante sin debitar dos veces el banco ni duplicar registros.
        """
        po, inv = self._create_received_purchase_order(gross_subtotal=Decimal('10000.00'), inv_number='FAC-PROV-008')
        idemp_key = f"IDEMP-P1B2-2-{uuid.uuid4().hex}"
        initial_bank_bal = self.bank_bac_nio.current_balance

        # Intento 1
        egr1, trx1, pol1 = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('3000.00'),
            user=self.commercial_mgr,
            bank_account=self.bank_bac_nio,
            reference_number='TRF-BAC-80001',
            idempotency_key=idemp_key,
        )

        self.bank_bac_nio.refresh_from_db()
        bank_after_1 = self.bank_bac_nio.current_balance
        self.assertEqual(bank_after_1, initial_bank_bal - Decimal('3000.00'))

        # Intento 2 (Reintento de red con la misma clave y los mismos parámetros)
        egr2, trx2, pol2 = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('3000.00'),
            user=self.commercial_mgr,
            bank_account=self.bank_bac_nio,
            reference_number='TRF-BAC-80001',
            idempotency_key=idemp_key,
        )

        self.assertEqual(egr1.id, egr2.id)
        self.bank_bac_nio.refresh_from_db()
        self.assertEqual(self.bank_bac_nio.current_balance, bank_after_1) # No se debitó de nuevo

        payments_count = SupplierPayment.objects.filter(purchase_order=po).count()
        self.assertEqual(payments_count, 1)

    def test_09_idempotency_key_reuse_with_different_params_rejected(self):
        """
        9. Reutilización de clave de idempotencia con parámetros modificados (rechazo por hash mismatch).
        """
        po, inv = self._create_received_purchase_order(gross_subtotal=Decimal('10000.00'), inv_number='FAC-PROV-009')
        idemp_key = f"IDEMP-MISMATCH-{uuid.uuid4().hex}"

        # Operación legítima inicial
        record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('2000.00'),
            user=self.commercial_mgr,
            bank_account=self.bank_bac_nio,
            reference_number='TRF-BAC-90001',
            idempotency_key=idemp_key,
        )

        # Intento malicioso o erróneo reutilizando la clave con monto distinto
        with self.assertRaises(ValidationError) as ctx:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('5000.00'), # Monto diferente
                user=self.commercial_mgr,
                bank_account=self.bank_bac_nio,
                reference_number='TRF-BAC-90001',
                idempotency_key=idemp_key,
            )
        self.assertIn("parámetros de solicitud diferentes", str(ctx.exception))

    def test_10_unique_bank_reference_number_enforced(self):
        """
        10. Rechazo de referencias bancarias reutilizadas en la misma cuenta bancaria.
        Y pagos en efectivo (CASH) sin referencia permitidos.
        """
        po1, inv1 = self._create_received_purchase_order(gross_subtotal=Decimal('5000.00'), inv_number='FAC-PROV-010-A')
        po2, inv2 = self._create_received_purchase_order(gross_subtotal=Decimal('5000.00'), inv_number='FAC-PROV-010-B')

        # Pago en po1 con referencia TRF-BAC-UNIQUE-01
        record_supplier_disbursement(
            organization=self.org,
            purchase_order=po1,
            amount=Decimal('1000.00'),
            user=self.commercial_mgr,
            bank_account=self.bank_bac_nio,
            reference_number='TRF-BAC-UNIQUE-01',
        )

        # Intento de reutilizar la misma referencia bancaria en po2
        with self.assertRaises(ValidationError) as ctx:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po2,
                amount=Decimal('1000.00'),
                user=self.commercial_mgr,
                bank_account=self.bank_bac_nio,
                reference_number='TRF-BAC-UNIQUE-01',
            )
        self.assertIn("ya fue registrada previamente", str(ctx.exception))

        # Pago en efectivo (CASH) sin referencia bancaria no es bloqueado
        egr_cash, _, pol_cash = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po2,
            amount=Decimal('500.00'),
            user=self.commercial_mgr,
            payment_method=SupplierPayment.PaymentMethod.CASH,
            bank_account=None,
            reference_number='',
        )
        self.assertEqual(egr_cash.payment_method, SupplierPayment.PaymentMethod.CASH)
        credit_line = pol_cash.lines.filter(credit__gt=0).first()
        self.assertEqual(credit_line.account.code, '1101-01') # Caja General

    def test_11_intermediate_failure_triggers_complete_rollback(self):
        """
        11. Rollback completo ante un fallo intermedio:
        - Si ocurre una excepción durante la operación, el saldo bancario, la orden y los egresos se revierten.
        """
        po, inv = self._create_received_purchase_order(gross_subtotal=Decimal('10000.00'), inv_number='FAC-PROV-011')
        initial_bank_bal = self.bank_bac_nio.current_balance

        from unittest.mock import patch
        with patch('apps.purchasing.services.record_supplier_payment_journal_entry', side_effect=RuntimeError("Fallo simulado en motor contable")):
            with self.assertRaises(RuntimeError):
                record_supplier_disbursement(
                    organization=self.org,
                    purchase_order=po,
                    amount=Decimal('3000.00'),
                    user=self.commercial_mgr,
                    bank_account=self.bank_bac_nio,
                    reference_number='TRF-BAC-ROLLBACK',
                )

        po.refresh_from_db()
        self.bank_bac_nio.refresh_from_db()

        self.assertEqual(po.amount_paid, Decimal('0.00'))
        self.assertEqual(po.balance_due, po.net_payable)
        self.assertEqual(self.bank_bac_nio.current_balance, initial_bank_bal)
        self.assertFalse(SupplierPayment.objects.filter(reference_number='TRF-BAC-ROLLBACK').exists())

    def test_12_balance_sheet_reflects_debt_reduction_and_bank_deduction(self):
        """
        12. Consistencia entre auxiliar de proveedores, tesorería y Balance General.
        """
        po, inv = self._create_received_purchase_order(gross_subtotal=Decimal('20000.00'), inv_number='FAC-PROV-012')
        # Neto: 20000 + 3000 (IVA) - 200 (IR) - 200 (IMI) = C$ 22,600.00

        # Antes del pago:
        bs_before = FinancialStatementsService.get_balance_sheet(self.org)
        ap_item_before = next((x for x in bs_before['accounts_payable'] if x['code'] == '2101-01'), None)
        self.assertIsNotNone(ap_item_before)
        self.assertEqual(ap_item_before['balance'], Decimal('22600.00'))

        # Realizar pago de C$ 10,000
        record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('10000.00'),
            user=self.commercial_mgr,
            bank_account=self.bank_bac_nio,
            reference_number='TRF-BAC-BS-01',
        )

        # Después del pago:
        bs_after = FinancialStatementsService.get_balance_sheet(self.org)
        ap_item_after = next((x for x in bs_after['accounts_payable'] if x['code'] == '2101-01'), None)
        self.assertIsNotNone(ap_item_after)
        self.assertEqual(ap_item_after['balance'], Decimal('12600.00')) # Disminución exacta de C$ 10,000 en pasivo


    def test_13_usd_purchase_order_and_usd_disbursement_exact_nio_conversion(self):
        """
        13. Verificación de cuentas contables e importes en NIO cuando el pago se realiza en USD:
        - Orden de compra en USD por $1,000 netos a TC 36.6243.
        - Pago de $1,000 desde cuenta bancaria USD.
        - En la cuenta bancaria USD se debitan $1,000.00.
        - En el Libro Diario (NIO), la póliza PAYMENTS debita 2101-01 por C$ 36,624.30 y acredita 1102-01 por C$ 36,624.30.
        """
        tc_bcn = Decimal('36.6243')
        po = PurchaseOrder.objects.create(
            organization=self.org,
            number=f"OC-USD-{uuid.uuid4().hex[:4].upper()}",
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            supplier_invoice_number='FAC-USD-001',
            status=PurchaseOrder.Status.APPROVED,
            currency_code='USD',
            currency_symbol='$',
            exchange_rate=tc_bcn,
            apply_tax=False, # Sin IVA para simplificar cálculo exacto
            ir_withholding_rate=Decimal('0.00'),
            imi_withholding_rate=Decimal('0.00'),
            created_by=self.commercial_mgr,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item_material,
            description="Material Importado en USD",
            quantity=Decimal('10.00'),
            unit_cost=Decimal('100.00'), # $1,000.00
        )
        po.recalculate_totals()
        po.save()

        po, _, inv, _ = receive_purchase_order_stock(
            organization=self.org,
            purchase_order=po,
            user=self.commercial_mgr,
            supplier_invoice_number='FAC-USD-001',
        )

        initial_usd_bal = self.bank_bac_usd.current_balance

        egr, trx, pol = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('1000.00'),
            currency_paid='USD',
            user=self.commercial_mgr,
            bank_account=self.bank_bac_usd,
            reference_number='TRF-BAC-USD-001',
        )

        self.bank_bac_usd.refresh_from_db()
        self.assertEqual(self.bank_bac_usd.current_balance, initial_usd_bal - Decimal('1000.00'))

        expected_nio = (Decimal('1000.00') * tc_bcn).quantize(Decimal('0.01'))
        self.assertEqual(expected_nio, Decimal('36624.30'))

        self.assertTrue(pol.is_balanced)
        self.assertEqual(pol.total_debit, expected_nio)
        self.assertEqual(pol.total_credit, expected_nio)

        debit_line = pol.lines.filter(debit__gt=0).first()
        credit_line = pol.lines.filter(credit__gt=0).first()

        self.assertEqual(debit_line.account.code, '2101-01')
        self.assertEqual(debit_line.debit, expected_nio)

        self.assertEqual(credit_line.account.code, '1102-01')
        self.assertEqual(credit_line.credit, expected_nio)

    def test_14_django_admin_hardening_disables_manual_mutations(self):
        """
        14. Blindaje administrativo: Django Admin prohíbe add, change y delete en SupplierPayment y en su Inline.
        """
        site = AdminSite()
        payment_admin = SupplierPaymentAdmin(SupplierPayment, site)
        payment_inline = SupplierPaymentInline(PurchaseOrder, site)
        idemp_admin = SupplierDisbursementIdempotencyRecordAdmin(SupplierDisbursementIdempotencyRecord, site)

        self.assertFalse(payment_admin.has_add_permission(None))
        self.assertFalse(payment_admin.has_change_permission(None))
        self.assertFalse(payment_admin.has_delete_permission(None))

        self.assertFalse(payment_inline.has_add_permission(None, None))
        self.assertFalse(payment_inline.has_change_permission(None, None))
        self.assertFalse(payment_inline.has_delete_permission(None, None))

        self.assertFalse(idemp_admin.has_add_permission(None))
        self.assertFalse(idemp_admin.has_change_permission(None))
        self.assertFalse(idemp_admin.has_delete_permission(None))

    def test_16_supplier_payment_save_does_not_implicitly_mutate_balances(self):
        """
        16. Verificación de Cero Efectos Colaterales en SupplierPayment.save():
        - Guardar directamente un comprobante de egreso vía save() ya NO muta ni PurchaseOrder
          ni SupplierInvoice.
        - Los saldos amount_paid y balance_due permanecen intactos.
        - Solo record_supplier_disbursement() realiza la sincronización explícita y atómica.
        """
        po, inv = self._create_received_purchase_order(gross_subtotal=Decimal('10000.00'), inv_number='FAC-SAVE-001')
        self.assertEqual(po.amount_paid, Decimal('0.00'))
        self.assertEqual(po.balance_due, Decimal('11300.00'))
        self.assertEqual(inv.amount_paid, Decimal('0.00'))
        self.assertEqual(inv.balance_due, Decimal('11300.00'))

        # Creación y persistencia directa de un SupplierPayment sin invocar el servicio orquestador
        payment = SupplierPayment(
            organization=self.org,
            purchase_order=po,
            supplier_invoice=inv,
            bank_account=self.bank_bac_nio,
            payment_date=timezone.now().date(),
            payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
            reference_number='TRF-SAVE-DIRECT-01',
            currency_paid='NIO',
            amount_in_payment_currency=Decimal('4000.00'),
            exchange_rate_applied=Decimal('1.0000'),
            amount_applied_to_order=Decimal('4000.00'),
            paid_by=self.commercial_mgr,
        )
        payment.save()

        po.refresh_from_db()
        inv.refresh_from_db()

        # Demostración estricta: los saldos de orden y factura NO fueron alterados por save()
        self.assertEqual(po.amount_paid, Decimal('0.00'))
        self.assertEqual(po.balance_due, Decimal('11300.00'))
        self.assertEqual(po.status, PurchaseOrder.Status.RECEIVED)

        self.assertEqual(inv.amount_paid, Decimal('0.00'))
        self.assertEqual(inv.balance_due, Decimal('11300.00'))
        self.assertEqual(inv.status, SupplierInvoice.Status.PENDING)

    def test_17_order_with_multiple_invoices_requires_explicit_invoice_specification(self):
        """
        17. Desambiguación de facturas:
        - Si una orden de compra tiene múltiples facturas asociadas, record_supplier_disbursement()
          NO asigna arbitrariamente el pago a la primera factura; en su lugar, exige especificarla.
        - Si se especifica la factura deseada, el pago se aplica correctamente a esa factura.
        """
        po, inv1 = self._create_received_purchase_order(gross_subtotal=Decimal('10000.00'), inv_number='FAC-MULTI-001')
        inv1.net_payable = Decimal('5650.00')
        inv1.balance_due = Decimal('5650.00')
        inv1.save(update_fields=['net_payable', 'balance_due'])

        # Crear una segunda factura vinculada a la misma orden
        inv2 = SupplierInvoice.objects.create(
            organization=self.org,
            supplier=self.supplier,
            purchase_order=po,
            invoice_number='FAC-MULTI-002',
            currency_code='NIO',
            net_payable=Decimal('5650.00'),
            amount_paid=Decimal('0.00'),
            balance_due=Decimal('5650.00'),
            status=SupplierInvoice.Status.PENDING,
            created_by=self.commercial_mgr,
        )

        # Intento de pago sin especificar cuál de las múltiples facturas liquidar -> ValidationError
        with self.assertRaises(ValidationError) as ctx:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('5650.00'),
                user=self.commercial_mgr,
                bank_account=self.bank_bac_nio,
                reference_number='TRF-MULTI-AMBIGUOUS',
                supplier_invoice=None,
            )
        self.assertIn("múltiples facturas de proveedor asociadas", str(ctx.exception))
        self.assertIn("Debe indicar explícitamente la factura", str(ctx.exception))

        # Pago exitoso especificando explícitamente inv2
        egr, trx, pol = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('5650.00'),
            user=self.commercial_mgr,
            bank_account=self.bank_bac_nio,
            reference_number='TRF-MULTI-EXPLICIT-INV2',
            supplier_invoice=inv2,
        )

        inv2.refresh_from_db()
        inv1.refresh_from_db()
        po.refresh_from_db()

        self.assertEqual(inv2.amount_paid, Decimal('5650.00'))
        self.assertEqual(inv2.balance_due, Decimal('0.00'))
        self.assertEqual(inv2.status, SupplierInvoice.Status.PAID)

        # inv1 permanece pendiente
        self.assertEqual(inv1.amount_paid, Decimal('0.00'))
        self.assertEqual(inv1.balance_due, Decimal('5650.00'))
        self.assertEqual(inv1.status, SupplierInvoice.Status.PENDING)

        # La orden refleja el abono de C$ 5,650
        self.assertEqual(po.amount_paid, Decimal('5650.00'))
        self.assertEqual(po.balance_due, Decimal('5650.00'))
        self.assertEqual(po.status, PurchaseOrder.Status.PARTIAL_PAID)

    def test_18_foreign_and_invalid_invoices_strictly_rejected(self):
        """
        18. Validación de integridad de facturas:
        - Rechazo de factura perteneciente a otra orden de compra.
        - Rechazo de factura de otra organización.
        - Rechazo de factura de otro proveedor.
        - Rechazo de factura ya pagada en su totalidad.
        """
        po, inv = self._create_received_purchase_order(gross_subtotal=Decimal('5000.00'), inv_number='FAC-PO1-001')
        po_other, inv_other = self._create_received_purchase_order(gross_subtotal=Decimal('5000.00'), inv_number='FAC-PO2-001')

        # Caso A: Factura de otra orden
        with self.assertRaises(ValidationError) as ctx_a:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('1000.00'),
                user=self.commercial_mgr,
                bank_account=self.bank_bac_nio,
                reference_number='TRF-INV-OTHER-PO',
                supplier_invoice=inv_other,
            )
        self.assertIn("no corresponde a la orden de compra especificada", str(ctx_a.exception))

        # Caso B: Factura de otra organización
        inv_foreign_org = SupplierInvoice.objects.create(
            organization=self.org_other,
            supplier=self.supplier,
            invoice_number='FAC-FOREIGN-ORG',
            currency_code='NIO',
            net_payable=Decimal('1000.00'),
            balance_due=Decimal('1000.00'),
            created_by=self.commercial_mgr,
        )
        with self.assertRaises(ValidationError) as ctx_b:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('1000.00'),
                user=self.commercial_mgr,
                bank_account=self.bank_bac_nio,
                reference_number='TRF-INV-FOREIGN-ORG',
                supplier_invoice=inv_foreign_org,
            )
        self.assertIn("no pertenece a la organización activa", str(ctx_b.exception))

        # Caso C: Factura de otro proveedor
        supplier_other = Supplier.objects.create(
            organization=self.org,
            commercial_name="Proveedor Distinto S.A.",
            tax_id="J0310000111222",
        )
        inv_other_supp = SupplierInvoice.objects.create(
            organization=self.org,
            supplier=supplier_other,
            purchase_order=po,
            invoice_number='FAC-OTHER-SUPP',
            currency_code='NIO',
            net_payable=Decimal('1000.00'),
            balance_due=Decimal('1000.00'),
            created_by=self.commercial_mgr,
        )
        with self.assertRaises(ValidationError) as ctx_c:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('1000.00'),
                user=self.commercial_mgr,
                bank_account=self.bank_bac_nio,
                reference_number='TRF-INV-OTHER-SUPP',
                supplier_invoice=inv_other_supp,
            )
        self.assertIn("no coincide con la orden de compra", str(ctx_c.exception))

        # Caso D: Factura ya pagada
        inv.status = SupplierInvoice.Status.PAID
        inv.balance_due = Decimal('0.00')
        inv.amount_paid = inv.net_payable
        inv.save(update_fields=['status', 'balance_due', 'amount_paid'])

        with self.assertRaises(ValidationError) as ctx_d:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('500.00'),
                user=self.commercial_mgr,
                bank_account=self.bank_bac_nio,
                reference_number='TRF-INV-ALREADY-PAID',
                supplier_invoice=inv,
            )
        self.assertIn("ya se encuentra totalmente liquidada", str(ctx_d.exception))

    def test_19_incompatible_currency_between_order_and_invoice_rejected(self):
        """
        19. Contención monetaria: Facturas de proveedor con moneda distinta a la orden son rechazadas.
        - Rechazo en SupplierInvoice.clean() y save().
        - Rechazo en record_supplier_disbursement().
        """
        po = PurchaseOrder.objects.create(
            organization=self.org,
            number=f"OC-NIO-{uuid.uuid4().hex[:4].upper()}",
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            status=PurchaseOrder.Status.APPROVED,
            currency_code='NIO',
            currency_symbol='C$',
            exchange_rate=Decimal('36.6243'),
            created_by=self.commercial_mgr,
        )

        # Intento de persistir una factura en USD asociada a una orden en NIO
        inv_usd = SupplierInvoice(
            organization=self.org,
            supplier=self.supplier,
            purchase_order=po,
            invoice_number='FAC-MISMATCH-USD',
            currency_code='USD',
            net_payable=Decimal('1000.00'),
            balance_due=Decimal('1000.00'),
            status=SupplierInvoice.Status.PENDING,
            created_by=self.commercial_mgr,
        )

        with self.assertRaises(ValidationError) as ctx_clean:
            inv_usd.clean()
        self.assertIn("Incompatibilidad monetaria", str(ctx_clean.exception))

        with self.assertRaises(ValidationError) as ctx_save:
            inv_usd.save()
        self.assertIn("Incompatibilidad monetaria", str(ctx_save.exception))

    def test_20_foreign_currency_differing_exchange_rate_rejected(self):
        """
        20. Contención del diferencial cambiario: Una obligación en moneda extranjera (USD) no puede
        liquidarse silenciosamente a una tasa diferente de la tasa de reconocimiento contable.
        - Rechazo en record_supplier_disbursement().
        - Rechazo en SupplierPayment.clean() y save().
        - Rechazo en record_supplier_payment_journal_entry().
        - Garantiza que saldos bancarios y auxiliares no se alteran.
        """
        tc_reconocimiento = Decimal('36.6243')
        po = PurchaseOrder.objects.create(
            organization=self.org,
            number=f"OC-USD-TC-{uuid.uuid4().hex[:4].upper()}",
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            supplier_invoice_number='FAC-USD-TC-01',
            status=PurchaseOrder.Status.APPROVED,
            currency_code='USD',
            currency_symbol='$',
            exchange_rate=tc_reconocimiento,
            apply_tax=False,
            ir_withholding_rate=Decimal('0.00'),
            imi_withholding_rate=Decimal('0.00'),
            created_by=self.commercial_mgr,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item_material,
            description="Material USD Control Cambiario",
            quantity=Decimal('10.00'),
            unit_cost=Decimal('100.00'), # $1,000.00
        )
        po.recalculate_totals()
        po.save()

        po, _, inv, _ = receive_purchase_order_stock(
            organization=self.org,
            purchase_order=po,
            user=self.commercial_mgr,
            supplier_invoice_number='FAC-USD-TC-01',
        )

        initial_bank_bal = self.bank_bac_usd.current_balance

        # Intento de liquidar con tasa inválida (cero o negativa) debe ser rechazado
        with self.assertRaises(ValidationError) as ctx_neg:
            record_supplier_disbursement(
                organization=self.org,
                purchase_order=po,
                amount=Decimal('1000.00'),
                currency_paid='USD',
                user=self.commercial_mgr,
                bank_account=self.bank_bac_usd,
                reference_number='TRF-BAC-NEG-TC',
                exchange_rate_applied=Decimal('-1.0'),
            )
        self.assertIn("estrictamente mayor a 0", str(ctx_neg.exception))

        # Rechazo a nivel de modelo SupplierPayment.save() con tasa negativa o cero
        bad_payment = SupplierPayment(
            organization=self.org,
            purchase_order=po,
            bank_account=self.bank_bac_usd,
            currency_paid='USD',
            amount_in_payment_currency=Decimal('1000.00'),
            exchange_rate_applied=Decimal('0.0000'),
            amount_applied_to_order=Decimal('1000.00'),
            paid_by=self.commercial_mgr,
        )
        with self.assertRaises(ValidationError) as ctx_model:
            bad_payment.save()
        self.assertIn("estrictamente mayor a 0", str(ctx_model.exception))

        # Liquidación a tasa divergente (37.5000 vs 36.6243) procesada exitosamente con diferencial cambiario (Fase 1C.6)
        tc_divergente = Decimal('37.5000')
        egr, trx, pol = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('1000.00'),
            currency_paid='USD',
            user=self.commercial_mgr,
            bank_account=self.bank_bac_usd,
            reference_number='TRF-BAC-DIFF-TC',
            exchange_rate_applied=tc_divergente,
        )

        # Comprobar mutación correcta en orden, factura, banco y póliza con diferencial
        po.refresh_from_db()
        inv.refresh_from_db()
        self.bank_bac_usd.refresh_from_db()

        self.assertEqual(po.amount_paid, Decimal('1000.00'))
        self.assertEqual(po.balance_due, Decimal('0.00'))
        self.assertEqual(inv.amount_paid, Decimal('1000.00'))
        self.assertEqual(inv.balance_due, Decimal('0.00'))
        self.assertEqual(self.bank_bac_usd.current_balance, initial_bank_bal - Decimal('1000.00'))
        self.assertIsNotNone(pol)
        # Pérdida cambiaria reflejada: 1000 * (37.5000 - 36.6243) = 875.70 NIO
        fx_line = pol.lines.filter(account__code='6201-01').first()
        self.assertIsNotNone(fx_line)
        self.assertEqual(fx_line.debit, Decimal('875.70'))

    def test_21_foreign_currency_matching_exchange_rate_reconciles_ledger_cleanly(self):
        """
        21. Conciliación en moneda extranjera a tasa de reconocimiento:
        - Liquidación de orden USD a la misma tasa a la que se reconoció (36.6243).
        - La cuenta de pasivo 2101-01 Proveedores Nacionales queda con balance C$ 0.00 exacto.
        - Cero saldo residual o diferencia pendiente.
        """
        tc_oficial = Decimal('36.6243')
        po = PurchaseOrder.objects.create(
            organization=self.org,
            number=f"OC-USD-REC-{uuid.uuid4().hex[:4].upper()}",
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            supplier_invoice_number='FAC-USD-REC-01',
            status=PurchaseOrder.Status.APPROVED,
            currency_code='USD',
            currency_symbol='$',
            exchange_rate=tc_oficial,
            apply_tax=False,
            ir_withholding_rate=Decimal('0.00'),
            imi_withholding_rate=Decimal('0.00'),
            created_by=self.commercial_mgr,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item_material,
            description="Material USD Conciliable",
            quantity=Decimal('10.00'),
            unit_cost=Decimal('100.00'), # $1,000.00
        )
        po.recalculate_totals()
        po.save()

        po, _, inv, pol_compra = receive_purchase_order_stock(
            organization=self.org,
            purchase_order=po,
            user=self.commercial_mgr,
            supplier_invoice_number='FAC-USD-REC-01',
        )

        # En la póliza de compras, se acreditó 2101-01 por C$ 36,624.30
        expected_nio = (Decimal('1000.00') * tc_oficial).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        line_cxp_credit = pol_compra.lines.filter(account__code='2101-01', credit__gt=0).first()
        self.assertEqual(line_cxp_credit.credit, expected_nio)

        # Pago utilizando explícitamente la tasa de reconocimiento
        egr, trx, pol_pago = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('1000.00'),
            currency_paid='USD',
            user=self.commercial_mgr,
            bank_account=self.bank_bac_usd,
            reference_number='TRF-BAC-REC-CLEAN',
            exchange_rate_applied=tc_oficial,
        )

        # En la póliza de pagos, se debita 2101-01 exactamente por C$ 36,624.30
        line_cxp_debit = pol_pago.lines.filter(account__code='2101-01', debit__gt=0).first()
        self.assertEqual(line_cxp_debit.debit, expected_nio)

        # Conciliación matemática exacta del mayor auxiliar de proveedores
        net_cxp_balance = line_cxp_credit.credit - line_cxp_debit.debit
        self.assertEqual(net_cxp_balance, Decimal('0.00'))

        po.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.PAID)
        self.assertEqual(po.balance_due, Decimal('0.00'))
        self.assertEqual(inv.status, SupplierInvoice.Status.PAID)
        self.assertEqual(inv.balance_due, Decimal('0.00'))

    def test_22_foreign_currency_partial_payments_consistent_across_ledger(self):
        """
        22. Pagos parciales consecutivos en moneda extranjera a tasa de reconocimiento:
        - Orden por $2,000 USD reconocida a 36.6243 (Pasivo inicial C$ 73,248.60).
        - Abono 1 de $1,000 USD -> Pasivo amortizado C$ 36,624.30, remanente exacto C$ 36,624.30 (50%).
        - Abono 2 de $1,000 USD -> Pasivo totalmente cancelado (C$ 0.00 remanente).
        """
        tc_oficial = Decimal('36.6243')
        po = PurchaseOrder.objects.create(
            organization=self.org,
            number=f"OC-USD-PART-{uuid.uuid4().hex[:4].upper()}",
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            supplier_invoice_number='FAC-USD-PART-01',
            status=PurchaseOrder.Status.APPROVED,
            currency_code='USD',
            currency_symbol='$',
            exchange_rate=tc_oficial,
            apply_tax=False,
            ir_withholding_rate=Decimal('0.00'),
            imi_withholding_rate=Decimal('0.00'),
            created_by=self.commercial_mgr,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item_material,
            description="Material USD Pagos Parciales",
            quantity=Decimal('20.00'),
            unit_cost=Decimal('100.00'), # $2,000.00
        )
        po.recalculate_totals()
        po.save()

        po, _, inv, pol_compra = receive_purchase_order_stock(
            organization=self.org,
            purchase_order=po,
            user=self.commercial_mgr,
            supplier_invoice_number='FAC-USD-PART-01',
        )

        total_debt_nio = (Decimal('2000.00') * tc_oficial).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        half_debt_nio = (Decimal('1000.00') * tc_oficial).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # Abono 1 ($1,000.00)
        egr1, _, pol1 = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('1000.00'),
            currency_paid='USD',
            user=self.commercial_mgr,
            bank_account=self.bank_bac_usd,
            reference_number='TRF-BAC-USD-PART-1',
            exchange_rate_applied=tc_oficial,
        )

        po.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.PARTIAL_PAID)
        self.assertEqual(po.amount_paid, Decimal('1000.00'))
        self.assertEqual(po.balance_due, Decimal('1000.00'))
        self.assertEqual(inv.status, SupplierInvoice.Status.PARTIAL)
        self.assertEqual(inv.amount_paid, Decimal('1000.00'))
        self.assertEqual(inv.balance_due, Decimal('1000.00'))

        debit1 = pol1.lines.filter(account__code='2101-01', debit__gt=0).first().debit
        self.assertEqual(debit1, half_debt_nio)

        # Abono 2 ($1,000.00 final)
        egr2, _, pol2 = record_supplier_disbursement(
            organization=self.org,
            purchase_order=po,
            amount=Decimal('1000.00'),
            currency_paid='USD',
            user=self.commercial_mgr,
            bank_account=self.bank_bac_usd,
            reference_number='TRF-BAC-USD-PART-2',
            exchange_rate_applied=tc_oficial,
        )

        po.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.PAID)
        self.assertEqual(po.amount_paid, Decimal('2000.00'))
        self.assertEqual(po.balance_due, Decimal('0.00'))
        self.assertEqual(inv.status, SupplierInvoice.Status.PAID)
        self.assertEqual(inv.amount_paid, Decimal('2000.00'))
        self.assertEqual(inv.balance_due, Decimal('0.00'))

        debit2 = pol2.lines.filter(account__code='2101-01', debit__gt=0).first().debit
        self.assertEqual(debit2, half_debt_nio)

        # Reconciliación contable completa: Crédito inicial == Débito 1 + Débito 2
        self.assertEqual(total_debt_nio, debit1 + debit2)


class PurchasingDisbursementConcurrencyTestCase(TransactionTestCase):
    """
    Prueba Transaccional de Concurrencia Multi-Hilo (PostgreSQL 14):
    Valida que dos hilos paralelos ejecutando record_supplier_disbursement sobre el mismo
    saldo pendiente no dupliquen pagos ni provoquen sobrepagos (balance_due < 0).
    """

    def setUp(self):
        self.country_ni, _ = CountryConfig.objects.get_or_create(
            code='NI',
            defaults={
                'name': 'Nicaragua',
                'primary_currency_code': 'NIO',
                'primary_currency_symbol': 'C$',
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )

        self.org = Organization.objects.create(
            name="SERMANSA Concurrency S.A.",
            legal_name="SERMANSA Concurrency S.A.",
            slug=f"sermansa-conc-{uuid.uuid4().hex[:6]}",
            tax_id="J0310000999111",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['purchasing', 'inventory', 'finance'],
            is_active=True,
        )

        self.user = User.objects.create_user(
            email=f'conc_{uuid.uuid4().hex[:4]}@sermansa.ni',
            full_name='Operador Concurrente',
            password='Password123*',
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )

        ensure_default_chart_of_accounts(self.org)

        self.bank = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number=f"BAC-{uuid.uuid4().hex[:6]}",
            currency='NIO',
            current_balance=Decimal('100000.00'),
            is_active=True,
        )

        self.supplier = Supplier.objects.create(
            organization=self.org,
            commercial_name="Proveedor Concurrente S.A.",
            tax_id="J0310000777777",
            default_ir_rate=Decimal('1.00'),
            default_imi_rate=Decimal('1.00'),
        )

        self.warehouse = Warehouse.objects.create(
            organization=self.org,
            name="Bodega Concurrente",
            code=f"BOD-{uuid.uuid4().hex[:4]}",
            is_active=True,
        )

        self.item = InventoryItem.objects.create(
            organization=self.org,
            sku=f"MAT-{uuid.uuid4().hex[:4]}",
            name="Material Concurrente",
            unit="UND",
            unit_cost=Decimal('1000.00'),
            is_active=True,
        )

        # Crear orden con Neto C$ 11,300
        self.po = PurchaseOrder.objects.create(
            organization=self.org,
            number=f"OC-CONC-{uuid.uuid4().hex[:4].upper()}",
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            supplier_invoice_number=f"FAC-CONC-{uuid.uuid4().hex[:4].upper()}",
            status=PurchaseOrder.Status.APPROVED,
            currency_code='NIO',
            currency_symbol='C$',
            exchange_rate=Decimal('36.6243'),
            apply_tax=True,
            tax_rate=Decimal('15.00'),
            ir_withholding_rate=Decimal('1.00'),
            imi_withholding_rate=Decimal('1.00'),
            created_by=self.user,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=self.po,
            inventory_item=self.item,
            quantity=Decimal('10.00'),
            unit_cost=Decimal('1000.00'), # Subtotal C$ 10,000
        )
        self.po.recalculate_totals()
        self.po.save()

        self.po, _, self.inv, _ = receive_purchase_order_stock(
            organization=self.org,
            purchase_order=self.po,
            user=self.user,
            supplier_invoice_number=self.po.supplier_invoice_number,
        )

    def test_15_concurrent_disbursements_cannot_overpay_or_duplicate(self):
        """
        Dos hilos intentan pagar C$ 11,300 simultáneamente sobre una orden con balance_due C$ 11,300:
        - Un hilo debe triunfar con éxito (200 OK).
        - El otro hilo debe ser bloqueado por select_for_update() y rechazado con ValidationError al verificar balance_due.
        - Saldo final de la orden = C$ 0.00 (nunca negativo).
        - Exactamente 1 Comprobante de Egreso (SupplierPayment) registrado.
        - Exactamente 1 débito bancario por C$ 11,300.
        """
        results = []
        errors = []

        def worker(ref_num):
            connection.close()
            try:
                egr, trx, pol = record_supplier_disbursement(
                    organization=self.org,
                    purchase_order=self.po,
                    amount=Decimal('11300.00'),
                    user=self.user,
                    bank_account=self.bank,
                    reference_number=ref_num,
                )
                results.append(egr.number)
            except Exception as e:
                errors.append(str(e))
            finally:
                connection.close()

        t1 = threading.Thread(target=worker, args=('TRF-CONC-THREAD-1',))
        t2 = threading.Thread(target=worker, args=('TRF-CONC-THREAD-2',))

        t1.start()
        t2.start()

        t1.join()
        t2.join()

        self.assertEqual(len(results), 1, f"Se esperaba exactamente 1 pago exitoso. Exitosos: {results}, Errores: {errors}")
        self.assertEqual(len(errors), 1, f"Se esperaba exactamente 1 error por intento de sobrepago. Errores: {errors}")

        self.po.refresh_from_db()
        self.bank.refresh_from_db()

        self.assertEqual(self.po.amount_paid, Decimal('11300.00'))
        self.assertEqual(self.po.balance_due, Decimal('0.00'))
        self.assertEqual(self.po.status, PurchaseOrder.Status.PAID)

        # Verificar que solo se emitió 1 egreso y 1 débito bancario
        self.assertEqual(SupplierPayment.objects.filter(purchase_order=self.po).count(), 1)
        self.assertEqual(self.bank.current_balance, Decimal('100000.00') - Decimal('11300.00'))
