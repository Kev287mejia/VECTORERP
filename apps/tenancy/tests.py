import hashlib
import json
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse

from apps.core.models import CountryConfig
from apps.tenancy.constants import BusinessVertical
from apps.tenancy.forms import OrganizationOnboardingForm
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel
from apps.tenancy.views_onboarding import DashboardWorkspaceView

User = get_user_model()


class TenancyOnboardingAndStrictModularTestCase(TestCase):
    """
    Suite de pruebas para validar:
    1. Registro enriquecido con Moneda Funcional, Régimen Fiscal DGI, Giro/Rubro y Plan Contable.
    2. Modulación Estricta: Lo que el usuario selecciona es ESTRICTAMENTE lo que se habilita.
    """

    def setUp(self):
        self.country_ni, _ = CountryConfig.objects.get_or_create(
            code='NI',
            defaults={
                'name': 'Nicaragua',
                'flag_emoji': '🇳🇮',
                'primary_currency_code': 'NIO',
                'primary_currency_symbol': 'C$',
                'tax_id_label': 'RUC',
                'tax_id_regex': r'^[A-Z]\d{13}$',
                'is_active': True,
                'default_tax_rate': Decimal('15.00'),
                'default_tax_name': 'IVA',
            }
        )
        self.user = User.objects.create_user(
            email='cfo@corporacion.ni',
            password='Password123!',
            full_name='Carlos Contreras',
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_onboarding_form_contains_fiscal_and_industry_fields(self):
        """Verifica que el formulario de onboarding valide los nuevos rubros técnicos."""
        form = OrganizationOnboardingForm(data={
            'country_code': 'NI',
            'name': 'Energía y Construcción S.A.',
            'legal_name': 'Energía y Construcción de Nicaragua Sociedad Anónima',
            'tax_id': 'J0310000099999',
            'currency': Organization.Currency.USD,
            'tax_regime': Organization.TaxRegime.SPECIAL,
            'industry': Organization.Industry.ENERGY_TELCO,
            'accounting_template': Organization.AccountingTemplate.NIIF_PYMES,
            'enabled_modules': ['crm', 'finance'],
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['currency'], Organization.Currency.USD)
        self.assertEqual(form.cleaned_data['tax_regime'], Organization.TaxRegime.SPECIAL)
        self.assertEqual(form.cleaned_data['industry'], Organization.Industry.ENERGY_TELCO)
        self.assertEqual(form.cleaned_data['accounting_template'], Organization.AccountingTemplate.NIIF_PYMES)

    def test_onboarding_wizard_post_persists_organization_and_sets_active_session(self):
        """Verifica el flujo completo de creación de organización con los rubros seleccionados."""
        response = self.client.post(reverse('tenancy:onboarding'), {
            'country_code': 'NI',
            'name': 'Telecom Redes',
            'legal_name': 'Telecomunicaciones y Redes de Occidente S.A.',
            'tax_id': 'J0310000088888',
            'currency': Organization.Currency.USD,
            'tax_regime': Organization.TaxRegime.GENERAL,
            'industry': Organization.Industry.ENERGY_TELCO,
            'accounting_template': Organization.AccountingTemplate.NIIF_PYMES,
            'enabled_modules': ['crm', 'finance'],
        }, follow=True)

        self.assertEqual(response.status_code, 200)

        org = Organization.objects.get(tax_id='J0310000088888')
        self.assertEqual(org.name, 'Telecom Redes')
        self.assertEqual(org.currency, Organization.Currency.USD)
        self.assertEqual(org.currency_symbol, '$')
        self.assertEqual(org.tax_regime, Organization.TaxRegime.GENERAL)
        self.assertEqual(org.industry, Organization.Industry.ENERGY_TELCO)
        self.assertEqual(org.accounting_template, Organization.AccountingTemplate.NIIF_PYMES)
        self.assertEqual(org.enabled_modules, ['crm', 'finance'])

        # Membresía Owner creada
        membership = OrganizationMembership.objects.get(user=self.user, organization=org)
        self.assertEqual(membership.role_level, RoleLevel.OWNER_ADMIN)
        self.assertTrue(membership.is_default_org)

    def test_strict_module_access_rules(self):
        """
        Regla Estricta: Lo que el usuario seleccione es lo que se habilitará.
        Si la empresa sólo contrató ['crm', 'finance'], los módulos 'operations' y 'sst'
        quedan estrictamente denegados incluso para el OWNER_ADMIN.
        """
        org = Organization.objects.create(
            country=self.country_ni,
            name='Comercializadora Alfa',
            legal_name='Comercializadora Alfa S.A.',
            tax_id='J0310000077777',
            currency=Organization.Currency.NIO,
            tax_regime=Organization.TaxRegime.GENERAL,
            industry=Organization.Industry.COMMERCE,
            accounting_template=Organization.AccountingTemplate.SIMPLIFIED,
            slug='comercializadora-alfa',
            enabled_modules=['crm', 'finance'],
            onboarding_completed=True,
            is_active=True,
        )

        owner_membership = OrganizationMembership.objects.create(
            user=self.user,
            organization=org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )

        # Módulos habilitados
        self.assertTrue(org.is_module_enabled('crm'))
        self.assertTrue(org.is_module_enabled('finance'))
        self.assertTrue(owner_membership.has_module_access('crm'))
        self.assertTrue(owner_membership.has_module_access('finance'))

        # Módulos NO contratados quedan estrictamente denegados
        self.assertFalse(org.is_module_enabled('operations'))
        self.assertFalse(org.is_module_enabled('sst'))
        self.assertFalse(org.is_module_enabled('inventory'))
        self.assertFalse(owner_membership.has_module_access('operations'))
        self.assertFalse(owner_membership.has_module_access('sst'))
        self.assertFalse(owner_membership.has_module_access('inventory'))

    def test_dashboard_kpis_reflect_strictly_enabled_modules(self):
        """El dashboard renderiza tarjetas de KPI estrictamente para los módulos contratados."""
        org = Organization.objects.create(
            country=self.country_ni,
            name='Servicios Puro Finanzas',
            legal_name='Servicios Financieros S.A.',
            tax_id='J0310000066666',
            currency=Organization.Currency.USD,
            tax_regime=Organization.TaxRegime.SPECIAL,
            industry=Organization.Industry.SERVICES,
            accounting_template=Organization.AccountingTemplate.NIIF_PYMES,
            slug='servicios-puro-finanzas',
            enabled_modules=['finance'],
            onboarding_completed=True,
            is_active=True,
        )

        OrganizationMembership.objects.create(
            user=self.user,
            organization=org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )

        session = self.client.session
        session['active_org_id'] = str(org.id)
        session.save()

        response = self.client.get(reverse('tenancy:dashboard'))
        self.assertEqual(response.status_code, 200)

        kpi_cards = response.context['kpi_cards']
        card_ids = [c['id'] for c in kpi_cards]

        # MOD-FIN y governance deben estar presentes
        self.assertIn('finance', card_ids)
        self.assertIn('governance', card_ids)

        # MOD-OPS, MOD-SST, MOD-CRM, MOD-TECH, MOD-MKT no contratados NO deben aparecer en kpi_cards
        self.assertNotIn('operations', card_ids)
        self.assertNotIn('sst', card_ids)
        self.assertNotIn('crm', card_ids)
        self.assertNotIn('tech', card_ids)
        self.assertNotIn('marketing', card_ids)

    def test_tech_and_marketing_modules_access_and_isolation(self):
        """Verifica la habilitación de MOD-TECH y MOD-MKT y el control de acceso estricto."""
        tech_org = Organization.objects.create(
            country=self.country_ni,
            name='Tech Solutions Lab',
            legal_name='Tech Solutions Lab S.A.',
            tax_id='J0310000088888',
            currency=Organization.Currency.USD,
            tax_regime=Organization.TaxRegime.GENERAL,
            industry=Organization.Industry.TECHNOLOGY,
            accounting_template=Organization.AccountingTemplate.NIIF_PYMES,
            slug='tech-solutions-lab',
            enabled_modules=['tech', 'marketing'],
            onboarding_completed=True,
            is_active=True,
        )

        owner_mem = OrganizationMembership.objects.create(
            user=self.user,
            organization=tech_org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )

        # Módulos contratados
        self.assertTrue(tech_org.is_module_enabled('tech'))
        self.assertTrue(tech_org.is_module_enabled('marketing'))
        self.assertTrue(owner_mem.has_module_access('tech'))
        self.assertTrue(owner_mem.has_module_access('marketing'))

        session = self.client.session
        session['active_org_id'] = str(tech_org.id)
        session.save()

        # Acceso permitido a sus consolas respectivas
        resp_tech = self.client.get(reverse('tenancy:tech_dashboard'))
        self.assertEqual(resp_tech.status_code, 200)
        self.assertContains(resp_tech, 'Tecnología, Soporte IT')

        resp_mkt = self.client.get(reverse('tenancy:marketing_dashboard'))
        self.assertEqual(resp_mkt.status_code, 200)
        self.assertContains(resp_mkt, 'Marketing Digital, Campañas')

        # Dashboard refleja KPI cards de tech y marketing
        resp_dash = self.client.get(reverse('tenancy:dashboard'))
        self.assertEqual(resp_dash.status_code, 200)
        kpi_ids = [c['id'] for c in resp_dash.context['kpi_cards']]
        self.assertIn('tech', kpi_ids)
        self.assertIn('marketing', kpi_ids)
        self.assertNotIn('finance', kpi_ids)

    def test_finance_action_buttons_rendered_with_event_dispatch(self):
        """Los botones de acción en el módulo de Finanzas renderizan despachadores de eventos de ventana."""
        fin_org = Organization.objects.create(
            country=self.country_ni,
            name='Finanzas Corporativas',
            legal_name='Finanzas Corporativas S.A.',
            tax_id='J0310000099999',
            currency=Organization.Currency.NIO,
            tax_regime=Organization.TaxRegime.GENERAL,
            industry=Organization.Industry.SERVICES,
            accounting_template=Organization.AccountingTemplate.NIIF_PYMES,
            slug='finanzas-corporativas',
            enabled_modules=['finance'],
            onboarding_completed=True,
            is_active=True,
        )

        OrganizationMembership.objects.create(
            user=self.user,
            organization=fin_org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )

        session = self.client.session
        session['active_org_id'] = str(fin_org.id)
        session.save()

        resp = self.client.get(reverse('finance:dashboard'))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()

        self.assertIn('btn-register-expense', content)
        self.assertIn('btn-request-replenish', content)
        self.assertIn('btn-open-fund', content)
        self.assertIn('btn-create-bank-account', content)
        self.assertIn("window.dispatchEvent(new CustomEvent('open-expense-modal'))", content)
        self.assertIn("window.dispatchEvent(new CustomEvent('open-replenish-modal'))", content)
        self.assertIn("window.dispatchEvent(new CustomEvent('open-fund-modal'))", content)
        self.assertIn("window.dispatchEvent(new CustomEvent('open-bank-modal'))", content)

    def test_dynamic_onboarding_wizard_context(self):
        """Verifica que el OnboardingWizardView provea countries_json y modules_catalog dinámicos."""
        response = self.client.get(reverse('tenancy:onboarding'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('countries_json', response.context)
        self.assertIn('modules_catalog', response.context)
        
        # Validar atributos serializados
        import json
        data = json.loads(response.context['countries_json'])
        self.assertIn('NI', data)
        self.assertEqual(data['NI']['name'], 'Nicaragua')
        self.assertEqual(data['NI']['primary_currency_code'], 'NIO')
        self.assertEqual(data['NI']['tax_id_label'], 'RUC')

    def test_dynamic_dashboard_metrics_and_rbac_distribution(self):
        """Verifica que el dashboard consulte métricas reales en BD y construya rbac_distribution."""
        org = Organization.objects.create(
            country=self.country_ni,
            name='Ingeniería Total',
            legal_name='Ingeniería Total S.A.',
            tax_id='J0310000055555',
            currency=Organization.Currency.NIO,
            tax_regime=Organization.TaxRegime.GENERAL,
            industry=Organization.Industry.CONSTRUCTION,
            accounting_template=Organization.AccountingTemplate.NIIF_PYMES,
            slug='ingenieria-total',
            enabled_modules=['crm', 'operations', 'sst'],
            onboarding_completed=True,
            is_active=True,
        )

        owner_mem = OrganizationMembership.objects.create(
            user=self.user,
            organization=org,
            role_level=RoleLevel.OWNER_ADMIN,
            job_title='Director General',
            is_active=True,
            is_default_org=True,
        )

        second_user = User.objects.create_user(
            email='tech@corporacion.ni',
            password='Password123!',
            full_name='Técnico Electricista',
        )
        tech_mem = OrganizationMembership.objects.create(
            user=second_user,
            organization=org,
            role_level=RoleLevel.TECHNICIAN,
            job_title='Liniero de Distribución',
            is_active=True,
        )

        session = self.client.session
        session['active_org_id'] = str(org.id)
        session.save()

        response = self.client.get(reverse('tenancy:dashboard'))
        self.assertEqual(response.status_code, 200)

        # Contexto de métricas reales
        self.assertEqual(response.context['total_members_count'], 2)
        self.assertEqual(len(response.context['enabled_modules_list']), 3)
        self.assertEqual(response.context['accessible_modules_count'], 3)
        self.assertEqual(len(response.context['recent_members']), 2)

        # Distribución RBAC
        rbac_dist = response.context['rbac_distribution']
        owner_entry = next(r for r in rbac_dist if r['level'] == 100)
        tech_entry = next(r for r in rbac_dist if r['level'] == 20)
        self.assertEqual(owner_entry['members_count'], 1)
        self.assertEqual(tech_entry['members_count'], 1)
        self.assertTrue(owner_entry['is_current_session_level'])
        self.assertTrue(tech_entry['can_current_user_approve'])

    def test_team_management_endpoint_responds_200(self):
        """Verifica que /settings/team/ responda con 200."""
        org = Organization.objects.create(
            country=self.country_ni,
            name='Equipo Central',
            legal_name='Equipo Central S.A.',
            tax_id='J0310000044444',
            currency=Organization.Currency.NIO,
            slug='equipo-central',
            enabled_modules=['crm'],
            onboarding_completed=True,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )
        session = self.client.session
        session['active_org_id'] = str(org.id)
        session.save()

        response = self.client.get(reverse('tenancy:team_management'))
        self.assertEqual(response.status_code, 200)

    def test_team_member_enrollment_with_expanded_profile(self):
        """Verifica el enrolamiento integral de un colaborador con ficha laboral y SST."""
        org = Organization.objects.create(
            country=self.country_ni,
            name='Ingeniería Eléctrica',
            legal_name='Ingeniería Eléctrica S.A.',
            tax_id='J0310000033333',
            currency=Organization.Currency.NIO,
            slug='ingenieria-electrica',
            enabled_modules=['crm', 'operations', 'sst'],
            onboarding_completed=True,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )
        session = self.client.session
        session['active_org_id'] = str(org.id)
        session.save()

        # Enrolamiento con expediente completo
        response = self.client.post(reverse('tenancy:team_create'), {
            'full_name': 'Ing. Roberto Silva Morales',
            'email': 'rsilva@ingenieria.ni',
            'personal_id_number': '001-100592-0003V',
            'phone': '+505 8899-1122',
            'job_title': 'Supervisor de Alta Tensión',
            'role_level': 50,
            'password': 'PasswordSegura2026!',
            'department': 'OPERATIONS',
            'contract_type': 'PROJECT',
            'social_security_number': '9876543-2',
            'hire_date': '2026-03-01',
            'work_location': 'Frente Subestación Oriental',
            'blood_type': 'O+',
            'emergency_contact_name': 'Carmen Silva (Hermana)',
            'emergency_contact_phone': '+505 8765-4321',
        }, follow=True)

        self.assertEqual(response.status_code, 200)

        created_user = User.objects.get(email='rsilva@ingenieria.ni')
        self.assertEqual(created_user.full_name, 'Ing. Roberto Silva Morales')
        self.assertEqual(created_user.personal_id_number, '001-100592-0003V')

        membership = OrganizationMembership.objects.get(user=created_user, organization=org)
        self.assertEqual(membership.role_level, 50)
        self.assertEqual(membership.job_title, 'Supervisor de Alta Tensión')
        self.assertEqual(membership.department, 'OPERATIONS')
        self.assertEqual(membership.contract_type, 'PROJECT')
        self.assertEqual(membership.social_security_number, '9876543-2')
        self.assertEqual(str(membership.hire_date), '2026-03-01')
        self.assertEqual(membership.work_location, 'Frente Subestación Oriental')
        self.assertEqual(membership.blood_type, 'O+')
        self.assertEqual(membership.emergency_contact_name, 'Carmen Silva (Hermana)')
        self.assertEqual(membership.emergency_contact_phone, '+505 8765-4321')

    def test_team_member_enrollment_backward_compatibility_minimal_fields(self):
        """Verifica que el enrolamiento funcione con valores por defecto cuando solo se envían credenciales."""
        org = Organization.objects.create(
            country=self.country_ni,
            name='Construcciones del Norte',
            legal_name='Construcciones del Norte S.A.',
            tax_id='J0310000022222',
            currency=Organization.Currency.NIO,
            slug='construcciones-norte',
            enabled_modules=['operations'],
            onboarding_completed=True,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )
        session = self.client.session
        session['active_org_id'] = str(org.id)
        session.save()

        # Solo credenciales mínimas
        response = self.client.post(reverse('tenancy:team_create'), {
            'full_name': 'Mario López Mendoza',
            'email': 'mario@norte.ni',
            'personal_id_number': '001-201088-0005A',
            'role_level': 20,
        }, follow=True)

        self.assertEqual(response.status_code, 200)

        created_user = User.objects.get(email='mario@norte.ni')
        membership = OrganizationMembership.objects.get(user=created_user, organization=org)
        self.assertEqual(membership.role_level, 20)
        self.assertEqual(membership.department, 'OPERATIONS')
        self.assertEqual(membership.contract_type, 'PERMANENT')
        self.assertEqual(membership.blood_type, 'UNKNOWN')
        self.assertEqual(membership.social_security_number, '')
        self.assertIsNone(membership.hire_date)

    def test_prepare_sandbox_command_execution(self):
        """Verifica la ejecución del comando prepare_sandbox."""
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('prepare_sandbox', reset=True, stdout=out)
        output = out.getvalue()
        self.assertIn('ENTORNO SANDBOX PREPARADO CORRECTAMENTE', output)
        self.assertIn('http://127.0.0.1:8000/auth/register/', output)


class SandboxViewControlsTestCase(TestCase):
    """
    Suite de pruebas para los controladores interactivos de carga y vaciado del Sandbox.
    """

    def setUp(self):
        self.country_ni, _ = CountryConfig.objects.get_or_create(
            code='NI',
            defaults={
                'name': 'Nicaragua',
                'flag_emoji': 'NI',
                'primary_currency_code': 'NIO',
                'primary_currency_symbol': 'C$',
                'tax_id_label': 'RUC',
                'tax_id_regex': r'^[A-Z]\d{13}$',
                'is_active': True,
                'default_tax_rate': Decimal('15.00'),
                'default_tax_name': 'IVA',
            }
        )
        self.owner = User.objects.create_user(
            email='owner.sandbox@empresa.ni',
            password='Password123!',
            full_name='Owner Sandbox',
        )
        self.subordinate = User.objects.create_user(
            email='subordinate@empresa.ni',
            password='Password123!',
            full_name='Subordinate User',
        )
        self.org = Organization.objects.create(
            name='CONSTRUCCIONES S.A.',
            legal_name='Construcciones de Nicaragua S.A.',
            tax_id='J0310000112233',
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            tax_regime=Organization.TaxRegime.GENERAL,
            industry=Organization.Industry.CONSTRUCTION,
            accounting_template=Organization.AccountingTemplate.NIIF_PYMES,
            enabled_modules=['crm', 'operations', 'sst', 'inventory', 'finance'],
        )
        OrganizationMembership.objects.create(
            user=self.owner,
            organization=self.org,
            role_level=RoleLevel.OWNER_ADMIN,
            job_title='Director General',
            is_active=True,
            is_default_org=True,
        )
        OrganizationMembership.objects.create(
            user=self.subordinate,
            organization=self.org,
            role_level=RoleLevel.ENGINEER,
            job_title='Ingeniero Residente',
            is_active=True,
        )
        self.client = Client()

    def test_load_sandbox_data_allowed_for_owner(self):
        """Verifica que el Owner pueda cargar el escenario mediante POST a /settings/sandbox/load/."""
        self.client.force_login(self.owner)
        session = self.client.session
        session['active_org_id'] = str(self.org.id)
        session.save()

        response = self.client.post(reverse('tenancy:sandbox_load'), follow=True)
        self.assertEqual(response.status_code, 200)

        from apps.crm.models import Account
        self.assertEqual(Account.objects.filter(organization=self.org).count(), 5)

    def test_load_sandbox_data_forbidden_for_subordinate(self):
        """Verifica que un rol inferior a 100 reciba 403 Forbidden al intentar cargar el escenario."""
        self.client.force_login(self.subordinate)
        session = self.client.session
        session['active_org_id'] = str(self.org.id)
        session.save()

        response = self.client.post(reverse('tenancy:sandbox_load'))
        self.assertEqual(response.status_code, 403)

    def test_reset_sandbox_data_allowed_for_owner(self):
        """Verifica que el Owner pueda vaciar el escenario mediante POST a /settings/sandbox/reset/."""
        self.client.force_login(self.owner)
        session = self.client.session
        session['active_org_id'] = str(self.org.id)
        session.save()

        # Poblar primero
        self.client.post(reverse('tenancy:sandbox_load'), follow=True)

        # Vaciar
        response = self.client.post(reverse('tenancy:sandbox_reset'), follow=True)
        self.assertEqual(response.status_code, 200)

        from apps.crm.models import Account
        self.assertEqual(Account.objects.filter(organization=self.org).count(), 0)

    def test_reset_sandbox_data_forbidden_for_subordinate(self):
        """Verifica que un subordinado reciba 403 Forbidden al intentar vaciar datos de la empresa."""
        self.client.force_login(self.subordinate)
        session = self.client.session
        session['active_org_id'] = str(self.org.id)
        session.save()

        response = self.client.post(reverse('tenancy:sandbox_reset'))
        self.assertEqual(response.status_code, 403)


class BusinessVerticalArchitectureTestCase(TestCase):
    """
    Suite de Pruebas de Arquitectura de Giros de Negocio (Paso 16.2).
    Garantiza:
    1. Que la Topbar no exponga el selector interactivo de cambio de giro en caliente,
       sino una píldora estática técnica de solo lectura correspondiente a la empresa activa.
    2. Que el Onboarding Wizard de 4 pasos persista permanentemente el business_vertical
       elegido por el fundador (ENGINEERING, HARDWARE, PHARMACY, SME_RETAIL).
    3. Que la conmutación de organización (SwitchOrganizationView) adapte los módulos
       y la navegación en el menú lateral según el giro de la empresa activa.
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

        self.user = User.objects.create_user(
            email='evaluador@eternallabs.ni',
            full_name='Evaluador de Producto',
            password='Password123*',
        )

        # Empresa 1: Ingeniería
        self.org_eng = Organization.objects.create(
            name="SERMANSA Ingeniería S.A.",
            legal_name="Servicios y Mantenimiento Electromecánico S.A.",
            slug="sermansa-ing-test",
            tax_id="J0310000458912",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            business_vertical=BusinessVertical.ENGINEERING_SERVICES,
            business_profile=Organization.BusinessProfile.ENGINEERING_SERVICES,
            enabled_modules=['crm', 'operations', 'sst', 'inventory', 'finance', 'billing', 'purchasing', 'payroll'],
            onboarding_completed=True,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org_eng,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )

        # Empresa 2: Ferretería
        self.org_hardware = Organization.objects.create(
            name="Ferretería El Maestro S.A.",
            legal_name="Distribuidora y Ferretería El Maestro S.A.",
            slug="ferreteria-test",
            tax_id="J0310000771234",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            business_vertical=BusinessVertical.HARDWARE_STORE,
            business_profile=Organization.BusinessProfile.COMMERCIAL_RETAIL,
            enabled_modules=['crm', 'inventory', 'finance', 'billing', 'purchasing', 'payroll'],
            onboarding_completed=True,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org_hardware,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=False,
        )

        # Empresa 3: Farmacia
        self.org_pharmacy = Organization.objects.create(
            name="Farmacia Santa Lucía S.A.",
            legal_name="Droguería y Farmacia Santa Lucía S.A.",
            slug="farmacia-test",
            tax_id="J0310000885678",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            business_vertical=BusinessVertical.PHARMACY_HEALTH,
            business_profile=Organization.BusinessProfile.COMMERCIAL_RETAIL,
            enabled_modules=['crm', 'inventory', 'finance', 'billing', 'purchasing', 'payroll'],
            onboarding_completed=True,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org_pharmacy,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=False,
        )

        self.client = Client()
        self.client.force_login(self.user)

    def test_topbar_renders_static_vertical_pill_and_no_interactive_dropdown(self):
        """Verifica que la Topbar muestre la píldora técnica estática y carezca del selector interactivo de vertical."""
        session = self.client.session
        session['active_org_id'] = str(self.org_eng.id)
        session.save()

        response = self.client.get(reverse('tenancy:dashboard'))
        self.assertEqual(response.status_code, 200)

        # Debe mostrar la etiqueta estática limpia
        self.assertContains(response, 'Ingeniería')

        # NO debe contener el formulario interactivo para cambiar de giro
        self.assertNotContains(response, 'name="business_vertical"')

    def test_onboarding_wizard_persists_vertical_and_assigns_preset_modules(self):
        """Verifica que el wizard cree la organización con su vertical correspondiente y módulos asignados."""
        # 1. Alta con Farmacia
        response = self.client.post(reverse('tenancy:onboarding'), {
            'country_code': 'NI',
            'business_vertical': 'PHARMACY',
            'name': 'Farmacia Central Granada',
            'legal_name': 'Farmacia Central de Granada S.A.',
            'tax_id': 'J0310000333333',
            'currency': Organization.Currency.NIO,
            'tax_regime': Organization.TaxRegime.GENERAL,
            'industry': Organization.Industry.COMMERCE,
            'accounting_template': Organization.AccountingTemplate.NIIF_PYMES,
            'enabled_modules': ['crm', 'inventory', 'billing', 'finance'],
        }, follow=True)
        self.assertEqual(response.status_code, 200)

        org_pharmacy = Organization.objects.get(tax_id='J0310000333333')
        self.assertEqual(org_pharmacy.business_vertical, BusinessVertical.PHARMACY_HEALTH)
        self.assertTrue(org_pharmacy.is_pharmacy_vertical())
        self.assertFalse(org_pharmacy.is_module_enabled('operations'))

        # 2. Alta con Ferretería
        response = self.client.post(reverse('tenancy:onboarding'), {
            'country_code': 'NI',
            'business_vertical': 'HARDWARE',
            'name': 'Ferretería El Tornillo',
            'legal_name': 'Ferretería y Materiales El Tornillo S.A.',
            'tax_id': 'J0310000444444',
            'currency': Organization.Currency.NIO,
            'tax_regime': Organization.TaxRegime.GENERAL,
            'industry': Organization.Industry.COMMERCE,
            'accounting_template': Organization.AccountingTemplate.NIIF_PYMES,
            'enabled_modules': ['crm', 'inventory', 'billing', 'purchasing'],
        }, follow=True)
        self.assertEqual(response.status_code, 200)

        org_hardware = Organization.objects.get(tax_id='J0310000444444')
        self.assertEqual(org_hardware.business_vertical, BusinessVertical.HARDWARE_STORE)
        self.assertTrue(org_hardware.is_hardware_vertical())

    def test_switch_organization_adapts_sidebar_and_modules(self):
        """Verifica que al conmutar entre organizaciones, los módulos y menú se adapten automáticamente."""
        # 1. En SERMANSA (Ingeniería): Operaciones y SST están activos
        self.client.post(reverse('tenancy:switch_org_post'), {'org_id': str(self.org_eng.id)}, follow=True)
        response = self.client.get(reverse('tenancy:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ingeniería')
        self.assertContains(response, 'Órdenes de Trabajo')

        # 2. Conmutar a Farmacia Santa Lucía: Operaciones y SST quedan apagados en el menú
        self.client.post(reverse('tenancy:switch_org_post'), {'org_id': str(self.org_pharmacy.id)}, follow=True)
        response = self.client.get(reverse('tenancy:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Farmacia')
        self.assertNotContains(response, 'Órdenes de Trabajo')


class SaasHqGovernanceAndHardeningTestSuite(TestCase):
    """
    Suite oficial de Gobierno SaaS, Cuotas de Planes, Suspensión por Mora y Telemetría v1.0.0-GA.
    Verifica:
    1. Acceso 200 OK a /saas-hq/ para Nivel 100 y bloqueo 403 Forbidden para niveles inferiores (85, 80, 50, 20).
    2. Bloqueo operativo cuando una organización pasa a subscription_status = 'SUSPENDED' y restauración inmediata al volver a 'ACTIVE'.
    3. Bloqueo de alta de nuevos miembros cuando se alcanza max_users_quota.
    4. Descarga del respaldo JSON con sello SHA-256 (/saas-hq/backup-json/<org_id>/).
    5. Endpoint público de telemetría /healthz/ con version '1.0.0-GA'.
    """

    def setUp(self):
        self.country_ni, _ = CountryConfig.objects.get_or_create(
            code='NI',
            defaults={
                'name': 'Nicaragua',
                'flag_emoji': '🇳🇮',
                'primary_currency_code': 'NIO',
                'primary_currency_symbol': 'C$',
                'tax_id_label': 'RUC',
                'tax_id_regex': r'^[A-Z]\d{13}$',
                'is_active': True,
                'default_tax_rate': Decimal('15.00'),
                'default_tax_name': 'IVA',
            }
        )

        self.org = Organization.objects.create(
            name='SERMANSA Soluciones Industriales',
            slug='sermansa-soluciones',
            legal_name='SERMANSA Soluciones Industriales S.A.',
            tax_id='J0310000458999',
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            subscription_plan=Organization.SubscriptionPlan.ENTERPRISE,
            subscription_status=Organization.SubscriptionStatus.ACTIVE,
            monthly_fee_usd=Decimal('199.00'),
            max_users_quota=50,
            max_users=50,
            enabled_modules=['crm', 'operations', 'inventory', 'finance', 'billing', 'purchasing'],
            onboarding_completed=True,
            is_active=True,
        )

        # 1. Owner Admin (Nivel 100)
        self.user_owner = User.objects.create_user(
            email='owner@sermansa.ni',
            password='Password123!',
            full_name='Ing. Carlos Mendoza (Owner)',
        )
        self.mem_owner = OrganizationMembership.objects.create(
            organization=self.org,
            user=self.user_owner,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )
        self.client_owner = Client()
        self.client_owner.force_login(self.user_owner)
        session_owner = self.client_owner.session
        session_owner['active_org_id'] = str(self.org.id)
        session_owner.save()

        # Superadministrador de Plataforma Global (Eternal Labs)
        self.org_platform = Organization.objects.create(
            name='Eternal Labs Master HQ',
            slug='eternal-labs-master-hq',
            legal_name='Eternal Labs Inc.',
            tax_id='J0310000000001',
            country=self.country_ni,
            currency=Organization.Currency.USD,
            subscription_plan=Organization.SubscriptionPlan.ENTERPRISE,
            subscription_status=Organization.SubscriptionStatus.ACTIVE,
            is_active=True,
        )
        self.user_superadmin = User.objects.create_user(
            email='superadmin@eternallabs.com',
            password='Password123!',
            full_name='Superadmin Eternal Labs',
            is_superuser=True,
            is_staff=True,
        )
        self.mem_superadmin = OrganizationMembership.objects.create(
            organization=self.org_platform,
            user=self.user_superadmin,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )
        self.client_superadmin = Client()
        self.client_superadmin.force_login(self.user_superadmin)
        session_superadmin = self.client_superadmin.session
        session_superadmin['active_org_id'] = str(self.org_platform.id)
        session_superadmin.save()

        # 2. Gerente Comercial (Nivel 85)
        self.user_mgr85 = User.objects.create_user(
            email='mgr85@sermansa.ni',
            password='Password123!',
            full_name='Lic. Valentina Jarquín (85)',
        )
        self.mem_mgr85 = OrganizationMembership.objects.create(
            organization=self.org,
            user=self.user_mgr85,
            role_level=RoleLevel.COMMERCIAL_MGR,
            is_active=True,
        )
        self.client_mgr85 = Client()
        self.client_mgr85.force_login(self.user_mgr85)
        session_mgr85 = self.client_mgr85.session
        session_mgr85['active_org_id'] = str(self.org.id)
        session_mgr85.save()

        # 3. Coordinador Operativo (Nivel 80)
        self.user_coord80 = User.objects.create_user(
            email='coord80@sermansa.ni',
            password='Password123!',
            full_name='Ing. Roberto Solís (80)',
        )
        self.mem_coord80 = OrganizationMembership.objects.create(
            organization=self.org,
            user=self.user_coord80,
            role_level=RoleLevel.OPERATIONS_COORD,
            is_active=True,
        )
        self.client_coord80 = Client()
        self.client_coord80.force_login(self.user_coord80)
        session_coord80 = self.client_coord80.session
        session_coord80['active_org_id'] = str(self.org.id)
        session_coord80.save()

        # 4. Supervisor de Proyecto / Residente (Nivel 50)
        self.user_sup50 = User.objects.create_user(
            email='sup50@sermansa.ni',
            password='Password123!',
            full_name='Ing. Francisco Gómez (50)',
        )
        self.mem_sup50 = OrganizationMembership.objects.create(
            organization=self.org,
            user=self.user_sup50,
            role_level=RoleLevel.ENGINEER,
            is_active=True,
        )
        self.client_sup50 = Client()
        self.client_sup50.force_login(self.user_sup50)
        session_sup50 = self.client_sup50.session
        session_sup50['active_org_id'] = str(self.org.id)
        session_sup50.save()

        # 5. Técnico de Campo (Nivel 20)
        self.user_tech20 = User.objects.create_user(
            email='tech20@sermansa.ni',
            password='Password123!',
            full_name='Téc. Pedro Martínez (20)',
        )
        self.mem_tech20 = OrganizationMembership.objects.create(
            organization=self.org,
            user=self.user_tech20,
            role_level=RoleLevel.TECHNICIAN,
            is_active=True,
        )
        self.client_tech20 = Client()
        self.client_tech20.force_login(self.user_tech20)
        session_tech20 = self.client_tech20.session
        session_tech20['active_org_id'] = str(self.org.id)
        session_tech20.save()

    def test_saas_hq_access_control_level_100_vs_lower_levels(self):
        """
        Verifica acceso 200 OK a /saas-hq/ EXCLUSIVAMENTE para Superusuario global de Plataforma
        y bloqueo estricto 403 Forbidden para inquilinos cliente (incluso Nivel 100) y niveles inferiores (85, 80, 50, 20).
        """
        # Superusuario de Plataforma: Acceso pleno permitido
        res_super = self.client_superadmin.get(reverse('tenancy:saas_hq'))
        self.assertEqual(res_super.status_code, 200)
        self.assertContains(res_super, 'Consola de Gobierno Multi-Tenant')

        # Nivel 100 de inquilino (sin superusuario global): Bloqueo 403 Forbidden estricto
        res_owner = self.client_owner.get(reverse('tenancy:saas_hq'))
        self.assertEqual(res_owner.status_code, 403)

        # Nivel 85: Bloqueo 403 Forbidden
        res_85 = self.client_mgr85.get(reverse('tenancy:saas_hq'))
        self.assertEqual(res_85.status_code, 403)

        # Nivel 80: Bloqueo 403 Forbidden
        res_80 = self.client_coord80.get(reverse('tenancy:saas_hq'))
        self.assertEqual(res_80.status_code, 403)

        # Nivel 50: Bloqueo 403 Forbidden
        res_50 = self.client_sup50.get(reverse('tenancy:saas_hq'))
        self.assertEqual(res_50.status_code, 403)

        # Nivel 20: Bloqueo 403 Forbidden
        res_20 = self.client_tech20.get(reverse('tenancy:saas_hq'))
        self.assertEqual(res_20.status_code, 403)

    def test_suspension_status_blocks_operations_and_restores_on_active(self):
        """
        Verifica bloqueo operativo cuando subscription_status = 'SUSPENDED'
        y restauración inmediata al volver a 'ACTIVE'.
        """
        from apps.crm.models import Account

        # 1. Poner en SUSPENDED
        self.org.subscription_status = Organization.SubscriptionStatus.SUSPENDED
        self.org.save(update_fields=['subscription_status'])

        # Intento de mutación (POST) en CRM queda bloqueado con 403 y plantilla de suspensión
        blocked_res = self.client_owner.post(
            reverse('crm:account_create'),
            data={
                'commercial_name': 'Cliente Bloqueado por Mora Test',
                'legal_name': 'Cliente Bloqueado Test S.A.',
                'account_type': Account.AccountType.CORPORATE,
                'lifecycle_stage': Account.LifecycleStage.PROSPECT,
                'tax_id': 'J0310000778899',
                'city': 'Managua',
                'default_currency': 'NIO',
                'payment_terms_days': 0,
            }
        )
        self.assertEqual(blocked_res.status_code, 403)
        self.assertContains(blocked_res, "SUSPENSIÓN COMERCIAL SAAS", status_code=403)
        self.assertFalse(Account.objects.filter(tax_id='J0310000778899').exists())

        # 2. Restauración a ACTIVE
        self.org.subscription_status = Organization.SubscriptionStatus.ACTIVE
        self.org.save(update_fields=['subscription_status'])

        # Ahora el POST se ejecuta con éxito
        allowed_res = self.client_owner.post(
            reverse('crm:account_create'),
            data={
                'commercial_name': 'Cliente Activo Post Restauración',
                'legal_name': 'Cliente Activo S.A.',
                'account_type': Account.AccountType.CORPORATE,
                'lifecycle_stage': Account.LifecycleStage.PROSPECT,
                'tax_id': 'J0310000778899',
                'city': 'Managua',
                'default_currency': 'NIO',
                'payment_terms_days': 0,
            },
            follow=True,
        )
        self.assertEqual(allowed_res.status_code, 200)
        self.assertTrue(Account.objects.filter(tax_id='J0310000778899').exists())

    def test_member_creation_blocked_when_max_users_quota_reached(self):
        """
        Verifica que el alta de colaboradores se bloquee cuando se alcance la cuota
        máxima del plan (max_users_quota) y se permita al ampliarla.
        """
        # La organización tiene 5 miembros actualmente (owner, mgr, coord, sup, tech)
        self.assertEqual(int(self.org.active_members_count), 5)

        # Ajustamos cuota al límite actual (5)
        self.org.max_users_quota = 5
        self.org.max_users = 5
        self.org.save(update_fields=['max_users_quota', 'max_users'])
        self.assertTrue(self.org.is_quota_exceeded)

        # Intento de crear un 6to miembro debe ser rechazado
        fail_res = self.client_owner.post(
            reverse('tenancy:team_create'),
            data={
                'email': 'extra_user@sermansa.ni',
                'full_name': 'Colaborador Extra Fuera de Cuota',
                'personal_id_number': '001-201095-0009P',
                'phone': '+505 8899-7766',
                'job_title': 'Especialista Adicional',
                'role_level': RoleLevel.TECHNICIAN,
                'department': 'OPERATIONS',
                'contract_type': 'PERMANENT',
            }
        )
        self.assertEqual(fail_res.status_code, 403)
        self.assertFalse(User.objects.filter(email='extra_user@sermansa.ni').exists())

        # Ampliar cuota a 10 asientos
        self.org.max_users_quota = 10
        self.org.max_users = 10
        self.org.save(update_fields=['max_users_quota', 'max_users'])
        self.assertFalse(self.org.is_quota_exceeded)

        # Ahora el alta se realiza exitosamente
        success_res = self.client_owner.post(
            reverse('tenancy:team_create'),
            data={
                'email': 'extra_user@sermansa.ni',
                'full_name': 'Colaborador Extra Con Cuota',
                'personal_id_number': '001-201095-0009P',
                'phone': '+505 8899-7766',
                'job_title': 'Especialista Adicional',
                'role_level': RoleLevel.TECHNICIAN,
                'department': 'OPERATIONS',
                'contract_type': 'PERMANENT',
                'password': 'Password123!',
            },
            follow=True,
        )
        self.assertEqual(success_res.status_code, 200)
        self.assertTrue(User.objects.filter(email='extra_user@sermansa.ni').exists())

    def test_tenant_vault_backup_export_sha256_and_security(self):
        """
        Verifica la exportación del respaldo portable JSON con sello de integridad SHA-256
        exclusivo para Superusuarios de Plataforma, y que usuarios de inquilino (incluso Nivel 100) reciban 403.
        """
        # Descarga permitida para Superusuario de Plataforma
        res = self.client_superadmin.get(reverse('tenancy:saas_backup_export', args=[str(self.org.id)]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'application/json')
        self.assertIn('attachment;', res['Content-Disposition'])

        backup_data = res.json()
        self.assertIn('integrity_seal_sha256', backup_data)
        self.assertIn('payload', backup_data)

        payload = backup_data['payload']
        self.assertEqual(payload['vault_version'], '1.0.0-GA')
        self.assertEqual(payload['organization']['slug'], self.org.slug)

        # Validar cálculo criptográfico SHA-256 sobre payload determinístico
        raw_deterministic = json.dumps(payload, sort_keys=True, default=str)
        expected_seal = hashlib.sha256(raw_deterministic.encode('utf-8')).hexdigest()
        self.assertEqual(backup_data['integrity_seal_sha256'], expected_seal)

        # Descarga rechazada con 403 para Owner de Inquilino (Nivel 100 sin superuser global)
        res_owner_unauthorized = self.client_owner.get(reverse('tenancy:saas_backup_export', args=[str(self.org.id)]))
        self.assertEqual(res_owner_unauthorized.status_code, 403)

        # Descarga Nivel 85 (Comercial) debe ser rechazada con 403
        res_unauthorized = self.client_mgr85.get(reverse('tenancy:saas_backup_export', args=[str(self.org.id)]))
        self.assertEqual(res_unauthorized.status_code, 403)

    def test_healthz_telemetry_endpoint_v1_0_0_ga(self):
        """
        Verifica endpoint público /healthz/ con versión 1.0.0-GA, DB status y jurisdicciones activas.
        """
        anon_client = Client()
        res = anon_client.get('/healthz/')
        self.assertEqual(res.status_code, 200)

        data = res.json()
        self.assertEqual(data.get('status'), 'ok')
        self.assertEqual(data.get('version'), '1.0.0-GA')
        self.assertEqual(data.get('service'), 'VECTOR-ERP-SAS')
        self.assertEqual(data.get('database'), 'connected')
        self.assertGreaterEqual(data.get('active_jurisdictions', 0), 1)
        self.assertIn('latency_ms', data)
        self.assertIn('timestamp', data)


class D01DashboardAndSwitchOrgTestCase(TestCase):
    """
    Casos de prueba de regresión para Defecto D-01:
    Verifica que el renderizado del dashboard (incluyendo templates/dashboard/index.html y
    el switcher de organizaciones de la topbar) funcione correctamente sin errores NoReverseMatch,
    genere los enlaces correctos para tenancy:switch_org y mantenga la seguridad del endpoint.
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
        self.user = User.objects.create_user(
            email='multiempresa@eternallabs.ni',
            full_name='Usuario Multiempresa',
            password='Password123*',
        )
        self.org1 = Organization.objects.create(
            name="Empresa Primaria S.A.",
            slug="empresa-primaria-d01",
            tax_id="J0310000888111",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['crm', 'operations', 'billing', 'finance'],
        )
        self.membership1 = OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org1,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )

    def test_d01_01_user_with_single_organization(self):
        """1. Usuario con 1 organización: el dashboard renderiza HTTP 200 sin colisión."""
        self.client.force_login(self.user)
        session = self.client.session
        session['active_org_id'] = str(self.org1.id)
        session.save()

        response = self.client.get(reverse('tenancy:dashboard'))
        self.assertEqual(response.status_code, 200)

        # Probar renderizado directo de templates/dashboard/index.html con 1 membresía
        from django.template.loader import render_to_string
        html = render_to_string('dashboard/index.html', {
            'user_memberships': OrganizationMembership.objects.filter(user=self.user),
            'current_organization': self.org1,
            'metrics': {},
            'sample_work_orders': [],
        }, request=response.wsgi_request)
        self.assertNotIn("Organizaciones Asociadas a su Cuenta", html)

    def test_d01_02_user_with_two_or_more_organizations_renders_correct_switch_links(self):
        """2. Usuario con 2 o más organizaciones activas: el selector genera enlaces válidos a tenancy:switch_org sin NoReverseMatch."""
        org2 = Organization.objects.create(
            name="Empresa Secundaria S.A.",
            slug="empresa-secundaria-d01",
            tax_id="J0310000888222",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            enabled_modules=['crm', 'billing'],
        )
        membership2 = OrganizationMembership.objects.create(
            user=self.user,
            organization=org2,
            role_level=RoleLevel.COMMERCIAL_MGR,
            is_active=True,
        )

        self.client.force_login(self.user)
        session = self.client.session
        session['active_org_id'] = str(self.org1.id)
        session.save()

        # Renderizado de la consola operativa principal (que incluye base_erp.html)
        response = self.client.get(reverse('tenancy:dashboard'))
        self.assertEqual(response.status_code, 200)

        # Renderizado específico de templates/dashboard/index.html donde residía D-01
        from django.template.loader import render_to_string
        memberships_qs = OrganizationMembership.objects.filter(user=self.user).order_by('created_at')
        html = render_to_string('dashboard/index.html', {
            'user_memberships': memberships_qs,
            'current_organization': self.org1,
            'metrics': {},
            'sample_work_orders': [],
        }, request=response.wsgi_request)
        self.assertIn("Organizaciones Asociadas a su Cuenta", html)
        expected_url_org1 = reverse('tenancy:switch_org', args=[self.org1.id])
        expected_url_org2 = reverse('tenancy:switch_org', args=[org2.id])
        self.assertIn(f'href="{expected_url_org1}"', html)
        self.assertIn(f'href="{expected_url_org2}"', html)

    def test_d01_03_user_without_active_membership_redirects_to_onboarding(self):
        """3. Usuario sin membresías activas: middleware redirige a onboarding."""
        user_no_org = User.objects.create_user(
            email='huerfano@eternallabs.ni',
            full_name='Usuario Sin Tenant',
            password='Password123*',
        )
        self.client.force_login(user_no_org)
        response = self.client.get(reverse('tenancy:dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('tenancy:onboarding'), response.url)

    def test_d01_04_switch_organization_security_and_authorization(self):
        """4. Endpoint tenancy:switch_org exige autenticación y membresía activa (404 ante organizaciones ajenas o inactivas)."""
        anon_client = Client()
        res_anon = anon_client.get(reverse('tenancy:switch_org', args=[self.org1.id]))
        self.assertEqual(res_anon.status_code, 302)
        self.assertIn(reverse('accounts_auth:login'), res_anon.url)

        # Intento de conmutar a organización ajena
        foreign_org = Organization.objects.create(
            name="Empresa Ajena S.A.",
            slug="empresa-ajena-d01",
            tax_id="J0310000999999",
            country=self.country_ni,
            currency=Organization.Currency.NIO,
        )
        self.client.force_login(self.user)
        res_foreign = self.client.get(reverse('tenancy:switch_org', args=[foreign_org.id]))
        self.assertEqual(res_foreign.status_code, 404)

        # Conmutación legítima mediante GET
        res_switch = self.client.get(reverse('tenancy:switch_org', args=[self.org1.id]), follow=True)
        self.assertEqual(res_switch.status_code, 200)
        self.assertEqual(self.client.session.get('active_org_id'), str(self.org1.id))

    def test_d04_sales_advisor_module_access_and_isolation(self):
        """Verifica aislamiento estricto de RoleLevel.SALES_ADVISOR (15) y context processor."""
        user_adv = User.objects.create_user(
            email='advisor_test@sermansa.ni',
            password='Password123!',
            full_name='Asesor Test (15)',
        )
        mem_adv = OrganizationMembership.objects.create(
            organization=self.org1,
            user=user_adv,
            role_level=RoleLevel.SALES_ADVISOR,
            is_active=True,
        )

        # 1. Acceso a CRM permitido
        self.assertTrue(mem_adv.has_module_access('crm'))

        # 2. Acceso denegado a módulos operativos y financieros
        self.assertFalse(mem_adv.has_module_access('operations'))
        self.assertFalse(mem_adv.has_module_access('sst'))
        self.assertFalse(mem_adv.has_module_access('inventory'))
        self.assertFalse(mem_adv.has_module_access('finance'))
        self.assertFalse(mem_adv.has_module_access('billing'))
        self.assertFalse(mem_adv.has_module_access('purchasing'))
        self.assertFalse(mem_adv.has_module_access('payroll'))

        # 3. Roles técnicos excluidos de CRM
        user_eng = User.objects.create_user(
            email='eng_crm_test@sermansa.ni',
            password='Password123!',
            full_name='Ingeniero Test (50)',
        )
        mem_eng = OrganizationMembership.objects.create(
            organization=self.org1,
            user=user_eng,
            role_level=RoleLevel.ENGINEER,
            is_active=True,
        )
        self.assertFalse(mem_eng.has_module_access('crm'))

        user_tech = User.objects.create_user(
            email='tech_crm_test@sermansa.ni',
            password='Password123!',
            full_name='Técnico Test (20)',
        )
        mem_tech = OrganizationMembership.objects.create(
            organization=self.org1,
            user=user_tech,
            role_level=RoleLevel.TECHNICIAN,
            is_active=True,
        )
        self.assertFalse(mem_tech.has_module_access('crm'))
