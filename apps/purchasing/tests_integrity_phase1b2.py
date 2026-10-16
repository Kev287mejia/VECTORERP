"""
Suite de Pruebas de Integridad, Concurrencia y Aislamiento Multi-Tenant - Fase 1B.2.1
Compras, Recepción de Mercancías, Kardex, Separación de Factura de Proveedor y Contabilidad
"""
import datetime
import threading
from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, IntegrityError
from django.test import TestCase, TransactionTestCase, Client
from django.urls import reverse
from django.utils import timezone

from apps.core.models import CountryConfig
from apps.inventory.models import InventoryItem, StockBalance, StockMovement, Warehouse
from apps.finance.models import AccountingAccount, JournalEntry, JournalEntryLine
from apps.finance.services import ensure_default_chart_of_accounts, sync_commercial_and_purchasing_to_ledger, record_purchase_order_journal_entry
from apps.finance.services_statements import FinancialStatementsService
from apps.purchasing.models import PurchaseOrder, PurchaseOrderItem, Supplier, SupplierInvoice, SupplierPayment
from apps.purchasing.services import receive_purchase_order_stock
from apps.purchasing.admin import PurchaseOrderAdmin, SupplierPaymentAdmin, SupplierPaymentInline
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel

User = get_user_model()


class PurchasingIntegrityPhase1B2TestCase(TestCase):
    """
    Certificación de Integridad de Compras, Recepción e Inventario (Fase 1B.2.1).
    """

    def setUp(self):
        # 1. Configuración Regional
        self.country_ni, _ = CountryConfig.objects.get_or_create(
            code='NI',
            defaults={
                'name': 'Nicaragua',
                'primary_currency_code': 'NIO',
                'primary_currency_symbol': 'C$',
                'tax_id_label': 'RUC',
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )

        # 2. Organización Principal y Organización Aislada (Competencia)
        self.org = Organization.objects.create(
            name="SERMANSA Ingeniería S.A.",
            legal_name="Servicios Electromecánicos S.A.",
            slug="sermansa-ni",
            tax_id="J0310000458912",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['inventory', 'finance', 'billing', 'purchasing'],
            is_active=True,
        )

        self.org_other = Organization.objects.create(
            name="Competencia Industrial S.A.",
            slug="competencia-ni",
            tax_id="J0310000999999",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['purchasing'],
            is_active=True,
        )

        # 3. Usuarios
        self.user_owner = User.objects.create_user(
            email='owner@sermansa.ni',
            password='Password123*',
            full_name='Ing. Carlos Mendoza',
        )
        OrganizationMembership.objects.create(
            user=self.user_owner,
            organization=self.org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )

        self.ops_coord = User.objects.create_user(
            email='almacen@sermansa.ni',
            password='Password123*',
            full_name='Lic. Marcos Fonseca (Bodega)',
        )
        OrganizationMembership.objects.create(
            user=self.ops_coord,
            organization=self.org,
            role_level=RoleLevel.OPERATIONS_COORD,
            is_active=True,
        )

        # 4. Proveedor y Bodega
        self.supplier = Supplier.objects.create(
            organization=self.org,
            commercial_name="SINSA Industrial",
            legal_name="SINSA S.A.",
            tax_id="J0310000111111",
            credit_days=30,
            default_ir_rate=Decimal('1.00'),
            default_imi_rate=Decimal('1.00'),
        )

        self.warehouse = Warehouse.objects.create(
            organization=self.org,
            name="Bodega Central Managua",
            code="BOD-01",
            warehouse_type=Warehouse.WarehouseType.CENTRAL,
            is_active=True,
        )

        # 5. Ítems del Catálogo de Inventario
        self.item_cable = InventoryItem.objects.create(
            organization=self.org,
            sku="MAT-CAB-15KV",
            name="Cable de Potencia 15kV",
            unit="MTS",
            unit_cost=Decimal('100.00'),
            is_active=True,
        )

        self.item_breaker = InventoryItem.objects.create(
            organization=self.org,
            sku="ELE-BRK-100A",
            name="Disyuntor Termomagnético 100A",
            unit="UND",
            unit_cost=Decimal('500.00'),
            is_active=True,
        )

        # Catálogo de Cuentas
        self.accounts = ensure_default_chart_of_accounts(self.org)

    def test_01_single_receipt_increments_stock_and_creates_kardex_with_po_fk(self):
        """
        Verifica que la recepción de mercancía incremente existencias exactamente una vez
        y que cada movimiento de Kardex mantenga ForeignKey hacia la Orden de Compra y la partida.
        """
        po = PurchaseOrder.objects.create(
            organization=self.org,
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            currency_code='NIO',
            status=PurchaseOrder.Status.APPROVED,
            created_by=self.user_owner,
        )
        item_line1 = PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item_cable,
            quantity=Decimal('20.00'),
            unit_cost=Decimal('120.00'),
        )
        item_line2 = PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item_breaker,
            quantity=Decimal('5.00'),
            unit_cost=Decimal('600.00'),
        )
        po.recalculate_totals()
        po.save()

        # Ejecutar recepción segura
        po_rec, movements, inv, j_entry = receive_purchase_order_stock(
            organization=self.org,
            purchase_order=po,
            user=self.ops_coord,
            supplier_invoice_number='FAC-SIN-4091',
        )

        # 1. Estado de la Orden
        po_rec.refresh_from_db()
        self.assertTrue(po_rec.stock_received)
        self.assertEqual(po_rec.status, PurchaseOrder.Status.RECEIVED)
        self.assertEqual(po_rec.received_by, self.ops_coord)
        self.assertIsNotNone(po_rec.received_at)
        self.assertEqual(po_rec.supplier_invoice_number, 'FAC-SIN-4091')

        # 2. Partidas con quantity_received actualizada
        item_line1.refresh_from_db()
        item_line2.refresh_from_db()
        self.assertEqual(item_line1.quantity_received, Decimal('20.00'))
        self.assertEqual(item_line2.quantity_received, Decimal('5.00'))

        # 3. Existencias en almacén
        bal_cable = StockBalance.objects.get(organization=self.org, warehouse=self.warehouse, item=self.item_cable)
        bal_breaker = StockBalance.objects.get(organization=self.org, warehouse=self.warehouse, item=self.item_breaker)
        self.assertEqual(bal_cable.quantity, Decimal('20.00'))
        self.assertEqual(bal_breaker.quantity, Decimal('5.00'))

        # 4. Trazabilidad relacional en Kardex (StockMovement)
        self.assertEqual(len(movements), 2)
        for mov in movements:
            self.assertEqual(mov.purchase_order, po_rec)
            self.assertIn(mov.purchase_order_item, [item_line1, item_line2])
            self.assertEqual(mov.movement_type, StockMovement.MovementType.IN_PURCHASE)

        # 5. Consulta inversa por ORM
        self.assertEqual(po_rec.stock_movements.count(), 2)
        self.assertEqual(item_line1.stock_movements.count(), 1)
        self.assertEqual(item_line2.stock_movements.count(), 1)

    def test_02_reception_creates_supplier_invoice_and_separates_vendor_document(self):
        """
        Verifica la separación arquitectónica entre la orden interna de compra
        y el documento fiscal/comercial del proveedor (SupplierInvoice).
        """
        po = PurchaseOrder.objects.create(
            organization=self.org,
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            currency_code='NIO',
            status=PurchaseOrder.Status.APPROVED,
            created_by=self.user_owner,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item_cable,
            quantity=Decimal('10.00'),
            unit_cost=Decimal('150.00'),
        )
        po.recalculate_totals()
        po.save()

        po_rec, movements, inv, j_entry = receive_purchase_order_stock(
            organization=self.org,
            purchase_order=po,
            user=self.ops_coord,
            supplier_invoice_number='FACTURA-LEGAL-7721',
        )

        self.assertIsNotNone(inv)
        self.assertEqual(inv.supplier, self.supplier)
        self.assertEqual(inv.purchase_order, po)
        self.assertEqual(inv.invoice_number, 'FACTURA-LEGAL-7721')
        self.assertEqual(inv.subtotal, po.subtotal)
        self.assertEqual(inv.tax_total, po.tax_total)
        self.assertEqual(inv.net_payable, po.net_payable)
        self.assertEqual(inv.balance_due, po.net_payable)
        self.assertEqual(inv.status, SupplierInvoice.Status.PENDING)

        # Verificación de relación inversa
        self.assertEqual(po_rec.supplier_invoices.count(), 1)
        self.assertEqual(self.supplier.invoices.count(), 1)

    def test_03_supplier_invoice_uniqueness_per_vendor(self):
        """
        Garantía de base de datos: un mismo proveedor no puede registrar
        dos facturas con el mismo folio en la misma organización.
        """
        SupplierInvoice.objects.create(
            organization=self.org,
            supplier=self.supplier,
            invoice_number='FACTURA-DUPLICADA-01',
            subtotal=Decimal('1000.00'),
            net_payable=Decimal('1000.00'),
            balance_due=Decimal('1000.00'),
        )

        with self.assertRaises(IntegrityError):
            SupplierInvoice.objects.create(
                organization=self.org,
                supplier=self.supplier,
                invoice_number='FACTURA-DUPLICADA-01',
                subtotal=Decimal('2000.00'),
                net_payable=Decimal('2000.00'),
                balance_due=Decimal('2000.00'),
            )

    def test_04_automatic_purchases_journal_entry_in_nio(self):
        """
        Verifica que la recepción de mercancía asiente de forma automática e inmediata
        una póliza PURCHASES cuadrada en el Libro Diario debitando la cuenta de Activo
        Circulante de Inventarios (1106-01) en lugar de Costo de Ventas (5101-01).
        """
        po = PurchaseOrder.objects.create(
            organization=self.org,
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            currency_code='NIO',
            status=PurchaseOrder.Status.APPROVED,
            created_by=self.user_owner,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item_cable,
            quantity=Decimal('10.00'),
            unit_cost=Decimal('1000.00'),  # Subtotal: 10,000. IVA 15%: 1,500. Ret IR 1%: 100. Ret IMI 1%: 100. Neto: 11,300.
        )
        po.recalculate_totals()
        po.save()

        po_rec, movements, inv, j_entry = receive_purchase_order_stock(
            organization=self.org,
            purchase_order=po,
            user=self.ops_coord,
            supplier_invoice_number='FAC-CONTABLE-01',
        )

        self.assertIsNotNone(j_entry)
        self.assertEqual(j_entry.entry_type, JournalEntry.EntryType.PURCHASES)
        self.assertEqual(j_entry.source_purchase_order, po)
        self.assertEqual(j_entry.source_supplier_invoice, inv)
        self.assertTrue(j_entry.is_balanced)

        # Verificar líneas de la póliza
        lines = list(j_entry.lines.all())
        # Débito Inventarios (1106-01): C$ 10,000.00
        line_inv = next(l for l in lines if l.account.code == '1106-01')
        self.assertEqual(line_inv.debit, Decimal('10000.00'))
        self.assertEqual(line_inv.credit, Decimal('0.00'))

        # Débito IVA Crédito Fiscal (1104-01): C$ 1,500.00
        line_iva = next(l for l in lines if l.account.code == '1104-01')
        self.assertEqual(line_iva.debit, Decimal('1500.00'))

        # Crédito Retención IR DGI (2103-01): C$ 100.00
        line_ir = next(l for l in lines if l.account.code == '2103-01')
        self.assertEqual(line_ir.credit, Decimal('100.00'))

        # Crédito Retención IMI Alcaldía (2103-02): C$ 100.00
        line_imi = next(l for l in lines if l.account.code == '2103-02')
        self.assertEqual(line_imi.credit, Decimal('100.00'))

        # Crédito Proveedores Nacionales CxP (2101-01): C$ 11,300.00
        line_cxp = next(l for l in lines if l.account.code == '2101-01')
        self.assertEqual(line_cxp.credit, Decimal('11300.00'))

        # Sumas iguales
        self.assertEqual(j_entry.total_debit, Decimal('11500.00'))
        self.assertEqual(j_entry.total_credit, Decimal('11500.00'))

    def test_05_usd_purchase_order_converts_to_nio_at_bcn_rate_in_ledger(self):
        """
        Verifica que compras en USD se conviertan a Córdobas (NIO) al tipo de cambio
        oficial BCN almacenado en la orden al momento de asentar en el Libro Diario.
        """
        bcn_rate = Decimal('36.6243')
        po_usd = PurchaseOrder.objects.create(
            organization=self.org,
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            currency_code='USD',
            exchange_rate=bcn_rate,
            status=PurchaseOrder.Status.APPROVED,
            created_by=self.user_owner,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po_usd,
            inventory_item=self.item_cable,
            quantity=Decimal('2.00'),
            unit_cost=Decimal('500.00'),  # Subtotal USD: 1,000. IVA: 150. IR: 10. IMI: 10. Neto: 1,130 USD.
        )
        po_usd.recalculate_totals()
        po_usd.save()

        po_rec, movements, inv, j_entry = receive_purchase_order_stock(
            organization=self.org,
            purchase_order=po_usd,
            user=self.ops_coord,
            supplier_invoice_number='FAC-USD-0099',
        )

        expected_subtotal_nio = (Decimal('1000.00') * bcn_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        expected_tax_nio = (Decimal('150.00') * bcn_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        expected_cxp_nio = (Decimal('1130.00') * bcn_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        line_inv = j_entry.lines.get(account__code='1106-01')
        line_cxp = j_entry.lines.get(account__code='2101-01')

        self.assertEqual(line_inv.debit, expected_subtotal_nio)
        self.assertEqual(line_cxp.credit, expected_cxp_nio)
        self.assertTrue(j_entry.is_balanced)

    def test_06_purchases_journal_entry_database_uniqueness(self):
        """
        Garantía de base de datos contra duplicidad de pólizas PURCHASES para una misma orden de compra.
        """
        po = PurchaseOrder.objects.create(
            organization=self.org,
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            currency_code='NIO',
            status=PurchaseOrder.Status.APPROVED,
            created_by=self.user_owner,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item_breaker,
            quantity=Decimal('1.00'),
            unit_cost=Decimal('500.00'),
        )
        po.recalculate_totals()
        po.save()

        # Primera creación legítima
        entry1 = record_purchase_order_journal_entry(self.org, po, self.user_owner)
        self.assertIsNotNone(entry1)

        # Reintento mediante servicio retorna exactamente la misma póliza
        entry2 = record_purchase_order_journal_entry(self.org, po, self.user_owner)
        self.assertEqual(entry1.pk, entry2.pk)
        self.assertEqual(
            JournalEntry.objects.filter(organization=self.org, source_purchase_order=po, entry_type=JournalEntry.EntryType.PURCHASES).count(),
            1
        )

        # Intento directo de violar el constraint único debe disparar IntegrityError
        with self.assertRaises(IntegrityError):
            JournalEntry.objects.create(
                organization=self.org,
                entry_type=JournalEntry.EntryType.PURCHASES,
                concept="Intento de Póliza Duplicada",
                source_purchase_order=po,
                created_by=self.user_owner,
            )

    def test_07_intermediate_failure_rolls_back_kardex_stock_and_journal(self):
        """
        Atomicidad: si ocurre un fallo durante la recepción, se revierte todo
        (el saldo físico no aumenta, no hay movimientos de Kardex ni póliza).
        """
        po = PurchaseOrder.objects.create(
            organization=self.org,
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            currency_code='NIO',
            status=PurchaseOrder.Status.APPROVED,
            created_by=self.user_owner,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item_cable,
            quantity=Decimal('10.00'),
            unit_cost=Decimal('100.00'),
        )
        po.recalculate_totals()
        po.save()

        # Inactivar bodega justo antes para provocar una falla en la validación
        self.warehouse.is_active = False
        self.warehouse.save()

        with self.assertRaises(ValidationError):
            receive_purchase_order_stock(
                organization=self.org,
                purchase_order=po,
                user=self.ops_coord,
                supplier_invoice_number='FAC-FAIL-01',
            )

        # Verificar reversión completa
        po.refresh_from_db()
        self.assertFalse(po.stock_received)
        self.assertEqual(po.status, PurchaseOrder.Status.APPROVED)
        self.assertEqual(StockMovement.objects.filter(purchase_order=po).count(), 0)
        self.assertEqual(JournalEntry.objects.filter(source_purchase_order=po).count(), 0)
        self.assertEqual(SupplierInvoice.objects.filter(purchase_order=po).count(), 0)

    def test_08_cross_tenant_isolation_rejected(self):
        """
        Aislamiento Multi-Tenant: rechaza recepciones de órdenes, almacenes o ítems de otro tenant.
        """
        # Orden de otra organización
        po_other = PurchaseOrder.objects.create(
            organization=self.org_other,
            supplier=Supplier.objects.create(organization=self.org_other, commercial_name="Otro Proveedor", tax_id="J9999999999999"),
            target_warehouse=Warehouse.objects.create(organization=self.org_other, name="Bodega Ajena", code="BOD-AJENA"),
            status=PurchaseOrder.Status.APPROVED,
        )

        with self.assertRaises(ValidationError) as ctx:
            receive_purchase_order_stock(
                organization=self.org,
                purchase_order=po_other,
                user=self.ops_coord,
            )
        self.assertIn("no pertenece a la organización activa", str(ctx.exception))

    def test_09_django_admin_cannot_create_or_delete_supplier_payments(self):
        """
        Verifica que Django Admin impida add, change y delete directos en SupplierPayment
        para no eludir el flujo financiero y de tesorería.
        """
        from django.contrib.admin.sites import AdminSite
        site = AdminSite()
        admin_instance = SupplierPaymentAdmin(SupplierPayment, site)
        inline_instance = SupplierPaymentInline(PurchaseOrder, site)

        self.assertFalse(admin_instance.has_add_permission(None))
        self.assertFalse(admin_instance.has_change_permission(None))
        self.assertFalse(admin_instance.has_delete_permission(None))

        self.assertFalse(inline_instance.has_add_permission(None, None))
        self.assertFalse(inline_instance.has_change_permission(None, None))
        self.assertFalse(inline_instance.has_delete_permission(None, None))

    def test_10_django_admin_purchase_order_has_readonly_financial_fields(self):
        """
        Verifica que campos calculados de PurchaseOrder sean de solo lectura en Django Admin.
        """
        from django.contrib.admin.sites import AdminSite
        site = AdminSite()
        po_admin = PurchaseOrderAdmin(PurchaseOrder, site)
        expected_readonly = [
            'number', 'subtotal', 'tax_total', 'gross_total',
            'ir_withheld_amount', 'imi_withheld_amount', 'net_payable',
            'amount_paid', 'balance_due', 'stock_received', 'received_at',
            'withholding_certificate_number'
        ]
        for field in expected_readonly:
            self.assertIn(field, po_admin.readonly_fields)

    def test_11_sync_commercial_and_purchasing_to_ledger_uses_new_service(self):
        """
        Verifica que sync_commercial_and_purchasing_to_ledger delegue en el nuevo servicio
        y cree pólizas PURCHASES idempotentes debitando la cuenta de inventario.
        """
        po = PurchaseOrder.objects.create(
            organization=self.org,
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            currency_code='NIO',
            status=PurchaseOrder.Status.RECEIVED,
            stock_received=True,
            created_by=self.user_owner,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item_cable,
            quantity=Decimal('5.00'),
            unit_cost=Decimal('200.00'),
        )
        po.recalculate_totals()
        po.save()

        stats = sync_commercial_and_purchasing_to_ledger(self.org, self.user_owner)
        self.assertEqual(stats['purchases_synced'], 1)

        entry = JournalEntry.objects.filter(source_purchase_order=po, entry_type=JournalEntry.EntryType.PURCHASES).first()
        self.assertIsNotNone(entry)
        self.assertTrue(entry.lines.filter(account__code='1106-01').exists())

        # Segunda sincronización no duplica
        stats_retry = sync_commercial_and_purchasing_to_ledger(self.org, self.user_owner)
        self.assertEqual(stats_retry['purchases_synced'], 0)
        self.assertEqual(JournalEntry.objects.filter(source_purchase_order=po).count(), 1)

    def test_12_balance_sheet_reflects_inventory_and_accounts_payable(self):
        """
        Verifica que tras la recepción contable, el Balance General muestre el activo de
        inventarios en 'inventories' (1106-01) y la cuenta por pagar en 'accounts_payable' (2101-01),
        conservando la ecuación contable cuadrada.
        """
        po = PurchaseOrder.objects.create(
            organization=self.org,
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            currency_code='NIO',
            status=PurchaseOrder.Status.APPROVED,
            created_by=self.user_owner,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=po,
            inventory_item=self.item_cable,
            quantity=Decimal('10.00'),
            unit_cost=Decimal('100.00'),
        )
        po.recalculate_totals()
        po.save()

        receive_purchase_order_stock(self.org, po, self.ops_coord, supplier_invoice_number='INV-BS-01')

        bs = FinancialStatementsService.get_balance_sheet(self.org)
        self.assertTrue(bs['is_balanced'])
        self.assertTrue(any(item['code'] == '1106-01' for item in bs['inventories']))
        self.assertTrue(any(item['code'] == '2101-01' for item in bs['accounts_payable']))


class PurchasingConcurrencyPhase1B2TestCase(TransactionTestCase):
    """
    Prueba de Concurrencia Transaccional en PostgreSQL:
    Dos hilos concurrentes intentan recepcionar la misma orden de compra simultáneamente.
    El bloqueo pesimista con select_for_update() garantiza que solo uno de los dos triunfe
    y el otro sea rechazado, impidiendo duplicidad en Kardex o en contabilidad.
    """

    def setUp(self):
        self.country_ni, _ = CountryConfig.objects.get_or_create(
            code='NI',
            defaults={
                'name': 'Nicaragua',
                'primary_currency_code': 'NIO',
                'primary_currency_symbol': 'C$',
                'tax_id_label': 'RUC',
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )

        self.org = Organization.objects.create(
            name="SERMANSA Concurrencia S.A.",
            legal_name="SERMANSA Concurrencia S.A.",
            slug="sermansa-conc",
            tax_id="J0310000458913",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['inventory', 'finance', 'purchasing'],
            is_active=True,
        )

        self.user = User.objects.create_user(
            email='bodeguero@sermansa.ni',
            password='Password123*',
            full_name='Bodeguero Concurrente',
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            role_level=RoleLevel.OPERATIONS_COORD,
            is_active=True,
        )

        self.supplier = Supplier.objects.create(
            organization=self.org,
            commercial_name="Proveedor Concurrente S.A.",
            tax_id="J0310000777777",
        )

        self.warehouse = Warehouse.objects.create(
            organization=self.org,
            name="Bodega de Pruebas Concurrentes",
            code="BOD-CONC",
            is_active=True,
        )

        self.item = InventoryItem.objects.create(
            organization=self.org,
            sku="CONC-ITEM-01",
            name="Material para Prueba de Concurrencia",
            unit="UND",
            unit_cost=Decimal('500.00'),
            is_active=True,
        )

        ensure_default_chart_of_accounts(self.org)

        self.po = PurchaseOrder.objects.create(
            organization=self.org,
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            currency_code='NIO',
            status=PurchaseOrder.Status.APPROVED,
            created_by=self.user,
        )
        PurchaseOrderItem.objects.create(
            organization=self.org,
            purchase_order=self.po,
            inventory_item=self.item,
            quantity=Decimal('50.00'),
            unit_cost=Decimal('500.00'),
        )
        self.po.recalculate_totals()
        self.po.save()

    def test_13_concurrent_receptions_cannot_duplicate_stock_or_kardex(self):
        """
        Dos hilos ejecutando receive_purchase_order_stock simultáneamente:
        Uno triunfa y el otro es rechazado por haber sido recepcionada previamente.
        Las existencias aumentan únicamente una vez (+50.00 UND).
        """
        successes = []
        errors = []

        def worker(invoice_num):
            connection.close()
            try:
                po_rec, movements, inv, j_entry = receive_purchase_order_stock(
                    organization=self.org,
                    purchase_order=self.po,
                    user=self.user,
                    supplier_invoice_number=invoice_num,
                )
                successes.append(po_rec.pk)
            except (ValidationError, Exception) as e:
                errors.append(str(e))
            finally:
                connection.close()

        import time
        t1 = threading.Thread(target=worker, args=('FAC-THREAD-1',))
        t2 = threading.Thread(target=worker, args=('FAC-THREAD-2',))

        t1.start()
        time.sleep(0.01)
        t2.start()

        t1.join()
        t2.join()

        # Debe haber exactamente 1 éxito y 1 fallo por orden ya recepcionada
        self.assertEqual(len(successes), 1, f"Éxitos: {len(successes)}, Errores: {errors}")
        self.assertEqual(len(errors), 1, f"Errores esperados: 1, encontrados: {len(errors)}")

        self.po.refresh_from_db()
        self.assertTrue(self.po.stock_received)
        self.assertEqual(self.po.status, PurchaseOrder.Status.RECEIVED)

        # Existencias físicas incrementadas una sola vez
        balance = StockBalance.objects.get(organization=self.org, warehouse=self.warehouse, item=self.item)
        self.assertEqual(balance.quantity, Decimal('50.00'))

        # Movimientos de Kardex exactamente 1
        self.assertEqual(StockMovement.objects.filter(purchase_order=self.po).count(), 1)

        # Asientos contables exactamente 1
        self.assertEqual(JournalEntry.objects.filter(source_purchase_order=self.po).count(), 1)
