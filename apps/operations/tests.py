import hashlib
import json
import threading
import uuid
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction, IntegrityError
from django.test import TestCase, Client, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import CountryConfig
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel
from apps.crm.models import Account, Quote, QuoteItem
from apps.operations.models import (
    Crew,
    WorkOrder,
    WorkOrderItem,
    WorkOrderEvidence,
    WorkOrderTimelineLog,
)

User = get_user_model()


class OperationsModuleEndToEndTestCase(TestCase):
    """
    Suite de Pruebas End-to-End para el Módulo de Operaciones e Ingeniería (apps/operations).
    Verifica:
    1. Conversión en 1 Clic (Quote -> WorkOrder) con estricta Confidencialidad Financiera.
    2. Regla de Bloqueo SST en labores de Alto Riesgo.
    3. Aislamiento estricto del Técnico de Campo (Nivel 20) y Matriz de Permisos RBAC.
    4. Inmutabilidad de la Bitácora Cronológica de Auditoría (WorkOrderTimelineLog).
    5. Aislamiento Multi-Tenant Estricto por Organización.
    """

    def setUp(self):
        # 1. Configuración de País y Moneda Fiscal
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
            slug="vector-ni-ops",
            tax_id="J0310000199999",
            country=self.country_ni,
            enabled_modules=['crm', 'operations', 'sst'],
            is_active=True,
        )

        self.org_intruder = Organization.objects.create(
            name="Constructora Invasora, S.A.",
            legal_name="Invasora S.A.",
            slug="invasora-ni-ops",
            tax_id="J0310000188888",
            country=self.country_ni,
            enabled_modules=['operations'],
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

        # Nivel 70: Oficial SST (Seguridad y Salud en el Trabajo)
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

        # Nivel 20: Técnico de Campo #1
        self.user_tech1 = User.objects.create_user(
            email='tecnico1@vector.com.ni',
            full_name='Juan Pérez (Técnico 1)',
            password='password123'
        )
        OrganizationMembership.objects.create(
            user=self.user_tech1,
            organization=self.org,
            role_level=RoleLevel.TECHNICIAN,
            is_active=True,
            is_default_org=True,
        )

        # Nivel 20: Técnico de Campo #2 (Cuadrilla distinta)
        self.user_tech2 = User.objects.create_user(
            email='tecnico2@vector.com.ni',
            full_name='Pedro Gómez (Técnico 2)',
            password='password123'
        )
        OrganizationMembership.objects.create(
            user=self.user_tech2,
            organization=self.org,
            role_level=RoleLevel.TECHNICIAN,
            is_active=True,
            is_default_org=True,
        )

        # 4. Cliente para Pruebas Operativas
        self.account = Account.objects.create(
            organization=self.org,
            commercial_name="Ingenio Monte Rosa",
            legal_name="Pantaleon Nicaragua S.A.",
            tax_id="J0310000112233",
            city="Chinandega",
            address="Km 135 Carretera Chinandega - Corinto",
            default_currency="NIO",
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
    # TEST 1: Conversión en 1 Clic (Quote -> WorkOrder) y Confidencialidad
    # --------------------------------------------------------------------------
    def test_quote_to_workorder_conversion_and_financial_confidentiality(self):
        """
        Verifica:
        1. Cotización aprobada COT-2026-0001 se convierte en OT-2026-0001 mediante POST
           a /operations/from-quote/<pk>/ ejecutado por el Coordinador (80).
        2. Se copian exactamente las partidas técnicas (BOM) sin precios ni montos monetarios.
        3. Se genera la bitácora inicial FROM_QUOTE en WorkOrderTimelineLog.
        4. Al visualizar la OT con roles de campo (Ingeniero 50, Técnico 20, SST 70),
           el HTML NUNCA expone precios (50000, 25000, 109250) ni el símbolo de moneda 'C$'.
        """
        # Crear Cotización con montos comerciales y 2 ítems
        quote = Quote.objects.create(
            organization=self.org,
            account=self.account,
            title="Mantenimiento Mayor de Subestación Eléctrica 138kV",
            notes="Instrucciones de ingeniería: revisar protecciones y aislamiento.",
            currency_code="NIO",
            tax_rate=Decimal('15.00'),
            apply_tax=True,
            status=Quote.Status.APPROVED,
            approved_by=self.user_ops_coord,
            approved_at=timezone.now(),
        )

        item1 = QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type=QuoteItem.ItemType.SERVICE,
            code="SRV-ING-01",
            description="Servicio de Ingeniería y Montaje Electromecánico",
            unit="UND",
            quantity=Decimal('1.00'),
            unit_price=Decimal('50000.00'),
            discount_percent=Decimal('0.00'),
        )

        item2 = QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type=QuoteItem.ItemType.MATERIAL,
            code="MAT-CAB-01",
            description="Cable de Cobre Aislado THHN #4/0",
            unit="MTS",
            quantity=Decimal('2.00'),
            unit_price=Decimal('25000.00'),
            discount_percent=Decimal('10.00'),
        )

        # Actualizar totales de la cotización
        quote.subtotal = Decimal('100000.00')
        quote.discount_total = Decimal('5000.00')
        quote.tax_total = Decimal('14250.00')
        quote.grand_total = Decimal('109250.00')
        quote.save()

        # Ejecutar Conversión en 1 Clic con Coordinador de Operaciones (Nivel 80)
        client_coord = self._get_client_for_user(self.user_ops_coord)
        url_convert = reverse('operations:convert_from_quote', kwargs={'quote_pk': quote.pk})
        response = client_coord.post(url_convert)
        self.assertEqual(response.status_code, 302)

        # Verificar que la WorkOrder fue creada
        wo = WorkOrder.objects.filter(source_quote=quote).first()
        self.assertIsNotNone(wo)
        self.assertTrue(wo.number.startswith('OT-'))
        self.assertEqual(wo.account, self.account)
        self.assertEqual(wo.title, quote.title)
        self.assertEqual(wo.phase, WorkOrder.Phase.DRAFT)

        # Verificar copiado de partidas técnicas (BOM)
        bom_items = wo.items.all().order_by('created_at')
        self.assertEqual(bom_items.count(), 2)

        bom_srv = bom_items.filter(code="SRV-ING-01").first()
        self.assertIsNotNone(bom_srv)
        self.assertEqual(bom_srv.description, "Servicio de Ingeniería y Montaje Electromecánico")
        self.assertEqual(bom_srv.unit, "UND")
        self.assertEqual(bom_srv.planned_quantity, Decimal('1.00'))
        self.assertEqual(bom_srv.executed_quantity, Decimal('0.00'))
        self.assertFalse(bom_srv.is_completed)

        bom_mat = bom_items.filter(code="MAT-CAB-01").first()
        self.assertIsNotNone(bom_mat)
        self.assertEqual(bom_mat.description, "Cable de Cobre Aislado THHN #4/0")
        self.assertEqual(bom_mat.unit, "MTS")
        self.assertEqual(bom_mat.planned_quantity, Decimal('2.00'))
        self.assertEqual(bom_mat.executed_quantity, Decimal('0.00'))

        # Verificar primer registro en la Bitácora Inmutable
        log_initial = wo.timeline_logs.filter(event_type='FROM_QUOTE').first()
        self.assertIsNotNone(log_initial)
        self.assertIn(quote.number, log_initial.summary)
        self.assertEqual(log_initial.actor, self.user_ops_coord)

        # Verificar Confidencialidad Financiera Estricta en HTML para roles de campo
        client_eng = self._get_client_for_user(self.user_engineer)
        url_detail = reverse('operations:workorder_detail', kwargs={'pk': wo.pk})
        resp_detail = client_eng.get(url_detail)
        self.assertEqual(resp_detail.status_code, 200)

        html_content = resp_detail.content.decode('utf-8')
        # Cero exposición de precios unitarios o totales de cotización
        self.assertNotIn("50000", html_content)
        self.assertNotIn("25000", html_content)
        self.assertNotIn("109250", html_content)
        self.assertNotIn("C$", html_content)

        # Verificar que sí se muestran las partidas técnicas y cantidades
        self.assertIn("SRV-ING-01", html_content)
        self.assertIn("MAT-CAB-01", html_content)
        self.assertIn("1.00", html_content)
        self.assertIn("2.00", html_content)
        self.assertIn("MTS", html_content)

    # --------------------------------------------------------------------------
    # DEF-06: Idempotencia, Unicidad y Concurrencia de Quote -> WorkOrder
    # --------------------------------------------------------------------------
    def test_quote_to_workorder_idempotency_second_request_does_not_duplicate(self):
        """
        DEF-06: Verifica que una segunda solicitud de conversión (doble clic o reintento)
        no cree una segunda Orden de Trabajo ni duplique las partidas técnicas del BOM.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.account,
            title="Instalación de Paneles Solares Industriales",
            status=Quote.Status.APPROVED,
            approved_by=self.user_ops_coord,
            approved_at=timezone.now(),
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type=QuoteItem.ItemType.MATERIAL,
            code="SOL-MOD-01",
            description="Módulo Fotovoltaico Monocristalino 550W",
            quantity=Decimal('20.00'),
            unit_price=Decimal('150.00'),
        )

        client_coord = self._get_client_for_user(self.user_ops_coord)
        url_convert = reverse('operations:convert_from_quote', kwargs={'quote_pk': quote.pk})

        # Primera solicitud: Crea la OT
        resp1 = client_coord.post(url_convert, follow=True)
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(WorkOrder.objects.filter(source_quote=quote).count(), 1)
        wo1 = WorkOrder.objects.get(source_quote=quote)
        self.assertEqual(wo1.items.count(), 1)
        self.assertEqual(wo1.timeline_logs.filter(event_type='FROM_QUOTE').count(), 1)

        # Segunda solicitud (reintento / doble clic): No duplica
        resp2 = client_coord.post(url_convert, follow=True)
        self.assertEqual(resp2.status_code, 200)
        # Sigue existiendo exactamente 1 OT
        self.assertEqual(WorkOrder.objects.filter(source_quote=quote).count(), 1)
        self.assertEqual(wo1.items.count(), 1)
        self.assertEqual(wo1.timeline_logs.filter(event_type='FROM_QUOTE').count(), 1)
        # Mensaje informativo/advertencia devuelto
        messages_list = list(resp2.context['messages'])
        self.assertTrue(any("ya posee la Orden de Trabajo" in m.message or "ya cuenta con una Orden de Trabajo" in m.message for m in messages_list))

    def test_quote_to_workorder_direct_get_request_handled_gracefully(self):
        """
        DEF-06: Verifica que peticiones GET directas a la URL de conversión
        respondan de manera controlada y sin mutaciones de estado ni errores 405.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.account,
            title="Mantenimiento de Banco de Capacitores",
            status=Quote.Status.APPROVED,
        )
        client_coord = self._get_client_for_user(self.user_ops_coord)
        url_convert = reverse('operations:convert_from_quote', kwargs={'quote_pk': quote.pk})

        # Caso 1: Aún no existe OT -> redirige a crm:quote_detail con mensaje
        resp_get1 = client_coord.get(url_convert, follow=True)
        self.assertEqual(resp_get1.status_code, 200)
        self.assertRedirects(resp_get1, reverse('crm:quote_detail', kwargs={'pk': quote.pk}))
        self.assertEqual(WorkOrder.objects.filter(source_quote=quote).count(), 0)

        # Crear la OT formalmente vía POST
        client_coord.post(url_convert)
        wo = WorkOrder.objects.get(source_quote=quote)

        # Caso 2: Ya existe OT -> redirige a operations:workorder_detail con mensaje
        resp_get2 = client_coord.get(url_convert, follow=True)
        self.assertEqual(resp_get2.status_code, 200)
        self.assertRedirects(resp_get2, reverse('operations:workorder_detail', kwargs={'pk': wo.pk}))

    def test_quote_to_workorder_unapproved_quote_rejected(self):
        """
        DEF-06: Cotizaciones en DRAFT, SENT o REJECTED no pueden convertirse en OT.
        """
        quote_draft = Quote.objects.create(
            organization=self.org,
            account=self.account,
            title="Cotización Borrador No Aprobada",
            status=Quote.Status.DRAFT,
        )
        client_coord = self._get_client_for_user(self.user_ops_coord)
        url = reverse('operations:convert_from_quote', kwargs={'quote_pk': quote_draft.pk})
        resp = client_coord.post(url, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertRedirects(resp, reverse('crm:quote_detail', kwargs={'pk': quote_draft.pk}))
        self.assertEqual(WorkOrder.objects.filter(source_quote=quote_draft).count(), 0)

    def test_quote_to_workorder_cross_tenant_isolation(self):
        """
        DEF-06: Un usuario de otra organización no puede convertir ni consultar
        cotizaciones u órdenes de trabajo ajenas.
        """
        user_intruder = User.objects.create_user(
            email='intruder_coord@invasora.com.ni',
            full_name='Coordinador Invasor',
            password='password123'
        )
        OrganizationMembership.objects.create(
            user=user_intruder,
            organization=self.org_intruder,
            role_level=RoleLevel.OPERATIONS_COORD,
            is_active=True,
            is_default_org=True,
        )

        quote_org1 = Quote.objects.create(
            organization=self.org,
            account=self.account,
            title="Proyecto Confidencial Vector",
            status=Quote.Status.APPROVED,
        )

        client_intruder = self._get_client_for_user(user_intruder, org=self.org_intruder)
        url_convert = reverse('operations:convert_from_quote', kwargs={'quote_pk': quote_org1.pk})

        resp = client_intruder.post(url_convert)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(WorkOrder.objects.filter(source_quote=quote_org1).count(), 0)

    def test_quote_to_workorder_database_unique_constraint_enforcement(self):
        """
        DEF-06: La base de datos impide duplicados incluso si dos procesos
        intentan insertar concurrentemente con el mismo source_quote.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.account,
            title="Subestación Tipitapa Fase 2",
            status=Quote.Status.APPROVED,
        )

        wo1 = WorkOrder.objects.create(
            organization=self.org,
            account=self.account,
            source_quote=quote,
            title=quote.title,
            created_by=self.user_ops_coord,
        )
        self.assertIsNotNone(wo1.pk)

        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                WorkOrder.objects.create(
                    organization=self.org,
                    account=self.account,
                    source_quote=quote,
                    title="Duplicado no autorizado",
                    created_by=self.user_ops_coord,
                )

        direct_wo1 = WorkOrder.objects.create(
            organization=self.org,
            account=self.account,
            source_quote=None,
            title="OT Directa 1",
            created_by=self.user_ops_coord,
        )
        direct_wo2 = WorkOrder.objects.create(
            organization=self.org,
            account=self.account,
            source_quote=None,
            title="OT Directa 2",
            created_by=self.user_ops_coord,
        )
        self.assertIsNotNone(direct_wo1.pk)
        self.assertIsNotNone(direct_wo2.pk)

    def test_quote_detail_ui_shows_link_when_workorder_exists(self):
        """
        DEF-06: En la interfaz quote_detail.html, si ya existe una OT asociada,
        se oculta el botón de conversión y se muestra el enlace directo a la OT existente.
        """
        quote = Quote.objects.create(
            organization=self.org,
            account=self.account,
            title="Reemplazo de Seccionadores 69kV",
            status=Quote.Status.APPROVED,
        )
        client_owner = self._get_client_for_user(self.user_owner)
        url_detail = reverse('crm:quote_detail', kwargs={'pk': quote.pk})

        resp_before = client_owner.get(url_detail)
        self.assertEqual(resp_before.status_code, 200)
        self.assertContains(resp_before, "Generar Orden de Trabajo")

        url_convert = reverse('operations:convert_from_quote', kwargs={'quote_pk': quote.pk})
        client_owner.post(url_convert)
        wo = WorkOrder.objects.get(source_quote=quote)

        resp_after = client_owner.get(url_detail)
        self.assertEqual(resp_after.status_code, 200)
        self.assertNotContains(resp_after, "Generar Orden de Trabajo")
        self.assertContains(resp_after, reverse('operations:workorder_detail', kwargs={'pk': wo.pk}))
        self.assertContains(resp_after, wo.number)

    # --------------------------------------------------------------------------
    # TEST 2: Regla de Seguridad SST y Bloqueo en Alto Riesgo
    # --------------------------------------------------------------------------
    def test_sst_high_risk_lock_and_clearance(self):
        """
        Verifica:
        1. Si is_high_risk=True y sst_cleared=False, pasar a IN_PROGRESS es bloqueado.
        2. Solo el Oficial SST (70+) puede otorgar o revocar la liberación SST.
        3. Una vez liberada por SST, la OT puede iniciar en campo (IN_PROGRESS).
        """
        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.account,
            title="Reparación en Transformador de Potencia de Alta Tensión",
            is_high_risk=True,
            sst_cleared=False,
            phase=WorkOrder.Phase.SCHEDULED,
            created_by=self.user_ops_coord,
        )

        can_start, reason = wo.can_start_field_execution()
        self.assertFalse(can_start)
        self.assertIn("Alto Riesgo", reason)

        # Intento de pasar a IN_PROGRESS por el Ingeniero (Nivel 50)
        client_eng = self._get_client_for_user(self.user_engineer)
        url_trans = reverse('operations:workorder_phase_transition', kwargs={'pk': wo.pk})
        response = client_eng.post(url_trans, data={'phase': WorkOrder.Phase.IN_PROGRESS})
        self.assertEqual(response.status_code, 302)

        # Comprobar que NO cambió de fase debido al bloqueo SST
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.SCHEDULED)

        # Intento de liberación SST por Técnico (Nivel 20) -> Recibe 403 Forbidden
        client_tech = self._get_client_for_user(self.user_tech1)
        url_sst = reverse('operations:workorder_sst_clearance', kwargs={'pk': wo.pk})
        resp_forbidden = client_tech.post(url_sst, data={'action': 'grant'})
        self.assertEqual(resp_forbidden.status_code, 403)

        # Liberación legítima otorgada por el Oficial SST (Nivel 70)
        client_sst = self._get_client_for_user(self.user_sst)
        resp_sst = client_sst.post(url_sst, data={
            'action': 'grant',
            'notes': 'Permiso de trabajo en altura y análisis de riesgo AST revisado y aprobado.'
        })
        self.assertEqual(resp_sst.status_code, 302)

        wo.refresh_from_db()
        self.assertTrue(wo.sst_cleared)

        # Verificar registro en bitácora de auditoría SST
        log_sst = wo.timeline_logs.filter(event_type='SST_CLEARANCE').first()
        self.assertIsNotNone(log_sst)
        self.assertIn("Liberación SST Otorgada", log_sst.summary)
        self.assertEqual(log_sst.actor, self.user_sst)

        # Ahora el Ingeniero intenta nuevamente pasar a IN_PROGRESS -> Exitoso
        resp_success = client_eng.post(url_trans, data={'phase': WorkOrder.Phase.IN_PROGRESS})
        self.assertEqual(resp_success.status_code, 302)

        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.IN_PROGRESS)

    # --------------------------------------------------------------------------
    # TEST 3: Aislamiento del Técnico (Nivel 20) y Matriz de Permisos RBAC
    # --------------------------------------------------------------------------
    def test_technician_isolation_and_rbac_matrix(self):
        """
        Verifica:
        1. Técnico 1 (perteneciente a Cuadrilla Alfa) solo ve las OTs de su cuadrilla
           en /operations/ ("Mis Tareas de Hoy"). No ve las OTs de Cuadrilla Beta.
        2. Técnico 1 puede marcar partidas técnicas ejecutadas y subir evidencias.
        3. Técnico 1 recibe 403 si intenta acceder a planeación de cuadrillas (/crews/),
           crear OTs manualmente o cambiar fases de ingeniería.
        4. Ingeniero (50) NO puede cerrar definitivamente una OT (COMPLETED -> 403).
        5. Coordinador (80) sí puede autorizar el cierre definitivo (COMPLETED).
        """
        # Crear Cuadrillas
        crew_alpha = Crew.objects.create(
            organization=self.org,
            code="CUAD-01",
            name="Cuadrilla Electromecánica Alfa",
            specialty="Media y Alta Tensión",
            leader=self.user_engineer,
            vehicle_plate="M 345-890",
            vehicle_description="Hilux 4x4",
            is_active=True,
        )
        crew_alpha.members.add(self.user_tech1)

        crew_beta = Crew.objects.create(
            organization=self.org,
            code="CUAD-02",
            name="Cuadrilla de Obras Civiles Beta",
            specialty="Fundaciones y Obra Gris",
            leader=self.user_engineer,
            vehicle_plate="M 987-654",
            is_active=True,
        )
        crew_beta.members.add(self.user_tech2)

        # Crear dos OTs asignadas a cada cuadrilla
        ot_alpha = WorkOrder.objects.create(
            organization=self.org,
            account=self.account,
            title="Instalación de Celdas de Media Tensión (Alfa)",
            assigned_crew=crew_alpha,
            phase=WorkOrder.Phase.IN_PROGRESS,
        )
        item_alpha = WorkOrderItem.objects.create(
            organization=self.org,
            work_order=ot_alpha,
            code="INS-01",
            description="Montaje físico de celda",
            unit="UND",
            planned_quantity=Decimal('1.00'),
            executed_quantity=Decimal('0.00'),
        )

        ot_beta = WorkOrder.objects.create(
            organization=self.org,
            account=self.account,
            title="Fundación de Concreto para Transformador (Beta)",
            assigned_crew=crew_beta,
            phase=WorkOrder.Phase.IN_PROGRESS,
        )

        # 1. Técnico 1 consulta el listado ("Mis Tareas de Hoy")
        client_tech1 = self._get_client_for_user(self.user_tech1)
        resp_list = client_tech1.get(reverse('operations:workorder_list'))
        self.assertEqual(resp_list.status_code, 200)

        html_list = resp_list.content.decode('utf-8')
        # Ve su orden asignada (ot_alpha)
        self.assertIn(ot_alpha.number, html_list)
        # NUNCA ve la orden de la otra cuadrilla (ot_beta)
        self.assertNotIn(ot_beta.number, html_list)

        # 2. Técnico 1 ejecuta partida técnica
        url_exec = reverse('operations:workorder_item_execute', kwargs={
            'wo_pk': ot_alpha.pk,
            'item_pk': item_alpha.pk,
        })
        resp_exec = client_tech1.post(url_exec, data={
            'executed_quantity': '1.00',
            'is_completed': '1',
        })
        self.assertEqual(resp_exec.status_code, 302)

        item_alpha.refresh_from_db()
        self.assertEqual(item_alpha.executed_quantity, Decimal('1.00'))
        self.assertTrue(item_alpha.is_completed)
        self.assertEqual(item_alpha.completed_by, self.user_tech1)

        # 3. Técnico 1 sube evidencia técnica de campo
        url_evidence = reverse('operations:workorder_evidence_add', kwargs={'pk': ot_alpha.pk})
        resp_evid = client_tech1.post(url_evidence, data={
            'evidence_type': WorkOrderEvidence.EvidenceType.PHOTO_PROGRESS,
            'title': 'Fijación de tornillería y anclajes completada',
            'notes': 'Torque aplicado según especificación del fabricante (45 Nm).',
            'file_reference': 'FOTO-ANCLAJE-01.JPG',
        })
        self.assertEqual(resp_evid.status_code, 302)

        evid = ot_alpha.evidences.first()
        self.assertIsNotNone(evid)
        self.assertEqual(evid.uploaded_by, self.user_tech1)
        self.assertEqual(evid.title, 'Fijación de tornillería y anclajes completada')

        # 4. Restricciones RBAC para Técnico (Nivel 20):
        # Intentar acceder a Planeación de Cuadrillas -> 403 Forbidden
        resp_crew_plan = client_tech1.get(reverse('operations:crew_planning'))
        self.assertEqual(resp_crew_plan.status_code, 403)

        # Intentar crear cuadrilla -> 403 Forbidden
        resp_crew_create = client_tech1.get(reverse('operations:crew_create'))
        self.assertEqual(resp_crew_create.status_code, 403)

        # Intentar crear OT manual -> 403 Forbidden
        resp_wo_create = client_tech1.get(reverse('operations:workorder_create'))
        self.assertEqual(resp_wo_create.status_code, 403)

        # Intentar cambiar de fase la OT -> 403 Forbidden
        url_trans = reverse('operations:workorder_phase_transition', kwargs={'pk': ot_alpha.pk})
        resp_tech_trans = client_tech1.post(url_trans, data={'phase': WorkOrder.Phase.QUALITY_CHECK})
        self.assertEqual(resp_tech_trans.status_code, 403)

        # Ingeniero (Nivel 50) avanza la OT a QUALITY_CHECK según la FSM
        client_eng = self._get_client_for_user(self.user_engineer)
        resp_eng_qc = client_eng.post(url_trans, data={'phase': WorkOrder.Phase.QUALITY_CHECK})
        self.assertEqual(resp_eng_qc.status_code, 302)
        ot_alpha.refresh_from_db()
        self.assertEqual(ot_alpha.phase, WorkOrder.Phase.QUALITY_CHECK)

        # 5. Ingeniero (Nivel 50) intenta Cierre Definitivo (COMPLETED) -> 403 Forbidden
        resp_eng_close = client_eng.post(url_trans, data={'phase': WorkOrder.Phase.COMPLETED})
        self.assertEqual(resp_eng_close.status_code, 403)

        # 6. Coordinador de Operaciones (Nivel 80) sí puede autorizar el Cierre Definitivo
        client_coord = self._get_client_for_user(self.user_ops_coord)
        resp_coord_close = client_coord.post(url_trans, data={
            'phase': WorkOrder.Phase.COMPLETED,
            'notes': 'Acta de entrega técnica firmada a satisfacción por el cliente.',
        })
        self.assertEqual(resp_coord_close.status_code, 302)

        ot_alpha.refresh_from_db()
        self.assertEqual(ot_alpha.phase, WorkOrder.Phase.COMPLETED)
        self.assertIsNotNone(ot_alpha.completed_at)

    # --------------------------------------------------------------------------
    # TEST 4: Inmutabilidad de la Bitácora Cronológica de Auditoría
    # --------------------------------------------------------------------------
    def test_timeline_log_immutability(self):
        """
        Verifica que WorkOrderTimelineLog sea estrictamente inmutable:
        1. Modificar un registro existente lanza ValidationError.
        2. Eliminar un registro existente lanza ValidationError.
        """
        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.account,
            title="Prueba de Bitácora Inmutable",
        )

        log = WorkOrderTimelineLog.objects.create(
            organization=self.org,
            work_order=wo,
            event_type='CREATED',
            summary="Apertura inicial",
            actor=self.user_ops_coord,
        )
        self.assertIsNotNone(log.pk)

        # Intento de modificación posterior
        log.summary = "Resumen modificado maliciosamente"
        with self.assertRaises(ValidationError) as ctx:
            log.save()
        self.assertIn("inmutable", str(ctx.exception).lower())

        # Intento de eliminación posterior
        with self.assertRaises(ValidationError) as ctx_del:
            log.delete()
        self.assertIn("inmutable", str(ctx_del.exception).lower())

    # --------------------------------------------------------------------------
    # TEST 5: Aislamiento Multi-Tenant Estricto
    # --------------------------------------------------------------------------
    def test_multi_tenant_isolation(self):
        """
        Verifica que los modelos de operaciones respeten el aislamiento por organización.
        """
        # Crear OT en la organización invasora
        acc_intruder = Account.objects.create(
            organization=self.org_intruder,
            commercial_name="Cliente de Empresa Competidora",
            tax_id="J0310000188888",
        )
        wo_intruder = WorkOrder.objects.create(
            organization=self.org_intruder,
            account=acc_intruder,
            title="OT Privada de la Competencia",
        )

        # Filtrar por self.org NUNCA debe retornar la OT de org_intruder
        org_work_orders = WorkOrder.objects.for_org(self.org)
        self.assertNotIn(wo_intruder, org_work_orders)

        # Intento de acceso a la OT invasora mediante usuario de self.org -> 404 Not Found
        client_coord = self._get_client_for_user(self.user_ops_coord, org=self.org)
        url_intruder = reverse('operations:workorder_detail', kwargs={'pk': wo_intruder.pk})
        resp = client_coord.get(url_intruder)
        self.assertEqual(resp.status_code, 404)

    # --------------------------------------------------------------------------
    # TEST 6: Firma Digital de Conformidad, Sello SHA-256 e Inmutabilidad
    # --------------------------------------------------------------------------
    def test_client_digital_signature_and_sha256_immutability(self):
        """
        Verifica:
        1. Técnico (20) asignado a la cuadrilla captura la firma táctil del cliente en Base64.
        2. Se calcula el sello criptográfico SHA-256 (64 hex) y se estampa la fecha client_signed_at.
        3. La OT avanza automáticamente de IN_PROGRESS a QUALITY_CHECK.
        4. Se registra el evento 'CLIENT_SIGNED' en WorkOrderTimelineLog.
        5. Cualquier intento de sobrescribir la firma o modificar campos sellados es bloqueado
           con ValidationError por la regla de inmutabilidad estricta.
        """
        crew = Crew.objects.create(
            organization=self.org,
            code="CUAD-SIG",
            name="Cuadrilla de Montaje Especial",
            leader=self.user_engineer,
            is_active=True,
        )
        crew.members.add(self.user_tech1)

        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.account,
            title="Mantenimiento de Línea de Distribución",
            assigned_crew=crew,
            phase=WorkOrder.Phase.IN_PROGRESS,
            created_by=self.user_ops_coord,
        )

        sig_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        signer_name = "Ing. Roberto Calero"
        signer_title = "Gerente de Planta"
        signer_id = "001-120580-0002A"

        # Captura legítima vía POST por el Técnico asignado (20)
        client_tech = self._get_client_for_user(self.user_tech1)
        url_sign = reverse('operations:workorder_sign', kwargs={'pk': wo.pk})
        response = client_tech.post(url_sign, data={
            'signer_name': signer_name,
            'signer_title': signer_title,
            'signer_id': signer_id,
            'client_signature_data': sig_b64,
        })
        self.assertEqual(response.status_code, 302)

        wo.refresh_from_db()
        self.assertEqual(wo.client_signer_name, signer_name)
        self.assertEqual(wo.client_signer_title, signer_title)
        self.assertEqual(wo.client_signer_id, signer_id)
        self.assertEqual(wo.client_signature_data, sig_b64)
        self.assertIsNotNone(wo.client_signed_at)
        self.assertEqual(wo.phase, WorkOrder.Phase.QUALITY_CHECK)

        # Verificar hash SHA-256
        expected_hash = hashlib.sha256(
            f"{wo.number}|{signer_name}|{signer_id}|{sig_b64}".encode()
        ).hexdigest()
        self.assertEqual(wo.signature_hash, expected_hash)
        self.assertEqual(len(wo.signature_hash), 64)

        # Verificar Timeline Log
        log_sig = wo.timeline_logs.filter(event_type='CLIENT_SIGNED').first()
        self.assertIsNotNone(log_sig)
        self.assertEqual(log_sig.actor, self.user_tech1)
        self.assertIn("Firma digital de conformidad", log_sig.summary)

        # 5. Inmutabilidad: Intento directo en modelo de sobrescribir firma
        with self.assertRaises(ValidationError) as ctx:
            wo.record_client_signature(
                signer_name="Intruso",
                signer_title="Hacker",
                signer_id="000-000000-0000X",
                signature_base64=sig_b64,
                actor=self.user_ops_coord,
            )
        self.assertIn("inmutable", str(ctx.exception).lower())

        # Inmutabilidad en clean() al intentar alterar datos con firma sellada
        wo.client_signer_name = "Nombre Alterado"
        with self.assertRaises(ValidationError):
            wo.clean()

    # --------------------------------------------------------------------------
    # TEST 7: Aislamiento RBAC y Segregación de Funciones en Firma Digital
    # --------------------------------------------------------------------------
    def test_client_signature_rbac_segregation(self):
        """
        Verifica:
        1. Un Técnico (20) que NO pertenece a la cuadrilla asignada recibe HTTP 403 Forbidden.
        2. Un Oficial SST (70) que intente estampar la firma de recepción técnica recibe HTTP 403
           por segregación estricta de funciones de auditoría vs ejecución.
        """
        crew_alpha = Crew.objects.create(
            organization=self.org,
            code="CUAD-ALFA",
            name="Cuadrilla Alfa",
            leader=self.user_engineer,
            is_active=True,
        )
        crew_alpha.members.add(self.user_tech1)

        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.account,
            title="Instalación Industrial",
            assigned_crew=crew_alpha,
            phase=WorkOrder.Phase.IN_PROGRESS,
        )

        sig_data = {
            'signer_name': 'Cliente Test',
            'signer_title': 'Supervisor',
            'signer_id': '001-000000-0000A',
            'signature_data': "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        }
        url_sign = reverse('operations:workorder_sign', kwargs={'pk': wo.pk})

        # Intento por Técnico 2 (de otra cuadrilla) -> 403 Forbidden
        client_tech2 = self._get_client_for_user(self.user_tech2)
        resp_tech2 = client_tech2.post(url_sign, data=sig_data)
        self.assertEqual(resp_tech2.status_code, 403)

        # Intento por Oficial SST (Nivel 70) -> 403 Forbidden
        client_sst = self._get_client_for_user(self.user_sst)
        resp_sst = client_sst.post(url_sign, data=sig_data)
        self.assertEqual(resp_sst.status_code, 403)

    # --------------------------------------------------------------------------
    # TEST 8: Endpoint de Sincronización Offline de Campo (/operations/field-sync/)
    # --------------------------------------------------------------------------
    def test_field_offline_sync_endpoint(self):
        """
        Verifica que el endpoint /operations/field-sync/ procese de forma atómica:
        1. Actualizaciones de partidas ejecutadas en modo offline.
        2. Carga de evidencias fotográficas con métricas de compresión en cliente.
        3. Firma digital capturada sin conexión.
        4. Bloqueo 403 a usuarios no autorizados (SST o técnicos no asignados).
        """
        crew = Crew.objects.create(
            organization=self.org,
            code="CUAD-SYNC",
            name="Cuadrilla Sync",
            leader=self.user_engineer,
            is_active=True,
        )
        crew.members.add(self.user_tech1)

        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.account,
            title="Mantenimiento en Subestación Aislada",
            assigned_crew=crew,
            phase=WorkOrder.Phase.IN_PROGRESS,
        )

        item = WorkOrderItem.objects.create(
            organization=self.org,
            work_order=wo,
            code="ITEM-SYNC-01",
            description="Revisión de aisladores",
            unit="UND",
            planned_quantity=Decimal('4.00'),
            executed_quantity=Decimal('0.00'),
        )

        sync_payload = {
            'work_order_id': str(wo.pk),
            'items_executed': [
                {
                    'item_id': str(item.pk),
                    'executed_quantity': '4.00',
                    'is_completed': True,
                    'notes': 'Completado sin señal en sitio',
                }
            ],
            'evidences': [
                {
                    'title': 'Foto Aislador Limpio',
                    'evidence_type': WorkOrderEvidence.EvidenceType.PHOTO_PROGRESS,
                    'compressed_image_data': 'data:image/jpeg;base64,/9j/4AAQSkZJRg==',
                    'original_size_kb': 3800,
                    'compressed_size_kb': 125,
                    'notes': 'Compresión en cliente 96.7%',
                }
            ],
            'signature': {
                'signer_name': 'Donaldo Triminio',
                'signer_title': 'Jefe de Mantenimiento',
                'signer_id': '081-220475-0001B',
                'signature_data': "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            }
        }

        client_tech1 = self._get_client_for_user(self.user_tech1)
        url_sync = reverse('operations:field_offline_sync')
        resp = client_tech1.post(
            url_sync,
            data=json.dumps(sync_payload),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get('status'), 'synced')
        self.assertGreaterEqual(data.get('processed_count', 0), 2)

        # Verificar actualización del ítem
        item.refresh_from_db()
        self.assertEqual(item.executed_quantity, Decimal('4.00'))
        self.assertTrue(item.is_completed)
        self.assertEqual(item.completed_by, self.user_tech1)

        # Verificar evidencia creada con compresión
        evidence = wo.evidences.filter(title='Foto Aislador Limpio').first()
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.original_size_kb, 3800)
        self.assertEqual(evidence.compressed_size_kb, 125)

        # Verificar firma sellada y fase
        wo.refresh_from_db()
        self.assertEqual(wo.client_signer_name, 'Donaldo Triminio')
        self.assertEqual(len(wo.signature_hash), 64)
        self.assertEqual(wo.phase, WorkOrder.Phase.QUALITY_CHECK)

        # Verificar rechazo 403 si un Oficial SST intenta usar el endpoint de campo
        client_sst = self._get_client_for_user(self.user_sst)
        resp_sst = client_sst.post(
            url_sync,
            data=json.dumps(sync_payload),
            content_type='application/json',
        )
        self.assertEqual(resp_sst.status_code, 403)

    # --------------------------------------------------------------------------
    # TEST 9: Rutas de Infraestructura PWA (/manifest.json y /service-worker.js)
    # --------------------------------------------------------------------------
    def test_pwa_manifest_and_service_worker(self):
        """
        Verifica que los endpoints de PWA respondan con HTTP 200, los tipos MIME
        adecuados y las cabeceras requeridas para instalación en móviles.
        """
        client = Client()

        # 1. Manifest PWA
        resp_manifest = client.get(reverse('pwa_manifest'))
        self.assertEqual(resp_manifest.status_code, 200)
        self.assertEqual(resp_manifest['Content-Type'], 'application/manifest+json')
        manifest_data = json.loads(resp_manifest.content.decode('utf-8'))
        self.assertEqual(manifest_data.get('short_name'), 'ERP SAS')
        self.assertEqual(manifest_data.get('display'), 'standalone')
        self.assertIn(manifest_data.get('theme_color'), ['#0F172A', '#18181B'])

        # 2. Service Worker
        resp_sw = client.get(reverse('pwa_service_worker'))
        self.assertEqual(resp_sw.status_code, 200)
        self.assertIn('application/javascript', resp_sw['Content-Type'])
        self.assertEqual(resp_sw['Service-Worker-Allowed'], '/')
        sw_code = resp_sw.content.decode('utf-8')
        self.assertIn('erp-sas-v1', sw_code)
        self.assertIn('fetch', sw_code)

    # --------------------------------------------------------------------------
    # TEST 10: Acta Oficial de Entrega Imprimible con Firma y Sello SHA-256
    # --------------------------------------------------------------------------
    def test_workorder_certificate_print_view(self):
        """
        Verifica que la vista de Acta Oficial de Entrega (/operations/orders/<pk>/certificate/)
        renderice los datos técnicos, la firma digital y el sello SHA-256 inmutable.
        """
        wo = WorkOrder.objects.create(
            organization=self.org,
            account=self.account,
            title="Construcción de Malla de Puesta a Tierra",
            phase=WorkOrder.Phase.QUALITY_CHECK,
            client_signer_name="Lic. Marcio Blandón",
            client_signer_title="Auditor de Calidad",
            client_signer_id="001-150482-0003M",
            client_signature_data="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            signature_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            client_signed_at=timezone.now(),
        )

        client_eng = self._get_client_for_user(self.user_engineer)
        url_cert = reverse('operations:workorder_certificate_print', kwargs={'pk': wo.pk})
        resp = client_eng.get(url_cert)
        self.assertEqual(resp.status_code, 200)

        html = resp.content.decode('utf-8')
        self.assertIn("ACTA DE ENTREGA TÉCNICA", html)
        self.assertIn(wo.number, html)
        self.assertIn("Lic. Marcio Blandón", html)
        self.assertIn("001-150482-0003M", html)
        self.assertIn("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", html)


class OperationsQuoteToWorkOrderConcurrencyTestCase(TransactionTestCase):
    """
    Prueba de Concurrencia Real Multi-Hilo para Conversión Quote -> WorkOrder (DEF-06).
    Ejecutada sobre PostgreSQL (TransactionTestCase):
    Dos hilos concurrentes intentan convertir simultáneamente la misma cotización aprobada.
    Verifica:
    1. Exactamente una Orden de Trabajo generada en la base de datos.
    2. Exactamente una copia de partidas de BOM (sin duplicados).
    3. Exactamente un evento de timeline FROM_QUOTE.
    4. Ambos hilos terminan exitosamente con respuestas controladas (302 a la OT).
    5. Cero excepciones 500 y ninguna transacción rota.
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
                'is_active': True,
            }
        )
        self.org = Organization.objects.create(
            name="Vector Concurrency Test S.A.",
            legal_name="Vector Concurrency S.A.",
            slug=f"vector-conc-{uuid.uuid4().hex[:8]}",
            tax_id=f"J031{uuid.uuid4().hex[:10].upper()}",
            country=self.country_ni,
            enabled_modules=['crm', 'operations'],
            is_active=True,
        )
        self.account = Account.objects.create(
            organization=self.org,
            commercial_name="Cliente Concurrencia",
        )
        self.user_coord = User.objects.create_user(
            email=f'coord_{uuid.uuid4().hex[:6]}@vector.com.ni',
            full_name='Ing. Concurrente',
            password='password123'
        )
        OrganizationMembership.objects.create(
            user=self.user_coord,
            organization=self.org,
            role_level=RoleLevel.OPERATIONS_COORD,
            is_active=True,
            is_default_org=True,
        )
        self.quote = Quote.objects.create(
            organization=self.org,
            account=self.account,
            title="Suministro de Transformadores 500kVA",
            status=Quote.Status.APPROVED,
            approved_by=self.user_coord,
            approved_at=timezone.now(),
        )
        self.item1 = QuoteItem.objects.create(
            organization=self.org,
            quote=self.quote,
            item_type=QuoteItem.ItemType.MATERIAL,
            code="TRF-500KVA",
            description="Transformador Trifásico Sumergido en Aceite",
            quantity=Decimal('2.00'),
            unit_price=Decimal('12000.00'),
        )

    def test_concurrent_quote_conversion_creates_single_work_order(self):
        """
        Dos hilos concurrentes ejecutando POST /operations/from-quote/<pk>/ a la vez:
        - Exactamente 1 WorkOrder creada.
        - Exactamente 1 ítem en el BOM de la WorkOrder.
        - Exactamente 1 log FROM_QUOTE.
        - Ambos hilos responden con código 302 y sin error 500.
        """
        if connection.vendor != 'postgresql':
            self.skipTest("Esta prueba de concurrencia multi-hilo requiere PostgreSQL.")

        url_convert = reverse('operations:convert_from_quote', kwargs={'quote_pk': self.quote.pk})
        results = []
        errors = []

        def worker():
            connection.close()
            try:
                client = Client()
                client.force_login(self.user_coord)
                session = client.session
                session['active_org_id'] = str(self.org.id)
                session.save()

                resp = client.post(url_convert)
                results.append({
                    'status_code': resp.status_code,
                    'location': resp.url if hasattr(resp, 'url') else resp.get('Location', ''),
                })
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

        # 1. Cero excepciones no controladas
        self.assertEqual(len(errors), 0, f"Errores concurrentes inesperados: {errors}")
        self.assertEqual(len(results), 2, "Ambos hilos debieron recibir respuesta HTTP")

        # 2. Ambos deben recibir redirección HTTP 302 (a la OT creada o existente)
        for r in results:
            self.assertEqual(r['status_code'], 302, f"Respuesta no controlada: {r}")

        # 3. Exactamente una Orden de Trabajo en la base de datos
        wos = list(WorkOrder.objects.filter(source_quote=self.quote))
        self.assertEqual(len(wos), 1, f"Se crearon {len(wos)} órdenes de trabajo en lugar de 1")
        wo = wos[0]

        # 4. Ambos hilos deben haber redirigido a la misma OT
        expected_url = reverse('operations:workorder_detail', kwargs={'pk': wo.pk})
        for r in results:
            self.assertEqual(r['location'], expected_url)

        # 5. Exactamente una partida técnica copiada
        self.assertEqual(wo.items.count(), 1)
        self.assertEqual(wo.items.first().code, "TRF-500KVA")

        # 6. Exactamente un registro de bitácora FROM_QUOTE
        self.assertEqual(wo.timeline_logs.filter(event_type='FROM_QUOTE').count(), 1)


# ==============================================================================
# FASE 1C.32: SUITE DE MÁQUINA DE ESTADOS FINITOS Y CICLO DE VIDA (DEF-19)
# ==============================================================================

class WorkOrderLifecycleFSMTestCase(TestCase):
    """
    Suite de Pruebas de Integridad del Ciclo de Vida y Transiciones de OT (DEF-19).
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
                'is_active': True,
            }
        )

        # 2. Organización Principal Alpha
        self.org_a = Organization.objects.create(
            name="Energía y Maniobras Alpha S.A.",
            legal_name="Energía y Maniobras Alpha Nicaragua S.A.",
            slug=f"org-alpha-{uuid.uuid4().hex[:6]}",
            country=self.country_ni,
            enabled_modules=['crm', 'operations', 'inventory'],
            is_active=True,
        )

        # 3. Organización Aislada Beta
        self.org_b = Organization.objects.create(
            name="Servicios Eléctricos Beta S.A.",
            legal_name="Servicios Eléctricos Beta Nicaragua S.A.",
            slug=f"org-beta-{uuid.uuid4().hex[:6]}",
            country=self.country_ni,
            enabled_modules=['crm', 'operations', 'inventory'],
            is_active=True,
        )

        # 4. Usuarios por Nivel Jerárquico en Org A
        self.user_tech = User.objects.create_user(
            email=f'tech_{uuid.uuid4().hex[:6]}@alpha.com.ni',
            full_name='Técnico Campo Alfa',
            password='password123',
        )
        OrganizationMembership.objects.create(
            user=self.user_tech,
            organization=self.org_a,
            role_level=RoleLevel.TECHNICIAN,
            is_active=True,
            is_default_org=True,
        )

        self.user_eng = User.objects.create_user(
            email=f'eng_{uuid.uuid4().hex[:6]}@alpha.com.ni',
            full_name='Ing. Supervisor Alfa',
            password='password123',
        )
        OrganizationMembership.objects.create(
            user=self.user_eng,
            organization=self.org_a,
            role_level=RoleLevel.ENGINEER,
            is_active=True,
            is_default_org=True,
        )

        self.user_sst = User.objects.create_user(
            email=f'sst_{uuid.uuid4().hex[:6]}@alpha.com.ni',
            full_name='Oficial SST Alfa',
            password='password123',
        )
        OrganizationMembership.objects.create(
            user=self.user_sst,
            organization=self.org_a,
            role_level=RoleLevel.SST_OFFICER,
            is_active=True,
            is_default_org=True,
        )

        self.user_coord = User.objects.create_user(
            email=f'coord_{uuid.uuid4().hex[:6]}@alpha.com.ni',
            full_name='Coordinador Operativo Alfa',
            password='password123',
        )
        OrganizationMembership.objects.create(
            user=self.user_coord,
            organization=self.org_a,
            role_level=RoleLevel.OPERATIONS_COORD,
            is_active=True,
            is_default_org=True,
        )

        self.user_owner = User.objects.create_user(
            email=f'owner_{uuid.uuid4().hex[:6]}@alpha.com.ni',
            full_name='Owner Administrador Alfa',
            password='password123',
        )
        OrganizationMembership.objects.create(
            user=self.user_owner,
            organization=self.org_a,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )

        # 5. Usuario en Org B (para pruebas de aislamiento)
        self.user_beta_coord = User.objects.create_user(
            email=f'coord_{uuid.uuid4().hex[:6]}@beta.com.ni',
            full_name='Coordinador Beta',
            password='password123',
        )
        OrganizationMembership.objects.create(
            user=self.user_beta_coord,
            organization=self.org_b,
            role_level=RoleLevel.OPERATIONS_COORD,
            is_active=True,
            is_default_org=True,
        )

        # 6. Cuenta y Cuadrilla
        self.account_a = Account.objects.create(
            organization=self.org_a,
            commercial_name="Cliente Industrial Managua S.A.",
        )
        self.crew_a = Crew.objects.create(
            organization=self.org_a,
            name="Cuadrilla Alfa Especializada",
            leader=self.user_eng,
        )
        self.crew_a.members.add(self.user_tech)

    def _get_client(self, user, org=None):
        client = Client()
        client.force_login(user)
        session = client.session
        target_org = org or self.org_a
        session['active_org_id'] = str(target_org.id)
        session.save()
        return client

    def _create_work_order(self, phase=WorkOrder.Phase.DRAFT, is_high_risk=False, sst_cleared=False):
        return WorkOrder.objects.create(
            organization=self.org_a,
            number=f"OT-FSM-{uuid.uuid4().hex[:6].upper()}",
            title="Mantenimiento Subestación 138kV",
            account=self.account_a,
            assigned_engineer=self.user_eng,
            assigned_crew=self.crew_a,
            phase=phase,
            is_high_risk=is_high_risk,
            sst_cleared=sst_cleared,
            created_by=self.user_coord,
        )

    # --------------------------------------------------------------------------
    # 1. FLUJO SECUENCIAL COMPLETO VÁLIDO (DEF19-D04)
    # --------------------------------------------------------------------------
    def test_ordinary_sequential_flow_happy_path(self):
        """
        Recorre exitosamente todo el ciclo de vida ordinario:
        DRAFT -> ENGINEERING -> SCHEDULED -> IN_PROGRESS -> QUALITY_CHECK -> COMPLETED.
        """
        wo = self._create_work_order(phase=WorkOrder.Phase.DRAFT)
        client_eng = self._get_client(self.user_eng)
        client_coord = self._get_client(self.user_coord)
        url_trans = reverse('operations:workorder_phase_transition', kwargs={'pk': wo.pk})

        # 1. DRAFT -> ENGINEERING (Ingeniero 50)
        resp = client_eng.post(url_trans, {'phase': WorkOrder.Phase.ENGINEERING})
        self.assertEqual(resp.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.ENGINEERING)
        self.assertTrue(wo.timeline_logs.filter(event_type='PHASE_CHANGE').exists())

        # 2. ENGINEERING -> SCHEDULED (Ingeniero 50)
        resp = client_eng.post(url_trans, {'phase': WorkOrder.Phase.SCHEDULED})
        self.assertEqual(resp.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.SCHEDULED)

        # 3. SCHEDULED -> IN_PROGRESS (Ingeniero 50, bajo riesgo)
        resp = client_eng.post(url_trans, {'phase': WorkOrder.Phase.IN_PROGRESS})
        self.assertEqual(resp.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.IN_PROGRESS)

        # 4. IN_PROGRESS -> QUALITY_CHECK (Ingeniero 50)
        resp = client_eng.post(url_trans, {'phase': WorkOrder.Phase.QUALITY_CHECK})
        self.assertEqual(resp.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.QUALITY_CHECK)

        # 5. QUALITY_CHECK -> COMPLETED (Coordinador 80)
        resp = client_coord.post(url_trans, {
            'phase': WorkOrder.Phase.COMPLETED,
            'notes': 'Recepción a satisfacción total de la obra.',
        })
        self.assertEqual(resp.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.COMPLETED)
        self.assertIsNotNone(wo.completed_at)

    # --------------------------------------------------------------------------
    # 2. SALTOS DE FASE Y RETROCESOS NO PERMITIDOS
    # --------------------------------------------------------------------------
    def test_arbitrary_phase_jumps_rejected(self):
        """Saltos arbitrarios son rechazados en el servidor sin modificar estado ni bitácora."""
        wo = self._create_work_order(phase=WorkOrder.Phase.DRAFT)
        client_eng = self._get_client(self.user_eng)
        client_coord = self._get_client(self.user_coord)
        url_trans = reverse('operations:workorder_phase_transition', kwargs={'pk': wo.pk})

        initial_logs_count = wo.timeline_logs.count()

        # DRAFT -> QUALITY_CHECK
        resp = client_eng.post(url_trans, {'phase': WorkOrder.Phase.QUALITY_CHECK})
        self.assertEqual(resp.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.DRAFT)
        self.assertEqual(wo.timeline_logs.count(), initial_logs_count)

        # DRAFT -> COMPLETED (incluso por Coordinador)
        resp = client_coord.post(url_trans, {'phase': WorkOrder.Phase.COMPLETED})
        self.assertEqual(resp.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.DRAFT)

        # SCHEDULED -> QUALITY_CHECK
        wo_sched = self._create_work_order(phase=WorkOrder.Phase.SCHEDULED)
        url_sched = reverse('operations:workorder_phase_transition', kwargs={'pk': wo_sched.pk})
        resp = client_eng.post(url_sched, {'phase': WorkOrder.Phase.QUALITY_CHECK})
        self.assertEqual(resp.status_code, 302)
        wo_sched.refresh_from_db()
        self.assertEqual(wo_sched.phase, WorkOrder.Phase.SCHEDULED)

    def test_unapproved_rollbacks_rejected(self):
        """Retrocesos ordinarios no aprobados son rechazados (DEF19-D04)."""
        client_eng = self._get_client(self.user_eng)

        # ENGINEERING -> DRAFT
        wo_eng = self._create_work_order(phase=WorkOrder.Phase.ENGINEERING)
        url = reverse('operations:workorder_phase_transition', kwargs={'pk': wo_eng.pk})
        resp = client_eng.post(url, {'phase': WorkOrder.Phase.DRAFT})
        self.assertEqual(resp.status_code, 302)
        wo_eng.refresh_from_db()
        self.assertEqual(wo_eng.phase, WorkOrder.Phase.ENGINEERING)

        # SCHEDULED -> ENGINEERING
        wo_sched = self._create_work_order(phase=WorkOrder.Phase.SCHEDULED)
        url_sched = reverse('operations:workorder_phase_transition', kwargs={'pk': wo_sched.pk})
        resp = client_eng.post(url_sched, {'phase': WorkOrder.Phase.ENGINEERING})
        self.assertEqual(resp.status_code, 302)
        wo_sched.refresh_from_db()
        self.assertEqual(wo_sched.phase, WorkOrder.Phase.SCHEDULED)

    # --------------------------------------------------------------------------
    # 3. REGLAS DE CANCELACIÓN (DEF19-D05)
    # --------------------------------------------------------------------------
    def test_cancellation_by_coordinator_authorized_from_early_phases(self):
        """Coordinador (80+) cancela exitosamente desde DRAFT, ENGINEERING y SCHEDULED con motivo."""
        client_coord = self._get_client(self.user_coord)

        # Desde DRAFT
        wo1 = self._create_work_order(phase=WorkOrder.Phase.DRAFT)
        url1 = reverse('operations:workorder_phase_transition', kwargs={'pk': wo1.pk})
        resp1 = client_coord.post(url1, {'phase': WorkOrder.Phase.CANCELLED, 'notes': 'Proyecto desistido por el cliente'})
        self.assertEqual(resp1.status_code, 302)
        wo1.refresh_from_db()
        self.assertEqual(wo1.phase, WorkOrder.Phase.CANCELLED)
        self.assertTrue(wo1.timeline_logs.filter(event_type='CANCELLED').exists())

        # Desde ENGINEERING
        wo2 = self._create_work_order(phase=WorkOrder.Phase.ENGINEERING)
        url2 = reverse('operations:workorder_phase_transition', kwargs={'pk': wo2.pk})
        resp2 = client_coord.post(url2, {'phase': WorkOrder.Phase.CANCELLED, 'notes': 'Inviabilidad técnica estructural'})
        self.assertEqual(resp2.status_code, 302)
        wo2.refresh_from_db()
        self.assertEqual(wo2.phase, WorkOrder.Phase.CANCELLED)

        # Desde SCHEDULED
        wo3 = self._create_work_order(phase=WorkOrder.Phase.SCHEDULED)
        url3 = reverse('operations:workorder_phase_transition', kwargs={'pk': wo3.pk})
        resp3 = client_coord.post(url3, {'phase': WorkOrder.Phase.CANCELLED, 'notes': 'Cancelada antes de movilización de cuadrilla'})
        self.assertEqual(resp3.status_code, 302)
        wo3.refresh_from_db()
        self.assertEqual(wo3.phase, WorkOrder.Phase.CANCELLED)

    def test_cancellation_rejected_without_reason_or_by_engineer(self):
        """Cancelación sin motivo o por rol inferior a 80 es rechazada."""
        client_coord = self._get_client(self.user_coord)
        client_eng = self._get_client(self.user_eng)
        wo = self._create_work_order(phase=WorkOrder.Phase.DRAFT)
        url = reverse('operations:workorder_phase_transition', kwargs={'pk': wo.pk})

        # Sin motivo (notas vacías) por Coordinador
        resp = client_coord.post(url, {'phase': WorkOrder.Phase.CANCELLED, 'notes': '   '})
        self.assertEqual(resp.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.DRAFT)

        # Por Ingeniero (Rol 50)
        resp_eng = client_eng.post(url, {'phase': WorkOrder.Phase.CANCELLED, 'notes': 'Intento de cancelación por supervisor'})
        self.assertEqual(resp_eng.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.DRAFT)

    def test_cancellation_rejected_from_active_or_terminal_phases(self):
        """No se permite cancelar desde IN_PROGRESS, QUALITY_CHECK, COMPLETED ni CANCELLED."""
        client_coord = self._get_client(self.user_coord)

        for blocked_phase in [WorkOrder.Phase.IN_PROGRESS, WorkOrder.Phase.QUALITY_CHECK, WorkOrder.Phase.COMPLETED, WorkOrder.Phase.CANCELLED]:
            wo = self._create_work_order(phase=blocked_phase)
            url = reverse('operations:workorder_phase_transition', kwargs={'pk': wo.pk})
            resp = client_coord.post(url, {'phase': WorkOrder.Phase.CANCELLED, 'notes': 'Cancelación no permitida en esta fase'})
            self.assertEqual(resp.status_code, 302)
            wo.refresh_from_db()
            self.assertEqual(wo.phase, blocked_phase, f"Fase {blocked_phase} no debió permitir cancelación.")

    # --------------------------------------------------------------------------
    # 4. RECHAZO DE CALIDAD Y PRESERVACIÓN DE FIRMA (DEF19-D02 / DEF19-D04)
    # --------------------------------------------------------------------------
    def test_quality_rejection_requires_reason_and_returns_to_in_progress(self):
        """Rechazo desde QUALITY_CHECK exige motivo y retorna la OT a IN_PROGRESS."""
        client_eng = self._get_client(self.user_eng)
        wo = self._create_work_order(phase=WorkOrder.Phase.QUALITY_CHECK)
        url = reverse('operations:workorder_phase_transition', kwargs={'pk': wo.pk})

        # Sin motivo -> Rechazado
        resp_empty = client_eng.post(url, {'phase': WorkOrder.Phase.IN_PROGRESS, 'notes': ''})
        self.assertEqual(resp_empty.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.QUALITY_CHECK)

        # Con motivo válido -> Exitoso
        resp_ok = client_eng.post(url, {
            'phase': WorkOrder.Phase.IN_PROGRESS,
            'notes': 'No conformidad: Se detectó fuga de aceite dieléctrico en borne secundario.',
        })
        self.assertEqual(resp_ok.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.IN_PROGRESS)
        log = wo.timeline_logs.filter(event_type='QUALITY_REJECT').first()
        self.assertIsNotNone(log)
        self.assertIn("Rechazo de Calidad", log.summary)

    def test_quality_rejection_preserves_original_signature_in_db(self):
        """El rechazo de calidad preserva íntegramente los campos de la firma original del cliente."""
        wo = self._create_work_order(phase=WorkOrder.Phase.IN_PROGRESS)
        dummy_png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

        orig_hash = wo.record_client_signature(
            signer_name="Don Roberto Cliente",
            signer_title="Gerente de Planta",
            signer_id="001-201085-0001A",
            signature_base64=dummy_png,
            actor=self.user_tech,
        )
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.QUALITY_CHECK)
        self.assertEqual(wo.signature_hash, orig_hash)

        # Ejecutar rechazo de calidad
        client_eng = self._get_client(self.user_eng)
        url = reverse('operations:workorder_phase_transition', kwargs={'pk': wo.pk})
        resp = client_eng.post(url, {
            'phase': WorkOrder.Phase.IN_PROGRESS,
            'notes': 'Rechazado en auditoría por falta de pruebas de termografía.',
        })
        self.assertEqual(resp.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.IN_PROGRESS)

        # Confirmación byte por byte de preservación de firma
        self.assertEqual(wo.signature_hash, orig_hash)
        self.assertEqual(wo.client_signature_data, dummy_png)
        self.assertEqual(wo.client_signer_name, "Don Roberto Cliente")
        self.assertEqual(wo.client_signer_id, "001-201085-0001A")

    # --------------------------------------------------------------------------
    # 5. REAPERTURA EXCEPCIONAL (DEF19-D01)
    # --------------------------------------------------------------------------
    def test_exceptional_reopen_by_owner_admin_success(self):
        """Owner/Admin (100) reabre OT COMPLETED hacia IN_PROGRESS con motivo >= 15 chars."""
        wo = self._create_work_order(phase=WorkOrder.Phase.COMPLETED)
        wo.completed_at = timezone.now()
        wo.save(update_fields=['completed_at'])

        client_owner = self._get_client(self.user_owner)
        url = reverse('operations:workorder_phase_transition', kwargs={'pk': wo.pk})

        resp = client_owner.post(url, {
            'phase': WorkOrder.Phase.IN_PROGRESS,
            'notes': 'Reapertura excepcional autorizada por dirección general para subsanar garantía.',
        })
        self.assertEqual(resp.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.IN_PROGRESS)
        self.assertIsNone(wo.completed_at, "completed_at debe ser limpiado al reabrir una OT completada.")

        log = wo.timeline_logs.filter(event_type='EXCEPTIONAL_REOPEN').first()
        self.assertIsNotNone(log)
        self.assertIn("Reapertura excepcional", log.summary)
        self.assertEqual(log.actor, self.user_owner)

        # También verificar reapertura excepcional desde CANCELLED
        wo_canc = self._create_work_order(phase=WorkOrder.Phase.CANCELLED)
        url_canc = reverse('operations:workorder_phase_transition', kwargs={'pk': wo_canc.pk})
        resp_canc = client_owner.post(url_canc, {
            'phase': WorkOrder.Phase.IN_PROGRESS,
            'notes': 'Reapertura excepcional: Reactivación de contrato y financiamiento bancario.',
        })
        self.assertEqual(resp_canc.status_code, 302)
        wo_canc.refresh_from_db()
        self.assertEqual(wo_canc.phase, WorkOrder.Phase.IN_PROGRESS)
        self.assertIsNone(wo_canc.completed_at)

    def test_exceptional_reopen_rejected_for_inferior_roles(self):
        """Coordinador (80) e Ingeniero (50) no pueden reabrir órdenes terminales."""
        wo = self._create_work_order(phase=WorkOrder.Phase.COMPLETED)
        url = reverse('operations:workorder_phase_transition', kwargs={'pk': wo.pk})

        # Coordinador (80)
        client_coord = self._get_client(self.user_coord)
        resp_coord = client_coord.post(url, {
            'phase': WorkOrder.Phase.IN_PROGRESS,
            'notes': 'Intento de reapertura por coordinación sin aval de gerencia.',
        })
        self.assertEqual(resp_coord.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.COMPLETED)

        # Ingeniero (50)
        client_eng = self._get_client(self.user_eng)
        resp_eng = client_eng.post(url, {
            'phase': WorkOrder.Phase.IN_PROGRESS,
            'notes': 'Intento de reapertura por ingeniero residente en campo.',
        })
        self.assertEqual(resp_eng.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.COMPLETED)

    def test_exceptional_reopen_rejected_with_short_reason(self):
        """Motivo de reapertura con menos de 15 caracteres es rechazado."""
        wo = self._create_work_order(phase=WorkOrder.Phase.COMPLETED)
        url = reverse('operations:workorder_phase_transition', kwargs={'pk': wo.pk})
        client_owner = self._get_client(self.user_owner)

        resp = client_owner.post(url, {
            'phase': WorkOrder.Phase.IN_PROGRESS,
            'notes': 'Reabrir OT      ',  # 10 chars tras strip()
        })
        self.assertEqual(resp.status_code, 302)
        wo.refresh_from_db()
        self.assertEqual(wo.phase, WorkOrder.Phase.COMPLETED)

    def test_exceptional_reopen_invalid_destination_rejected(self):
        """Reapertura excepcional hacia destinos distintos de IN_PROGRESS es rechazada."""
        wo = self._create_work_order(phase=WorkOrder.Phase.COMPLETED)
        url = reverse('operations:workorder_phase_transition', kwargs={'pk': wo.pk})
        client_owner = self._get_client(self.user_owner)

        for invalid_target in [WorkOrder.Phase.QUALITY_CHECK, WorkOrder.Phase.DRAFT, WorkOrder.Phase.ENGINEERING]:
            resp = client_owner.post(url, {
                'phase': invalid_target,
                'notes': 'Motivo suficientemente largo para la prueba pero con destino inválido.',
            })
            self.assertEqual(resp.status_code, 302)
            wo.refresh_from_db()
            self.assertEqual(wo.phase, WorkOrder.Phase.COMPLETED)

    # --------------------------------------------------------------------------
    # 6. AISLAMIENTO MULTI-TENANT Y SEGURIDAD SST
    # --------------------------------------------------------------------------
    def test_multitenant_isolation_rejects_cross_tenant_transition(self):
        """Un usuario de otra organización no puede modificar una OT ajena."""
        wo_a = self._create_work_order(phase=WorkOrder.Phase.DRAFT)
        client_beta = self._get_client(self.user_beta_coord, org=self.org_b)
        url = reverse('operations:workorder_phase_transition', kwargs={'pk': wo_a.pk})

        resp = client_beta.post(url, {'phase': WorkOrder.Phase.ENGINEERING})
        self.assertEqual(resp.status_code, 302)
        wo_a.refresh_from_db()
        self.assertEqual(wo_a.phase, WorkOrder.Phase.DRAFT)

    def test_sst_lock_enforced_in_fsm_for_high_risk(self):
        """FSM bloquea la transición a IN_PROGRESS si la OT es de alto riesgo y no ha sido liberada por SST."""
        wo_risk = self._create_work_order(
            phase=WorkOrder.Phase.SCHEDULED,
            is_high_risk=True,
            sst_cleared=False,
        )
        client_eng = self._get_client(self.user_eng)
        url = reverse('operations:workorder_phase_transition', kwargs={'pk': wo_risk.pk})

        resp = client_eng.post(url, {'phase': WorkOrder.Phase.IN_PROGRESS})
        self.assertEqual(resp.status_code, 302)
        wo_risk.refresh_from_db()
        self.assertEqual(wo_risk.phase, WorkOrder.Phase.SCHEDULED, "Debe permanecer bloqueada por SST.")


class WorkOrderLifecycleConcurrencyTestCase(TransactionTestCase):
    """
    Prueba de Concurrencia Real Multi-Hilo para Transiciones de Fase de OT (DEF-19).
    Ejecutada sobre PostgreSQL con select_for_update():
    Dos hilos concurrentes intentan transicionar simultáneamente la misma OT.
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
                'is_active': True,
            }
        )
        self.org = Organization.objects.create(
            name="Vector Concurrency FSM S.A.",
            legal_name="Vector Concurrency FSM S.A.",
            slug=f"vector-fsm-conc-{uuid.uuid4().hex[:8]}",
            country=self.country_ni,
            enabled_modules=['crm', 'operations', 'inventory'],
            is_active=True,
        )
        self.account = Account.objects.create(
            organization=self.org,
            commercial_name="Cliente Concurrencia FSM",
        )
        self.user_coord = User.objects.create_user(
            email=f'coord_fsm_{uuid.uuid4().hex[:6]}@vector.com.ni',
            full_name='Coord FSM',
            password='password123',
        )
        OrganizationMembership.objects.create(
            user=self.user_coord,
            organization=self.org,
            role_level=RoleLevel.OPERATIONS_COORD,
            is_active=True,
            is_default_org=True,
        )
        self.wo = WorkOrder.objects.create(
            organization=self.org,
            number=f"OT-CONC-{uuid.uuid4().hex[:6].upper()}",
            title="OT Concurrente FSM",
            account=self.account,
            phase=WorkOrder.Phase.DRAFT,
            created_by=self.user_coord,
        )

    def test_concurrent_phase_transitions_serialized_with_select_for_update(self):
        """
        Dos hilos intentan transicionar la misma OT en DRAFT:
        - Hilo 1 intenta avanzar a ENGINEERING.
        - Hilo 2 intenta cancelar a CANCELLED con motivo.
        select_for_update() garantiza que solo una transición ocurra primero y la segunda
        sea evaluada bajo el nuevo estado resultante, sin corrupciones ni errores 500.
        """
        if connection.vendor != 'postgresql':
            self.skipTest("Esta prueba de concurrencia multi-hilo requiere PostgreSQL.")

        url_trans = reverse('operations:workorder_phase_transition', kwargs={'pk': self.wo.pk})
        results = []
        errors = []

        def worker(target_phase, notes):
            try:
                client = Client()
                client.force_login(self.user_coord)
                session = client.session
                session['active_org_id'] = str(self.org.id)
                session.save()

                resp = client.post(url_trans, {'phase': target_phase, 'notes': notes})
                results.append(resp.status_code)
            except Exception as e:
                errors.append(e)
            finally:
                connections.close_all()

        t1 = threading.Thread(target=worker, args=(WorkOrder.Phase.ENGINEERING, 'Avanzar a ingeniería'))
        t2 = threading.Thread(target=worker, args=(WorkOrder.Phase.CANCELLED, 'Cancelar por imprevisto'))

        t1.start()
        t2.start()

        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertFalse(t1.is_alive(), "El hilo 1 no finalizó dentro del tiempo límite.")
        self.assertFalse(t2.is_alive(), "El hilo 2 no finalizó dentro del tiempo límite.")
        self.assertEqual(errors, [], f"Errores en hilos concurrentes: {errors}")
        self.assertEqual(len(results), 2, "Ambos hilos debieron recibir respuesta HTTP.")
        for status in results:
            self.assertEqual(status, 302, "Ambos hilos deben recibir redirección controlada.")

        self.wo.refresh_from_db()
        # Debe haber finalizado en una de las dos fases legales (ENGINEERING o CANCELLED)
        self.assertIn(self.wo.phase, [WorkOrder.Phase.ENGINEERING, WorkOrder.Phase.CANCELLED])

    def tearDown(self):
        connections.close_all()
        super().tearDown()

