import threading
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, Client
from django.urls import reverse
from django.utils import timezone

from apps.core.models import CountryConfig
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel
from apps.crm.models import Account
from apps.operations.models import Crew, WorkOrder, WorkOrderItem, WorkOrderTimelineLog
from apps.inventory.models import (
    Warehouse,
    InventoryItem,
    StockBalance,
    StockMovement,
)
from apps.inventory.services import (
    record_stock_entry,
    transfer_stock_between_warehouses,
    dispatch_stock_to_work_order,
)

User = get_user_model()


class InventoryModuleEndToEndTestCase(TestCase):
    """
    Suite de pruebas End-to-End para el Módulo de Inventario, Bodegas Móviles,
    Kardex Transaccional y Despacho a OTs (apps/inventory).
    Verifica:
    1. Ciclo Logístico Completo: Entrada -> Traslado a Unidad Móvil -> Despacho a OT.
    2. Prevención estricta de saldo negativo y protección transaccional.
    3. Inmutabilidad del Libro Mayor (StockMovement).
    4. Confidencialidad de costos y segregación de funciones RBAC (Niveles 100, 80, 70, 50 y 20).
    5. Aislamiento Multi-Tenant Estricto por Organización.
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
                'personal_id_label': 'Cédula de Identidad',
                'personal_id_regex': r'^\d{3}-\d{6}-\d{4}[A-Z]$',
                'default_tax_name': 'IVA',
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )

        # 2. Organizaciones Multi-Tenant
        self.org = Organization.objects.create(
            name="Vector Ingeniería Nicaragua, S.A.",
            legal_name="Vector Ingeniería de Centroamérica, S.A.",
            slug="vector-ni-inventory",
            tax_id="J0310000199999",
            country=self.country_ni,
            enabled_modules=['crm', 'operations', 'sst', 'inventory'],
            is_active=True,
        )

        self.org_intruder = Organization.objects.create(
            name="Constructora Invasora, S.A.",
            legal_name="Invasora S.A.",
            slug="invasora-ni-inventory",
            tax_id="J0310000188888",
            country=self.country_ni,
            enabled_modules=['inventory'],
            is_active=True,
        )

        # 3. Usuarios de Distintos Niveles Jerárquicos RBAC
        # Nivel 100: Owner / Administrador General
        self.user_owner = User.objects.create_user(
            email='owner@vector.com.ni',
            full_name='Lic. Fernando Morales (Owner)',
            password='password123'
        )
        OrganizationMembership.objects.create(
            user=self.user_owner,
            organization=self.org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )

        # Nivel 80: Coordinador de Operaciones
        self.user_ops_coord = User.objects.create_user(
            email='operaciones@vector.com.ni',
            full_name='Ing. Marcos Ruiz (Coordinador)',
            password='password123'
        )
        OrganizationMembership.objects.create(
            user=self.user_ops_coord,
            organization=self.org,
            role_level=RoleLevel.OPERATIONS_COORD,
            is_active=True,
            is_default_org=True,
        )

        # Nivel 70: Oficial SST
        self.user_sst = User.objects.create_user(
            email='sst@vector.com.ni',
            full_name='Lic. Carmen Silva (Oficial SST)',
            password='password123'
        )
        OrganizationMembership.objects.create(
            user=self.user_sst,
            organization=self.org,
            role_level=RoleLevel.SST_OFFICER,
            is_active=True,
            is_default_org=True,
        )

        # Nivel 50: Ingeniero Residente / Supervisor
        self.user_engineer = User.objects.create_user(
            email='ingeniero@vector.com.ni',
            full_name='Ing. Roberto Solís (Ingeniero)',
            password='password123'
        )
        OrganizationMembership.objects.create(
            user=self.user_engineer,
            organization=self.org,
            role_level=RoleLevel.ENGINEER,
            is_active=True,
            is_default_org=True,
        )

        # Nivel 20: Técnico de Campo
        self.user_tech = User.objects.create_user(
            email='tecnico@vector.com.ni',
            full_name='Juan Pérez (Técnico)',
            password='password123'
        )
        OrganizationMembership.objects.create(
            user=self.user_tech,
            organization=self.org,
            role_level=RoleLevel.TECHNICIAN,
            is_active=True,
            is_default_org=True,
        )

        # Usuario en Organización Invasora
        self.user_intruder = User.objects.create_user(
            email='intruder@invasora.com.ni',
            full_name='Carlos Espía',
            password='password123'
        )
        OrganizationMembership.objects.create(
            user=self.user_intruder,
            organization=self.org_intruder,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )

        # 4. Datos Base de Operaciones: Cuadrilla Alfa y Orden de Trabajo OT-2026-0001
        self.crew_alfa = Crew.objects.create(
            organization=self.org,
            code="CUAD-01",
            name="Cuadrilla Alfa - Alta Tensión",
            specialty="Media y Alta Tensión",
            leader=self.user_engineer,
            vehicle_plate="M 345-890",
            vehicle_description="Hilux 4x4 #04",
            is_active=True,
        )
        self.crew_alfa.members.add(self.user_tech)

        self.account = Account.objects.create(
            organization=self.org,
            commercial_name="Ingenio Monte Rosa",
            legal_name="Pantaleon Nicaragua S.A.",
            tax_id="J0310000112233",
            city="Chinandega",
            address="Km 135 Carretera Chinandega - Corinto",
            default_currency="NIO",
        )

        self.work_order = WorkOrder.objects.create(
            organization=self.org,
            number="OT-2026-0001",
            title="Mantenimiento Subestación Eléctrica 138kV",
            account=self.account,
            assigned_crew=self.crew_alfa,
            assigned_engineer=self.user_engineer,
            phase=WorkOrder.Phase.IN_PROGRESS,
            is_high_risk=False,
            sst_cleared=True,
        )

        # Partida planificada en la OT (BOM)
        self.wo_item = WorkOrderItem.objects.create(
            organization=self.org,
            work_order=self.work_order,
            item_type=WorkOrderItem.ItemType.MATERIAL,
            code="MAT-CAB-01",
            description="Cable de Potencia XLP 15kV Calibre 2 AWG",
            unit="MTS",
            planned_quantity=Decimal('2.00'),
            executed_quantity=Decimal('0.00'),
            is_completed=False,
        )

    def _get_client_for_user(self, user, org=None):
        """Helper para autenticar cliente HTTP con sesión de organización activa."""
        client = Client()
        client.force_login(user)
        session = client.session
        target_org = org or self.org
        session['active_org_id'] = str(target_org.id)
        session.save()
        return client

    # --------------------------------------------------------------------------
    # TEST 1: Ciclo Logístico Completo (Entrada -> Traslado Móvil -> Despacho OT)
    # --------------------------------------------------------------------------
    def test_complete_logistics_cycle_and_bom_auto_execution(self):
        """
        1) Coordinador (80) crea Bodega Central y Unidad Móvil Cuadrilla Alfa,
           da de alta el SKU MAT-CAB-01 y registra una entrada de 10.00 MTS a C$ 20,000.00 c/u.
        2) Coordinador (80) traslada 4.00 MTS de Bodega Central a Unidad Móvil Cuadrilla Alfa.
           (StockBalance: 6.00 MTS en Central, 4.00 MTS en Móvil).
        3) Ingeniero (50) despacha 2.00 MTS desde Unidad Móvil hacia OT-2026-0001.
           El saldo móvil baja a 2.00 MTS, el WorkOrderItem de la OT actualiza automáticamente
           executed_quantity = 2.00 (is_completed = True) y se crea el evento inmutable
           MATERIAL_DISPATCHED en WorkOrderTimelineLog.
        """
        client_coord = self._get_client_for_user(self.user_ops_coord)

        # 1a. Crear Bodega Central
        resp = client_coord.post(reverse('inventory:warehouse_create'), {
            'code': 'BOD-01',
            'name': 'Bodega Central Managua',
            'warehouse_type': Warehouse.WarehouseType.CENTRAL,
            'address': 'Carretera Norte Km 5.5, Managua',
        })
        self.assertEqual(resp.status_code, 302)
        wh_central = Warehouse.objects.get(organization=self.org, code='BOD-01')

        # 1b. Crear Bodega Móvil de Cuadrilla Alfa
        resp = client_coord.post(reverse('inventory:warehouse_create'), {
            'code': 'BOD-MOV-01',
            'name': 'Unidad Móvil Hilux Cuadrilla Alfa',
            'warehouse_type': Warehouse.WarehouseType.MOBILE_CREW,
            'linked_crew': str(self.crew_alfa.id),
            'address': 'En Tránsito / Furgoneta',
        })
        self.assertEqual(resp.status_code, 302)
        wh_mobile = Warehouse.objects.get(organization=self.org, code='BOD-MOV-01')
        self.assertEqual(wh_mobile.linked_crew, self.crew_alfa)

        # 1c. Dar de alta SKU MAT-CAB-01
        resp = client_coord.post(reverse('inventory:item_create'), {
            'sku': 'MAT-CAB-01',
            'name': 'Cable de Potencia XLP 15kV Calibre 2 AWG',
            'category': InventoryItem.Category.ELECTRICAL,
            'unit': 'MTS',
            'min_stock_level': '3.00',
            'unit_cost': '0.00',
        })
        self.assertEqual(resp.status_code, 302)
        item = InventoryItem.objects.get(organization=self.org, sku='MAT-CAB-01')

        # 1d. Entrada por Compra de 10.00 MTS a C$ 20,000.00
        resp = client_coord.post(reverse('inventory:stock_entry'), {
            'warehouse': str(wh_central.id),
            'item': str(item.id),
            'quantity': '10.00',
            'unit_cost': '20000.00',
            'movement_type': StockMovement.MovementType.IN_PURCHASE,
            'reference_note': 'Factura Proveedor Elvatron #8812 - Recepción Inicial',
        })
        self.assertEqual(resp.status_code, 302)

        # Validar saldos tras entrada
        bal_central = StockBalance.objects.get(organization=self.org, warehouse=wh_central, item=item)
        self.assertEqual(bal_central.quantity, Decimal('10.00'))
        item.refresh_from_db()
        self.assertEqual(item.unit_cost, Decimal('20000.00'))
        self.assertEqual(item.total_stock(), Decimal('10.00'))
        self.assertFalse(item.is_low_stock())

        kardex_1 = StockMovement.objects.get(organization=self.org, movement_type=StockMovement.MovementType.IN_PURCHASE)
        self.assertEqual(kardex_1.number, "KDX-2026-0001")
        self.assertEqual(kardex_1.balance_after, Decimal('10.00'))

        # 2. Traslado de 4.00 MTS desde Bodega Central hacia Unidad Móvil Cuadrilla Alfa
        resp = client_coord.post(reverse('inventory:stock_transfer'), {
            'source_warehouse': str(wh_central.id),
            'target_warehouse': str(wh_mobile.id),
            'item': str(item.id),
            'quantity': '4.00',
            'reference_note': 'Carga inicial para labores en Subestación Eléctrica',
        })
        self.assertEqual(resp.status_code, 302)

        bal_central.refresh_from_db()
        bal_mobile = StockBalance.objects.get(organization=self.org, warehouse=wh_mobile, item=item)
        self.assertEqual(bal_central.quantity, Decimal('6.00'))
        self.assertEqual(bal_mobile.quantity, Decimal('4.00'))
        self.assertEqual(item.total_stock(), Decimal('10.00'))  # Suma total organizacional no cambia

        # 3. Ingeniero (50) despacha 2.00 MTS desde Unidad Móvil hacia OT-2026-0001
        client_eng = self._get_client_for_user(self.user_engineer)
        resp = client_eng.post(reverse('inventory:stock_dispatch_ot'), {
            'warehouse': str(wh_mobile.id),
            'item': str(item.id),
            'work_order': str(self.work_order.id),
            'quantity': '2.00',
            'reference_note': 'Instalación de acometida subterránea 15kV',
        })
        self.assertEqual(resp.status_code, 302)

        # Verificación de saldo en unidad móvil
        bal_mobile.refresh_from_db()
        self.assertEqual(bal_mobile.quantity, Decimal('2.00'))

        # Verificación de BOM en la OT
        self.wo_item.refresh_from_db()
        self.assertEqual(self.wo_item.executed_quantity, Decimal('2.00'))
        self.assertTrue(self.wo_item.is_completed)

        # Verificación del movimiento de Kardex tipo OUT_WORKORDER
        kardex_out = StockMovement.objects.get(
            organization=self.org,
            movement_type=StockMovement.MovementType.OUT_WORKORDER
        )
        self.assertEqual(kardex_out.work_order, self.work_order)
        self.assertEqual(kardex_out.work_order_item, self.wo_item)
        self.assertEqual(kardex_out.quantity, Decimal('2.00'))
        self.assertEqual(kardex_out.balance_after, Decimal('2.00'))

        # Verificación de evento en Bitácora de la OT (WorkOrderTimelineLog)
        timeline_event = WorkOrderTimelineLog.objects.filter(
            work_order=self.work_order,
            event_type='MATERIAL_DISPATCHED'
        ).latest('created_at')
        self.assertIn("MAT-CAB-01", timeline_event.summary)
        self.assertIn("2.00 MTS", timeline_event.summary)
        self.assertIn(kardex_out.number, timeline_event.details)

    # --------------------------------------------------------------------------
    # TEST 2: Prevención Estricta de Saldo Negativo
    # --------------------------------------------------------------------------
    def test_prevent_negative_stock_balance(self):
        """
        Verifica que intentar despachar o trasladar más existencias de las disponibles
        lanza ValidationError y rechaza la transacción de manera atómica sin alterar saldos.
        """
        wh_central = Warehouse.objects.create(
            organization=self.org,
            code="BOD-NEG-01",
            name="Bodega Negativo",
            warehouse_type=Warehouse.WarehouseType.CENTRAL,
        )
        wh_target = Warehouse.objects.create(
            organization=self.org,
            code="BOD-NEG-02",
            name="Bodega Negativo Destino",
            warehouse_type=Warehouse.WarehouseType.PROJECT,
        )
        item = InventoryItem.objects.create(
            organization=self.org,
            sku="MAT-SEC-01",
            name="Seccionador Tripolar 15kV",
            unit="UND",
            unit_cost=Decimal('15000.00'),
        )

        # Ingresar 2 unidades a bodega central
        record_stock_entry(
            organization=self.org,
            warehouse=wh_central,
            item=item,
            quantity=Decimal('2.00'),
            unit_cost=Decimal('15000.00'),
            reference_note="Stock limitado",
            user=self.user_ops_coord,
        )

        bal = StockBalance.objects.get(organization=self.org, warehouse=wh_central, item=item)
        self.assertEqual(bal.quantity, Decimal('2.00'))

        # Intento de traslado de 50.00 unidades (exceso de stock)
        with self.assertRaises(ValidationError) as ctx:
            transfer_stock_between_warehouses(
                organization=self.org,
                source_warehouse=wh_central,
                target_warehouse=wh_target,
                item=item,
                quantity=Decimal('50.00'),
                reference_note="Intento inválido de sobregiro",
                user=self.user_ops_coord,
            )
        self.assertIn("Existencia insuficiente", str(ctx.exception))

        # Partida en BOM para MAT-SEC-01 (necesaria para política estricta DEF-16)
        WorkOrderItem.objects.create(
            organization=self.org,
            work_order=self.work_order,
            item_type=WorkOrderItem.ItemType.MATERIAL,
            code="MAT-SEC-01",
            description="Seccionador Tripolar 15kV",
            unit="UND",
            planned_quantity=Decimal('10.00'),
            executed_quantity=Decimal('0.00'),
        )

        # Intento de despacho a OT de 10.00 unidades (exceso de stock)
        with self.assertRaises(ValidationError) as ctx:
            dispatch_stock_to_work_order(
                organization=self.org,
                warehouse=wh_central,
                item=item,
                work_order=self.work_order,
                quantity=Decimal('10.00'),
                user=self.user_engineer,
            )
        self.assertIn("Existencia insuficiente", str(ctx.exception))

        # El saldo no debe haber cambiado
        bal.refresh_from_db()
        self.assertEqual(bal.quantity, Decimal('2.00'))

    # --------------------------------------------------------------------------
    # TEST 3: Inmutabilidad del Kardex (StockMovement)
    # --------------------------------------------------------------------------
    def test_kardex_stock_movement_immutability(self):
        """
        Verifica que una vez creado un registro en StockMovement, cualquier intento
        de actualización (.save()) o eliminación (.delete()) lanza ValidationError.
        """
        wh = Warehouse.objects.create(
            organization=self.org,
            code="BOD-INM-01",
            name="Bodega Inmutable",
            warehouse_type=Warehouse.WarehouseType.CENTRAL,
        )
        item = InventoryItem.objects.create(
            organization=self.org,
            sku="MAT-INM-01",
            name="Fusible de Expulsión 25A",
            unit="UND",
            unit_cost=Decimal('450.00'),
        )

        movement = record_stock_entry(
            organization=self.org,
            warehouse=wh,
            item=item,
            quantity=Decimal('5.00'),
            unit_cost=Decimal('450.00'),
            reference_note="Lote inicial inmutable",
            user=self.user_ops_coord,
        )

        # 1. Intento de modificación
        movement.quantity = Decimal('100.00')
        with self.assertRaises(ValidationError) as ctx:
            movement.save()
        self.assertIn("El registro de Kardex es inmutable", str(ctx.exception))

        # 2. Intento de eliminación
        with self.assertRaises(ValidationError) as ctx:
            movement.delete()
        self.assertIn("El registro de Kardex es inmutable", str(ctx.exception))

    # --------------------------------------------------------------------------
    # TEST 4: Confidencialidad de Costos y Matriz RBAC
    # --------------------------------------------------------------------------
    def test_cost_confidentiality_and_rbac_matrix(self):
        """
        Verifica:
        1. ENGINEER (50):
           - Puede consultar existencias físicas en /inventory/.
           - El HTML NUNCA expone costos unitarios ni valorización monetaria ('20000.00', '20,000.00').
           - Recibe HTTP 403 al intentar registrar entradas de stock o traslados (reservado a 80+).
        2. SST_OFFICER (70):
           - Puede consultar el almacén en /inventory/.
           - Recibe HTTP 403 al intentar despachar materiales técnicos a una OT (segregación de funciones).
        3. TECHNICIAN (20):
           - Recibe HTTP 403 inmediato al intentar acceder a /inventory/.
        """
        wh = Warehouse.objects.create(
            organization=self.org,
            code="BOD-CONF-01",
            name="Bodega Confidencial",
            warehouse_type=Warehouse.WarehouseType.CENTRAL,
        )
        item = InventoryItem.objects.create(
            organization=self.org,
            sku="MAT-CONF-01",
            name="Transformador de Distribución 25kVA",
            unit="UND",
            unit_cost=Decimal('78500.00'),
        )
        record_stock_entry(
            organization=self.org,
            warehouse=wh,
            item=item,
            quantity=Decimal('3.00'),
            unit_cost=Decimal('78500.00'),
            reference_note="Recepción Transformadores",
            user=self.user_ops_coord,
        )

        # 1. Pruebas con ENGINEER (50)
        client_eng = self._get_client_for_user(self.user_engineer)
        resp_eng = client_eng.get(reverse('inventory:dashboard'))
        self.assertEqual(resp_eng.status_code, 200)

        # Ve el SKU y cantidades físicas
        self.assertContains(resp_eng, "MAT-CONF-01")
        self.assertContains(resp_eng, "3.00")

        # NUNCA ve los costos financieros
        self.assertNotContains(resp_eng, "78500.00")
        self.assertNotContains(resp_eng, "78,500.00")
        self.assertNotContains(resp_eng, "Valorización Total")

        # Intento no autorizado de registrar entrada de stock -> HTTP 403
        resp_eng_entry = client_eng.post(reverse('inventory:stock_entry'), {
            'warehouse': str(wh.id),
            'item': str(item.id),
            'quantity': '1.00',
            'unit_cost': '78500.00',
            'movement_type': StockMovement.MovementType.IN_PURCHASE,
            'reference_note': 'Entrada por Ingeniero no permitida',
        })
        self.assertEqual(resp_eng_entry.status_code, 403)

        # 2. Pruebas con SST_OFFICER (70)
        client_sst = self._get_client_for_user(self.user_sst)
        resp_sst = client_sst.get(reverse('inventory:dashboard'))
        self.assertEqual(resp_sst.status_code, 200)
        self.assertContains(resp_sst, "MAT-CONF-01")

        # Intento de despachar materiales a una OT -> HTTP 403 (Segregación de funciones)
        resp_sst_dispatch = client_sst.post(reverse('inventory:stock_dispatch_ot'), {
            'warehouse': str(wh.id),
            'item': str(item.id),
            'work_order': str(self.work_order.id),
            'quantity': '1.00',
            'reference_note': 'Despacho intentado por SST',
        })
        self.assertEqual(resp_sst_dispatch.status_code, 403)

        # 3. Pruebas con TECHNICIAN (20) -> HTTP 403 inmediato
        client_tech = self._get_client_for_user(self.user_tech)
        resp_tech = client_tech.get(reverse('inventory:dashboard'))
        self.assertEqual(resp_tech.status_code, 403)

        # 4. Verificación con COORDINADOR (80) -> Sí ve costos y valorización
        client_coord = self._get_client_for_user(self.user_ops_coord)
        resp_coord = client_coord.get(reverse('inventory:dashboard'))
        self.assertEqual(resp_coord.status_code, 200)
        self.assertContains(resp_coord, "Valorización")
        self.assertContains(resp_coord, "78500.00")

    # --------------------------------------------------------------------------
    # TEST 5: Aislamiento Multi-Tenant Estricto por Organización
    # --------------------------------------------------------------------------
    def test_multi_tenant_isolation_in_inventory(self):
        """
        Verifica que los almacenes, existencias y movimientos de una organización
        son completamente invisibles para usuarios de otra organización.
        """
        # Crear bodega y producto en org invasora
        wh_intruder = Warehouse.objects.create(
            organization=self.org_intruder,
            code="BOD-INT-01",
            name="Bodega Invasora",
            warehouse_type=Warehouse.WarehouseType.CENTRAL,
        )
        item_intruder = InventoryItem.objects.create(
            organization=self.org_intruder,
            sku="SKU-INT-99",
            name="Material Invasor Confidencial",
            unit="UND",
            unit_cost=Decimal('100.00'),
        )

        # Crear item específico en org principal
        item_vector = InventoryItem.objects.create(
            organization=self.org,
            sku="SKU-VEC-99",
            name="Cable Primario Exclusivo Vector",
            unit="UND",
            unit_cost=Decimal('500.00'),
        )

        client_intruder = self._get_client_for_user(self.user_intruder, org=self.org_intruder)
        resp_intruder = client_intruder.get(reverse('inventory:dashboard'))
        self.assertEqual(resp_intruder.status_code, 200)

        # El invasor solo ve sus datos
        self.assertContains(resp_intruder, "SKU-INT-99")
        self.assertContains(resp_intruder, "Material Invasor Confidencial")
        self.assertNotContains(resp_intruder, "SKU-VEC-99")
        self.assertNotContains(resp_intruder, "Cable Primario Exclusivo Vector")

        # La org principal no ve los datos del invasor
        client_coord = self._get_client_for_user(self.user_ops_coord)
        resp_vector = client_coord.get(reverse('inventory:dashboard'))
        self.assertContains(resp_vector, "SKU-VEC-99")
        self.assertContains(resp_vector, "Cable Primario Exclusivo Vector")
        self.assertNotContains(resp_vector, "SKU-INT-99")
        self.assertNotContains(resp_vector, "Material Invasor Confidencial")


# ==============================================================================
# FASE 1C.28: SUITE DE INTEGRIDAD DEL DESPACHO DE MATERIALES (DEF-15..18)
# ==============================================================================

class StockDispatchIntegrityTestSuite(TestCase):
    """
    Suite de Pruebas de Regresión Focalizadas para la Integridad de Despacho a OTs:
    - DEF-15: Aislamiento multiempresa estricto (Bodega, Material, OT y Partida BOM).
    - DEF-16: Vinculación inequívoca con partida técnica del BOM (rechazo de discrepancias y ambigüedades).
    - DEF-17: Rechazo de despachos a OTs en fase COMPLETED o CANCELLED.
    - DEF-18: Integridad transaccional y rollback completo ante fallos posteriores.
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
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )

        # 2. Organización Principal (Alpha)
        self.org_a = Organization.objects.create(
            name="Alpha Corp S.A.",
            legal_name="Alpha Corp Nicaragua S.A.",
            slug="alpha-corp",
            country=self.country_ni,
            enabled_modules=['inventory', 'operations', 'crm'],
            is_active=True,
        )

        # 3. Organización Ajena (Beta / Intruso)
        self.org_b = Organization.objects.create(
            name="Beta Industrial S.A.",
            legal_name="Beta Industrial Nicaragua S.A.",
            slug="beta-industrial",
            country=self.country_ni,
            enabled_modules=['inventory', 'operations', 'crm'],
            is_active=True,
        )

        # 4. Usuarios
        self.user_eng_a = User.objects.create_user(
            email='eng_a@alphacorp.com.ni',
            full_name='Ing. Roberto Alpha',
            password='password123',
        )
        OrganizationMembership.objects.create(
            user=self.user_eng_a,
            organization=self.org_a,
            role_level=RoleLevel.ENGINEER,
            is_active=True,
            is_default_org=True,
        )

        self.user_eng_b = User.objects.create_user(
            email='eng_b@betaindustrial.com.ni',
            full_name='Ing. Carlos Beta',
            password='password123',
        )
        OrganizationMembership.objects.create(
            user=self.user_eng_b,
            organization=self.org_b,
            role_level=RoleLevel.ENGINEER,
            is_active=True,
            is_default_org=True,
        )

        # 5. Bodegas y Materiales para Org Alpha
        self.wh_a = Warehouse.objects.create(
            organization=self.org_a,
            code="BOD-ALP-01",
            name="Bodega Alpha Central",
            warehouse_type=Warehouse.WarehouseType.CENTRAL,
        )
        self.item_cable_a = InventoryItem.objects.create(
            organization=self.org_a,
            sku="SKU-CABLE-100",
            name="Cable de Potencia 15kV",
            unit="MTS",
            unit_cost=Decimal('50.00'),
        )
        record_stock_entry(
            organization=self.org_a,
            warehouse=self.wh_a,
            item=self.item_cable_a,
            quantity=Decimal('100.00'),
            unit_cost=Decimal('50.00'),
            reference_note="Stock Inicial Alpha",
            user=self.user_eng_a,
        )
        self.bal_cable_a = StockBalance.objects.get(
            organization=self.org_a, warehouse=self.wh_a, item=self.item_cable_a
        )

        # 6. Bodegas y Materiales para Org Beta
        self.wh_b = Warehouse.objects.create(
            organization=self.org_b,
            code="BOD-BET-01",
            name="Bodega Beta Central",
            warehouse_type=Warehouse.WarehouseType.CENTRAL,
        )
        self.item_cable_b = InventoryItem.objects.create(
            organization=self.org_b,
            sku="SKU-CABLE-100",
            name="Cable de Potencia Beta 15kV",
            unit="MTS",
            unit_cost=Decimal('60.00'),
        )
        record_stock_entry(
            organization=self.org_b,
            warehouse=self.wh_b,
            item=self.item_cable_b,
            quantity=Decimal('50.00'),
            unit_cost=Decimal('60.00'),
            reference_note="Stock Inicial Beta",
            user=self.user_eng_b,
        )

        # 7. Órdenes de Trabajo y Partidas BOM
        self.account_a = Account.objects.create(
            organization=self.org_a,
            commercial_name="Cliente Alpha",
            legal_name="Cliente Alpha S.A.",
        )
        self.wo_a = WorkOrder.objects.create(
            organization=self.org_a,
            number="OT-ALP-001",
            title="Mantenimiento Subestación Alpha",
            account=self.account_a,
            phase=WorkOrder.Phase.IN_PROGRESS,
            assigned_engineer=self.user_eng_a,
        )
        self.bom_cable_a = WorkOrderItem.objects.create(
            organization=self.org_a,
            work_order=self.wo_a,
            item_type=WorkOrderItem.ItemType.MATERIAL,
            code="SKU-CABLE-100",
            description="Cable de Potencia 15kV Calibre 2",
            unit="MTS",
            planned_quantity=Decimal('20.00'),
            executed_quantity=Decimal('0.00'),
            is_completed=False,
        )

        self.account_b = Account.objects.create(
            organization=self.org_b,
            commercial_name="Cliente Beta",
            legal_name="Cliente Beta S.A.",
        )
        self.wo_b = WorkOrder.objects.create(
            organization=self.org_b,
            number="OT-BET-001",
            title="Instalación Planta Beta",
            account=self.account_b,
            phase=WorkOrder.Phase.IN_PROGRESS,
            assigned_engineer=self.user_eng_b,
        )
        self.bom_cable_b = WorkOrderItem.objects.create(
            organization=self.org_b,
            work_order=self.wo_b,
            item_type=WorkOrderItem.ItemType.MATERIAL,
            code="SKU-CABLE-100",
            description="Cable de Potencia Beta",
            unit="MTS",
            planned_quantity=Decimal('10.00'),
            executed_quantity=Decimal('0.00'),
            is_completed=False,
        )

    # --------------------------------------------------------------------------
    # DEF-15: PRUEBAS DE AISLAMIENTO MULTIEMPRESA
    # --------------------------------------------------------------------------
    def test_01_warehouse_of_another_org_rejected(self):
        """Intento de despacho usando bodega perteneciente a otra organización es rechazado."""
        with self.assertRaises(ValidationError) as ctx:
            dispatch_stock_to_work_order(
                organization=self.org_a,
                warehouse=self.wh_b,  # Bodega de Org Beta
                item=self.item_cable_a,
                work_order=self.wo_a,
                quantity=Decimal('5.00'),
                user=self.user_eng_a,
            )
        self.assertIn("Aislamiento multiempresa violado: La bodega", str(ctx.exception))
        self.bal_cable_a.refresh_from_db()
        self.assertEqual(self.bal_cable_a.quantity, Decimal('100.00'))
        self.assertEqual(StockMovement.objects.count(), 2)  # Solo los 2 iniciales

    def test_02_item_of_another_org_rejected(self):
        """Intento de despacho usando ítem perteneciente a otra organización es rechazado."""
        with self.assertRaises(ValidationError) as ctx:
            dispatch_stock_to_work_order(
                organization=self.org_a,
                warehouse=self.wh_a,
                item=self.item_cable_b,  # Ítem de Org Beta
                work_order=self.wo_a,
                quantity=Decimal('5.00'),
                user=self.user_eng_a,
            )
        self.assertIn("Aislamiento multiempresa violado: El material", str(ctx.exception))
        self.bal_cable_a.refresh_from_db()
        self.assertEqual(self.bal_cable_a.quantity, Decimal('100.00'))

    def test_03_work_order_of_another_org_rejected(self):
        """Intento de despacho asignando una OT de otra organización es rechazado."""
        with self.assertRaises(ValidationError) as ctx:
            dispatch_stock_to_work_order(
                organization=self.org_a,
                warehouse=self.wh_a,
                item=self.item_cable_a,
                work_order=self.wo_b,  # OT de Org Beta
                quantity=Decimal('5.00'),
                user=self.user_eng_a,
            )
        self.assertIn("Aislamiento multiempresa violado: La orden de trabajo", str(ctx.exception))
        self.bal_cable_a.refresh_from_db()
        self.assertEqual(self.bal_cable_a.quantity, Decimal('100.00'))
        self.bom_cable_b.refresh_from_db()
        self.assertEqual(self.bom_cable_b.executed_quantity, Decimal('0.00'))
        self.assertFalse(self.wo_b.timeline_logs.exists())

    def test_04_bom_item_of_another_org_rejected(self):
        """Intento de despacho especificando partida BOM de otra organización es rechazado."""
        with self.assertRaises(ValidationError) as ctx:
            dispatch_stock_to_work_order(
                organization=self.org_a,
                warehouse=self.wh_a,
                item=self.item_cable_a,
                work_order=self.wo_a,
                quantity=Decimal('5.00'),
                user=self.user_eng_a,
                work_order_item=self.bom_cable_b,  # Partida de Org Beta
            )
        self.assertIn("Aislamiento multiempresa violado: La partida del BOM", str(ctx.exception))
        self.bal_cable_a.refresh_from_db()
        self.assertEqual(self.bal_cable_a.quantity, Decimal('100.00'))

    # --------------------------------------------------------------------------
    # DEF-16: PRUEBAS DE INTEGRIDAD DEL BOM (POLÍTICA ESTRICTA)
    # --------------------------------------------------------------------------
    def test_05_bom_item_belonging_to_another_work_order_rejected(self):
        """Intento de pasar un BOM perteneciente a otra OT de la misma organización es rechazado."""
        wo_a2 = WorkOrder.objects.create(
            organization=self.org_a,
            number="OT-ALP-002",
            title="Segunda OT Alpha",
            account=self.account_a,
            phase=WorkOrder.Phase.IN_PROGRESS,
        )
        bom_a2 = WorkOrderItem.objects.create(
            organization=self.org_a,
            work_order=wo_a2,
            item_type=WorkOrderItem.ItemType.MATERIAL,
            code="SKU-CABLE-100",
            description="Cable para OT 2",
            planned_quantity=Decimal('5.00'),
        )
        with self.assertRaises(ValidationError) as ctx:
            dispatch_stock_to_work_order(
                organization=self.org_a,
                warehouse=self.wh_a,
                item=self.item_cable_a,
                work_order=self.wo_a,  # Despachando a wo_a pero con partida de wo_a2
                quantity=Decimal('2.00'),
                user=self.user_eng_a,
                work_order_item=bom_a2,
            )
        self.assertIn("no pertenece a la Orden de Trabajo", str(ctx.exception))
        self.bal_cable_a.refresh_from_db()
        self.assertEqual(self.bal_cable_a.quantity, Decimal('100.00'))

    def test_06_bom_item_code_mismatch_with_item_sku_rejected(self):
        """Si la partida técnica especificada no coincide con el SKU del material, se rechaza."""
        bom_mismatch = WorkOrderItem.objects.create(
            organization=self.org_a,
            work_order=self.wo_a,
            item_type=WorkOrderItem.ItemType.MATERIAL,
            code="SKU-OTHER-99",
            description="Partida de otro material",
            planned_quantity=Decimal('5.00'),
        )
        with self.assertRaises(ValidationError) as ctx:
            dispatch_stock_to_work_order(
                organization=self.org_a,
                warehouse=self.wh_a,
                item=self.item_cable_a,
                work_order=self.wo_a,
                quantity=Decimal('2.00'),
                user=self.user_eng_a,
                work_order_item=bom_mismatch,
            )
        self.assertIn("Discrepancia de código", str(ctx.exception))
        self.bal_cable_a.refresh_from_db()
        self.assertEqual(self.bal_cable_a.quantity, Decimal('100.00'))

    def test_07_sku_without_match_in_bom_rejected(self):
        """Material sin partida técnica en la OT es rechazado estrictamente sin descontar existencias."""
        item_unbudgeted = InventoryItem.objects.create(
            organization=self.org_a,
            sku="SKU-UNBUDGETED",
            name="Insumo no Presupuestado",
            unit="UND",
            unit_cost=Decimal('10.00'),
        )
        record_stock_entry(
            organization=self.org_a,
            warehouse=self.wh_a,
            item=item_unbudgeted,
            quantity=Decimal('10.00'),
            unit_cost=Decimal('10.00'),
            reference_note="Entrada Insumo Extra",
            user=self.user_eng_a,
        )
        bal_unbud = StockBalance.objects.get(
            organization=self.org_a, warehouse=self.wh_a, item=item_unbudgeted
        )
        self.assertEqual(bal_unbud.quantity, Decimal('10.00'))

        with self.assertRaises(ValidationError) as ctx:
            dispatch_stock_to_work_order(
                organization=self.org_a,
                warehouse=self.wh_a,
                item=item_unbudgeted,
                work_order=self.wo_a,
                quantity=Decimal('2.00'),
                user=self.user_eng_a,
            )
        self.assertIn("No se encontró ninguna partida técnica en la OT", str(ctx.exception))
        bal_unbud.refresh_from_db()
        self.assertEqual(bal_unbud.quantity, Decimal('10.00'))  # Saldo inalterado

    def test_08_multiple_matching_bom_items_rejected_without_explicit_target(self):
        """
        Si existen múltiples partidas con el mismo código, se prohíbe el uso de .first()
        y se rechaza hasta que el operador indique la partida explícita.
        """
        bom_cable_tramo2 = WorkOrderItem.objects.create(
            organization=self.org_a,
            work_order=self.wo_a,
            item_type=WorkOrderItem.ItemType.MATERIAL,
            code="SKU-CABLE-100",
            description="Cable de Potencia 15kV (Tramo Subterráneo 2)",
            unit="MTS",
            planned_quantity=Decimal('15.00'),
            executed_quantity=Decimal('0.00'),
        )

        # 1. Despacho sin especificar work_order_item -> Debe fallar por ambigüedad
        with self.assertRaises(ValidationError) as ctx:
            dispatch_stock_to_work_order(
                organization=self.org_a,
                warehouse=self.wh_a,
                item=self.item_cable_a,
                work_order=self.wo_a,
                quantity=Decimal('5.00'),
                user=self.user_eng_a,
            )
        self.assertIn("Existen múltiples partidas técnicas (2)", str(ctx.exception))
        self.bal_cable_a.refresh_from_db()
        self.assertEqual(self.bal_cable_a.quantity, Decimal('100.00'))

        # 2. Despacho especificando explícitamente la segunda partida -> Exitoso
        mov = dispatch_stock_to_work_order(
            organization=self.org_a,
            warehouse=self.wh_a,
            item=self.item_cable_a,
            work_order=self.wo_a,
            quantity=Decimal('5.00'),
            user=self.user_eng_a,
            work_order_item=bom_cable_tramo2,
        )
        self.assertEqual(mov.work_order_item, bom_cable_tramo2)
        bom_cable_tramo2.refresh_from_db()
        self.assertEqual(bom_cable_tramo2.executed_quantity, Decimal('5.00'))
        self.bom_cable_a.refresh_from_db()
        self.assertEqual(self.bom_cable_a.executed_quantity, Decimal('0.00'))  # La primera no fue alterada
        self.bal_cable_a.refresh_from_db()
        self.assertEqual(self.bal_cable_a.quantity, Decimal('95.00'))

    # --------------------------------------------------------------------------
    # DEF-17: PRUEBAS DE FASE ACTIVA DE LA OT
    # --------------------------------------------------------------------------
    def test_09_work_order_completed_and_cancelled_phases_rejected(self):
        """No se permite despachar materiales a OTs cerradas (COMPLETED) o canceladas (CANCELLED)."""
        # Fase COMPLETED
        self.wo_a.phase = WorkOrder.Phase.COMPLETED
        self.wo_a.save(update_fields=['phase'])

        with self.assertRaises(ValidationError) as ctx:
            dispatch_stock_to_work_order(
                organization=self.org_a,
                warehouse=self.wh_a,
                item=self.item_cable_a,
                work_order=self.wo_a,
                quantity=Decimal('2.00'),
                user=self.user_eng_a,
            )
        self.assertIn("se encuentra en fase 06 · Entregada y Cerrada", str(ctx.exception))
        self.bal_cable_a.refresh_from_db()
        self.assertEqual(self.bal_cable_a.quantity, Decimal('100.00'))

        # Fase CANCELLED
        self.wo_a.phase = WorkOrder.Phase.CANCELLED
        self.wo_a.save(update_fields=['phase'])

        with self.assertRaises(ValidationError) as ctx:
            dispatch_stock_to_work_order(
                organization=self.org_a,
                warehouse=self.wh_a,
                item=self.item_cable_a,
                work_order=self.wo_a,
                quantity=Decimal('2.00'),
                user=self.user_eng_a,
            )
        self.assertIn("se encuentra en fase Cancelada", str(ctx.exception))
        self.bal_cable_a.refresh_from_db()
        self.assertEqual(self.bal_cable_a.quantity, Decimal('100.00'))

    # --------------------------------------------------------------------------
    # DEF-18: PRUEBAS DE TRANSACCIONALIDAD Y ROLLBACK
    # --------------------------------------------------------------------------
    def test_10_full_rollback_when_subsequent_step_fails(self):
        """
        Si la inserción en la bitácora o un paso posterior falla con error imprevisto,
        la transacción revierte al 100%: saldo, BOM y Kardex quedan limpios.
        """
        with patch('apps.operations.models.WorkOrderTimelineLog.objects.create', side_effect=RuntimeError("Simulated DB Crash")):
            with self.assertRaises(RuntimeError):
                dispatch_stock_to_work_order(
                    organization=self.org_a,
                    warehouse=self.wh_a,
                    item=self.item_cable_a,
                    work_order=self.wo_a,
                    quantity=Decimal('10.00'),
                    user=self.user_eng_a,
                )

        # Verificación de Rollback completo
        self.bal_cable_a.refresh_from_db()
        self.assertEqual(self.bal_cable_a.quantity, Decimal('100.00'))
        self.bom_cable_a.refresh_from_db()
        self.assertEqual(self.bom_cable_a.executed_quantity, Decimal('0.00'))
        self.assertFalse(self.bom_cable_a.is_completed)
        self.assertFalse(
            StockMovement.objects.filter(
                organization=self.org_a, movement_type=StockMovement.MovementType.OUT_WORKORDER
            ).exists()
        )

    def test_11_valid_dispatch_updates_stock_bom_kardex_timeline_consistently(self):
        """Despacho legítimo actualiza en simultáneo y de forma consistente saldo, BOM, Kardex y Bitácora."""
        mov = dispatch_stock_to_work_order(
            organization=self.org_a,
            warehouse=self.wh_a,
            item=self.item_cable_a,
            work_order=self.wo_a,
            quantity=Decimal('20.00'),
            user=self.user_eng_a,
        )

        # 1. Saldo en bodega
        self.bal_cable_a.refresh_from_db()
        self.assertEqual(self.bal_cable_a.quantity, Decimal('80.00'))

        # 2. BOM en la OT
        self.bom_cable_a.refresh_from_db()
        self.assertEqual(self.bom_cable_a.executed_quantity, Decimal('20.00'))
        self.assertTrue(self.bom_cable_a.is_completed)
        self.assertEqual(self.bom_cable_a.completed_by, self.user_eng_a)

        # 3. Kardex inmutable
        self.assertEqual(mov.movement_type, StockMovement.MovementType.OUT_WORKORDER)
        self.assertEqual(mov.quantity, Decimal('20.00'))
        self.assertEqual(mov.balance_after, Decimal('80.00'))
        self.assertEqual(mov.work_order, self.wo_a)
        self.assertEqual(mov.work_order_item, self.bom_cable_a)

        # 4. Bitácora de la OT
        log = WorkOrderTimelineLog.objects.filter(
            work_order=self.wo_a, event_type='MATERIAL_DISPATCHED'
        ).latest('created_at')
        self.assertIn("SKU-CABLE-100", log.summary)
        self.assertIn(mov.number, log.details)
        self.assertIn(self.bom_cable_a.description, log.details)


class StockDispatchConcurrencyTestSuite(TransactionTestCase):
    """
    Prueba de Concurrencia Multi-hilo (DEF-18) para despachos sobre la misma partida técnica.
    Requiere PostgreSQL real para verificar la serialización de bloqueos vía select_for_update.
    """

    def setUp(self):
        if connection.vendor != 'postgresql':
            return

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
            name=f"Org Conc Dispatch {uuid.uuid4().hex[:6]}",
            slug=f"org-conc-disp-{uuid.uuid4().hex[:6]}",
            country=self.country_ni,
            enabled_modules=['inventory', 'operations'],
            is_active=True,
        )
        self.wh = Warehouse.objects.create(
            organization=self.org,
            code="BOD-CONC-01",
            name="Bodega Concurrente",
            warehouse_type=Warehouse.WarehouseType.CENTRAL,
        )
        self.item = InventoryItem.objects.create(
            organization=self.org,
            sku="MAT-CONC-01",
            name="Conector de Alta Potencia",
            unit="UND",
            unit_cost=Decimal('250.00'),
        )
        self.user = User.objects.create_user(
            email=f'conc_eng_{uuid.uuid4().hex[:6]}@vector.com.ni',
            full_name='Ingeniero Concurrente',
            password='password123',
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            role_level=RoleLevel.ENGINEER,
            is_active=True,
            is_default_org=True,
        )
        record_stock_entry(
            organization=self.org,
            warehouse=self.wh,
            item=self.item,
            quantity=Decimal('50.00'),
            unit_cost=Decimal('250.00'),
            reference_note="Stock Concurrencia",
            user=self.user,
        )
        self.account = Account.objects.create(
            organization=self.org,
            commercial_name="Cliente Concurrente",
        )
        self.wo = WorkOrder.objects.create(
            organization=self.org,
            number=f"OT-CONC-{uuid.uuid4().hex[:4]}",
            title="OT Concurrencia Despacho",
            account=self.account,
            phase=WorkOrder.Phase.IN_PROGRESS,
            assigned_engineer=self.user,
        )
        self.bom_item = WorkOrderItem.objects.create(
            organization=self.org,
            work_order=self.wo,
            item_type=WorkOrderItem.ItemType.MATERIAL,
            code="MAT-CONC-01",
            description="Conector de Alta Potencia",
            unit="UND",
            planned_quantity=Decimal('20.00'),
            executed_quantity=Decimal('0.00'),
        )

    def test_concurrent_dispatch_to_same_bom_item_prevents_lost_updates(self):
        """
        Dos hilos concurrentes despachando 5.00 unidades a la misma partida técnica:
        - Bloqueos select_for_update serializan la actualización de StockBalance y WorkOrderItem.
        - Saldo final disminuye exactamente en 10.00 (5.00 + 5.00 = 40.00 restantes).
        - executed_quantity final del BOM es exactamente 10.00 (sin lost update).
        - Se generan exactamente 2 movimientos de Kardex OUT_WORKORDER.
        """
        if connection.vendor != 'postgresql':
            self.skipTest("Esta prueba de concurrencia multi-hilo requiere PostgreSQL.")

        errors = []

        def worker():
            try:
                dispatch_stock_to_work_order(
                    organization=self.org,
                    warehouse=self.wh,
                    item=self.item,
                    work_order=self.wo,
                    quantity=Decimal('5.00'),
                    user=self.user,
                )
            except Exception as e:
                errors.append(e)
            finally:
                connections.close_all()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)

        t1.start()
        t2.start()

        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertFalse(t1.is_alive(), "El hilo 1 no finalizó dentro del tiempo límite.")
        self.assertFalse(t2.is_alive(), "El hilo 2 no finalizó dentro del tiempo límite.")

        self.assertEqual(errors, [], f"Errores en hilos concurrentes: {errors}")

        bal = StockBalance.objects.get(organization=self.org, warehouse=self.wh, item=self.item)
        self.assertEqual(bal.quantity, Decimal('40.00'))

        self.bom_item.refresh_from_db()
        self.assertEqual(self.bom_item.executed_quantity, Decimal('10.00'))

        movements = StockMovement.objects.filter(
            organization=self.org, movement_type=StockMovement.MovementType.OUT_WORKORDER
        )
        self.assertEqual(movements.count(), 2)

    def tearDown(self):
        connections.close_all()
        super().tearDown()


