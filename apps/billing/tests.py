import json
from decimal import Decimal
import datetime
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Invoice, InvoiceItem, PaymentReceipt, PosRegisterShift
from apps.billing.services_pos_shift import open_pos_shift, compute_shift_x_cut_metrics, close_pos_shift_z_cut
from apps.billing.services import execute_pos_sale
from apps.finance.models import BankAccount, FinancialTransaction, JournalEntry
from apps.finance.services import ensure_default_chart_of_accounts
from apps.inventory.models import InventoryItem, Warehouse, StockBalance
from apps.core.models import CountryConfig
from apps.crm.models import Account, Quote, QuoteItem
from apps.operations.models import WorkOrder, WorkOrderItem, WorkOrderTimelineLog
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel

User = get_user_model()


class BillingModuleTestSuite(TestCase):
    """
    Suite Integral de Pruebas Unitarias y de Integración para el Módulo Fiscal y Cartera (apps/billing).
    Garantiza:
    1. Aislamiento Multi-Tenant estricto.
    2. Precisión Matemática Ley 822 DGI & IMI Alcaldía sobre Base Imponible Gravable.
    3. Conversión bimonetaria y cierre de saldo a PAID en Recibos Oficiales de Caja (ROC).
    4. Inmutabilidad de los Recibos ROC.
    5. Conversión en 1 Clic desde Cotización (Quote -> Invoice) y Orden de Trabajo (WorkOrder -> Invoice).
    6. Versatilidad de Perfiles de Giro de Negocio (Business Presets).
    7. Matriz de Autorización RBAC por nivel jerárquico.
    """

    def setUp(self):
        # 1. Configuración Regional Nicaragua
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

        # 2. Organización Principal y Organización Invasora
        self.org = Organization.objects.create(
            name="SERMANSA Ingeniería S.A.",
            legal_name="Servicios y Mantenimiento Electromecánico S.A.",
            slug="sermansa-ni",
            tax_id="J0310000458912",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['crm', 'operations', 'sst', 'inventory', 'billing'],
            business_profile=Organization.BusinessProfile.ENGINEERING_SERVICES,
            is_active=True,
        )

        self.org_other = Organization.objects.create(
            name="Competencia Industrial S.A.",
            slug="competencia-ni",
            tax_id="J0310000999999",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['crm', 'billing'],
            is_active=True,
        )

        # 3. Usuarios de Distintos Niveles Jerárquicos
        # Nivel 100: Owner / Administrador General
        self.owner = User.objects.create_user(
            email='owner@sermansa.ni',
            full_name='Ing. Carlos Mendoza (Owner)',
            password='Password123*',
        )
        OrganizationMembership.objects.create(
            user=self.owner,
            organization=self.org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )

        # Nivel 85: Gerente Comercial
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

        # Nivel 80: Coordinador de Operaciones
        self.ops_coord = User.objects.create_user(
            email='operaciones@sermansa.ni',
            full_name='Ing. Marcos Fonseca (Operaciones)',
            password='Password123*',
        )
        OrganizationMembership.objects.create(
            user=self.ops_coord,
            organization=self.org,
            role_level=RoleLevel.OPERATIONS_COORD,
            is_active=True,
            is_default_org=True,
        )

        # Nivel 50: Ingeniero Residente
        self.engineer = User.objects.create_user(
            email='ingeniero@sermansa.ni',
            full_name='Ing. Roberto Dávila (Ingeniero)',
            password='Password123*',
        )
        OrganizationMembership.objects.create(
            user=self.engineer,
            organization=self.org,
            role_level=RoleLevel.ENGINEER,
            is_active=True,
            is_default_org=True,
        )

        # Nivel 20: Técnico de Campo
        self.technician = User.objects.create_user(
            email='tecnico@sermansa.ni',
            full_name='Téc. Pedro Martínez (Técnico)',
            password='Password123*',
        )
        OrganizationMembership.objects.create(
            user=self.technician,
            organization=self.org,
            role_level=RoleLevel.TECHNICIAN,
            is_active=True,
            is_default_org=True,
        )

        # 4. Clientes Corporativos
        self.client_enatrel = Account.objects.create(
            organization=self.org,
            code='CLI-001',
            commercial_name='ENATREL',
            legal_name='Empresa Nacional de Transmisión Eléctrica',
            tax_id='J0310000000018',
            is_tax_exempt=False,
        )

        self.client_exempt = Account.objects.create(
            organization=self.org,
            code='CLI-002',
            commercial_name='Zona Franca Las Mercedes S.A.',
            tax_id='J0310000088888',
            is_tax_exempt=True,
        )

        self.client = Client()

    def test_01_ley_822_exact_tax_and_retention_calculation(self):
        """
        Prueba el cálculo fiscal exacto según la Ley 822 de la República de Nicaragua:
        - Subtotal: C$ 95,000.00
        - Descuento: C$ 0.00
        - Base Imponible: C$ 95,000.00
        - IVA 15%: C$ 14,250.00
        - Total Facturado Bruto: C$ 109,250.00
        - Retención IR Servicios (2% sobre C$ 95,000.00): C$ 1,900.00
        - Retención Alcaldía IMI (1% sobre C$ 95,000.00): C$ 950.00
        - Total Retenciones: C$ 2,850.00
        - Neto a Cobrar en Caja: C$ 106,400.00
        """
        invoice = Invoice.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            status=Invoice.Status.ISSUED,
            currency_code='NIO',
            currency_symbol='C$',
            exchange_rate=Decimal('36.6243'),
            apply_tax=True,
            tax_name='IVA',
            tax_rate=Decimal('15.00'),
            ir_retention_rate=Decimal('2.00'),
            imi_retention_rate=Decimal('1.00'),
            created_by=self.commercial_mgr,
        )

        InvoiceItem.objects.create(
            organization=self.org,
            invoice=invoice,
            description="Mantenimiento preventivo subestación",
            quantity=Decimal('1.00'),
            unit_price=Decimal('65000.00'),
        )
        InvoiceItem.objects.create(
            organization=self.org,
            invoice=invoice,
            description="Calibración de interruptores SF6",
            quantity=Decimal('1.00'),
            unit_price=Decimal('30000.00'),
        )

        invoice.recalculate_totals()
        invoice.save()

        self.assertEqual(invoice.subtotal, Decimal('95000.00'))
        self.assertEqual(invoice.taxable_base, Decimal('95000.00'))
        self.assertEqual(invoice.tax_total, Decimal('14250.00'))
        self.assertEqual(invoice.gross_total, Decimal('109250.00'))
        self.assertEqual(invoice.ir_retention_amount, Decimal('1900.00'))
        self.assertEqual(invoice.imi_retention_amount, Decimal('950.00'))
        self.assertEqual(invoice.total_retentions, Decimal('2850.00'))
        self.assertEqual(invoice.net_receivable, Decimal('106400.00'))
        self.assertEqual(invoice.balance_due, Decimal('106400.00'))
        self.assertEqual(invoice.status, Invoice.Status.ISSUED)

    def test_02_tax_exempt_client_calculation(self):
        """
        Verifica que para un cliente exento de impuestos (Zona Franca), el IVA no se compute,
        pero las retenciones se apliquen correctamente sobre la base imponible.
        """
        invoice = Invoice.objects.create(
            organization=self.org,
            account=self.client_exempt,
            status=Invoice.Status.ISSUED,
            currency_code='NIO',
            apply_tax=True,
            tax_rate=Decimal('15.00'),
            ir_retention_rate=Decimal('1.00'),  # 1% Bienes
            imi_retention_rate=Decimal('1.00'),
            created_by=self.commercial_mgr,
        )

        InvoiceItem.objects.create(
            organization=self.org,
            invoice=invoice,
            description="Suministro de cable de cobre",
            quantity=Decimal('10.00'),
            unit_price=Decimal('5000.00'),
        )

        invoice.recalculate_totals()
        invoice.save()

        self.assertEqual(invoice.subtotal, Decimal('50000.00'))
        self.assertEqual(invoice.tax_total, Decimal('0.00'))
        self.assertEqual(invoice.gross_total, Decimal('50000.00'))
        self.assertEqual(invoice.ir_retention_amount, Decimal('500.00'))
        self.assertEqual(invoice.imi_retention_amount, Decimal('500.00'))
        self.assertEqual(invoice.net_receivable, Decimal('49000.00'))

    def test_03_bimonetary_cashier_and_balance_closure(self):
        """
        Verifica la caja bimonetaria y liquidación de saldo:
        1. Emisión con neto a cobrar C$ 106,400.00.
        2. Abono parcial de C$ 50,000.00 -> Estado pasa a PARTIAL (Saldo C$ 56,400.00).
        3. Liquidación restante en USD con tasa de cambio oficial BCN (C$ 36.6243).
           Monto USD = 56,400.00 / 36.6243 ≈ 1,539.96 USD.
           La factura pasa automáticamente a PAID y balance_due = 0.00.
        """
        invoice = Invoice.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            status=Invoice.Status.ISSUED,
            currency_code='NIO',
            currency_symbol='C$',
            exchange_rate=Decimal('36.6243'),
            apply_tax=True,
            tax_rate=Decimal('15.00'),
            ir_retention_rate=Decimal('2.00'),
            imi_retention_rate=Decimal('1.00'),
            created_by=self.commercial_mgr,
        )
        InvoiceItem.objects.create(
            organization=self.org,
            invoice=invoice,
            description="Servicio integral",
            quantity=Decimal('1.00'),
            unit_price=Decimal('95000.00'),
        )
        invoice.recalculate_totals()
        invoice.save()
        self.assertEqual(invoice.net_receivable, Decimal('106400.00'))

        # Primer Recibo Oficial de Caja: Abono de C$ 50,000.00
        roc1 = PaymentReceipt.objects.create(
            organization=self.org,
            invoice=invoice,
            currency_paid='NIO',
            amount_in_payment_currency=Decimal('50000.00'),
            exchange_rate_applied=invoice.exchange_rate,
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            received_by=self.commercial_mgr,
        )

        invoice.refresh_from_db()
        self.assertTrue(roc1.number.startswith('ROC-'))
        self.assertEqual(invoice.amount_paid, Decimal('50000.00'))
        self.assertEqual(invoice.balance_due, Decimal('56400.00'))
        self.assertEqual(invoice.status, Invoice.Status.PARTIAL)

        # Segundo Recibo Oficial de Caja: Pago en USD por el saldo remanente
        # 56,400 / 36.6243 = 1539.96 USD
        usd_amount = (Decimal('56400.00') / invoice.exchange_rate).quantize(Decimal('0.01'))
        roc2 = PaymentReceipt.objects.create(
            organization=self.org,
            invoice=invoice,
            currency_paid='USD',
            amount_in_payment_currency=usd_amount,
            exchange_rate_applied=invoice.exchange_rate,
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_LAFISE,
            received_by=self.commercial_mgr,
        )

        invoice.refresh_from_db()
        self.assertAlmostEqual(invoice.balance_due, Decimal('0.00'), delta=Decimal('0.05'))
        self.assertEqual(invoice.status, Invoice.Status.PAID)

    def test_04_payment_receipt_strict_immutability(self):
        """
        Verifica que una vez emitido un Recibo Oficial de Caja (ROC), no puede modificarse
        ni eliminarse, garantizando inviolabilidad de auditoría fiscal.
        """
        invoice = Invoice.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            status=Invoice.Status.ISSUED,
            created_by=self.commercial_mgr,
        )
        InvoiceItem.objects.create(
            organization=self.org,
            invoice=invoice,
            description="Item",
            quantity=Decimal('1.00'),
            unit_price=Decimal('1000.00'),
        )
        invoice.recalculate_totals()
        invoice.save()

        receipt = PaymentReceipt.objects.create(
            organization=self.org,
            invoice=invoice,
            currency_paid='NIO',
            amount_in_payment_currency=Decimal('500.00'),
            exchange_rate_applied=invoice.exchange_rate,
            received_by=self.commercial_mgr,
        )

        # Intentar modificar el recibo
        receipt.amount_in_payment_currency = Decimal('600.00')
        with self.assertRaises(ValidationError):
            receipt.save()

        # Intentar eliminar el recibo
        with self.assertRaises(ValidationError):
            receipt.delete()

    def test_05_convert_quote_to_invoice_1_click(self):
        """
        Verifica la conversión en 1 clic de una Cotización Aprobada a Factura Fiscal (Quote -> Invoice).
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Licitación Transformadores",
            status=Quote.Status.APPROVED,
            currency_code='NIO',
            currency_symbol='C$',
            exchange_rate=Decimal('36.6243'),
            apply_tax=True,
            tax_name='IVA',
            tax_rate=Decimal('15.00'),
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-001',
            description='Servicio de Termografía',
            unit='SRV',
            quantity=Decimal('2.00'),
            unit_price=Decimal('15000.00'),
        )

        self.client.force_login(self.commercial_mgr)
        url = reverse('billing:from_quote', kwargs={'quote_pk': quote.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        invoice = Invoice.objects.get(source_quote=quote)
        self.assertEqual(invoice.status, Invoice.Status.ISSUED)
        self.assertEqual(invoice.items.count(), 1)
        self.assertEqual(invoice.subtotal, Decimal('30000.00'))
        self.assertEqual(invoice.gross_total, Decimal('34500.00'))
        self.assertTrue(invoice.number.startswith('FAC-'))

    def test_06_convert_work_order_to_invoice_1_click(self):
        """
        Verifica la conversión en 1 clic de una Orden de Trabajo a Factura Fiscal (WorkOrder -> Invoice).
        Prioriza las cantidades ejecutadas (executed_quantity) y registra evento en WorkOrderTimelineLog.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Mantenimiento Subestaciones",
            status=Quote.Status.APPROVED,
            currency_code='NIO',
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-OIL',
            description='Tratamiento de aceite dieléctrico',
            quantity=Decimal('10.00'),
            unit_price=Decimal('2500.00'),
        )

        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            source_quote=quote,
            title="OT de Tratamiento de Aceite",
            phase=WorkOrder.Phase.COMPLETED,
            created_by=self.commercial_mgr,
        )
        WorkOrderItem.objects.create(
            organization=self.org,
            work_order=wo,
            item_type='SERVICE',
            code='SRV-OIL',
            description='Tratamiento de aceite dieléctrico',
            planned_quantity=Decimal('10.00'),
            executed_quantity=Decimal('12.00'),  # Se ejecutaron 12 unidades reales
        )

        self.client.force_login(self.commercial_mgr)
        url = reverse('billing:from_workorder', kwargs={'wo_pk': wo.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        invoice = Invoice.objects.get(source_work_order=wo)
        self.assertEqual(invoice.items.count(), 1)
        item = invoice.items.first()
        self.assertEqual(item.quantity, Decimal('12.00'))
        self.assertEqual(item.unit_price, Decimal('2500.00'))
        self.assertEqual(invoice.subtotal, Decimal('30000.00'))

        # Verificar bitácora cronológica inmutable en la OT
        log = WorkOrderTimelineLog.objects.filter(work_order=wo, event_type='INVOICED').first()
        self.assertIsNotNone(log)
        self.assertIn(invoice.number, log.summary)

    def test_07_business_profile_presets(self):
        """
        Verifica la versatilidad de los Perfiles de Giro de Negocio:
        Aplicar COMMERCIAL_RETAIL actualiza enabled_modules eliminando 'operations' y 'sst',
        dejando el flujo comercial CRM -> Inventario -> Facturación.
        """
        self.client.force_login(self.owner)
        url = reverse('tenancy:apply_preset')

        # Aplicar Preset Comercio / Ferretería
        response = self.client.post(url, {'preset': 'COMMERCIAL_RETAIL'})
        self.assertEqual(response.status_code, 302)

        self.org.refresh_from_db()
        self.assertEqual(self.org.business_profile, Organization.BusinessProfile.COMMERCIAL_RETAIL)
        self.assertIn('crm', self.org.enabled_modules)
        self.assertIn('inventory', self.org.enabled_modules)
        self.assertIn('billing', self.org.enabled_modules)
        self.assertNotIn('operations', self.org.enabled_modules)
        self.assertNotIn('sst', self.org.enabled_modules)

        # Aplicar Preset Servicios de Ingeniería
        response = self.client.post(url, {'preset': 'ENGINEERING_SERVICES'})
        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertIn('operations', self.org.enabled_modules)
        self.assertIn('sst', self.org.enabled_modules)

    def test_08_rbac_authorization_matrix(self):
        """
        Verifica la matriz de permisos RBAC para Facturación DGI (/billing/*):
        - COMMERCIAL_MGR (85): Emite facturas, aplica retenciones, registra pagos ROC (200 / 302).
        - OPERATIONS_COORD (80): Lectura permitida (200), pero mutación bloqueada con HTTP 403.
        - ENGINEER (50) & TECHNICIAN (20): Bloqueo total HTTP 403 en todo /billing/*.
        """
        invoice = Invoice.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            status=Invoice.Status.ISSUED,
            created_by=self.commercial_mgr,
        )
        InvoiceItem.objects.create(
            organization=self.org,
            invoice=invoice,
            description="Item",
            quantity=Decimal('1.00'),
            unit_price=Decimal('1000.00'),
        )
        invoice.recalculate_totals()
        invoice.save()

        list_url = reverse('billing:invoice_list')
        detail_url = reverse('billing:invoice_detail', kwargs={'pk': invoice.pk})
        retention_url = reverse('billing:apply_retentions', kwargs={'pk': invoice.pk})
        payment_url = reverse('billing:register_payment', kwargs={'pk': invoice.pk})

        # 1. COMMERCIAL_MGR (85): Acceso total
        self.client.force_login(self.commercial_mgr)
        self.assertEqual(self.client.get(list_url).status_code, 200)
        self.assertEqual(self.client.get(detail_url).status_code, 200)
        # Aplicar retenciones
        resp = self.client.post(retention_url, {'ir_retention_rate': '2.00', 'imi_retention_rate': '1.00'})
        self.assertEqual(resp.status_code, 302)

        # 2. OPERATIONS_COORD (80): Solo Lectura
        self.client.force_login(self.ops_coord)
        self.assertEqual(self.client.get(list_url).status_code, 200)
        self.assertEqual(self.client.get(detail_url).status_code, 200)
        # Intento de registrar cobro -> Bloqueado con HTTP 403
        resp = self.client.post(payment_url, {'amount': '100.00', 'currency_paid': 'NIO'})
        self.assertEqual(resp.status_code, 403)
        # Intento de modificar retenciones -> Bloqueado con HTTP 403
        resp = self.client.post(retention_url, {'ir_retention_rate': '2.00'})
        self.assertEqual(resp.status_code, 403)

        # 3. ENGINEER (50) y TECHNICIAN (20): Bloqueo estricto HTTP 403
        for user in [self.engineer, self.technician]:
            self.client.force_login(user)
            self.assertEqual(self.client.get(list_url).status_code, 403)
            self.assertEqual(self.client.get(detail_url).status_code, 403)

    def test_09_multi_tenant_isolation(self):
        """
        Verifica el aislamiento multi-tenant estricto:
        Un usuario de la organización A no puede ver ni consultar facturas de la organización B.
        """
        other_mgr = User.objects.create_user(
            email='mgr@competencia.ni',
            password='Password123*',
        )
        OrganizationMembership.objects.create(
            user=other_mgr,
            organization=self.org_other,
            role_level=RoleLevel.COMMERCIAL_MGR,
            is_active=True,
            is_default_org=True,
        )

        inv_org = Invoice.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            created_by=self.commercial_mgr,
        )

        self.client.force_login(other_mgr)
        detail_url = reverse('billing:invoice_detail', kwargs={'pk': inv_org.pk})
        response = self.client.get(detail_url)
        # Debe dar 404 porque el queryset está aislado con .for_org(request.organization)
        self.assertEqual(response.status_code, 404)


class PosRegisterShiftAndPwaTestCase(TestCase):
    """
    Suite de Pruebas Unitarias y de Integración para el Control de Turnos de Caja POS
    y la Infraestructura PWA de Contingencia en Campo (Paso 18.1).
    """

    def setUp(self):
        self.client = Client()
        self.country_ni, _ = CountryConfig.objects.get_or_create(
            code='NI',
            defaults={
                'name': 'Nicaragua',
                'flag_emoji': '🇳🇮',
                'primary_currency_code': 'NIO',
                'primary_currency_symbol': 'C$',
                'tax_id_label': 'RUC',
                'default_tax_name': 'IVA',
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )

        self.org = Organization.objects.create(
            name="Comercial El Ahorro S.A.",
            legal_name="Comercializadora El Ahorro Sociedad Anónima",
            slug="comercial-ahorro-pos",
            tax_id="J0310000789012",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['crm', 'inventory', 'billing'],
            business_profile=Organization.BusinessProfile.COMMERCIAL_RETAIL,
            is_active=True,
        )

        self.cashier = User.objects.create_user(
            email='cajero1@elahorro.ni',
            password='Password123*',
            full_name='Martha Lorena Gómez'
        )
        self.membership = OrganizationMembership.objects.create(
            user=self.cashier,
            organization=self.org,
            role_level=RoleLevel.COMMERCIAL_MGR,
            is_active=True,
            is_default_org=True,
        )

        self.warehouse = Warehouse.objects.create(
            organization=self.org,
            code='BOD-POS-01',
            name='Bodega Tienda Mostrador',
            warehouse_type=Warehouse.WarehouseType.CENTRAL,
            is_active=True,
        )

        self.client_account = Account.objects.create(
            organization=self.org,
            commercial_name='Consumidor Final',
            legal_name='Cliente de Mostrador',
            tax_id='',
            account_type=Account.AccountType.INDIVIDUAL,
        )

        self.item = InventoryItem.objects.create(
            organization=self.org,
            sku='PROD-POS-01',
            name='Taladro Percutor 1/2 Pulgada',
            price_retail=Decimal('100.00'),
            price_contractor=Decimal('90.00'),
            price_wholesale=Decimal('80.00'),
            unit_cost=Decimal('60.00'),
            is_active=True,
        )

        StockBalance.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            item=self.item,
            quantity=Decimal('50.00'),
        )

    def test_01_open_pos_shift_and_duplicate_prevention(self):
        """
        Verifica la apertura limpia de un turno POS y el bloqueo estricto
        a la existencia de turnos duplicados en estado OPEN para el mismo cajero.
        """
        shift = open_pos_shift(
            organization=self.org,
            cashier=self.cashier,
            opening_cash_nio=Decimal('1500.00'),
            register_name='CAJA-MOSTRADOR-01'
        )

        self.assertIsNotNone(shift.pk)
        self.assertTrue(shift.shift_number.startswith('TURNO-'))
        self.assertEqual(shift.status, PosRegisterShift.Status.OPEN)
        self.assertEqual(shift.opening_cash_nio, Decimal('1500.00'))
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('1500.00'))
        self.assertIsNone(shift.closed_at)
        self.assertEqual(shift.audit_hash, '')

        # Intento de abrir un segundo turno para el mismo cajero -> Bloqueo ValidationError
        with self.assertRaises(ValidationError):
            open_pos_shift(
                organization=self.org,
                cashier=self.cashier,
                opening_cash_nio=Decimal('2000.00'),
                register_name='CAJA-MOSTRADOR-02'
            )

    def test_02_sale_auto_linking_and_live_corte_x(self):
        """
        Verifica que al emitir una venta POS, la factura se vincule automáticamente
        al turno abierto de la organización y se recalculen las métricas de Corte X en vivo.
        """
        shift = open_pos_shift(
            organization=self.org,
            cashier=self.cashier,
            opening_cash_nio=Decimal('1000.00'),
            register_name='CAJA-MOSTRADOR-01'
        )

        # Ejecutar venta en efectivo de 2 unidades a C$ 100.00 + IVA = C$ 230.00
        invoice = execute_pos_sale(
            organization=self.org,
            user=self.cashier,
            warehouse=self.warehouse,
            cart_items=[{
                'item_id': str(self.item.id),
                'quantity': Decimal('2.00'),
                'price_tier': 'RETAIL',
                'batch_id': None,
            }],
            account=self.client_account,
            payment_method=PaymentReceipt.PaymentMethod.CASH,
            cash_received=Decimal('230.00'),
            notes='Venta mostrador vinculada a turno'
        )

        # Verificar vinculación automática al turno abierto
        self.assertEqual(invoice.pos_shift_id, shift.id)

        # Recalcular métricas de Corte X
        metrics = compute_shift_x_cut_metrics(shift)
        self.assertEqual(metrics['transactions_count'], 1)
        self.assertGreater(metrics['system_cash_sales_nio'], Decimal('0.00'))
        self.assertEqual(
            metrics['expected_cash_in_drawer_nio'],
            shift.opening_cash_nio + metrics['system_cash_sales_nio']
        )

    def test_03_close_pos_shift_z_cut_and_strict_immutability(self):
        """
        Verifica el Arqueo Ciego y Corte Z definitivo:
        1. Cuadre exacto (cash_difference_nio == 0.00).
        2. Generación del sello criptográfico SHA-256 de 64 caracteres.
        3. Bloqueo estricto de edición (save) y eliminación (delete) una vez cerrado.
        """
        shift = open_pos_shift(
            organization=self.org,
            cashier=self.cashier,
            opening_cash_nio=Decimal('1000.00'),
            register_name='CAJA-MOSTRADOR-01'
        )

        # Venta POS
        execute_pos_sale(
            organization=self.org,
            user=self.cashier,
            warehouse=self.warehouse,
            cart_items=[{
                'item_id': str(self.item.id),
                'quantity': Decimal('1.00'),
                'price_tier': 'RETAIL',
                'batch_id': None,
            }],
            account=self.client_account,
            payment_method=PaymentReceipt.PaymentMethod.CASH,
            cash_received=Decimal('115.00'),
        )

        metrics = compute_shift_x_cut_metrics(shift)
        expected_total = metrics['expected_cash_in_drawer_nio']

        # Cajero declara el monto esperado (arqueo cuadrado)
        closed_shift = close_pos_shift_z_cut(
            shift=shift,
            declared_cash_nio=expected_total,
            user=self.cashier,
            notes='Arqueo cuadrado al 100%'
        )

        self.assertEqual(closed_shift.status, PosRegisterShift.Status.CLOSED_Z)
        self.assertIsNotNone(closed_shift.closed_at)
        self.assertEqual(closed_shift.cash_difference_nio, Decimal('0.00'))
        self.assertEqual(len(closed_shift.audit_hash), 64)

        # REGLA DE INMUTABILIDAD: Modificar o borrar el turno cerrado debe lanzar ValidationError
        closed_shift.closing_notes = 'Intento de modificar notas en turno sellado'
        with self.assertRaises(ValidationError):
            closed_shift.save()

        with self.assertRaises(ValidationError):
            closed_shift.delete()

    def test_04_corte_z_discrepancy_calculation(self):
        """
        Verifica el cálculo de faltante y sobrante en el arqueo de gaveta.
        """
        shift = open_pos_shift(
            organization=self.org,
            cashier=self.cashier,
            opening_cash_nio=Decimal('2000.00'),
            register_name='CAJA-01'
        )

        # Faltante: Se esperaban 2,000 pero cajero declara 1,850 (-150)
        closed = close_pos_shift_z_cut(
            shift=shift,
            declared_cash_nio=Decimal('1850.00'),
            user=self.cashier,
            notes='Faltante detectado'
        )
        self.assertEqual(closed.cash_difference_nio, Decimal('-150.00'))

    def test_05_views_and_printable_z_report(self):
        """
        Verifica la navegación a la consola de turnos POS y el renderizado
        del reporte imprimible oficial de Corte Z con firmas y sello SHA-256.
        """
        self.client.force_login(self.cashier)

        # 1. Consola de turnos (GET)
        console_url = reverse('billing:pos_shifts')
        resp_console = self.client.get(console_url)
        self.assertEqual(resp_console.status_code, 200)

        # 2. Abrir turno vía POST
        resp_open = self.client.post(console_url, {
            'action': 'open_shift',
            'opening_cash_nio': '1200.00',
            'register_name': 'CAJA MOSTRADOR 01'
        })
        self.assertEqual(resp_open.status_code, 302)

        active_shift = PosRegisterShift.objects.filter(
            organization=self.org,
            status=PosRegisterShift.Status.OPEN
        ).first()
        self.assertIsNotNone(active_shift)

        # 3. Vista de impresión oficial de Corte Z (GET)
        print_url = reverse('billing:pos_shift_print_z', kwargs={'pk': str(active_shift.pk)})
        resp_print = self.client.get(print_url)
        self.assertEqual(resp_print.status_code, 200)
        self.assertIn('Acta Oficial de Arqueo y Corte Fiscal Z', resp_print.content.decode('utf-8'))

    def test_06_pwa_infrastructure_endpoints(self):
        """
        Verifica que los endpoints PWA para técnicos y cajeros respondan adecuadamente:
        - GET /manifest.json (application/json o manifest+json, status 200)
        - GET /service-worker.js (application/javascript, cabecera Service-Worker-Allowed: /)
        - GET /offline/ (Contingencia de campo Ley 618)
        """
        # 1. Manifest
        resp_manifest = self.client.get(reverse('pwa_manifest'))
        self.assertEqual(resp_manifest.status_code, 200)
        self.assertIn('application/manifest+json', resp_manifest['Content-Type'])
        manifest_data = json.loads(resp_manifest.content.decode('utf-8'))
        self.assertEqual(manifest_data.get('short_name'), 'ERP SAS')
        self.assertEqual(manifest_data.get('display'), 'standalone')

        # 2. Service Worker
        resp_sw = self.client.get(reverse('pwa_service_worker'))
        self.assertEqual(resp_sw.status_code, 200)
        self.assertIn('application/javascript', resp_sw['Content-Type'])
        self.assertEqual(resp_sw['Service-Worker-Allowed'], '/')

        # 3. Fallback Offline
        resp_offline = self.client.get(reverse('pwa_offline'))
        self.assertEqual(resp_offline.status_code, 200)
        self.assertIn('Modo Contingencia Offline', resp_offline.content.decode('utf-8'))
        self.assertIn('Ley 618', resp_offline.content.decode('utf-8'))


class D02ClientCollectionBankIsolationTestCase(TestCase):
    """
    Casos de prueba de regresión para Defecto D-02:
    Verifica que InvoiceRegisterPaymentView restrinja estrictamente la cuenta bancaria
    a la organización activa mediante BankAccount.objects.for_org(request.organization),
    habiendo eliminado el fallback global (BankAccount.objects.filter(pk=bank_account_id).first()).

    Garantiza:
    1. Cobro legítimo con cuenta bancaria válida de la organización activa.
    2. Rechazo ante cuenta bancaria inexistente con ValidationError ("La cuenta bancaria seleccionada no es válida.").
    3. Rechazo estricto ante cuenta perteneciente a otra organización (cero fuga de datos ni confirmación de existencia).
    4. Bloqueo para usuario sin contexto de organización activa.
    5. Cero creación de cobros (PaymentReceipt), transacciones (FinancialTransaction) ni pólizas
       (JournalEntry) ante entradas inválidas (atomicidad garantizada).
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

        # Organización A (Activa)
        self.org_a = Organization.objects.create(
            name="Corporación Constructora S.A.",
            legal_name="Corporación Constructora de Nicaragua S.A.",
            slug="corp-constructora-ni",
            tax_id="J0310000777111",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['crm', 'billing', 'finance'],
            is_active=True,
        )

        # Organización B (Invasora / Externa)
        self.org_b = Organization.objects.create(
            name="Entidad Externa S.A.",
            legal_name="Entidad Externa Multiempresa S.A.",
            slug="entidad-externa-ni",
            tax_id="J0310000777222",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['crm', 'billing', 'finance'],
            is_active=True,
        )

        ensure_default_chart_of_accounts(self.org_a)
        ensure_default_chart_of_accounts(self.org_b)

        # Usuario Comercial en Org A
        self.user_commercial = User.objects.create_user(
            email='comercial@constructora.ni',
            full_name='Lic. Roberto Blandón',
            password='TestPassword2026*',
        )
        self.membership_a = OrganizationMembership.objects.create(
            user=self.user_commercial,
            organization=self.org_a,
            role_level=RoleLevel.COMMERCIAL_MGR,
            is_active=True,
            is_default_org=True,
        )

        # Usuario sin organización
        self.user_no_org = User.objects.create_user(
            email='huerfano@constructora.ni',
            full_name='Usuario Sin Tenant',
            password='TestPassword2026*',
        )

        # Cuentas bancarias
        self.bank_org_a = BankAccount.objects.create(
            organization=self.org_a,
            bank_name=BankAccount.BankName.BAC,
            account_number='001-998877-01',
            currency='NIO',
            current_balance=Decimal('10000.00'),
            is_active=True,
        )

        self.bank_org_b = BankAccount.objects.create(
            organization=self.org_b,
            bank_name=BankAccount.BankName.BANPRO,
            account_number='002-334455-02',
            currency='NIO',
            current_balance=Decimal('50000.00'),
            is_active=True,
        )

        # Cliente y Factura en Org A
        self.client_account = Account.objects.create(
            organization=self.org_a,
            code='CLI-D02-001',
            commercial_name='Cliente Obras Civiles S.A.',
            tax_id='J0310000555444',
            is_tax_exempt=False,
        )

        self.invoice = Invoice.objects.create(
            organization=self.org_a,
            account=self.client_account,
            status=Invoice.Status.ISSUED,
            created_by=self.user_commercial,
            currency_code='NIO',
        )
        InvoiceItem.objects.create(
            organization=self.org_a,
            invoice=self.invoice,
            description="Supervisión Técnica de Obras Civiles",
            quantity=Decimal('1.00'),
            unit_price=Decimal('2000.00'),
        )
        self.invoice.recalculate_totals()
        self.invoice.save()

    def test_d02_01_valid_bank_account_of_active_org_succeeds(self):
        """1. Cuenta bancaria válida de la organización activa: registra cobro exitosamente."""
        self.client.force_login(self.user_commercial)
        session = self.client.session
        session['active_org_id'] = str(self.org_a.id)
        session.save()

        payment_url = reverse('billing:register_payment', kwargs={'pk': self.invoice.pk})
        response = self.client.post(payment_url, {
            'amount': '1000.00',
            'currency_paid': 'NIO',
            'payment_method': PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            'bank_account_id': str(self.bank_org_a.id),
            'reference_number': 'TRX-BAC-2026-001',
            'notes': 'Cobro parcial legítimo',
        }, follow=True)

        self.assertEqual(response.status_code, 200)

        # Verificar emisión del Recibo Oficial de Caja
        receipt = PaymentReceipt.objects.filter(organization=self.org_a, invoice=self.invoice).first()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.amount_in_payment_currency, Decimal('1000.00'))
        self.assertEqual(receipt.amount_applied_to_invoice, Decimal('1000.00'))

        # Verificar incremento en saldo de la cuenta bancaria de la organización activa
        self.bank_org_a.refresh_from_db()
        self.assertEqual(self.bank_org_a.current_balance, Decimal('11000.00'))

        # Verificar transacción bancaria y actualización de saldo en factura
        trx = FinancialTransaction.objects.filter(organization=self.org_a, bank_account=self.bank_org_a).first()
        self.assertIsNotNone(trx)
        self.assertEqual(trx.amount, Decimal('1000.00'))

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.amount_paid, Decimal('1000.00'))

    def test_d02_02_inexistent_bank_account_fails_validation_no_partial_entries(self):
        """2. Cuenta bancaria inexistente: falla validación y no crea cobros ni asientos parciales."""
        self.client.force_login(self.user_commercial)
        session = self.client.session
        session['active_org_id'] = str(self.org_a.id)
        session.save()

        initial_receipts = PaymentReceipt.objects.count()
        initial_trxs = FinancialTransaction.objects.count()
        initial_entries = JournalEntry.objects.count()
        initial_balance = self.bank_org_a.current_balance

        payment_url = reverse('billing:register_payment', kwargs={'pk': self.invoice.pk})
        response = self.client.post(payment_url, {
            'amount': '500.00',
            'currency_paid': 'NIO',
            'payment_method': PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            'bank_account_id': '00000000-0000-0000-0000-000000000000',
            'reference_number': 'TRX-FAIL-001',
        }, follow=True)

        self.assertEqual(response.status_code, 200)

        # Comprobar mensaje de error en la sesión/respuesta
        messages = list(response.context['messages']) if (hasattr(response, 'context') and response.context and 'messages' in response.context) else []
        self.assertTrue(any("La cuenta bancaria seleccionada no es válida." in str(m) for m in messages))

        # Integridad transaccional: cero registros parciales creados
        self.assertEqual(PaymentReceipt.objects.count(), initial_receipts)
        self.assertEqual(FinancialTransaction.objects.count(), initial_trxs)
        self.assertEqual(JournalEntry.objects.count(), initial_entries)

        self.bank_org_a.refresh_from_db()
        self.assertEqual(self.bank_org_a.current_balance, initial_balance)

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.amount_paid, Decimal('0.00'))

    def test_d02_03_foreign_organization_bank_account_rejected_without_leakage(self):
        """3. Cuenta perteneciente a otra organización: rechazada estrictamente sin revelar existencia."""
        self.client.force_login(self.user_commercial)
        session = self.client.session
        session['active_org_id'] = str(self.org_a.id)
        session.save()

        initial_receipts = PaymentReceipt.objects.count()
        initial_trxs = FinancialTransaction.objects.count()
        initial_entries = JournalEntry.objects.count()
        foreign_initial_balance = self.bank_org_b.current_balance

        payment_url = reverse('billing:register_payment', kwargs={'pk': self.invoice.pk})
        response = self.client.post(payment_url, {
            'amount': '500.00',
            'currency_paid': 'NIO',
            'payment_method': PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            'bank_account_id': str(self.bank_org_b.id),
            'reference_number': 'TRX-EXPLOIT-001',
        }, follow=True)

        self.assertEqual(response.status_code, 200)

        # El mensaje no debe confirmar ni revelar existencia de la cuenta ajena
        messages = list(response.context['messages']) if (hasattr(response, 'context') and response.context and 'messages' in response.context) else []
        self.assertTrue(any("La cuenta bancaria seleccionada no es válida." in str(m) for m in messages))
        # Garantizar que no se utilizó el fallback global ni se expusieron detalles del otro tenant
        for m in messages:
            self.assertNotIn("Entidad Externa", str(m))
            self.assertNotIn(str(self.org_b.id), str(m))

        # Integridad transaccional y financiera: saldo de cuenta ajena intocado
        self.assertEqual(PaymentReceipt.objects.count(), initial_receipts)
        self.assertEqual(FinancialTransaction.objects.count(), initial_trxs)
        self.assertEqual(JournalEntry.objects.count(), initial_entries)

        self.bank_org_b.refresh_from_db()
        self.assertEqual(self.bank_org_b.current_balance, foreign_initial_balance)

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.amount_paid, Decimal('0.00'))

    def test_d02_04_user_without_active_organization_blocked(self):
        """4. Usuario sin organización activa es bloqueado antes de procesar cobros."""
        self.client.force_login(self.user_no_org)
        payment_url = reverse('billing:register_payment', kwargs={'pk': self.invoice.pk})
        response = self.client.post(payment_url, {
            'amount': '500.00',
            'currency_paid': 'NIO',
            'payment_method': PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            'bank_account_id': str(self.bank_org_a.id),
            'reference_number': 'TRX-ORPHAN-001',
        })
        # Middleware o mixin redirige al onboarding/403
        self.assertIn(response.status_code, [302, 403])
        self.assertEqual(PaymentReceipt.objects.filter(invoice=self.invoice).count(), 0)
