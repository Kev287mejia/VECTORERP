import datetime
from decimal import Decimal
import json
import threading
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
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
    FinancialTransaction,
    JournalEntry,
    JournalEntryLine,
)
from apps.finance.services import (
    ensure_default_chart_of_accounts,
    record_client_collection,
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


class PosFinancialIntegrityTestCase(TestCase):
    """
    Suite de Verificación de Integridad Financiera Integral del Punto de Venta (Fase 1C.4):
    1. Venta POS en efectivo con ciclo contable canónico (SALES y COLLECTIONS cuadradas).
    2. Venta POS bancaria con actualización de saldo y FinancialTransaction.
    3. Idempotencia: replay idéntico seguro y sin duplicación de Kardex ni pólizas.
    4. Idempotencia: rechazo de solicitudes divergentes con misma clave.
    5. Control de existencias contra inventario negativo en artículos sin lote.
    6. Validación sanitaria FEFO y bloqueo de lotes MINSA vencidos.
    7. Rollback transaccional atómico ante fallo intermedio.
    8. Aislamiento multi-tenant en bodega, cliente, banco y producto.
    9. Unicidad de referencias bancarias.
    10. Endpoint PointOfSaleTerminalView con soporte de idempotencia y bancos.
    11. Cero regresión en record_client_collection.
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
            name="Ferretería El Tornillo Dorado S.A.",
            slug="tornillo-dorado-pos",
            country=self.country_ni,
            currency='NIO',
            enabled_modules=['crm', 'inventory', 'finance', 'billing'],
            is_active=True,
        )

        # Organización B (Aislamiento Tenant)
        self.org_b = Organization.objects.create(
            name="Farmacia Santa Lucía S.A.",
            slug="santa-lucia-pos",
            country=self.country_ni,
            currency='NIO',
            enabled_modules=['crm', 'inventory', 'finance', 'billing'],
            is_active=True,
        )

        # Usuarios
        self.user_a = User.objects.create_user(
            email='cajero_a@tornillo.ni',
            password='TestPassword2026*',
            full_name='Cajero Principal A',
        )
        self.user_b = User.objects.create_user(
            email='cajero_b@santalucia.ni',
            password='TestPassword2026*',
            full_name='Cajero Principal B',
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
            name='Bodega Mostrador Principal A',
            is_active=True,
        )
        self.warehouse_b = Warehouse.objects.create(
            organization=self.org_b,
            name='Bodega Mostrador B',
            is_active=True,
        )

        # Clientes
        self.client_a = Account.objects.create(
            organization=self.org_a,
            commercial_name='Constructora El Ceibo S.A.',
            tax_id='J0310000001234',
            account_type=Account.AccountType.CORPORATE,
        )
        self.client_b = Account.objects.create(
            organization=self.org_b,
            commercial_name='Cliente Tenant B',
            tax_id='J0310000009999',
            account_type=Account.AccountType.INDIVIDUAL,
        )

        # Cuentas bancarias
        self.bank_a = BankAccount.objects.create(
            organization=self.org_a,
            bank_name='BAC Credomatic',
            account_number='BAC-NIO-POS-01',
            account_type=BankAccount.AccountType.CHECKING,
            currency='NIO',
            current_balance=Decimal('50000.00'),
            is_active=True,
        )
        self.bank_b = BankAccount.objects.create(
            organization=self.org_b,
            bank_name='BANPRO',
            account_number='BANPRO-NIO-POS-02',
            account_type=BankAccount.AccountType.CHECKING,
            currency='NIO',
            current_balance=Decimal('20000.00'),
            is_active=True,
        )

        # Catálogos contables
        self.accounts_a = ensure_default_chart_of_accounts(self.org_a)
        self.accounts_b = ensure_default_chart_of_accounts(self.org_b)

        # Productos Org A
        self.item_hardware = InventoryItem.objects.create(
            organization=self.org_a,
            name='Taladro Percutor 1/2 650W',
            sku='TAL-650W-001',
            unit='UN',
            unit_cost=Decimal('800.00'),
            price_retail=Decimal('1200.00'),
            price_contractor=Decimal('1050.00'),
            price_wholesale=Decimal('950.00'),
            wholesale_min_qty=Decimal('5.00'),
            requires_batch_control=False,
            is_active=True,
        )
        self.stock_hardware = StockBalance.objects.create(
            organization=self.org_a,
            warehouse=self.warehouse_a,
            item=self.item_hardware,
            quantity=Decimal('20.00'),
        )

        # Producto Farmacéutico con lotes FEFO
        self.item_pharma = InventoryItem.objects.create(
            organization=self.org_a,
            name='Amoxicilina 500mg Cápsulas',
            sku='MED-AMOX-500',
            unit='CJA',
            unit_cost=Decimal('50.00'),
            price_retail=Decimal('100.00'),
            requires_batch_control=True,
            is_active=True,
        )
        StockBalance.objects.create(
            organization=self.org_a,
            warehouse=self.warehouse_a,
            item=self.item_pharma,
            quantity=Decimal('50.00'),
        )
        today = timezone.now().date()
        self.batch_valid = InventoryBatch.objects.create(
            organization=self.org_a,
            warehouse=self.warehouse_a,
            item=self.item_pharma,
            batch_number='LOTE-VAL-2026',
            manufacturing_date=today - datetime.timedelta(days=30),
            expiration_date=today + datetime.timedelta(days=180),
            quantity_available=Decimal('50.00'),
        )

        # Lote caducado
        self.item_expired = InventoryItem.objects.create(
            organization=self.org_a,
            name='Jarabe Vencido 120ml',
            sku='MED-JAR-VENC',
            unit='FCO',
            unit_cost=Decimal('30.00'),
            price_retail=Decimal('60.00'),
            requires_batch_control=True,
            is_active=True,
        )
        StockBalance.objects.create(
            organization=self.org_a,
            warehouse=self.warehouse_a,
            item=self.item_expired,
            quantity=Decimal('10.00'),
        )
        self.batch_expired = InventoryBatch.objects.create(
            organization=self.org_a,
            warehouse=self.warehouse_a,
            item=self.item_expired,
            batch_number='LOTE-EXPIRED-99',
            manufacturing_date=today - datetime.timedelta(days=400),
            expiration_date=today - datetime.timedelta(days=10),
            quantity_available=Decimal('10.00'),
        )

        # Producto Org B
        self.item_b = InventoryItem.objects.create(
            organization=self.org_b,
            name='Producto Exclusivo Org B',
            sku='ITEM-ORG-B-001',
            unit='UN',
            unit_cost=Decimal('100.00'),
            price_retail=Decimal('200.00'),
            is_active=True,
        )
        StockBalance.objects.create(
            organization=self.org_b,
            warehouse=self.warehouse_b,
            item=self.item_b,
            quantity=Decimal('15.00'),
        )

        # Turno POS abierto
        self.shift_a = open_pos_shift(
            organization=self.org_a,
            cashier=self.user_a,
            opening_cash_nio=Decimal('2000.00'),
            register_name='CAJA-MOSTRADOR-01'
        )

    def test_01_successful_pos_cash_sale_full_cycle(self):
        """
        Venta POS exitosa en efectivo:
        - Factura DGI emitida y pagada en su totalidad.
        - Saldo pendiente en 0.00.
        - Recibo Oficial de Caja (ROC) generado.
        - Descuento de existencias en StockBalance y Kardex OUT_SALE_POS inmutable.
        - Póliza de Venta (SALES) cuadrada: Débito Clientes (1103-01) vs Crédito Ventas (4101-01) e IVA (2102-01).
        - Póliza de Cobro (COLLECTIONS) cuadrada: Débito Caja (1101-01) vs Crédito Clientes (1103-01).
        - Saldo neto de la factura en Clientes por Cobrar (1103-01) igual a 0.00.
        """
        # 2 taladros a C$ 1,200.00 c/u = C$ 2,400.00 + IVA 15% (360.00) = C$ 2,760.00
        inv = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{
                'item_id': str(self.item_hardware.id),
                'quantity': Decimal('2.00'),
                'price_tier': 'RETAIL',
            }],
            account=self.client_a,
            payment_method=PaymentReceipt.PaymentMethod.CASH,
            cash_received=Decimal('3000.00'),
            notes='Venta mostrador contado efectivo'
        )

        self.assertEqual(inv.status, Invoice.Status.PAID)
        self.assertEqual(inv.subtotal, Decimal('2400.00'))
        self.assertEqual(inv.tax_total, Decimal('360.00'))
        self.assertEqual(inv.net_receivable, Decimal('2760.00'))
        self.assertEqual(inv.amount_paid, Decimal('2760.00'))
        self.assertEqual(inv.balance_due, Decimal('0.00'))
        self.assertEqual(inv.pos_shift_id, self.shift_a.id)

        # Existencia en StockBalance: 20 - 2 = 18
        self.stock_hardware.refresh_from_db()
        self.assertEqual(self.stock_hardware.quantity, Decimal('18.00'))

        # Kardex
        kardex = StockMovement.objects.filter(invoice=inv).first()
        self.assertIsNotNone(kardex)
        self.assertEqual(kardex.movement_type, StockMovement.MovementType.OUT_SALE_POS)
        self.assertEqual(kardex.quantity, Decimal('2.00'))
        self.assertEqual(kardex.balance_after, Decimal('18.00'))

        # Recibo ROC
        receipt = inv.payments.first()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.payment_method, PaymentReceipt.PaymentMethod.CASH)
        self.assertEqual(receipt.amount_in_payment_currency, Decimal('2760.00'))
        self.assertEqual(receipt.amount_applied_to_invoice, Decimal('2760.00'))

        # Póliza SALES
        sales_entry = inv.journal_entries.filter(entry_type=JournalEntry.EntryType.SALES).first()
        self.assertIsNotNone(sales_entry)
        self.assertTrue(sales_entry.is_balanced)
        lines_sales = {l.account.code: (l.debit, l.credit) for l in sales_entry.lines.all()}
        self.assertEqual(lines_sales['1103-01'], (Decimal('2760.00'), Decimal('0.00')))
        self.assertEqual(lines_sales['4101-01'], (Decimal('0.00'), Decimal('2400.00')))
        self.assertEqual(lines_sales['2102-01'], (Decimal('0.00'), Decimal('360.00')))

        # Póliza COLLECTIONS
        col_entry = JournalEntry.objects.filter(
            source_payment_receipt=receipt,
            entry_type=JournalEntry.EntryType.COLLECTIONS,
        ).first()
        self.assertIsNotNone(col_entry)
        self.assertTrue(col_entry.is_balanced)
        lines_col = {l.account.code: (l.debit, l.credit) for l in col_entry.lines.all()}
        self.assertEqual(lines_col['1101-01'], (Decimal('2760.00'), Decimal('0.00')))  # Débito Caja
        self.assertEqual(lines_col['1103-01'], (Decimal('0.00'), Decimal('2760.00')))  # Crédito Clientes

    def test_02_successful_pos_bank_transfer_sale_treasury_update(self):
        """
        Venta POS con transferencia bancaria:
        - Actualiza el saldo en BankAccount (current_balance += monto).
        - Genera FinancialTransaction con código de referencia del banco.
        - Póliza COLLECTIONS debita a Bancos Institucionales (1102-01) en lugar de Caja.
        """
        initial_balance = self.bank_a.current_balance
        ref_code = 'BAC-TRF-POS-9876'

        inv = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{
                'item_id': str(self.item_hardware.id),
                'quantity': Decimal('1.00'),
                'price_tier': 'RETAIL',
            }],
            account=self.client_a,
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_a,
            reference_number=ref_code,
            notes='Venta mostrador cancelada vía transferencia BAC'
        )

        expected_total = Decimal('1200.00') * Decimal('1.15')  # 1,380.00
        self.assertEqual(inv.net_receivable, expected_total)

        # Saldo bancario incrementado
        self.bank_a.refresh_from_db()
        self.assertEqual(self.bank_a.current_balance, initial_balance + expected_total)

        # Transacción de tesorería
        trx = FinancialTransaction.objects.filter(reference_code=ref_code).first()
        self.assertIsNotNone(trx)
        self.assertEqual(trx.amount, expected_total)
        self.assertEqual(trx.bank_account, self.bank_a)
        self.assertEqual(trx.transaction_type, FinancialTransaction.TransactionType.CLIENT_COLLECTION)

        # Póliza COLLECTIONS debita Bancos 1102-01
        receipt = inv.payments.first()
        col_entry = JournalEntry.objects.filter(source_payment_receipt=receipt).first()
        lines = {l.account.code: (l.debit, l.credit) for l in col_entry.lines.all()}
        self.assertEqual(lines['1102-01'], (expected_total, Decimal('0.00')))
        self.assertEqual(lines['1103-01'], (Decimal('0.00'), expected_total))

    def test_03_idempotency_identical_replay_returns_same_invoice(self):
        """
        Idempotencia estricta:
        - Una segunda llamada con la misma clave de idempotencia e idénticos parámetros
          retorna la factura ya emitida.
        - NO descuenta inventario dos veces.
        - NO genera recibos duplicados ni pólizas contables adicionales.
        """
        idem_key = 'IDEMP-TEST-POS-001'
        inv1 = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{
                'item_id': str(self.item_hardware.id),
                'quantity': Decimal('2.00'),
                'price_tier': 'RETAIL',
            }],
            account=self.client_a,
            payment_method=PaymentReceipt.PaymentMethod.CASH,
            idempotency_key=idem_key,
        )

        # Reintento idéntico
        inv2 = execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{
                'item_id': str(self.item_hardware.id),
                'quantity': Decimal('2.00'),
                'price_tier': 'RETAIL',
            }],
            account=self.client_a,
            payment_method=PaymentReceipt.PaymentMethod.CASH,
            idempotency_key=idem_key,
        )

        self.assertEqual(inv1.pk, inv2.pk)
        self.assertEqual(inv1.number, inv2.number)

        # Stock solo descontado una vez: 20 - 2 = 18 (NO 16)
        self.stock_hardware.refresh_from_db()
        self.assertEqual(self.stock_hardware.quantity, Decimal('18.00'))

        # Exactamente un recibo emitido
        self.assertEqual(PaymentReceipt.objects.filter(invoice=inv1).count(), 1)

        # Exactamente un asiento de ventas y uno de cobro
        self.assertEqual(JournalEntry.objects.filter(source_invoice=inv1, entry_type=JournalEntry.EntryType.SALES).count(), 1)
        receipt = inv1.payments.first()
        self.assertEqual(JournalEntry.objects.filter(source_payment_receipt=receipt).count(), 1)

        # Un solo registro de idempotencia registrado
        self.assertEqual(PaymentIdempotencyRecord.objects.filter(organization=self.org_a, idempotency_key=idem_key).count(), 1)

    def test_04_idempotency_divergent_payload_rejected(self):
        """
        Idempotencia: detección de colisión de clave con datos divergentes:
        - Si se reintenta una clave de idempotencia con diferente cantidad o producto,
          se rechaza tajantemente con ValidationError.
        """
        idem_key = 'IDEMP-TEST-POS-002'
        execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{
                'item_id': str(self.item_hardware.id),
                'quantity': Decimal('1.00'),
            }],
            idempotency_key=idem_key,
        )

        # Reintento con cantidad divergente (3 en lugar de 1)
        with self.assertRaises(ValidationError) as ctx:
            execute_pos_sale(
                organization=self.org_a,
                user=self.user_a,
                warehouse=self.warehouse_a,
                cart_items=[{
                    'item_id': str(self.item_hardware.id),
                    'quantity': Decimal('3.00'),
                }],
                idempotency_key=idem_key,
            )
        self.assertIn("utilizada con parámetros de solicitud diferentes", str(ctx.exception))

    def test_05_insufficient_stock_non_batch_item_rejected_no_negative_stock(self):
        """
        Protección inquebrantable contra inventario negativo en mostrador:
        - Solicitar más existencias de las disponibles en un producto sin lote
          lanza ValidationError y NO permite que el saldo caiga a negativo.
        """
        # Hay 20 disponibles, solicitamos 25
        with self.assertRaises(ValidationError) as ctx:
            execute_pos_sale(
                organization=self.org_a,
                user=self.user_a,
                warehouse=self.warehouse_a,
                cart_items=[{
                    'item_id': str(self.item_hardware.id),
                    'quantity': Decimal('25.00'),
                }],
            )
        self.assertIn("Existencia insuficiente para el producto", str(ctx.exception))

        # Saldo inalterado en 20.00
        self.stock_hardware.refresh_from_db()
        self.assertEqual(self.stock_hardware.quantity, Decimal('20.00'))

        # Cero facturas y cero recibos creados
        self.assertEqual(Invoice.objects.filter(organization=self.org_a).count(), 0)
        self.assertEqual(PaymentReceipt.objects.filter(organization=self.org_a).count(), 0)

    def test_06_invalid_quantities_rejected(self):
        """Cantidades menores o iguales a cero son rechazadas con ValidationError."""
        for qty in [Decimal('0.00'), Decimal('-2.00')]:
            with self.assertRaises(ValidationError):
                execute_pos_sale(
                    organization=self.org_a,
                    user=self.user_a,
                    warehouse=self.warehouse_a,
                    cart_items=[{
                        'item_id': str(self.item_hardware.id),
                        'quantity': qty,
                    }],
                )

    def test_07_expired_batch_rejected_sanitary_minsa(self):
        """Lotes vencidos son bloqueados automáticamente por normativa sanitaria MINSA."""
        with self.assertRaises(ValidationError) as ctx:
            execute_pos_sale(
                organization=self.org_a,
                user=self.user_a,
                warehouse=self.warehouse_a,
                cart_items=[{
                    'item_id': str(self.item_expired.id),
                    'quantity': Decimal('1.00'),
                    'batch_id': str(self.batch_expired.id),
                }],
            )
        self.assertIn("está vencido", str(ctx.exception))

        # Existencia del lote caducado inalterada
        self.batch_expired.refresh_from_db()
        self.assertEqual(self.batch_expired.quantity_available, Decimal('10.00'))

    def test_08_atomic_rollback_on_intermediate_failure(self):
        """
        Atomicidad transaccional:
        - Si ocurre una falla en cualquier punto de la venta POS (ej: error en contabilidad),
          se produce un rollback completo: no queda factura, ni recibo, ni movimiento Kardex,
          y las existencias en StockBalance permanecen intactas.
        """
        from unittest.mock import patch

        initial_stock = self.stock_hardware.quantity

        with patch('apps.billing.services.record_invoice_sales_journal_entry', side_effect=RuntimeError("Simulated accounting database outage")):
            with self.assertRaises(RuntimeError):
                execute_pos_sale(
                    organization=self.org_a,
                    user=self.user_a,
                    warehouse=self.warehouse_a,
                    cart_items=[{
                        'item_id': str(self.item_hardware.id),
                        'quantity': Decimal('3.00'),
                    }],
                )

        # Rollback verificado
        self.stock_hardware.refresh_from_db()
        self.assertEqual(self.stock_hardware.quantity, initial_stock)
        self.assertEqual(Invoice.objects.filter(organization=self.org_a).count(), 0)
        self.assertEqual(PaymentReceipt.objects.filter(organization=self.org_a).count(), 0)
        self.assertEqual(StockMovement.objects.filter(organization=self.org_a).count(), 0)
        self.assertEqual(JournalEntry.objects.filter(organization=self.org_a).count(), 0)

    def test_09_multi_tenant_isolation_foreign_objects_rejected(self):
        """
        Aislamiento multi-tenant estricto:
        - Bodega de otra organización -> Rechazado.
        - Cliente de otra organización -> Rechazado.
        - Producto de otra organización -> Rechazado.
        - Cuenta bancaria de otra organización -> Rechazado.
        """
        # 1. Bodega ajena
        with self.assertRaises(ValidationError) as ctx:
            execute_pos_sale(
                organization=self.org_a,
                user=self.user_a,
                warehouse=self.warehouse_b,
                cart_items=[{'item_id': str(self.item_hardware.id), 'quantity': Decimal('1.00')}],
            )
        self.assertIn("no pertenece a la organización", str(ctx.exception))

        # 2. Cliente ajeno
        with self.assertRaises(ValidationError) as ctx:
            execute_pos_sale(
                organization=self.org_a,
                user=self.user_a,
                warehouse=self.warehouse_a,
                account=self.client_b,
                cart_items=[{'item_id': str(self.item_hardware.id), 'quantity': Decimal('1.00')}],
            )
        self.assertIn("no pertenece a la organización", str(ctx.exception))

        # 3. Producto ajeno
        with self.assertRaises(ValidationError) as ctx:
            execute_pos_sale(
                organization=self.org_a,
                user=self.user_a,
                warehouse=self.warehouse_a,
                cart_items=[{'item_id': str(self.item_b.id), 'quantity': Decimal('1.00')}],
            )
        self.assertIn("no pertenece a la empresa", str(ctx.exception))

        # 4. Cuenta bancaria ajena
        with self.assertRaises(ValidationError) as ctx:
            execute_pos_sale(
                organization=self.org_a,
                user=self.user_a,
                warehouse=self.warehouse_a,
                bank_account=self.bank_b,
                payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
                cart_items=[{'item_id': str(self.item_hardware.id), 'quantity': Decimal('1.00')}],
            )
        self.assertIn("no pertenece a la organización", str(ctx.exception))

    def test_10_duplicate_bank_reference_in_same_account_rejected(self):
        """
        Unicidad de referencias bancarias por cuenta:
        - Dos ventas POS no pueden reutilizar el mismo número de referencia en la misma cuenta bancaria.
        """
        ref = 'TRF-UNIQUE-POS-2026'
        execute_pos_sale(
            organization=self.org_a,
            user=self.user_a,
            warehouse=self.warehouse_a,
            cart_items=[{'item_id': str(self.item_hardware.id), 'quantity': Decimal('1.00')}],
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_a,
            reference_number=ref,
            idempotency_key='IDEMP-POS-REF-1',
        )

        with self.assertRaises(ValidationError) as ctx:
            execute_pos_sale(
                organization=self.org_a,
                user=self.user_a,
                warehouse=self.warehouse_a,
                cart_items=[{'item_id': str(self.item_hardware.id), 'quantity': Decimal('1.00')}],
                payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
                bank_account=self.bank_a,
                reference_number=ref,
                idempotency_key='IDEMP-POS-REF-2',
            )
        self.assertIn("ya fue utilizada en esta cuenta bancaria", str(ctx.exception))

    def test_11_view_pos_terminal_post_creates_sale_and_redirects(self):
        """
        Verificación de integración en vista web PointOfSaleTerminalView.post:
        - Acepta payload JSON de partidas, idempotency_key y método de pago.
        - Redirige a invoice_detail con mensaje de éxito.
        - Factura e idempotencia creadas correctamente.
        """
        client = Client()
        client.force_login(self.user_a)
        session = client.session
        session['active_org_id'] = str(self.org_a.id)
        session.save()

        payload = [{
            'item_id': str(self.item_hardware.id),
            'quantity': 2,
            'price_tier': 'RETAIL',
        }]

        url = reverse('billing:pos_terminal')
        response = client.post(url, {
            'warehouse_id': str(self.warehouse_a.id),
            'payment_method': PaymentReceipt.PaymentMethod.CASH,
            'items_payload': json.dumps(payload),
            'idempotency_key': 'WEB-POS-IDEMP-999',
            'notes': 'Venta desde consola web POS',
        })

        self.assertEqual(response.status_code, 302)
        inv = Invoice.objects.filter(organization=self.org_a, notes='Venta desde consola web POS').first()
        self.assertIsNotNone(inv)
        self.assertEqual(inv.status, Invoice.Status.PAID)
        self.assertEqual(inv.amount_paid, inv.net_receivable)
        self.assertTrue(response.url.endswith(f"/billing/invoices/{inv.pk}/"))

    def test_12_regression_record_client_collection_intact(self):
        """
        Prueba de no-regresión:
        - Verifica que record_client_collection (Fase 1B.1) continúa funcionando
          impecablemente para ventas a crédito cobradas posteriormente.
        """
        credit_inv = Invoice.objects.create(
            organization=self.org_a,
            account=self.client_a,
            payment_condition=Invoice.PaymentCondition.CREDIT_30,
            status=Invoice.Status.ISSUED,
            currency_code='NIO',
            currency_symbol='C$',
            apply_tax=True,
            tax_rate=Decimal('15.00'),
            created_by=self.user_a,
        )
        InvoiceItem.objects.create(
            invoice=credit_inv,
            item_type='PRODUCT',
            code='TAL-01',
            description='Taladro',
            unit='UN',
            quantity=Decimal('1.00'),
            unit_price=Decimal('1000.00'),
            discount_percent=Decimal('0.00'),
        )
        credit_inv.recalculate_totals()
        credit_inv.save()

        receipt, trx, col_entry = record_client_collection(
            organization=self.org_a,
            invoice=credit_inv,
            amount=credit_inv.net_receivable,
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            user=self.user_a,
            bank_account=self.bank_a,
            reference_number='REGRESSION-REF-01',
            idempotency_key='REGRESSION-IDEMP-01',
        )

        self.assertIsNotNone(receipt)
        self.assertIsNotNone(trx)
        self.assertIsNotNone(col_entry)
        credit_inv.refresh_from_db()
        self.assertEqual(credit_inv.status, Invoice.Status.PAID)
        self.assertEqual(credit_inv.balance_due, Decimal('0.00'))


class PosConcurrencyTestCase(TransactionTestCase):
    """
    Pruebas de Concurrencia Real en PostgreSQL 14 (TransactionTestCase):
    1. Dos hilos enviando la misma solicitud POS con la misma clave de idempotencia:
       - Exactamente una factura creada.
       - Exactamente una salida de inventario.
       - Ambos hilos obtienen la misma factura.
    2. Dos hilos intentando comprar las últimas existencias disponibles (concurrencia de stock):
       - Uno triunfa, el otro es rechazado por existencia insuficiente.
       - El stock final nunca cae a valores negativos.
    """

    def setUp(self):
        self.country_ni, _ = CountryConfig.objects.get_or_create(
            code='NI',
            defaults={
                'name': 'Nicaragua',
                'flag_emoji': '🇳🇮',
                'primary_currency_code': 'NIO',
                'primary_currency_symbol': 'C$',
                'is_active': True,
            }
        )
        self.org = Organization.objects.create(
            name="Concurrencia POS S.A.",
            slug="concurrencia-pos",
            country=self.country_ni,
            currency='NIO',
            enabled_modules=['crm', 'inventory', 'finance', 'billing'],
            is_active=True,
        )
        self.user = User.objects.create_user(
            email='cajero_concurrent@test.ni',
            password='TestPassword2026*',
            full_name='Cajero Concurrente',
        )
        OrganizationMembership.objects.create(
            organization=self.org,
            user=self.user,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )
        self.warehouse = Warehouse.objects.create(
            organization=self.org,
            name='Bodega Concurrencia',
            is_active=True,
        )
        self.account = Account.objects.create(
            organization=self.org,
            commercial_name='Cliente Concurrente',
            account_type=Account.AccountType.INDIVIDUAL,
        )
        ensure_default_chart_of_accounts(self.org)

        self.item = InventoryItem.objects.create(
            organization=self.org,
            name='Cemento Portland Tipo I',
            sku='CEM-PORT-01',
            unit='BOL',
            unit_cost=Decimal('250.00'),
            price_retail=Decimal('350.00'),
            is_active=True,
        )
        # Saldo inicial: 10 bolsas
        self.stock = StockBalance.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            item=self.item,
            quantity=Decimal('10.00'),
        )

    def test_concurrent_pos_sales_same_idempotency_key(self):
        """
        Dos hilos enviando concurrentemente la misma venta POS con la misma clave de idempotencia:
        - Una sola factura emitida en base de datos.
        - Exactamente un recibo emitido.
        - Saldo de inventario descontado exactamente una vez (10 - 2 = 8, nunca 6).
        - Ambos hilos terminan exitosamente retornando la misma factura.
        """
        idem_key = 'CONCURRENT-POS-IDEMP-01'
        results = []
        errors = []

        def worker():
            connection.close()
            try:
                inv = execute_pos_sale(
                    organization=self.org,
                    user=self.user,
                    warehouse=self.warehouse,
                    cart_items=[{
                        'item_id': str(self.item.id),
                        'quantity': Decimal('2.00'),
                        'price_tier': 'RETAIL',
                    }],
                    account=self.account,
                    payment_method=PaymentReceipt.PaymentMethod.CASH,
                    idempotency_key=idem_key,
                )
                results.append(inv.pk)
            except Exception as e:
                errors.append(str(e))
            finally:
                connection.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)

        t1.start()
        t2.start()

        t1.join()
        t2.join()

        # Ambos hilos deben haber obtenido la factura sin excepciones
        self.assertEqual(len(errors), 0, f"Errores concurrentes inesperados: {errors}")
        self.assertEqual(len(results), 2, f"Resultados obtenidos: {len(results)}")
        self.assertEqual(results[0], results[1], "Ambos hilos debieron retornar el mismo ID de factura")

        # Exactamente 1 factura en la base de datos
        self.assertEqual(Invoice.objects.filter(organization=self.org).count(), 1)
        self.assertEqual(PaymentReceipt.objects.filter(organization=self.org).count(), 1)

        # Inventario descontado solo una vez: 10 - 2 = 8
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal('8.00'))

    def test_concurrent_stock_race_prevents_negative_inventory(self):
        """
        Condición de carrera por existencias limitadas:
        - Saldo disponible = 5 unidades.
        - Dos hilos intentan vender 4 unidades cada uno con claves de idempotencia diferentes.
        - Exactamente 1 hilo triunfa (descuenta 4, queda 1 disponible).
        - El 2do hilo es rechazado por existencia insuficiente (ValidationError).
        - El stock NUNCA cae a negativo (-3).
        """
        self.stock.quantity = Decimal('5.00')
        self.stock.save()

        successes = []
        failures = []

        def worker(thread_id):
            connection.close()
            try:
                inv = execute_pos_sale(
                    organization=self.org,
                    user=self.user,
                    warehouse=self.warehouse,
                    cart_items=[{
                        'item_id': str(self.item.id),
                        'quantity': Decimal('4.00'),
                        'price_tier': 'RETAIL',
                    }],
                    account=self.account,
                    payment_method=PaymentReceipt.PaymentMethod.CASH,
                    idempotency_key=f'STOCK-RACE-{thread_id}',
                )
                successes.append(inv.pk)
            except ValidationError as e:
                msg = e.message if hasattr(e, 'message') else str(e)
                failures.append(msg)
            except Exception as e:
                failures.append(f"Unexpected: {str(e)}")
            finally:
                connection.close()

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))

        t1.start()
        t2.start()

        t1.join()
        t2.join()

        self.assertEqual(len(successes), 1, f"Éxitos esperados 1, obtenidos: {len(successes)}, Fallos: {failures}")
        self.assertEqual(len(failures), 1, f"Fallos esperados 1, obtenidos: {len(failures)}, Éxitos: {successes}")
        self.assertTrue(any("insuficiente" in f.lower() for f in failures), f"Detalles de fallos: {failures}")

        # Stock remanente exacto: 5 - 4 = 1.00 (nunca -3.00)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal('1.00'))
        self.assertEqual(Invoice.objects.filter(organization=self.org).count(), 1)
