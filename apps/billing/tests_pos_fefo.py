import datetime
from decimal import Decimal
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Invoice, InvoiceItem, PaymentReceipt
from apps.billing.services import execute_pos_sale
from apps.core.models import CountryConfig
from apps.crm.models import Account
from apps.inventory.models import (
    InventoryBatch,
    InventoryItem,
    StockBalance,
    StockMovement,
    Warehouse,
)
from apps.tenancy.constants import BusinessVertical, VERTICAL_PRESETS_CONFIG
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel

User = get_user_model()


class PosAndFefoVerticalTestSuite(TestCase):
    """
    Suite Integral de Pruebas Automatizadas para Paso 12.1:
    1. Conmutación en caliente de giro comercial (BusinessVertical) y adaptación del Shell/Sidebar.
    2. Motor de Punto de Venta (POS) y selección FEFO automática de lotes sanitarios.
    3. Bloqueo estricto con ValidationError de lotes vencidos (normativa MINSA).
    4. Aplicación de escalas de precio ferreteras (Detalle, Contratista, Mayorista).
    5. Vistas web del Terminal POS y Monitor de Lotes FEFO.
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

        self.owner = User.objects.create_user(
            email='director@vector-erp.ni',
            password='TestPassword2026*',
            full_name='Lic. Gabriel Morales Silva',
        )

        self.collaborator = User.objects.create_user(
            email='vendedor@vector-erp.ni',
            password='TestPassword2026*',
            full_name='Vendedor Mostrador',
        )

        self.org = Organization.objects.create(
            name="Corporación Multi-Servicios S.A.",
            legal_name="Corporación Multi-Servicios Sociedad Anónima",
            slug="corp-multiservicios",
            tax_id="J0310000998877",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['crm', 'operations', 'sst', 'inventory', 'finance', 'billing', 'purchasing'],
            business_vertical=BusinessVertical.ENGINEERING,
            is_active=True,
        )

        self.owner_membership = OrganizationMembership.objects.create(
            user=self.owner,
            organization=self.org,
            role_level=RoleLevel.OWNER_ADMIN,
            job_title='Director General',
            is_active=True,
        )

        self.collab_membership = OrganizationMembership.objects.create(
            user=self.collaborator,
            organization=self.org,
            role_level=RoleLevel.ENGINEER,
            job_title='Cajero / Vendedor',
            is_active=True,
        )

        self.warehouse = Warehouse.objects.create(
            organization=self.org,
            code='BOD-CENTRAL',
            name='Bodega Central y Mostrador',
            is_active=True,
        )

        self.client_account = Account.objects.create(
            organization=self.org,
            code='CLI-POS-01',
            commercial_name='Consumidor Final / Ventas Mostrador',
            tax_id='000-000000-0000X',
            account_type=Account.AccountType.INDIVIDUAL,
            lifecycle_stage=Account.LifecycleStage.ACTIVE,
        )

        from apps.finance.models import BankAccount
        self.bank = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number='001-FEFO-TEST-01',
            currency='NIO',
            current_balance=Decimal('100000.00'),
            is_active=True,
        )

        self.client = Client()

    def test_01_vertical_switch_to_pharmacy_activates_pos_fefo_and_hides_operations(self):
        """
        Verifica que al conmutar a PHARMACY:
        - business_vertical cambia a PHARMACY.
        - enabled_modules desactiva 'operations' y 'sst', activando 'inventory', 'billing', 'crm'.
        - Los helpers booleanos reflejan la vertical.
        - El context processor y sidebar exponen enable_pos=True, enable_batches_expiry=True y ocultan OTs/SST.
        """
        self.client.force_login(self.owner)
        switch_url = reverse('tenancy:switch_vertical')

        response = self.client.post(switch_url, {'business_vertical': 'PHARMACY'})
        self.assertEqual(response.status_code, 302)

        self.org.refresh_from_db()
        self.assertEqual(self.org.business_vertical, BusinessVertical.PHARMACY)
        self.assertTrue(self.org.is_pharmacy_vertical())
        self.assertFalse(self.org.is_hardware_vertical())
        self.assertFalse(self.org.is_engineering_vertical())

        self.assertNotIn('operations', self.org.enabled_modules)
        self.assertNotIn('sst', self.org.enabled_modules)
        self.assertIn('billing', self.org.enabled_modules)
        self.assertIn('inventory', self.org.enabled_modules)

        # Verificar respuesta de plantilla en el terminal POS
        pos_url = reverse('billing:pos_terminal')
        res_pos = self.client.get(pos_url)
        self.assertEqual(res_pos.status_code, 200)

        # Context processor
        self.assertTrue(res_pos.context['vertical_features']['enable_pos'])
        self.assertTrue(res_pos.context['vertical_features']['enable_batches_expiry'])
        self.assertFalse(res_pos.context['vertical_features']['enable_workorders_sst'])

        content = res_pos.content.decode('utf-8')
        self.assertIn('Punto de Venta (POS)', content)
        self.assertIn('Control de Lotes &amp; FEFO', content)
        # OTs y SST no deben aparecer en la navegación activa
        self.assertNotIn('/operations/work-orders/', content)
        self.assertNotIn('/sst/safety/', content)

    def test_02_switch_vertical_requires_owner_admin(self):
        """
        Verifica que un usuario colaborador (Nivel 50) no pueda conmutar el giro de industria.
        """
        self.client.force_login(self.collaborator)
        switch_url = reverse('tenancy:switch_vertical')

        response = self.client.post(switch_url, {'business_vertical': 'HARDWARE'})
        # RoleLevelRequiredMixin rejects min_role_level 100 with 403 Forbidden
        self.assertEqual(response.status_code, 403)

        self.org.refresh_from_db()
        self.assertEqual(self.org.business_vertical, BusinessVertical.ENGINEERING)

    def test_03_pos_fefo_automatic_batch_selection_and_inventory_deduction(self):
        """
        Verifica que en giro Farmacia:
        - Si no se especifica lote, execute_pos_sale seleccione automáticamente el lote
          vigente más próximo a vencer (Algoritmo FEFO).
        - Descuente las existencias tanto del lote como del StockBalance.
        - Registre el Kardex con movimiento OUT_SALE_POS y FK al lote e invoice.
        - Exente de IVA a medicamentos humanos conforme a Ley 822 Art. 127.
        - Genere el Recibo Oficial de Caja (ROC) y cierre el saldo a PAID.
        """
        self.org.business_vertical = BusinessVertical.PHARMACY
        self.org.save(update_fields=['business_vertical'])

        med_item = InventoryItem.objects.create(
            organization=self.org,
            sku='MED-AMX-500',
            name='Amoxicilina + Ác. Clavulánico 500/125mg',
            category=InventoryItem.Category.CONSUMABLE,
            unit='CAJA',
            purchase_unit='CAJA',
            conversion_factor=Decimal('1.00'),
            unit_cost=Decimal('280.00'),
            price_retail=Decimal('450.00'),
            requires_batch_control=True,
            sanitary_registration='RS-NI-2026-0412',
            active_ingredient='Amoxicilina + Ác. Clavulánico',
            laboratory_brand='Laboratorios Ramos',
            requires_prescription=True,
            is_active=True,
        )

        today = timezone.localdate()

        # Lote 1: Expira en 40 días (Debe ser seleccionado por FEFO)
        batch_soon = InventoryBatch.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            item=med_item,
            batch_number='LOT-2026-SOON-01',
            manufacturing_date=today - datetime.timedelta(days=120),
            expiration_date=today + datetime.timedelta(days=40),
            quantity_available=Decimal('20.00'),
        )

        # Lote 2: Expira en 250 días (Óptimo)
        batch_optimal = InventoryBatch.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            item=med_item,
            batch_number='LOT-2026-OPT-02',
            manufacturing_date=today - datetime.timedelta(days=30),
            expiration_date=today + datetime.timedelta(days=250),
            quantity_available=Decimal('50.00'),
        )

        StockBalance.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            item=med_item,
            quantity=Decimal('70.00'),
        )

        # Venta en mostrador de 5 cajas sin especificar lote (FEFO automático)
        invoice = execute_pos_sale(
            organization=self.org,
            user=self.owner,
            warehouse=self.warehouse,
            cart_items=[{
                'item_id': str(med_item.id),
                'quantity': Decimal('5.00'),
                'price_tier': 'RETAIL',
                'batch_id': None,
            }],
            account=self.client_account,
            payment_method='CASH',
            cash_received=Decimal('2500.00'),
            notes='Venta demostración FEFO',
        )
        receipt = getattr(invoice, 'receipt', None) or invoice.payments.first()

        self.assertIsNotNone(invoice)
        self.assertIsNotNone(receipt)
        self.assertEqual(invoice.status, Invoice.Status.PAID)
        self.assertEqual(invoice.balance_due, Decimal('0.00'))

        # Medicamento exento en Farmacia: subtotal 2,250.00, IVA 0.00, total 2,250.00
        self.assertEqual(invoice.subtotal, Decimal('2250.00'))
        self.assertEqual(invoice.tax_total, Decimal('0.00'))
        self.assertEqual(invoice.net_receivable, Decimal('2250.00'))

        # Verificación del Recibo Oficial de Caja
        self.assertEqual(receipt.amount_applied_to_invoice, Decimal('2250.00'))

        # Verificación FEFO: batch_soon debe tener 15 restantes
        batch_soon.refresh_from_db()
        self.assertEqual(batch_soon.quantity_available, Decimal('15.00'))

        # batch_optimal no debe haber sido tocado
        batch_optimal.refresh_from_db()
        self.assertEqual(batch_optimal.quantity_available, Decimal('50.00'))

        # StockBalance total debe ser 65.00
        balance = StockBalance.objects.get(organization=self.org, warehouse=self.warehouse, item=med_item)
        self.assertEqual(balance.quantity, Decimal('65.00'))

        # Movimiento de Kardex
        kardex = StockMovement.objects.filter(
            organization=self.org,
            movement_type=StockMovement.MovementType.OUT_SALE_POS,
            item=med_item,
        ).first()
        self.assertIsNotNone(kardex)
        self.assertEqual(kardex.quantity, Decimal('5.00'))
        self.assertEqual(kardex.batch, batch_soon)
        self.assertEqual(kardex.invoice, invoice)

    def test_04_pos_fefo_strictly_blocks_expired_batches(self):
        """
        Candado Sanitario MINSA:
        Verifica que si un lote está vencido (expiration_date <= today), el sistema
        lance ValidationError de forma atómica y no descuente inventario ni genere factura.
        """
        self.org.business_vertical = BusinessVertical.PHARMACY
        self.org.save(update_fields=['business_vertical'])

        med_item = InventoryItem.objects.create(
            organization=self.org,
            sku='MED-LOS-50',
            name='Losartán Potásico 50mg MK',
            category=InventoryItem.Category.CONSUMABLE,
            unit='CAJA',
            purchase_unit='CAJA',
            conversion_factor=Decimal('1.00'),
            unit_cost=Decimal('110.00'),
            price_retail=Decimal('195.00'),
            requires_batch_control=True,
            sanitary_registration='RS-NI-2025-0199',
            active_ingredient='Losartán Potásico 50mg',
            laboratory_brand='MK / Tecnoquímicas',
            is_active=True,
        )

        today = timezone.localdate()

        # Lote vencido hace 5 días
        expired_batch = InventoryBatch.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            item=med_item,
            batch_number='LOT-2025-EXP-DEAD',
            manufacturing_date=today - datetime.timedelta(days=730),
            expiration_date=today - datetime.timedelta(days=5),
            quantity_available=Decimal('15.00'),
        )
        self.assertEqual(expired_batch.expiry_status, 'EXPIRED')

        StockBalance.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            item=med_item,
            quantity=Decimal('15.00'),
        )

        # Intento de venta del lote caducado debe fallar con ValidationError
        with self.assertRaises(ValidationError) as ctx:
            execute_pos_sale(
                organization=self.org,
                user=self.owner,
                warehouse=self.warehouse,
                cart_items=[{
                    'item_id': str(med_item.id),
                    'quantity': Decimal('2.00'),
                    'price_tier': 'RETAIL',
                    'batch_id': str(expired_batch.id),
                }],
                account=self.client_account,
                payment_method='CASH',
                cash_received=Decimal('500.00'),
            )

        self.assertIn('vencido', str(ctx.exception))
        self.assertIn('MINSA', str(ctx.exception))

        # El lote debe conservar sus 15 unidades intactas
        expired_batch.refresh_from_db()
        self.assertEqual(expired_batch.quantity_available, Decimal('15.00'))

        # Ninguna factura debe haber sido creada
        self.assertEqual(Invoice.objects.filter(organization=self.org).count(), 0)

    def test_05_hardware_tiered_pricing_retail_contractor_wholesale(self):
        """
        Verifica en giro Ferretería (HARDWARE) las 3 escalas de precios:
        - RETAIL: Precio de Mostrador / Detalle.
        - CONTRACTOR: Precio Maestro de Obra / Contratista.
        - WHOLESALE: Precio Mayorista.
        - Escalado automático a Mayorista si la cantidad supera wholesale_min_qty.
        - Cálculo de IVA general del 15% para materiales.
        """
        self.org.business_vertical = BusinessVertical.HARDWARE
        self.org.save(update_fields=['business_vertical'])

        cement = InventoryItem.objects.create(
            organization=self.org,
            sku='FER-CEM-CAN',
            name='Cemento Canal 42.5kg (UGC)',
            category=InventoryItem.Category.MECHANICAL,
            unit='BOL',
            purchase_unit='BOLSA',
            conversion_factor=Decimal('1.00'),
            unit_cost=Decimal('260.00'),
            price_retail=Decimal('340.00'),
            price_contractor=Decimal('315.00'),
            price_wholesale=Decimal('295.00'),
            wholesale_min_qty=Decimal('20.00'),
            is_active=True,
        )

        StockBalance.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            item=cement,
            quantity=Decimal('200.00'),
        )

        # 1. Venta Detalle (2 bolsas @ 340 = 680 + IVA 15% 102 = 782)
        inv_retail = execute_pos_sale(
            organization=self.org,
            user=self.owner,
            warehouse=self.warehouse,
            cart_items=[{
                'item_id': str(cement.id),
                'quantity': Decimal('2.00'),
                'price_tier': 'RETAIL',
            }],
            account=self.client_account,
            payment_method='CASH',
            cash_received=Decimal('800.00'),
        )
        self.assertEqual(inv_retail.subtotal, Decimal('680.00'))
        self.assertEqual(inv_retail.tax_total, Decimal('102.00'))
        self.assertEqual(inv_retail.net_receivable, Decimal('782.00'))

        # 2. Venta Contratista (2 bolsas @ 315 = 630 + IVA 15% 94.50 = 724.50)
        inv_contractor = execute_pos_sale(
            organization=self.org,
            user=self.owner,
            warehouse=self.warehouse,
            cart_items=[{
                'item_id': str(cement.id),
                'quantity': Decimal('2.00'),
                'price_tier': 'CONTRACTOR',
            }],
            account=self.client_account,
            payment_method='CASH',
            cash_received=Decimal('750.00'),
        )
        self.assertEqual(inv_contractor.subtotal, Decimal('630.00'))
        self.assertEqual(inv_contractor.tax_total, Decimal('94.50'))
        self.assertEqual(inv_contractor.net_receivable, Decimal('724.50'))

        # 3. Venta Mayorista forzada por volumen: 25 bolsas solicitadas en 'RETAIL'
        # Al ser 25 >= wholesale_min_qty (20), se escala automáticamente a 295.00
        # 25 * 295 = 7,375.00 + IVA 15% 1,106.25 = 8,481.25
        inv_volume = execute_pos_sale(
            organization=self.org,
            user=self.owner,
            warehouse=self.warehouse,
            cart_items=[{
                'item_id': str(cement.id),
                'quantity': Decimal('25.00'),
                'price_tier': 'RETAIL',
            }],
            account=self.client_account,
            payment_method='TRANSFER',
            bank_account=self.bank,
            cash_received=Decimal('8481.25'),
        )
        self.assertEqual(inv_volume.subtotal, Decimal('7375.00'))
        self.assertEqual(inv_volume.tax_total, Decimal('1106.25'))
        self.assertEqual(inv_volume.net_receivable, Decimal('8481.25'))

        # Verificar saldo acumulado: 200 - 2 - 2 - 25 = 171
        balance = StockBalance.objects.get(organization=self.org, warehouse=self.warehouse, item=cement)
        self.assertEqual(balance.quantity, Decimal('171.00'))

    def test_06_point_of_sale_terminal_view_get_and_post(self):
        """
        Verifica la interfaz web del Terminal POS (/billing/pos/):
        - GET: Devuelve HTTP 200, catálogo JSON con precios y lotes disponibles.
        - POST: Procesa el formulario, descuenta inventario y redirige a la factura creada.
        """
        self.client.force_login(self.owner)

        item = InventoryItem.objects.create(
            organization=self.org,
            sku='PRD-TEST-POS',
            name='Artículo para Prueba POS',
            category=InventoryItem.Category.ELECTRICAL,
            unit='UND',
            unit_cost=Decimal('50.00'),
            price_retail=Decimal('100.00'),
            is_active=True,
        )
        StockBalance.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            item=item,
            quantity=Decimal('50.00'),
        )

        pos_url = reverse('billing:pos_terminal')

        # Test GET
        res_get = self.client.get(pos_url)
        self.assertEqual(res_get.status_code, 200)
        self.assertIn('PRD-TEST-POS', res_get.content.decode('utf-8'))

        # Test POST
        cart_payload = json.dumps([{
            'item_id': str(item.id),
            'quantity': 3,
            'price_tier': 'RETAIL',
            'batch_id': None,
        }])

        post_data = {
            'warehouse_id': str(self.warehouse.id),
            'account_id': str(self.client_account.id),
            'cart_items': cart_payload,
            'payment_method': 'CASH',
            'cash_received': '350.00',
            'notes': 'Venta vía formulario web POS',
        }

        res_post = self.client.post(pos_url, post_data)
        self.assertEqual(res_post.status_code, 302)

        created_invoice = Invoice.objects.filter(organization=self.org).order_by('-created_at').first()
        self.assertIsNotNone(created_invoice)
        self.assertRedirects(res_post, reverse('billing:invoice_detail', kwargs={'pk': created_invoice.pk}))

        # 300 subtotal + 45 IVA = 345
        self.assertEqual(created_invoice.subtotal, Decimal('300.00'))
        self.assertEqual(created_invoice.tax_total, Decimal('45.00'))
        self.assertEqual(created_invoice.net_receivable, Decimal('345.00'))
        self.assertEqual(created_invoice.status, Invoice.Status.PAID)

    def test_07_batch_expiry_monitor_view_get_and_post(self):
        """
        Verifica el Tablero de Control FEFO (/inventory/batches-fefo/):
        - GET: Muestra KPIs sanitarios (óptimos, próximos a vencer, vencidos).
        - POST (/inventory/batches-fefo/create/): Registra un nuevo lote en almacén.
        """
        self.client.force_login(self.owner)

        med_item = InventoryItem.objects.create(
            organization=self.org,
            sku='MED-MON-01',
            name='Medicamento Monitor FEFO',
            category=InventoryItem.Category.CONSUMABLE,
            unit='FCO',
            unit_cost=Decimal('40.00'),
            price_retail=Decimal('80.00'),
            requires_batch_control=True,
            is_active=True,
        )

        today = timezone.localdate()

        # Crear 1 lote vencido y 1 lote óptimo
        InventoryBatch.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            item=med_item,
            batch_number='LOT-EXP-MON',
            expiration_date=today - datetime.timedelta(days=1),
            quantity_available=Decimal('10.00'),
        )
        InventoryBatch.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            item=med_item,
            batch_number='LOT-OPT-MON',
            expiration_date=today + datetime.timedelta(days=180),
            quantity_available=Decimal('25.00'),
        )

        # GET Monitor FEFO
        monitor_url = reverse('inventory:batches_fefo')
        res_get = self.client.get(monitor_url)
        self.assertEqual(res_get.status_code, 200)
        self.assertIn('LOT-EXP-MON', res_get.content.decode('utf-8'))
        self.assertIn('LOT-OPT-MON', res_get.content.decode('utf-8'))
        self.assertEqual(res_get.context['expired_count'], 1)
        self.assertEqual(res_get.context['optimal_count'], 1)

        # POST Crear nuevo lote
        create_url = reverse('inventory:batch_create')
        post_data = {
            'item_id': str(med_item.id),
            'warehouse_id': str(self.warehouse.id),
            'batch_number': 'LOT-NEW-2026',
            'manufacturing_date': (today - datetime.timedelta(days=10)).isoformat(),
            'expiration_date': (today + datetime.timedelta(days=365)).isoformat(),
            'quantity': '40.00',
        }

        res_post = self.client.post(create_url, post_data)
        self.assertEqual(res_post.status_code, 302)
        self.assertRedirects(res_post, monitor_url)

        new_batch = InventoryBatch.objects.filter(
            organization=self.org,
            batch_number='LOT-NEW-2026',
        ).first()
        self.assertIsNotNone(new_batch)
        self.assertEqual(new_batch.quantity_available, Decimal('40.00'))
