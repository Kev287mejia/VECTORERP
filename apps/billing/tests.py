import json
import threading
import time
import uuid
from decimal import Decimal
import datetime
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
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

    def test_d05_convert_quote_to_invoice_rejects_draft(self):
        """
        [D-05] Verifica que una cotización en estado DRAFT sea rechazada en backend
        y no genere factura fiscal ni cuenta por cobrar.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Propuesta Preliminar en Borrador",
            status=Quote.Status.DRAFT,
            currency_code='NIO',
            currency_symbol='C$',
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-PRE',
            description='Servicio preliminar',
            quantity=Decimal('1.00'),
            unit_price=Decimal('5000.00'),
        )

        self.client.force_login(self.commercial_mgr)
        url = reverse('billing:from_quote', kwargs={'quote_pk': quote.pk})
        response = self.client.post(url)
        self.assertRedirects(response, reverse('crm:quote_detail', kwargs={'pk': quote.pk}))

        self.assertFalse(Invoice.objects.filter(source_quote=quote).exists())

    def test_d05_convert_quote_to_invoice_rejects_sent(self):
        """
        [D-05] Verifica que una cotización en estado SENT (enviada pero no aprobada)
        sea rechazada en backend y no genere factura fiscal.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Propuesta Enviada al Cliente",
            status=Quote.Status.SENT,
            currency_code='NIO',
            currency_symbol='C$',
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-SNT',
            description='Servicio enviado',
            quantity=Decimal('1.00'),
            unit_price=Decimal('8000.00'),
        )

        self.client.force_login(self.commercial_mgr)
        url = reverse('billing:from_quote', kwargs={'quote_pk': quote.pk})
        response = self.client.post(url)
        self.assertRedirects(response, reverse('crm:quote_detail', kwargs={'pk': quote.pk}))

        self.assertFalse(Invoice.objects.filter(source_quote=quote).exists())

    def test_d05_convert_quote_to_invoice_rejects_rejected(self):
        """
        [D-05] Verifica que una cotización en estado REJECTED sea rechazada en backend
        y no genere factura fiscal.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Propuesta Rechazada por Cliente",
            status=Quote.Status.REJECTED,
            currency_code='NIO',
            currency_symbol='C$',
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-REJ',
            description='Servicio rechazado',
            quantity=Decimal('1.00'),
            unit_price=Decimal('12000.00'),
        )

        self.client.force_login(self.commercial_mgr)
        url = reverse('billing:from_quote', kwargs={'quote_pk': quote.pk})
        response = self.client.post(url)
        self.assertRedirects(response, reverse('crm:quote_detail', kwargs={'pk': quote.pk}))

        self.assertFalse(Invoice.objects.filter(source_quote=quote).exists())

    def test_d05_convert_quote_to_invoice_rejects_expired(self):
        """
        [D-05] Verifica que una cotización en estado EXPIRED sea rechazada en backend
        y no genere factura fiscal.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Propuesta Vencida",
            status=Quote.Status.EXPIRED,
            currency_code='NIO',
            currency_symbol='C$',
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-EXP',
            description='Servicio vencido',
            quantity=Decimal('1.00'),
            unit_price=Decimal('6000.00'),
        )

        self.client.force_login(self.commercial_mgr)
        url = reverse('billing:from_quote', kwargs={'quote_pk': quote.pk})
        response = self.client.post(url)
        self.assertRedirects(response, reverse('crm:quote_detail', kwargs={'pk': quote.pk}))

        self.assertFalse(Invoice.objects.filter(source_quote=quote).exists())

    def test_d05_convert_quote_to_invoice_unauthorized_role_forbidden(self):
        """
        [D-05] Verifica que un usuario con rol inferior a COMMERCIAL_MGR (ej. Coordinador Operativo nivel 80)
        reciba HTTP 403 Forbidden y no genere factura fiscal ni cuenta por cobrar.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Cotización Aprobada",
            status=Quote.Status.APPROVED,
            currency_code='NIO',
            currency_symbol='C$',
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-SEC',
            description='Servicio con restricción de rol',
            quantity=Decimal('1.00'),
            unit_price=Decimal('4000.00'),
        )

        self.client.force_login(self.ops_coord)
        url = reverse('billing:from_quote', kwargs={'quote_pk': quote.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

        self.assertFalse(Invoice.objects.filter(source_quote=quote).exists())

    def test_d05_convert_quote_to_invoice_tenant_isolation_404(self):
        """
        [D-05] Verifica que intentar convertir una cotización perteneciente a otra organización
        retorne HTTP 404 Not Found y preserve el aislamiento multi-tenant.
        """
        account_other = Account.objects.create(
            organization=self.org_other,
            code='CLI-OTH-01',
            commercial_name='Cliente Invasor S.A.',
            legal_name='Cliente Invasor S.A.',
            tax_id='J0310000888888',
        )
        quote_other = Quote.objects.create(
            organization=self.org_other,
            account=account_other,
            title="Cotización Organización Ajena",
            status=Quote.Status.APPROVED,
            currency_code='NIO',
            currency_symbol='C$',
            created_by=self.commercial_mgr,
        )

        self.client.force_login(self.commercial_mgr)
        url = reverse('billing:from_quote', kwargs={'quote_pk': quote_other.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

        self.assertFalse(Invoice.objects.filter(source_quote=quote_other).exists())

    def test_d05_convert_quote_to_invoice_prevents_duplicate_active_invoice(self):
        """
        [D-05] Verifica que una cotización aprobada no pueda convertirse dos veces
        en facturas activas duplicadas.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Cotización Única Aprobada",
            status=Quote.Status.APPROVED,
            currency_code='NIO',
            currency_symbol='C$',
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-DUP',
            description='Servicio único',
            quantity=Decimal('1.00'),
            unit_price=Decimal('10000.00'),
        )

        self.client.force_login(self.commercial_mgr)
        url = reverse('billing:from_quote', kwargs={'quote_pk': quote.pk})

        # Primera conversión: exitosa
        resp1 = self.client.post(url)
        self.assertEqual(resp1.status_code, 302)
        inv1 = Invoice.objects.get(source_quote=quote)
        self.assertRedirects(resp1, reverse('billing:invoice_detail', kwargs={'pk': inv1.pk}))

        # Segunda conversión: previene duplicado y redirige a la factura existente
        resp2 = self.client.post(url)
        self.assertEqual(resp2.status_code, 302)
        self.assertRedirects(resp2, reverse('billing:invoice_detail', kwargs={'pk': inv1.pk}))

        # Confirmar que solo existe 1 factura en BD
        self.assertEqual(Invoice.objects.filter(source_quote=quote).count(), 1)

    def test_d05_convert_quote_to_invoice_rejects_without_items(self):
        """
        [D-05-B] Rechaza la conversión a factura cuando la cotización no contenga partidas facturables.
        - No crea facturas vacías en base de datos.
        - No genera saldos, movimientos contables ni registros parciales.
        - Redirige al detalle de la cotización con mensaje de error informativo.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Cotización Sin Partidas",
            status=Quote.Status.APPROVED,
            currency_code='NIO',
            currency_symbol='C$',
            created_by=self.commercial_mgr,
        )
        self.assertEqual(quote.items.count(), 0)

        initial_invoices_count = Invoice.objects.count()
        initial_items_count = InvoiceItem.objects.count()

        self.client.force_login(self.commercial_mgr)
        url = reverse('billing:from_quote', kwargs={'quote_pk': quote.pk})
        response = self.client.post(url)

        self.assertRedirects(response, reverse('crm:quote_detail', kwargs={'pk': quote.pk}))

        # No se creó ninguna factura ni partida en BD
        self.assertFalse(Invoice.objects.filter(source_quote=quote).exists())
        self.assertEqual(Invoice.objects.count(), initial_invoices_count)
        self.assertEqual(InvoiceItem.objects.count(), initial_items_count)

    def test_d05_convert_quote_to_invoice_allows_reinvoicing_after_void(self):
        """
        [D-05-C] Permite una nueva facturación cuando la factura anterior vinculada
        a la cotización esté en estado VOID, siempre que la cotización continúe aprobada.
        - Una factura activa previa impide la conversión.
        - Al anularse (VOID), la cotización aprobada puede volver a convertirse.
        - Si la cotización no está aprobada (ej. REJECTED), la presencia de VOID no permite facturar.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Cotización para Refacturación",
            status=Quote.Status.APPROVED,
            currency_code='NIO',
            currency_symbol='C$',
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-REINV',
            description='Servicio para ciclo de anulación y refacturación',
            quantity=Decimal('1.00'),
            unit_price=Decimal('15000.00'),
        )

        self.client.force_login(self.commercial_mgr)
        url = reverse('billing:from_quote', kwargs={'quote_pk': quote.pk})

        # Paso 1: Primera conversión exitosa -> Factura 1 (ISSUED)
        resp1 = self.client.post(url)
        self.assertEqual(resp1.status_code, 302)
        inv1 = Invoice.objects.get(source_quote=quote)
        self.assertEqual(inv1.status, Invoice.Status.ISSUED)

        # Paso 2: Mientras inv1 esté activa (ISSUED), un segundo intento es bloqueado y redirige a inv1
        resp_blocked = self.client.post(url)
        self.assertRedirects(resp_blocked, reverse('billing:invoice_detail', kwargs={'pk': inv1.pk}))
        self.assertEqual(Invoice.objects.filter(source_quote=quote).count(), 1)

        # Paso 3: Se anula la factura inicial (cambio legítimo de estado a VOID)
        inv1.status = Invoice.Status.VOID
        inv1.save(update_fields=['status'])

        # Paso 4: Con la cotización aún APROBADA y la factura anterior en VOID,
        # se permite la refacturación creando una nueva Factura 2 activa (ISSUED)
        resp2 = self.client.post(url)
        self.assertEqual(resp2.status_code, 302)

        # En BD existen ahora 2 facturas vinculadas a la cotización: 1 VOID y 1 ISSUED
        all_invoices = Invoice.objects.filter(source_quote=quote).order_by('created_at')
        self.assertEqual(all_invoices.count(), 2)

        inv2 = all_invoices.last()
        self.assertNotEqual(inv1.pk, inv2.pk)
        self.assertEqual(inv2.status, Invoice.Status.ISSUED)
        self.assertRedirects(resp2, reverse('billing:invoice_detail', kwargs={'pk': inv2.pk}))

        # Exactamente 1 factura activa (ISSUED, PARTIAL o PAID)
        active_invoices = Invoice.objects.filter(
            source_quote=quote,
            status__in=[Invoice.Status.ISSUED, Invoice.Status.PARTIAL, Invoice.Status.PAID]
        )
        self.assertEqual(active_invoices.count(), 1)
        self.assertEqual(active_invoices.first().pk, inv2.pk)

        # Paso 5: Caso negativo: si se anula la 2da factura pero la cotización pasa a REJECTED,
        # la conversión es rechazada independientemente del historial VOID
        inv2.status = Invoice.Status.VOID
        inv2.save(update_fields=['status'])
        quote.status = Quote.Status.REJECTED
        quote.save(update_fields=['status'])

        resp3 = self.client.post(url)
        self.assertRedirects(resp3, reverse('crm:quote_detail', kwargs={'pk': quote.pk}))
        self.assertEqual(Invoice.objects.filter(source_quote=quote).count(), 2)

    def test_d05_convert_quote_to_invoice_atomic_rollback_on_failure(self):
        """
        [D-05] Verifica que un fallo durante la conversión ejecute un rollback atómico
        sin dejar facturas huérfanas o registros parciales.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Cotización con Fallo Simulado",
            status=Quote.Status.APPROVED,
            currency_code='NIO',
            currency_symbol='C$',
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-ERR',
            description='Servicio con error',
            quantity=Decimal('1.00'),
            unit_price=Decimal('7000.00'),
        )

        self.client.force_login(self.commercial_mgr)
        url = reverse('billing:from_quote', kwargs={'quote_pk': quote.pk})

        from unittest.mock import patch
        with patch.object(Invoice, 'recalculate_totals', side_effect=RuntimeError("Fallo simulado en motor financiero")):
            with self.assertRaises(RuntimeError):
                self.client.post(url)

        # Confirmar que no se creó factura ni partidas en base de datos
        self.assertFalse(Invoice.objects.filter(source_quote=quote).exists())
        self.assertEqual(InvoiceItem.objects.filter(code='SRV-ERR').count(), 0)

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

    def test_d05_convert_work_order_to_invoice_prevents_duplicate_active_invoice_of_quote(self):
        """
        [D-05] Verifica que una orden de trabajo vinculada a una cotización que ya posee
        una factura activa no genere una segunda factura activa y redirija a la factura existente.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Mantenimiento Subestaciones Eléctricas",
            status=Quote.Status.APPROVED,
            currency_code='NIO',
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-ELEC',
            description='Servicio Eléctrico',
            quantity=Decimal('2.00'),
            unit_price=Decimal('10000.00'),
        )

        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            source_quote=quote,
            title="OT Eléctrica",
            phase=WorkOrder.Phase.COMPLETED,
            created_by=self.commercial_mgr,
        )
        WorkOrderItem.objects.create(
            organization=self.org,
            work_order=wo,
            item_type='SERVICE',
            code='SRV-ELEC',
            description='Servicio Eléctrico',
            planned_quantity=Decimal('2.00'),
            executed_quantity=Decimal('2.00'),
        )

        self.client.force_login(self.commercial_mgr)

        # Paso 1: Convertir la cotización directamente -> Factura 1 (ISSUED)
        url_quote = reverse('billing:from_quote', kwargs={'quote_pk': quote.pk})
        resp1 = self.client.post(url_quote)
        self.assertEqual(resp1.status_code, 302)
        inv1 = Invoice.objects.get(source_quote=quote)
        self.assertEqual(inv1.status, Invoice.Status.ISSUED)

        # Paso 2: Intentar convertir la orden de trabajo vinculada a la misma cotización
        url_wo = reverse('billing:from_workorder', kwargs={'wo_pk': wo.pk})
        resp2 = self.client.post(url_wo)
        self.assertEqual(resp2.status_code, 302)
        self.assertRedirects(resp2, reverse('billing:invoice_detail', kwargs={'pk': inv1.pk}))

        # Verificar que solo existe exactamente 1 factura para la cotización en BD
        self.assertEqual(Invoice.objects.filter(source_quote=quote).count(), 1)
        self.assertFalse(Invoice.objects.filter(source_work_order=wo).exists())

    def test_d05_convert_work_order_to_invoice_allows_reinvoicing_after_void(self):
        """
        [D-05] Verifica que una orden de trabajo vinculada a una cotización cuya factura previa
        fue anulada (VOID) pueda ser facturada exitosamente si la cotización sigue aprobada.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Proyecto Renovación Líneas",
            status=Quote.Status.APPROVED,
            currency_code='NIO',
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-LIN',
            description='Tendido de líneas',
            quantity=Decimal('5.00'),
            unit_price=Decimal('8000.00'),
        )

        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            source_quote=quote,
            title="OT Tendido de Líneas",
            phase=WorkOrder.Phase.COMPLETED,
            created_by=self.commercial_mgr,
        )
        WorkOrderItem.objects.create(
            organization=self.org,
            work_order=wo,
            item_type='SERVICE',
            code='SRV-LIN',
            description='Tendido de líneas',
            planned_quantity=Decimal('5.00'),
            executed_quantity=Decimal('5.00'),
        )

        self.client.force_login(self.commercial_mgr)

        # Paso 1: Emitir primera factura desde la cotización y anularla (VOID)
        url_quote = reverse('billing:from_quote', kwargs={'quote_pk': quote.pk})
        self.client.post(url_quote)
        inv1 = Invoice.objects.get(source_quote=quote)
        inv1.status = Invoice.Status.VOID
        inv1.save(update_fields=['status'])

        # Paso 2: Facturar desde la orden de trabajo con factura previa en VOID
        url_wo = reverse('billing:from_workorder', kwargs={'wo_pk': wo.pk})
        resp = self.client.post(url_wo)
        self.assertEqual(resp.status_code, 302)

        inv2 = Invoice.objects.get(source_work_order=wo)
        self.assertEqual(inv2.status, Invoice.Status.ISSUED)
        self.assertEqual(inv2.source_quote, quote)
        self.assertRedirects(resp, reverse('billing:invoice_detail', kwargs={'pk': inv2.pk}))

        # 2 facturas en total vinculadas a la cotización (1 VOID, 1 ISSUED)
        self.assertEqual(Invoice.objects.filter(source_quote=quote).count(), 2)
        # Exactamente 1 factura activa
        self.assertEqual(
            Invoice.objects.filter(
                source_quote=quote,
                status__in=[Invoice.Status.ISSUED, Invoice.Status.PARTIAL, Invoice.Status.PAID]
            ).count(),
            1
        )

    def test_d05_convert_work_order_to_invoice_rejects_unapproved_quote(self):
        """
        [D-05] Verifica que una orden de trabajo vinculada a una cotización no aprobada (ej. DRAFT)
        no pueda convertirse en factura fiscal y redirija con error.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Propuesta en Borrador",
            status=Quote.Status.DRAFT,
            currency_code='NIO',
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-DFT',
            description='Servicio en borrador',
            quantity=Decimal('1.00'),
            unit_price=Decimal('5000.00'),
        )

        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            source_quote=quote,
            title="OT Vinculada a Borrador",
            phase=WorkOrder.Phase.COMPLETED,
            created_by=self.commercial_mgr,
        )
        WorkOrderItem.objects.create(
            organization=self.org,
            work_order=wo,
            item_type='SERVICE',
            code='SRV-DFT',
            description='Servicio en borrador',
            planned_quantity=Decimal('1.00'),
            executed_quantity=Decimal('1.00'),
        )

        self.client.force_login(self.commercial_mgr)
        url_wo = reverse('billing:from_workorder', kwargs={'wo_pk': wo.pk})
        resp = self.client.post(url_wo)

        self.assertRedirects(resp, reverse('operations:workorder_detail', kwargs={'pk': wo.pk}))
        self.assertFalse(Invoice.objects.filter(source_work_order=wo).exists())
        self.assertFalse(Invoice.objects.filter(source_quote=quote).exists())

    def test_d05_convert_work_order_to_invoice_unauthorized_role_forbidden(self):
        """
        [D-05] Verifica que un usuario con rol inferior a COMMERCIAL_MGR (ej. Coordinador Operativo nivel 80)
        reciba HTTP 403 Forbidden al intentar facturar una orden de trabajo.
        """
        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="OT Restringida",
            phase=WorkOrder.Phase.COMPLETED,
            created_by=self.commercial_mgr,
        )
        WorkOrderItem.objects.create(
            organization=self.org,
            work_order=wo,
            item_type='SERVICE',
            code='SRV-SEC',
            description='Servicio con restricción',
            planned_quantity=Decimal('1.00'),
            executed_quantity=Decimal('1.00'),
        )

        self.client.force_login(self.ops_coord)
        url = reverse('billing:from_workorder', kwargs={'wo_pk': wo.pk})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Invoice.objects.filter(source_work_order=wo).exists())

    def test_d05_convert_work_order_to_invoice_tenant_isolation_404(self):
        """
        [D-05] Verifica que intentar facturar una orden de trabajo perteneciente a otra organización
        retorne HTTP 404 Not Found.
        """
        account_other = Account.objects.create(
            organization=self.org_other,
            code='CLI-OTH-WO',
            commercial_name='Cliente Tenant Ajeno',
            tax_id='J0310000777000',
        )
        wo_other = WorkOrder.objects.create(
            organization=self.org_other,
            account=account_other,
            title="OT de Otra Empresa",
            phase=WorkOrder.Phase.COMPLETED,
            created_by=self.commercial_mgr,
        )

        self.client.force_login(self.commercial_mgr)
        url = reverse('billing:from_workorder', kwargs={'wo_pk': wo_other.pk})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(Invoice.objects.filter(source_work_order=wo_other).exists())

    def test_d05_convert_work_order_to_invoice_atomic_rollback_on_failure(self):
        """
        [D-05] Verifica que un fallo durante la conversión de una orden de trabajo ejecute un rollback atómico
        sin dejar facturas huérfanas, partidas parciales ni entradas en la bitácora de la OT.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            title="Cotización para Fallo en OT",
            status=Quote.Status.APPROVED,
            currency_code='NIO',
            created_by=self.commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type='SERVICE',
            code='SRV-FAIL',
            description='Servicio con fallo',
            quantity=Decimal('1.00'),
            unit_price=Decimal('12000.00'),
        )

        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.client_enatrel,
            source_quote=quote,
            title="OT para Fallo Simulado",
            phase=WorkOrder.Phase.COMPLETED,
            created_by=self.commercial_mgr,
        )
        WorkOrderItem.objects.create(
            organization=self.org,
            work_order=wo,
            item_type='SERVICE',
            code='SRV-FAIL',
            description='Servicio con fallo',
            planned_quantity=Decimal('1.00'),
            executed_quantity=Decimal('1.00'),
        )

        self.client.force_login(self.commercial_mgr)
        url = reverse('billing:from_workorder', kwargs={'wo_pk': wo.pk})

        from unittest.mock import patch
        with patch.object(Invoice, 'recalculate_totals', side_effect=RuntimeError("Fallo simulado en motor financiero de OT")):
            with self.assertRaises(RuntimeError):
                self.client.post(url)

        self.assertFalse(Invoice.objects.filter(source_work_order=wo).exists())
        self.assertFalse(Invoice.objects.filter(source_quote=quote).exists())
        self.assertEqual(InvoiceItem.objects.filter(code='SRV-FAIL').count(), 0)
        self.assertFalse(WorkOrderTimelineLog.objects.filter(work_order=wo, event_type='INVOICED').exists())

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


class D05QuoteToInvoiceConcurrencyTestCase(TransactionTestCase):
    """
    [D-05-A] Pruebas de Concurrencia Real Multi-Hilo sobre PostgreSQL 14 (TransactionTestCase):
    Verifica que dos transacciones concurrentes con conexiones independientes que intentan
    facturar la misma cotización aprobada queden serializadas mediante `select_for_update()`
    dentro de `transaction.atomic()`, produciendo como máximo una factura activa.
    """

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest("Prueba de concurrencia D-05 requiere PostgreSQL para validar select_for_update()")

        self.country_ni, _ = CountryConfig.objects.get_or_create(
            code='NI',
            defaults={
                'name': 'Nicaragua',
                'flag_emoji': '🇳🇮',
                'primary_currency_code': 'NIO',
                'primary_currency_symbol': 'C$',
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )
        self.org = Organization.objects.create(
            name="Constructora Concurrente D05 S.A.",
            legal_name="Constructora Concurrente D05 S.A.",
            slug=f"org-conc-d05-{uuid.uuid4().hex[:6]}",
            country=self.country_ni,
            currency='NIO',
            enabled_modules=['crm', 'billing', 'finance'],
            is_active=True,
        )
        self.commercial_mgr = User.objects.create_user(
            email=f'mgr_conc_{uuid.uuid4().hex[:4]}@vector.ni',
            password='TestPassword2026*',
            full_name='Gerente Comercial Concurrente',
        )
        OrganizationMembership.objects.create(
            organization=self.org,
            user=self.commercial_mgr,
            role_level=RoleLevel.COMMERCIAL_MGR,
            is_active=True,
        )
        self.client_account = Account.objects.create(
            organization=self.org,
            code=f"CLI-CONC-{uuid.uuid4().hex[:4].upper()}",
            commercial_name='Cliente Concurrente S.A.',
            legal_name='Cliente Concurrente S.A.',
            tax_id='J0310000777999',
        )
        self.quote = Quote.objects.create(
            organization=self.org,
            account=self.client_account,
            title="Licitación Electromecánica Concurrente",
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
            quote=self.quote,
            item_type='SERVICE',
            code='SRV-CONC-01',
            description='Montaje de transformador de potencia',
            quantity=Decimal('1.00'),
            unit_price=Decimal('50000.00'),
        )

    def test_d05_concurrent_quote_to_invoice_prevents_duplicate_active_invoice_in_postgresql(self):
        """
        Dos hilos concurrentes envían simultáneamente la solicitud POST de conversión
        de la misma cotización aprobada bajo conexiones independientes:
        - Se coordinan mediante un Barrier para disparar exactamente al mismo tiempo.
        - select_for_update() serializa la sección crítica en PostgreSQL.
        - Uno de los hilos crea la factura fiscal (ISSUED) exitosamente.
        - El segundo hilo es retenido en DB hasta el commit del primero; al desbloquearse
          revalida y detecta la factura activa existente, redirigiendo a ella sin duplicar.
        - En la base de datos existe exactamente 1 factura vinculada a la cotización.
        """
        url = reverse('billing:from_quote', kwargs={'quote_pk': self.quote.pk})
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker(thread_idx):
            connections.close_all()
            try:
                client = Client()
                client.force_login(self.commercial_mgr)
                # Sincronización explícita antes de ejecutar la conversión
                barrier.wait(timeout=10)
                resp = client.post(url)
                results.append({
                    'thread': thread_idx,
                    'status_code': resp.status_code,
                    'redirect_url': getattr(resp, 'url', None),
                })
            except Exception as exc:
                errors.append({
                    'thread': thread_idx,
                    'error': str(exc),
                })
            finally:
                connections.close_all()

        t1 = threading.Thread(target=worker, args=(1,), name="Thread-Quote-Convert-1")
        t2 = threading.Thread(target=worker, args=(2,), name="Thread-Quote-Convert-2")

        t1.start()
        t2.start()

        t1.join(timeout=25)
        t2.join(timeout=25)

        connections.close_all()

        # Comprobación 1: Sin excepciones
        self.assertEqual(len(errors), 0, f"Errores en ejecución concurrente: {errors}")

        # Comprobación 2: Ambos hilos terminaron
        self.assertEqual(len(results), 2, "Ambos hilos debieron completar la llamada HTTP")

        # Comprobación 3: Ambos recibieron HTTP 302
        for r in results:
            self.assertEqual(r['status_code'], 302, f"Código de estado inesperado en hilo {r['thread']}: {r['status_code']}")

        # Comprobación 4: Resultado final en BD: Exactamente UNA factura creada
        invoices = Invoice.objects.filter(source_quote=self.quote)
        self.assertEqual(
            invoices.count(),
            1,
            f"Se esperaba exactamente 1 factura en BD, se crearon {invoices.count()}"
        )

        created_invoice = invoices.first()
        self.assertEqual(created_invoice.status, Invoice.Status.ISSUED)
        self.assertEqual(created_invoice.items.count(), 1)
        self.assertEqual(created_invoice.gross_total, Decimal('57500.00'))

        # Comprobación 5: Ambos hilos redirigen a la misma factura activa creada
        expected_redirect = reverse('billing:invoice_detail', kwargs={'pk': created_invoice.pk})
        for r in results:
            self.assertEqual(
                r['redirect_url'],
                expected_redirect,
                f"El hilo {r['thread']} debió redirigir a {expected_redirect}, redirigió a {r['redirect_url']}"
            )

    def test_d05_concurrency_lock_blocks_competing_transaction_until_commit(self):
        """
        Demuestra explícitamente el mecanismo de bloqueo en PostgreSQL:
        - El Hilo A adquiere select_for_update() sobre la cotización dentro de atomic()
          y sostiene el bloqueo temporalmente.
        - El Hilo B inicia la conversión de la cotización mediante la vista oficial
          y queda bloqueado a nivel de PostgreSQL esperando el release del bloqueo.
        - Al confirmarse el Hilo A, el Hilo B se desbloquea, revalida la cotización y
          previene la duplicación de facturas activas.
        """
        url = reverse('billing:from_quote', kwargs={'quote_pk': self.quote.pk})
        lock_held_event = threading.Event()
        proceed_commit_event = threading.Event()
        thread_b_finished_event = threading.Event()
        results_b = []
        errors = []

        def thread_a_worker():
            connections.close_all()
            try:
                with transaction.atomic():
                    # Hilo A adquiere el bloqueo FOR UPDATE sobre la misma fila de Quote
                    locked_quote = Quote.objects.select_for_update().get(pk=self.quote.pk)
                    # Notificar a Hilo B que el bloqueo en BD está activo
                    lock_held_event.set()
                    # Esperar señal para proceder con commit
                    proceed_commit_event.wait(timeout=10)
            except Exception as exc:
                errors.append(('thread_a', str(exc)))
            finally:
                connections.close_all()

        def thread_b_worker():
            connections.close_all()
            try:
                client = Client()
                client.force_login(self.commercial_mgr)
                # Esperar a que Hilo A tenga la fila bloqueada en BD
                lock_held_event.wait(timeout=10)
                # Disparar conversión: debe bloquearse en DB hasta que Hilo A libere
                resp = client.post(url)
                results_b.append(resp)
            except Exception as exc:
                errors.append(('thread_b', str(exc)))
            finally:
                thread_b_finished_event.set()
                connections.close_all()

        ta = threading.Thread(target=thread_a_worker, name="Thread-A-LockHolder")
        tb = threading.Thread(target=thread_b_worker, name="Thread-B-Converter")

        ta.start()
        # Aseguramos que Hilo A adquirió el bloqueo FOR UPDATE
        self.assertTrue(lock_held_event.wait(timeout=5), "Hilo A no pudo adquirir el bloqueo FOR UPDATE a tiempo")

        tb.start()
        # Dar tiempo a Hilo B para que alcance select_for_update() y quede bloqueado en PostgreSQL
        time.sleep(0.3)
        self.assertFalse(
            thread_b_finished_event.is_set(),
            "Hilo B debió estar bloqueado por select_for_update() en PostgreSQL mientras Hilo A retenía la transacción"
        )

        # Ahora permitir a Hilo A hacer commit y liberar el bloqueo
        proceed_commit_event.set()

        ta.join(timeout=10)
        tb.join(timeout=10)
        connections.close_all()

        self.assertEqual(len(errors), 0, f"Errores en prueba de bloqueo: {errors}")
        self.assertEqual(len(results_b), 1, "Hilo B debió completar tras liberación del bloqueo")
        self.assertEqual(results_b[0].status_code, 302)

        # Confirmar factura única creada por Hilo B (puesto que Hilo A solo sostuvo el bloqueo sin crear)
        self.assertEqual(Invoice.objects.filter(source_quote=self.quote).count(), 1)

    def test_d05_cross_concurrency_quote_vs_work_order_prevents_duplicate_active_invoice(self):
        """
        [D-05-A / Cross-Concurrency] Verifica la competencia concurrente en tiempo real
        entre ConvertQuoteToInvoiceView y ConvertWorkOrderToInvoiceView:
        - Dos hilos paralelos ejecutan simultáneamente con conexiones separadas a PostgreSQL:
          * Hilo 1: POST a /billing/from-quote/<quote_pk>/
          * Hilo 2: POST a /billing/from-workorder/<wo_pk>/
        - Sincronizados explícitamente mediante threading.Barrier(2).
        - Ambos intentan facturar la misma cotización aprobada.
        - select_for_update() serializa la sección crítica en PostgreSQL.
        - Resultado en base de datos: EXACTAMENTE UNA factura activa vinculada a la cotización.
        - Ningún hilo falla con excepción y ambos reciben HTTP 302 redirigiendo a la factura creada.
        """
        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.client_account,
            source_quote=self.quote,
            title="OT Concurrente Cruzada",
            phase=WorkOrder.Phase.COMPLETED,
            created_by=self.commercial_mgr,
        )
        WorkOrderItem.objects.create(
            organization=self.org,
            work_order=wo,
            item_type='SERVICE',
            code='SRV-CONC-01',
            description='Montaje de transformador de potencia',
            planned_quantity=Decimal('1.00'),
            executed_quantity=Decimal('1.00'),
        )

        url_quote = reverse('billing:from_quote', kwargs={'quote_pk': self.quote.pk})
        url_wo = reverse('billing:from_workorder', kwargs={'wo_pk': wo.pk})

        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker_quote():
            connections.close_all()
            try:
                client = Client()
                client.force_login(self.commercial_mgr)
                barrier.wait(timeout=10)
                resp = client.post(url_quote)
                results.append({
                    'source': 'quote',
                    'status_code': resp.status_code,
                    'redirect_url': getattr(resp, 'url', None),
                })
            except Exception as exc:
                errors.append(('quote', str(exc)))
            finally:
                connections.close_all()

        def worker_wo():
            connections.close_all()
            try:
                client = Client()
                client.force_login(self.commercial_mgr)
                barrier.wait(timeout=10)
                resp = client.post(url_wo)
                results.append({
                    'source': 'work_order',
                    'status_code': resp.status_code,
                    'redirect_url': getattr(resp, 'url', None),
                })
            except Exception as exc:
                errors.append(('work_order', str(exc)))
            finally:
                connections.close_all()

        t1 = threading.Thread(target=worker_quote, name="Thread-Cross-Quote")
        t2 = threading.Thread(target=worker_wo, name="Thread-Cross-WorkOrder")

        t1.start()
        t2.start()

        t1.join(timeout=25)
        t2.join(timeout=25)

        connections.close_all()

        # 1. Sin excepciones
        self.assertEqual(len(errors), 0, f"Errores en concurrencia cruzada: {errors}")

        # 2. Ambos hilos completaron
        self.assertEqual(len(results), 2, "Ambos hilos debieron completar la petición HTTP")

        # 3. Ambos recibieron 302
        for r in results:
            self.assertEqual(r['status_code'], 302, f"Código inesperado en {r['source']}: {r['status_code']}")

        # 4. En base de datos: EXACTAMENTE UNA factura vinculada a la cotización
        invoices = Invoice.objects.filter(source_quote=self.quote)
        self.assertEqual(
            invoices.count(),
            1,
            f"Se esperaba exactamente 1 factura en BD vinculada a la cotización, se crearon {invoices.count()}"
        )

        created_invoice = invoices.first()
        self.assertEqual(created_invoice.status, Invoice.Status.ISSUED)

        # 5. Partidas creadas exactamente una vez
        self.assertEqual(InvoiceItem.objects.filter(invoice=created_invoice).count(), 1)

        # 6. Ambos hilos redirigen al detalle de la factura creada
        expected_redirect = reverse('billing:invoice_detail', kwargs={'pk': created_invoice.pk})
        for r in results:
            self.assertEqual(
                r['redirect_url'],
                expected_redirect,
                f"El hilo {r['source']} debió redirigir a {expected_redirect}, redirigió a {r['redirect_url']}"
            )

    def test_d05_work_order_respects_quote_lock_blocks_until_commit(self):
        """
        [D-05-A / Cross-Lock] Demuestra explícitamente que la conversión desde Orden de Trabajo
        respeta el bloqueo pesimista `select_for_update()` sobre Quote cuando otra transacción
        lo retiene, esperando a su liberación antes de proceder.
        """
        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.client_account,
            source_quote=self.quote,
            title="OT Bloqueo Cruzado",
            phase=WorkOrder.Phase.COMPLETED,
            created_by=self.commercial_mgr,
        )
        WorkOrderItem.objects.create(
            organization=self.org,
            work_order=wo,
            item_type='SERVICE',
            code='SRV-CONC-01',
            description='Montaje de transformador de potencia',
            planned_quantity=Decimal('1.00'),
            executed_quantity=Decimal('1.00'),
        )

        url_wo = reverse('billing:from_workorder', kwargs={'wo_pk': wo.pk})
        lock_held_event = threading.Event()
        proceed_commit_event = threading.Event()
        thread_wo_finished_event = threading.Event()
        results_wo = []
        errors = []

        def lock_holder_worker():
            connections.close_all()
            try:
                with transaction.atomic():
                    # Bloquear pesimísticamente la Quote
                    Quote.objects.select_for_update().get(pk=self.quote.pk)
                    lock_held_event.set()
                    proceed_commit_event.wait(timeout=10)
            except Exception as exc:
                errors.append(('lock_holder', str(exc)))
            finally:
                connections.close_all()

        def wo_converter_worker():
            connections.close_all()
            try:
                client = Client()
                client.force_login(self.commercial_mgr)
                lock_held_event.wait(timeout=10)
                # Intenta facturar la OT vinculada a la Quote bloqueada
                resp = client.post(url_wo)
                results_wo.append(resp)
            except Exception as exc:
                errors.append(('wo_converter', str(exc)))
            finally:
                thread_wo_finished_event.set()
                connections.close_all()

        t_holder = threading.Thread(target=lock_holder_worker, name="Thread-Quote-LockHolder")
        t_wo = threading.Thread(target=wo_converter_worker, name="Thread-WO-Converter")

        t_holder.start()
        self.assertTrue(lock_held_event.wait(timeout=5), "No se pudo adquirir el bloqueo de Quote a tiempo")

        t_wo.start()
        time.sleep(0.3)
        # Comprobar que Hilo WO está bloqueado esperando el release de la Quote
        self.assertFalse(
            thread_wo_finished_event.is_set(),
            "Hilo WO debió estar bloqueado por select_for_update() sobre la Quote mientras la otra transacción sostenía el bloqueo"
        )

        # Liberar la transacción
        proceed_commit_event.set()

        t_holder.join(timeout=10)
        t_wo.join(timeout=10)
        connections.close_all()

        self.assertEqual(len(errors), 0, f"Errores en prueba de bloqueo cruzado: {errors}")
        self.assertEqual(len(results_wo), 1, "Hilo WO debió completar tras liberación del bloqueo")
        self.assertEqual(results_wo[0].status_code, 302)

        # Factura única creada por Hilo WO
        self.assertEqual(Invoice.objects.filter(source_quote=self.quote).count(), 1)
