from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from apps.core.models import CountryConfig
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel
from apps.crm.models import Account, AccountContact, Opportunity, Quote, QuoteItem

User = get_user_model()


class CrmModuleEndToEndTestCase(TestCase):
    """
    Suite de Pruebas End-to-End para el Módulo Comercial (CRM).
    Valida Aislamiento Multi-Tenant, Matemática Bimonetaria de Cotizaciones (DGI Nicaragua),
    Efectos de Aprobación y Matriz Estricta de Seguridad RBAC.
    """
    def setUp(self):
        # 1. Configuración de Jurisdicción Fiscal Nicaragua (NI)
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

        # 2. Organización Operativa Principal
        self.org = Organization.objects.create(
            name="Vector Ingeniería Nicaragua, S.A.",
            legal_name="Vector Ingeniería de Centroamérica, S.A.",
            slug="vector-ni",
            tax_id="J0310000199999",
            country=self.country_ni,
            enabled_modules=['crm', 'operations', 'sst', 'inventory'],
            is_active=True,
        )

        # Organización Invasora (Para verificar Aislamiento Multi-Tenant)
        self.org_intruder = Organization.objects.create(
            name="Empresa Competidora S.A.",
            legal_name="Competidora S.A.",
            slug="competidora-ni",
            tax_id="J0310000188888",
            country=self.country_ni,
            enabled_modules=['crm'],
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

        # Nivel 85: Gerente Comercial
        self.user_comm_mgr = User.objects.create_user(
            email='comercial@vector.com.ni',
            full_name='Lic. Sofía Alemán (Comercial)',
            password='password123'
        )
        OrganizationMembership.objects.create(
            user=self.user_comm_mgr,
            organization=self.org,
            role_level=RoleLevel.COMMERCIAL_MGR,
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

        # Nivel 50: Ingeniero / Residente
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

        # Nivel 15: Asesor Comercial 1
        self.user_advisor = User.objects.create_user(
            email='asesor@vector.com.ni',
            full_name='Carlos Venta (Asesor Comercial)',
            password='password123'
        )
        self.membership_advisor = OrganizationMembership.objects.create(
            user=self.user_advisor,
            organization=self.org,
            role_level=RoleLevel.SALES_ADVISOR,
            is_active=True,
            is_default_org=True,
        )

        # Nivel 15: Asesor Comercial 2
        self.user_advisor_2 = User.objects.create_user(
            email='asesor2@vector.com.ni',
            full_name='Ana Venta (Asesora 2)',
            password='password123'
        )
        self.membership_advisor_2 = OrganizationMembership.objects.create(
            user=self.user_advisor_2,
            organization=self.org,
            role_level=RoleLevel.SALES_ADVISOR,
            is_active=True,
            is_default_org=True,
        )

        # Nivel 85: Gerente Comercial 2
        self.user_comm_mgr_2 = User.objects.create_user(
            email='comercial2@vector.com.ni',
            full_name='Elena Gomez (Gerente 2)',
            password='password123'
        )
        self.membership_comm_mgr_2 = OrganizationMembership.objects.create(
            user=self.user_comm_mgr_2,
            organization=self.org,
            role_level=RoleLevel.COMMERCIAL_MGR,
            is_active=True,
            is_default_org=True,
        )

    # --------------------------------------------------------------------------
    # TEST 1: Modelos Transaccionales, Códigos Correlativos y Aislamiento Tenant
    # --------------------------------------------------------------------------
    def test_account_creation_validation_and_autocode(self):
        """Verifica validación de RUC nicaragüense y correlativo automático CLI-0001."""
        account = Account.objects.create(
            organization=self.org,
            commercial_name="Ingenio Monte Rosa",
            legal_name="Pantaleon Nicaragua S.A.",
            tax_id="J0310000112233",
            account_type=Account.AccountType.CORPORATE,
            lifecycle_stage=Account.LifecycleStage.PROSPECT,
            city="Chinandega",
            default_currency="NIO",
            payment_terms_days=30,
        )
        self.assertEqual(account.code, "CLI-0001")
        self.assertEqual(account.commercial_name, "Ingenio Monte Rosa")

        # Contacto técnico asociado
        contact = AccountContact.objects.create(
            organization=self.org,
            account=account,
            full_name="Ing. Carlos Gutiérrez",
            job_title="Gerente de Planta",
            email="cgutierrez@monterosa.com.ni",
            phone="+505 8888-1234",
            is_primary=True,
        )
        self.assertTrue(contact.is_primary)
        self.assertEqual(account.contacts.count(), 1)

        # Oportunidad asociada
        opp = Opportunity.objects.create(
            organization=self.org,
            account=account,
            title="Modernización de Subestación Eléctrica 138kV",
            stage=Opportunity.Stage.NEW,
            currency="NIO",
            estimated_value=Decimal('150000.00'),
        )
        self.assertTrue(opp.code.startswith("OP-"))
        self.assertEqual(opp.account, account)

        # Verificar Aislamiento Multi-Tenant estricto
        org_accounts = Account.objects.for_org(self.org)
        intruder_accounts = Account.objects.for_org(self.org_intruder)
        self.assertIn(account, org_accounts)
        self.assertNotIn(account, intruder_accounts)

    # --------------------------------------------------------------------------
    # TEST 2: Matemática de Cotización, Precisión Decimal e IVA 15% DGI
    # --------------------------------------------------------------------------
    def test_quote_exact_calculation_and_approval_transition(self):
        """
        Prueba matemática estricta:
        - Partida 1 (Servicio): 1 UND x C$ 50,000.00, desc 0% = C$ 50,000.00
        - Partida 2 (Material): 2 UND x C$ 25,000.00, desc 10% = C$ 45,000.00 (Bruto 50,000.00 - Descuento 5,000.00)
        - Subtotal: C$ 100,000.00
        - Descuento Total: C$ 5,000.00
        - Base Imponible: C$ 95,000.00
        - IVA 15.00%: C$ 14,250.00 (95,000.00 x 0.15)
        - Total General: C$ 109,250.00
        """
        account = Account.objects.create(
            organization=self.org,
            commercial_name="Distribuidora Eléctrica del Norte S.A.",
            tax_id="J0310000112233",
            lifecycle_stage=Account.LifecycleStage.PROSPECT,
        )

        opp = Opportunity.objects.create(
            organization=self.org,
            account=account,
            title="Suministro de Transformadores",
            stage=Opportunity.Stage.PROPOSAL,
            currency="NIO",
            estimated_value=Decimal('109250.00'),
        )

        quote = Quote.objects.create(
            organization=self.org,
            account=account,
            opportunity=opp,
            title="Suministro e Instalación de Subestación",
            currency_code="NIO",
            currency_symbol="C$",
            exchange_rate=Decimal('36.6243'),
            tax_name="IVA",
            tax_rate=Decimal('15.00'),
            apply_tax=True,
            created_by=self.user_advisor,
        )
        self.assertTrue(quote.number.startswith("COT-"))

        # Agregar Partida 1: Servicio
        item1 = QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type=QuoteItem.ItemType.SERVICE,
            code="SRV-ING-01",
            description="Diagnóstico y Pruebas de Resistencia en Bobinados",
            unit="GLB",
            quantity=Decimal('1.00'),
            unit_price=Decimal('50000.00'),
            discount_percent=Decimal('0.00'),
        )
        self.assertEqual(item1.line_total, Decimal('50000.00'))

        # Agregar Partida 2: Material con 10% de descuento
        item2 = QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type=QuoteItem.ItemType.MATERIAL,
            code="MAT-CAB-01",
            description="Cable de Potencia 15kV y Terminales Contráctiles",
            unit="MTS",
            quantity=Decimal('2.00'),
            unit_price=Decimal('25000.00'),
            discount_percent=Decimal('10.00'),
        )
        self.assertEqual(item2.line_total, Decimal('45000.00'))

        # Recargar cotización para verificar totales automáticos
        quote.refresh_from_db()

        self.assertEqual(quote.subtotal, Decimal('100000.00'))
        self.assertEqual(quote.discount_total, Decimal('5000.00'))
        self.assertEqual(quote.tax_total, Decimal('14250.00'))
        self.assertEqual(quote.grand_total, Decimal('109250.00'))

        # Probar Transición de Aprobación Formal
        client = Client()
        client.login(email='comercial@vector.com.ni', password='password123')
        session = client.session
        session['active_org_id'] = str(self.org.id)
        session.save()

        response = client.post(
            reverse('crm:quote_status_transition', kwargs={'pk': quote.pk}),
            data={'status': Quote.Status.APPROVED}
        )
        self.assertEqual(response.status_code, 302)

        quote.refresh_from_db()
        account.refresh_from_db()
        opp.refresh_from_db()

        # Verificar Estado Aprobado y Trazabilidad
        self.assertEqual(quote.status, Quote.Status.APPROVED)
        self.assertEqual(quote.approved_by, self.user_comm_mgr)
        self.assertIsNotNone(quote.approved_at)

        # Verificar Efectos Colaterales de Aprobación
        self.assertEqual(account.lifecycle_stage, Account.LifecycleStage.ACTIVE)
        self.assertEqual(opp.stage, Opportunity.Stage.WON)

    # --------------------------------------------------------------------------
    # TEST 3: Manejo de Cliente Exento de IVA
    # --------------------------------------------------------------------------
    def test_tax_exempt_account_calculation(self):
        """Verifica que si la cuenta tiene is_tax_exempt=True, el IVA es 0.00."""
        account_exempt = Account.objects.create(
            organization=self.org,
            commercial_name="Organismo Internacional Exento",
            tax_id="J0310000112233",
            is_tax_exempt=True,
        )

        quote = Quote.objects.create(
            organization=self.org,
            account=account_exempt,
            title="Servicio Especial de Consultoría",
            currency_code="NIO",
            tax_rate=Decimal('15.00'),
            apply_tax=True,
        )

        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type=QuoteItem.ItemType.SERVICE,
            description="Estudio de Calidad de Energía",
            quantity=Decimal('1.00'),
            unit_price=Decimal('20000.00'),
            discount_percent=Decimal('0.00'),
        )

        quote.refresh_from_db()
        self.assertEqual(quote.subtotal, Decimal('20000.00'))
        self.assertEqual(quote.tax_total, Decimal('0.00'))
        self.assertEqual(quote.grand_total, Decimal('20000.00'))

    # --------------------------------------------------------------------------
    # TEST 4: Matriz Estricta de Jerarquía RBAC en Rutas /crm/*
    # --------------------------------------------------------------------------
    def test_rbac_hierarchy_permissions(self):
        """
        Verifica cumplimiento de permisos:
        1. OWNER (100) y COMMERCIAL_MGR (85): 200 en lectura, 200/302 en escritura/aprobación.
        2. OPERATIONS_COORD (80): 200 en lectura (directorio, pipeline, detalle cotización),
           pero 403 en escritura (crear cliente, agregar ítems, aprobar cotización, mover etapa).
        3. ENGINEER (50) y TECHNICIAN (20): 403 en TODAS las rutas de /crm/*.
        """
        account = Account.objects.create(
            organization=self.org,
            commercial_name="Empresa Minera San Albino",
            tax_id="J0310000112233",
        )
        opp = Opportunity.objects.create(
            organization=self.org,
            account=account,
            title="Mantenimiento Generador Diésel",
        )
        quote = Quote.objects.create(
            organization=self.org,
            account=account,
            title="Cotización Servicio Mina",
            currency_code="NIO",
            created_by=self.user_comm_mgr,
        )

        # ----------------------------------------------------------------------
        # A) Rol 85: Gerente Comercial -> Acceso Total
        # ----------------------------------------------------------------------
        client_85 = Client()
        client_85.login(email='comercial@vector.com.ni', password='password123')
        s85 = client_85.session
        s85['active_org_id'] = str(self.org.id)
        s85.save()

        # Lectura
        self.assertEqual(client_85.get(reverse('crm:account_list')).status_code, 200)
        self.assertEqual(client_85.get(reverse('crm:account_detail', kwargs={'pk': account.pk})).status_code, 200)
        self.assertEqual(client_85.get(reverse('crm:opportunity_kanban')).status_code, 200)
        self.assertEqual(client_85.get(reverse('crm:quote_list')).status_code, 200)
        self.assertEqual(client_85.get(reverse('crm:quote_detail', kwargs={'pk': quote.pk})).status_code, 200)
        self.assertEqual(client_85.get(reverse('crm:quote_print', kwargs={'pk': quote.pk})).status_code, 200)

        # Escritura
        self.assertEqual(client_85.get(reverse('crm:account_create')).status_code, 200)
        self.assertEqual(client_85.get(reverse('crm:quote_create')).status_code, 200)

        # ----------------------------------------------------------------------
        # B) Rol 80: Coordinador de Operaciones -> Solo Lectura Técnica
        # ----------------------------------------------------------------------
        client_80 = Client()
        client_80.login(email='operaciones@vector.com.ni', password='password123')
        s80 = client_80.session
        s80['active_org_id'] = str(self.org.id)
        s80.save()

        # Lectura Permitida (HTTP 200)
        self.assertEqual(client_80.get(reverse('crm:account_list')).status_code, 200)
        self.assertEqual(client_80.get(reverse('crm:account_detail', kwargs={'pk': account.pk})).status_code, 200)
        self.assertEqual(client_80.get(reverse('crm:opportunity_kanban')).status_code, 200)
        self.assertEqual(client_80.get(reverse('crm:quote_list')).status_code, 200)
        self.assertEqual(client_80.get(reverse('crm:quote_detail', kwargs={'pk': quote.pk})).status_code, 200)
        self.assertEqual(client_80.get(reverse('crm:quote_print', kwargs={'pk': quote.pk})).status_code, 200)

        # Escritura Bloqueada por RBAC (HTTP 403)
        self.assertEqual(client_80.get(reverse('crm:account_create')).status_code, 403)
        self.assertEqual(client_80.post(reverse('crm:account_create'), data={}).status_code, 403)
        self.assertEqual(client_80.get(reverse('crm:quote_create')).status_code, 403)
        self.assertEqual(
            client_80.post(
                reverse('crm:quote_add_item', kwargs={'pk': quote.pk}),
                data={'description': 'Intento Invalido'}
            ).status_code,
            403
        )
        self.assertEqual(
            client_80.post(
                reverse('crm:quote_status_transition', kwargs={'pk': quote.pk}),
                data={'status': Quote.Status.APPROVED}
            ).status_code,
            403
        )
        self.assertEqual(
            client_80.post(
                reverse('crm:opportunity_stage_update', kwargs={'pk': opp.pk}),
                data={'stage': Opportunity.Stage.WON}
            ).status_code,
            403
        )

        # ----------------------------------------------------------------------
        # C) Rol 50: Ingeniero / Residente -> Bloqueo Completo en CRM (HTTP 403)
        # ----------------------------------------------------------------------
        client_50 = Client()
        client_50.login(email='ingeniero@vector.com.ni', password='password123')
        s50 = client_50.session
        s50['active_org_id'] = str(self.org.id)
        s50.save()

        self.assertEqual(client_50.get(reverse('crm:account_list')).status_code, 403)
        self.assertEqual(client_50.get(reverse('crm:opportunity_kanban')).status_code, 403)
        self.assertEqual(client_50.get(reverse('crm:quote_list')).status_code, 403)
        self.assertEqual(client_50.get(reverse('crm:quote_detail', kwargs={'pk': quote.pk})).status_code, 403)

        # ----------------------------------------------------------------------
        # D) Rol 20: Técnico de Campo -> Bloqueo Inmediato en CRM (HTTP 403)
        # ----------------------------------------------------------------------
        client_20 = Client()
        client_20.login(email='tecnico@vector.com.ni', password='password123')
        s20 = client_20.session
        s20['active_org_id'] = str(self.org.id)
        s20.save()

        self.assertEqual(client_20.get(reverse('crm:account_list')).status_code, 403)
        self.assertEqual(client_20.get(reverse('crm:opportunity_kanban')).status_code, 403)
        self.assertEqual(client_20.get(reverse('crm:quote_list')).status_code, 403)
        self.assertEqual(client_20.get(reverse('crm:quote_detail', kwargs={'pk': quote.pk})).status_code, 403)

        # ----------------------------------------------------------------------
        # E) Rol 100: Owner / Administrador General -> Acceso Total
        # ----------------------------------------------------------------------
        client_100 = Client()
        client_100.login(email='owner@vector.com.ni', password='password123')
        s100 = client_100.session
        s100['active_org_id'] = str(self.org.id)
        s100.save()

        self.assertEqual(client_100.get(reverse('crm:account_list')).status_code, 200)
        self.assertEqual(client_100.get(reverse('crm:quote_detail', kwargs={'pk': quote.pk})).status_code, 200)
        self.assertEqual(client_100.get(reverse('crm:quote_create')).status_code, 200)

        # ----------------------------------------------------------------------
        # F) Rol 15: Asesor Comercial -> Lectura y Creación OK, Aprobación Bloqueada
        # ----------------------------------------------------------------------
        client_15 = Client()
        client_15.login(email='asesor@vector.com.ni', password='password123')
        s15 = client_15.session
        s15['active_org_id'] = str(self.org.id)
        s15.save()

        # Lectura Permitida (HTTP 200)
        self.assertEqual(client_15.get(reverse('crm:account_list')).status_code, 200)
        self.assertEqual(client_15.get(reverse('crm:account_detail', kwargs={'pk': account.pk})).status_code, 200)
        self.assertEqual(client_15.get(reverse('crm:opportunity_kanban')).status_code, 200)
        self.assertEqual(client_15.get(reverse('crm:quote_list')).status_code, 200)
        self.assertEqual(client_15.get(reverse('crm:quote_detail', kwargs={'pk': quote.pk})).status_code, 200)
        self.assertEqual(client_15.get(reverse('crm:quote_print', kwargs={'pk': quote.pk})).status_code, 200)

        # Escritura Permitida (HTTP 200)
        self.assertEqual(client_15.get(reverse('crm:account_create')).status_code, 200)
        self.assertEqual(client_15.get(reverse('crm:quote_create')).status_code, 200)

        # Aprobación Bloqueada para Asesor (HTTP 403)
        self.assertEqual(
            client_15.post(
                reverse('crm:quote_status_transition', kwargs={'pk': quote.pk}),
                data={'status': Quote.Status.APPROVED}
            ).status_code,
            403
        )

    # --------------------------------------------------------------------------
    # FASE 1C.8.8.2: SUITE DE PRUEBAS DE SEGURIDAD, RBAC COMERCIAL Y SoD
    # --------------------------------------------------------------------------

    def test_sales_advisor_crm_full_lifecycle(self):
        """
        1. El asesor entra al CRM y completa el flujo autorizado:
           Crear cuenta, contacto, oportunidad, cotización en borrador, agregar partida y marcar como enviada.
        """
        client = Client()
        client.login(email='asesor@vector.com.ni', password='password123')
        s = client.session
        s['active_org_id'] = str(self.org.id)
        s.save()

        # 1. Crear Cuenta
        resp = client.post(reverse('crm:account_create'), data={
            'commercial_name': 'Soluciones Industriales del Norte',
            'legal_name': 'Soluciones Industriales del Norte S.A.',
            'account_type': Account.AccountType.CORPORATE,
            'lifecycle_stage': Account.LifecycleStage.PROSPECT,
            'tax_id': 'J0310000888888',
            'email': 'contacto@norte.com.ni',
            'phone': '+505 2222-1111',
            'city': 'Matagalpa',
            'default_currency': 'NIO',
            'payment_terms_days': 15,
        })
        self.assertEqual(resp.status_code, 302)
        account = Account.objects.filter(commercial_name='Soluciones Industriales del Norte').first()
        self.assertIsNotNone(account)
        self.assertEqual(account.organization, self.org)

        # 2. Crear Contacto
        resp_contact = client.post(reverse('crm:account_contact_create', kwargs={'pk': account.pk}), data={
            'full_name': 'Ing. Gabriel Torres',
            'job_title': 'Jefe de Planta',
            'email': 'gtorres@norte.com.ni',
            'phone': '+505 8888-5555',
            'is_primary': True,
        })
        self.assertEqual(resp_contact.status_code, 302)
        self.assertTrue(account.contacts.filter(full_name='Ing. Gabriel Torres').exists())

        # 3. Crear Oportunidad
        resp_opp = client.post(reverse('crm:opportunity_create'), data={
            'account': str(account.pk),
            'title': 'Automatización Tableros de Control Matagalpa',
            'stage': Opportunity.Stage.QUALIFIED,
            'currency': 'NIO',
            'estimated_value': '85000.00',
        })
        self.assertEqual(resp_opp.status_code, 302)
        opp = Opportunity.objects.filter(account=account).first()
        self.assertIsNotNone(opp)

        # 4. Mover Etapa Kanban
        resp_stage = client.post(reverse('crm:opportunity_stage_update', kwargs={'pk': opp.pk}), data={
            'stage': Opportunity.Stage.PROPOSAL,
        })
        self.assertEqual(resp_stage.status_code, 302)
        opp.refresh_from_db()
        self.assertEqual(opp.stage, Opportunity.Stage.PROPOSAL)

        # 5. Crear Cotización Borrador
        resp_quote = client.post(reverse('crm:quote_create'), data={
            'account': str(account.pk),
            'opportunity': str(opp.pk),
            'title': 'Suministro e Instalación de Tableros PLC',
            'currency_code': 'NIO',
            'exchange_rate': '36.6241',
            'apply_tax': True,
            'tax_name': 'IVA',
            'tax_rate': '15.00',
        })
        self.assertEqual(resp_quote.status_code, 302)
        quote = Quote.objects.filter(account=account).first()
        self.assertIsNotNone(quote)
        self.assertEqual(quote.status, Quote.Status.DRAFT)
        self.assertEqual(quote.created_by, self.user_advisor)

        # 6. Agregar Partida
        resp_item = client.post(reverse('crm:quote_add_item', kwargs={'pk': quote.pk}), data={
            'item_type': QuoteItem.ItemType.SERVICE,
            'code': 'SRV-PLC-01',
            'description': 'Programación y Puesta en Marcha PLC Siemens',
            'unit': 'GLB',
            'quantity': '1.00',
            'unit_price': '50000.00',
            'discount_percent': '0.00',
        })
        self.assertEqual(resp_item.status_code, 302)
        self.assertEqual(quote.items.count(), 1)
        quote.refresh_from_db()
        self.assertEqual(quote.subtotal, Decimal('50000.00'))

        # 7. Marcar como Enviada (SENT)
        resp_send = client.post(reverse('crm:quote_status_transition', kwargs={'pk': quote.pk}), data={
            'status': Quote.Status.SENT,
        })
        self.assertEqual(resp_send.status_code, 302)
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.SENT)

    def _create_base_account_and_opp(self):
        account = Account.objects.create(
            organization=self.org,
            commercial_name="Empresa Eléctrica Central",
            legal_name="Empresa Eléctrica Central S.A.",
            tax_id="J0310000555555",
            account_type=Account.AccountType.CORPORATE,
            lifecycle_stage=Account.LifecycleStage.PROSPECT,
            city="Managua",
            default_currency="NIO",
            payment_terms_days=30,
        )
        opp = Opportunity.objects.create(
            organization=self.org,
            account=account,
            title="Modernización de Transformadores",
            stage=Opportunity.Stage.QUALIFIED,
            currency="NIO",
            estimated_value=Decimal('50000.00'),
            assigned_to=self.user_advisor,
        )
        return account, opp

    def test_sales_advisor_cannot_approve_quote(self):
        """2. El asesor no puede aprobar una cotización mediante POST directo (HTTP 403)."""
        account, _ = self._create_base_account_and_opp()
        quote = Quote.objects.create(
            organization=self.org,
            account=account,
            title='Prueba Bloqueo Asesor',
            status=Quote.Status.SENT,
            created_by=self.user_advisor,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-01',
            description='Servicio Base',
            unit='UND',
            quantity=Decimal('1.00'),
            unit_price=Decimal('1000.00'),
        )

        client = Client()
        client.login(email='asesor@vector.com.ni', password='password123')
        s = client.session
        s['active_org_id'] = str(self.org.id)
        s.save()

        resp = client.post(reverse('crm:quote_status_transition', kwargs={'pk': quote.pk}), data={
            'status': Quote.Status.APPROVED,
        })
        self.assertEqual(resp.status_code, 403)
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.SENT)
        self.assertIsNone(quote.approved_by)

    def test_commercial_manager_cannot_self_approve_quote_sod(self):
        """3. El gerente comercial no puede autoaprobar una cotización que creó (SoD HTTP 403)."""
        account, _ = self._create_base_account_and_opp()
        quote = Quote.objects.create(
            organization=self.org,
            account=account,
            title='Cotización del Gerente',
            status=Quote.Status.DRAFT,
            created_by=self.user_comm_mgr,  # Creada por el mismo gerente
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-02',
            description='Consultoría Avanzada',
            unit='HRS',
            quantity=Decimal('10.00'),
            unit_price=Decimal('100.00'),
        )

        client = Client()
        client.login(email='comercial@vector.com.ni', password='password123')
        s = client.session
        s['active_org_id'] = str(self.org.id)
        s.save()

        # Intento de autoaprobación
        resp = client.post(reverse('crm:quote_status_transition', kwargs={'pk': quote.pk}), data={
            'status': Quote.Status.APPROVED,
        })
        self.assertEqual(resp.status_code, 403)
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.DRAFT)
        self.assertIsNone(quote.approved_by)

    def test_commercial_manager_can_approve_quote_created_by_another_user(self):
        """4. El gerente puede aprobar una cotización creada por otro usuario (Asesor) -> 302 y efectos secundarios."""
        account, opp = self._create_base_account_and_opp()
        quote = Quote.objects.create(
            organization=self.org,
            account=account,
            opportunity=opp,
            title='Cotización por Asesor',
            status=Quote.Status.SENT,
            created_by=self.user_advisor,  # Creada por el asesor
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-03',
            description='Mantenimiento',
            unit='GLB',
            quantity=Decimal('1.00'),
            unit_price=Decimal('15000.00'),
        )

        client = Client()
        client.login(email='comercial@vector.com.ni', password='password123')
        s = client.session
        s['active_org_id'] = str(self.org.id)
        s.save()

        resp = client.post(reverse('crm:quote_status_transition', kwargs={'pk': quote.pk}), data={
            'status': Quote.Status.APPROVED,
        })
        self.assertEqual(resp.status_code, 302)
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.APPROVED)
        self.assertEqual(quote.approved_by, self.user_comm_mgr)
        self.assertIsNotNone(quote.approved_at)

        # Efectos secundarios: Cuenta pasa a ACTIVE y Oportunidad a WON
        account.refresh_from_db()
        self.assertEqual(account.lifecycle_stage, Account.LifecycleStage.ACTIVE)
        opp.refresh_from_db()
        self.assertEqual(opp.stage, Opportunity.Stage.WON)

    def test_owner_admin_can_self_approve_quote_with_exception(self):
        """5. El OWNER_ADMIN puede autoaprobar según la excepción de producto aprobada."""
        account, _ = self._create_base_account_and_opp()
        quote = Quote.objects.create(
            organization=self.org,
            account=account,
            title='Cotización del Owner',
            status=Quote.Status.DRAFT,
            created_by=self.user_owner,  # Creada por el Owner
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-04',
            description='Servicio Especial Owner',
            unit='GLB',
            quantity=Decimal('1.00'),
            unit_price=Decimal('20000.00'),
        )

        client = Client()
        client.login(email='owner@vector.com.ni', password='password123')
        s = client.session
        s['active_org_id'] = str(self.org.id)
        s.save()

        resp = client.post(reverse('crm:quote_status_transition', kwargs={'pk': quote.pk}), data={
            'status': Quote.Status.APPROVED,
        })
        self.assertEqual(resp.status_code, 302)
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.APPROVED)
        self.assertEqual(quote.approved_by, self.user_owner)

    def test_quote_items_immutable_when_approved(self):
        """6. Las partidas de cotizaciones aprobadas son inmutables."""
        account, _ = self._create_base_account_and_opp()
        quote = Quote.objects.create(
            organization=self.org,
            account=account,
            title='Cotización Aprobada Inmutable',
            status=Quote.Status.APPROVED,
            created_by=self.user_comm_mgr,
            approved_by=self.user_owner,
        )
        item = QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-05',
            description='Partida Fija',
            unit='UND',
            quantity=Decimal('1.00'),
            unit_price=Decimal('5000.00'),
        )

        client = Client()
        client.login(email='comercial@vector.com.ni', password='password123')
        s = client.session
        s['active_org_id'] = str(self.org.id)
        s.save()

        # Intento de agregar partida a cotización aprobada
        resp_add = client.post(reverse('crm:quote_add_item', kwargs={'pk': quote.pk}), data={
            'item_type': QuoteItem.ItemType.SERVICE,
            'code': 'SRV-ILLEGAL',
            'description': 'Partida No Autorizada',
            'unit': 'UND',
            'quantity': '1.00',
            'unit_price': '1000.00',
        })
        self.assertEqual(resp_add.status_code, 302)
        self.assertEqual(quote.items.count(), 1)  # No se agregó

        # Intento de eliminar partida de cotización aprobada
        resp_del = client.post(reverse('crm:quote_remove_item', kwargs={
            'quote_pk': quote.pk,
            'item_pk': item.pk,
        }))
        self.assertEqual(resp_del.status_code, 302)
        self.assertEqual(quote.items.count(), 1)  # No se eliminó

    def test_sales_advisor_can_only_edit_items_on_own_quotes(self):
        """7. La edición de partidas por el asesor se limita a sus cotizaciones creadas."""
        account, _ = self._create_base_account_and_opp()
        # Cotización creada por Asesor 1
        quote_advisor_1 = Quote.objects.create(
            organization=self.org,
            account=account,
            title='Cotización Asesor 1',
            status=Quote.Status.DRAFT,
            created_by=self.user_advisor,
        )
        item_1 = QuoteItem.objects.create(
            organization=self.org,
            quote=quote_advisor_1,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-A1',
            description='Partida de Asesor 1',
            unit='UND',
            quantity=Decimal('1.00'),
            unit_price=Decimal('3000.00'),
        )

        # Login como Asesor 2
        client_2 = Client()
        client_2.login(email='asesor2@vector.com.ni', password='password123')
        s2 = client_2.session
        s2['active_org_id'] = str(self.org.id)
        s2.save()

        # Asesor 2 intenta agregar partida a cotización de Asesor 1 -> 403 Forbidden
        resp_add = client_2.post(reverse('crm:quote_add_item', kwargs={'pk': quote_advisor_1.pk}), data={
            'item_type': QuoteItem.ItemType.SERVICE,
            'code': 'SRV-HACK',
            'description': 'Intrusión de Partida',
            'unit': 'UND',
            'quantity': '1.00',
            'unit_price': '1000.00',
        })
        self.assertEqual(resp_add.status_code, 403)
        self.assertEqual(quote_advisor_1.items.count(), 1)

        # Asesor 2 intenta eliminar partida de cotización de Asesor 1 -> 403 Forbidden
        resp_del = client_2.post(reverse('crm:quote_remove_item', kwargs={
            'quote_pk': quote_advisor_1.pk,
            'item_pk': item_1.pk,
        }))
        self.assertEqual(resp_del.status_code, 403)
        self.assertEqual(quote_advisor_1.items.count(), 1)

    def test_sales_advisor_denied_access_to_operational_and_financial_modules(self):
        """9. El asesor recibe denegación del lado del servidor (HTTP 403) en módulos ajenos."""
        client = Client()
        client.login(email='asesor@vector.com.ni', password='password123')
        s = client.session
        s['active_org_id'] = str(self.org.id)
        s.save()

        # Operaciones
        self.assertEqual(client.get(reverse('operations:workorder_list')).status_code, 403)
        # SST
        self.assertEqual(client.get(reverse('sst:index')).status_code, 403)
        # Inventario
        self.assertEqual(client.get(reverse('inventory:dashboard')).status_code, 403)
        # Finanzas
        self.assertEqual(client.get(reverse('finance:dashboard')).status_code, 403)
        # Facturación
        self.assertEqual(client.get(reverse('billing:invoice_list')).status_code, 403)
        # Compras
        self.assertEqual(client.get(reverse('purchasing:order_list')).status_code, 403)
        # Nómina
        self.assertEqual(client.get(reverse('payroll:dashboard')).status_code, 403)

    def test_organization_with_disabled_crm_module_blocks_all_access(self):
        """10. Las organizaciones con el módulo CRM deshabilitado continúan bloqueadas (HTTP 403)."""
        # Deshabilitar crm en la organización
        self.org.enabled_modules = ['operations', 'finance']
        self.org.save()

        client = Client()
        client.login(email='asesor@vector.com.ni', password='password123')
        s = client.session
        s['active_org_id'] = str(self.org.id)
        s.save()

        self.assertEqual(client.get(reverse('crm:account_list')).status_code, 403)
        self.assertEqual(client.get(reverse('crm:quote_list')).status_code, 403)

    def test_inactive_membership_denied_access(self):
        """11. Membresías inactivas no permiten acceso a CRM (redirige a onboarding)."""
        self.membership_advisor.is_active = False
        self.membership_advisor.save()

        client = Client()
        client.login(email='asesor@vector.com.ni', password='password123')
        s = client.session
        s['active_org_id'] = str(self.org.id)
        s.save()

        resp = client.get(reverse('crm:account_list'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('tenancy:onboarding'), resp.url)

    def test_quote_approval_requires_items_and_valid_source_state(self):
        """12. La aprobación rechaza cotizaciones vacías (sin partidas) o en estados inválidos."""
        account, _ = self._create_base_account_and_opp()
        # Cotización sin partidas
        empty_quote = Quote.objects.create(
            organization=self.org,
            account=account,
            title='Cotización Vacía',
            status=Quote.Status.DRAFT,
            created_by=self.user_advisor,
        )

        client_mgr = Client()
        client_mgr.login(email='comercial@vector.com.ni', password='password123')
        s = client_mgr.session
        s['active_org_id'] = str(self.org.id)
        s.save()

        # Intento de aprobar cotización sin partidas -> Rechazado
        resp = client_mgr.post(reverse('crm:quote_status_transition', kwargs={'pk': empty_quote.pk}), data={
            'status': Quote.Status.APPROVED,
        })
        self.assertEqual(resp.status_code, 302)
        empty_quote.refresh_from_db()
        self.assertEqual(empty_quote.status, Quote.Status.DRAFT)  # No se aprobó

    def test_atomic_quote_approval_rollback_on_failure(self):
        """12b. Los fallos durante la aprobación ejecutan rollback atómico sin actualizaciones parciales."""
        account, opp = self._create_base_account_and_opp()
        quote = Quote.objects.create(
            organization=self.org,
            account=account,
            opportunity=opp,
            title='Cotización Atómica',
            status=Quote.Status.SENT,
            created_by=self.user_advisor,
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-ATOM',
            description='Servicio Prueba Atomicidad',
            unit='GLB',
            quantity=Decimal('1.00'),
            unit_price=Decimal('1000.00'),
        )

        client_mgr = Client()
        client_mgr.login(email='comercial@vector.com.ni', password='password123')
        s = client_mgr.session
        s['active_org_id'] = str(self.org.id)
        s.save()

        from unittest.mock import patch
        with patch.object(Opportunity, 'save', side_effect=RuntimeError("Fallo simulado en BD")):
            with self.assertRaises(RuntimeError):
                client_mgr.post(reverse('crm:quote_status_transition', kwargs={'pk': quote.pk}), data={
                    'status': Quote.Status.APPROVED,
                })

        # Rollback atómico verificado
        quote.refresh_from_db()
        account.refresh_from_db()
        opp.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.SENT)
        self.assertIsNone(quote.approved_by)
        self.assertEqual(account.lifecycle_stage, Account.LifecycleStage.PROSPECT)
        self.assertEqual(opp.stage, Opportunity.Stage.QUALIFIED)

    def test_phantom_mutation_blocked_for_operations_coord(self):
        """
        REV-01: Verifica que peticiones con datos válidos enviadas por OPERATIONS_COORD (80)
        a vistas de escritura protegidas por CrmWriteRequiredMixin sean denegadas con HTTP 403
        ANTES de ejecutar cualquier handler, garantizando que NO se persiste ningún dato en BD.
        """
        account, opp = self._create_base_account_and_opp()
        quote = Quote.objects.create(
            organization=self.org,
            account=account,
            title='Cotización Base para Prueba Fantasma',
            status=Quote.Status.DRAFT,
            created_by=self.user_advisor,
        )

        client_80 = Client()
        client_80.login(email='operaciones@vector.com.ni', password='password123')
        s = client_80.session
        s['active_org_id'] = str(self.org.id)
        s.save()

        # 1. Intento de crear Cuenta con payload 100% válido
        resp_acc = client_80.post(reverse('crm:account_create'), data={
            'commercial_name': 'Industrias Metalurgicas Prohibidas',
            'legal_name': 'Industrias Metalurgicas Prohibidas S.A.',
            'account_type': Account.AccountType.CORPORATE,
            'lifecycle_stage': Account.LifecycleStage.PROSPECT,
            'tax_id': 'J0310000777777',
            'email': 'ilegal@prohibido.com.ni',
            'city': 'Leon',
            'default_currency': 'NIO',
            'payment_terms_days': 30,
        })
        self.assertEqual(resp_acc.status_code, 403)
        self.assertFalse(Account.objects.filter(commercial_name='Industrias Metalurgicas Prohibidas').exists())

        # 2. Intento de crear Oportunidad con payload válido
        resp_opp = client_80.post(reverse('crm:opportunity_create'), data={
            'account': str(account.pk),
            'title': 'Oportunidad Fantasma Prohibida',
            'stage': Opportunity.Stage.PROPOSAL,
            'currency': 'NIO',
            'estimated_value': '65000.00',
        })
        self.assertEqual(resp_opp.status_code, 403)
        self.assertFalse(Opportunity.objects.filter(title='Oportunidad Fantasma Prohibida').exists())

        # 3. Intento de mutación no-creación: Agregar partida con payload válido a cotización existente
        initial_items_count = quote.items.count()
        resp_item = client_80.post(reverse('crm:quote_add_item', kwargs={'pk': quote.pk}), data={
            'item_type': QuoteItem.ItemType.SERVICE,
            'code': 'SRV-ILLEGAL-80',
            'description': 'Servicio Inyectado por Nivel 80',
            'unit': 'GLB',
            'quantity': '1.00',
            'unit_price': '40000.00',
            'discount_percent': '0.00',
        })
        self.assertEqual(resp_item.status_code, 403)
        self.assertEqual(quote.items.count(), initial_items_count)
        self.assertFalse(QuoteItem.objects.filter(code='SRV-ILLEGAL-80').exists())

        # 4. Intento de mutación: Actualizar etapa Kanban de oportunidad
        resp_stage = client_80.post(reverse('crm:opportunity_stage_update', kwargs={'pk': opp.pk}), data={
            'stage': Opportunity.Stage.WON,
        })
        self.assertEqual(resp_stage.status_code, 403)
        opp.refresh_from_db()
        self.assertEqual(opp.stage, Opportunity.Stage.QUALIFIED)

    def test_quote_status_transition_authorship_enforced_for_sales_advisor(self):
        """
        REV-03: Verifica que un Asesor Comercial (15) no pueda modificar el estado (SENT, REJECTED, etc.)
        de una cotización elaborada por otro asesor aun conociendo su UUID (HTTP 403).
        El autor legítimo sí puede transicionar su propia cotización.
        """
        account, opp = self._create_base_account_and_opp()
        quote_advisor_1 = Quote.objects.create(
            organization=self.org,
            account=account,
            opportunity=opp,
            title='Cotización de Carlos (Asesor 1)',
            status=Quote.Status.DRAFT,
            created_by=self.user_advisor,  # Creada por Asesor 1
        )

        client_2 = Client()
        client_2.login(email='asesor2@vector.com.ni', password='password123')
        s2 = client_2.session
        s2['active_org_id'] = str(self.org.id)
        s2.save()

        # Asesor 2 intenta marcar como ENVIADA la cotización de Asesor 1 -> HTTP 403
        resp_send_alien = client_2.post(reverse('crm:quote_status_transition', kwargs={'pk': quote_advisor_1.pk}), data={
            'status': Quote.Status.SENT,
        })
        self.assertEqual(resp_send_alien.status_code, 403)
        quote_advisor_1.refresh_from_db()
        self.assertEqual(quote_advisor_1.status, Quote.Status.DRAFT)

        # Asesor 2 intenta marcar como RECHAZADA la cotización de Asesor 1 -> HTTP 403
        resp_rej_alien = client_2.post(reverse('crm:quote_status_transition', kwargs={'pk': quote_advisor_1.pk}), data={
            'status': Quote.Status.REJECTED,
        })
        self.assertEqual(resp_rej_alien.status_code, 403)
        quote_advisor_1.refresh_from_db()
        self.assertEqual(quote_advisor_1.status, Quote.Status.DRAFT)

        # Asesor 1 (autor) sí puede marcar como ENVIADA su propia cotización -> HTTP 302
        client_1 = Client()
        client_1.login(email='asesor@vector.com.ni', password='password123')
        s1 = client_1.session
        s1['active_org_id'] = str(self.org.id)
        s1.save()

        resp_send_own = client_1.post(reverse('crm:quote_status_transition', kwargs={'pk': quote_advisor_1.pk}), data={
            'status': Quote.Status.SENT,
        })
        self.assertEqual(resp_send_own.status_code, 302)
        quote_advisor_1.refresh_from_db()
        self.assertEqual(quote_advisor_1.status, Quote.Status.SENT)

    def test_quote_detail_template_can_approve_visibility(self):
        """
        REV-02: Verifica que el contexto 'can_approve' y la plantilla 'quote_detail.html'
        oculten el botón 'Aprobar cotización' al Gerente Comercial autor por SoD,
        pero lo hagan visible a otro Gerente Comercial y al Administrador General (Owner).
        """
        account, opp = self._create_base_account_and_opp()
        quote_mgr1 = Quote.objects.create(
            organization=self.org,
            account=account,
            opportunity=opp,
            title='Cotización del Gerente 1',
            status=Quote.Status.DRAFT,
            created_by=self.user_comm_mgr,  # Creada por Gerente 1
        )
        QuoteItem.objects.create(
            organization=self.org,
            quote=quote_mgr1,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-MGR1',
            description='Servicio Base',
            unit='GLB',
            quantity=Decimal('1.00'),
            unit_price=Decimal('10000.00'),
        )

        # 1. Gerente 1 (autor) inspecciona su cotización
        client_mgr1 = Client()
        client_mgr1.login(email='comercial@vector.com.ni', password='password123')
        s1 = client_mgr1.session
        s1['active_org_id'] = str(self.org.id)
        s1.save()

        resp_mgr1 = client_mgr1.get(reverse('crm:quote_detail', kwargs={'pk': quote_mgr1.pk}))
        self.assertEqual(resp_mgr1.status_code, 200)
        self.assertFalse(resp_mgr1.context['can_approve'])
        self.assertNotContains(resp_mgr1, 'Aprobar cotización')

        # 2. Gerente 2 (otro gerente comercial) inspecciona la cotización de Gerente 1
        client_mgr2 = Client()
        client_mgr2.login(email='comercial2@vector.com.ni', password='password123')
        s2 = client_mgr2.session
        s2['active_org_id'] = str(self.org.id)
        s2.save()

        resp_mgr2 = client_mgr2.get(reverse('crm:quote_detail', kwargs={'pk': quote_mgr1.pk}))
        self.assertEqual(resp_mgr2.status_code, 200)
        self.assertTrue(resp_mgr2.context['can_approve'])
        self.assertContains(resp_mgr2, 'Aprobar cotización')

        # 3. Asesor Comercial inspecciona la cotización
        client_adv = Client()
        client_adv.login(email='asesor@vector.com.ni', password='password123')
        s_adv = client_adv.session
        s_adv['active_org_id'] = str(self.org.id)
        s_adv.save()

        resp_adv = client_adv.get(reverse('crm:quote_detail', kwargs={'pk': quote_mgr1.pk}))
        self.assertEqual(resp_adv.status_code, 200)
        self.assertFalse(resp_adv.context['can_approve'])
        self.assertNotContains(resp_adv, 'Aprobar cotización')

        # 4. Administrador General (Owner) inspecciona la cotización (excepción SoD autorizada)
        client_owner = Client()
        client_owner.login(email='owner@vector.com.ni', password='password123')
        s_owner = client_owner.session
        s_owner['active_org_id'] = str(self.org.id)
        s_owner.save()

        resp_owner = client_owner.get(reverse('crm:quote_detail', kwargs={'pk': quote_mgr1.pk}))
        self.assertEqual(resp_owner.status_code, 200)
        self.assertTrue(resp_owner.context['can_approve'])
        self.assertContains(resp_owner, 'Aprobar cotización')
