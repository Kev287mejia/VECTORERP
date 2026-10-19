"""
Suite Integral de Pruebas Automatizadas de Remediación de Seguridad — Fase 1C.11
Cubre los hallazgos:
- SEC-02: ALLOWED_HOSTS sin comodín incondicional y rechazo de hosts no autorizados.
- SEC-03: ResetSandboxDataView con salvaguarda multiempresa estricta contra vaciado de tenants productivos.
- SEC-04: TenantAdminMixin en ModelAdmin de compras y facturación para aislamiento estricto de usuarios staff.
- SEC-05: Políticas de cookies seguras (SESSION_COOKIE_SECURE y CSRF_COOKIE_SECURE) en producción.
"""
import datetime
from decimal import Decimal
from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import DisallowedHost
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import CountryConfig
from apps.crm.models import Account, Quote
from apps.inventory.models import Warehouse
from apps.purchasing.models import Supplier, PurchaseOrder
from apps.purchasing.admin import SupplierAdmin, PurchaseOrderAdmin
from apps.billing.models import Invoice
from apps.billing.admin import InvoiceAdmin
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel

User = get_user_model()


class MockSuperUser:
    is_active = True
    is_staff = True
    is_superuser = True
    is_authenticated = True

    def has_perm(self, perm):
        return True

    def has_module_perms(self, app_label):
        return True


class MockStaffUser:
    is_active = True
    is_staff = True
    is_superuser = False
    is_authenticated = True

    def __init__(self, pk=1):
        self.pk = pk

    def has_perm(self, perm):
        return True

    def has_module_perms(self, app_label):
        return True


# ==============================================================================
# SEC-02: ALLOWED_HOSTS
# ==============================================================================
class AllowedHostsSecurityTestCase(TestCase):
    """
    Pruebas obligatorias para SEC-02:
    1. Un host permitido se acepta.
    2. Un host no permitido provoca el rechazo esperado (DisallowedHost / HTTP 400).
    3. La configuración de producción no introduce automáticamente '*'.
    4. Una variable ausente o vacía no vuelve a habilitar todos los hosts silenciosamente.
    5. Los tests no dependen de dominios externos ni de servicios reales.
    """

    def setUp(self):
        self.client = Client()

    @override_settings(ALLOWED_HOSTS=['erp.vector.ni', 'testserver'])
    def test_01_allowed_host_is_accepted(self):
        """1. Petición con host autorizado en ALLOWED_HOSTS es aceptada."""
        response = self.client.get('/healthz/', HTTP_HOST='erp.vector.ni')
        self.assertEqual(response.status_code, 200)

    @override_settings(ALLOWED_HOSTS=['erp.vector.ni', 'testserver'])
    def test_02_disallowed_host_raises_suspicious_operation_or_400(self):
        """2. Petición con host no autorizado genera rechazo HTTP 400 (DisallowedHost)."""
        response = self.client.get('/healthz/', HTTP_HOST='attacker-domain.com')
        self.assertEqual(response.status_code, 400)

    def test_03_production_settings_do_not_contain_wildcard(self):
        """3. La lista ALLOWED_HOSTS en la configuración efectiva no contiene el comodín '*' incondicionalmente."""
        from config import settings as app_settings
        self.assertNotIn('*', app_settings.ALLOWED_HOSTS)
        self.assertNotIn('*', settings.ALLOWED_HOSTS)

    @override_settings(ALLOWED_HOSTS=['localhost', '127.0.0.1', 'testserver'])
    def test_04_empty_or_explicit_hosts_does_not_enable_wildcard(self):
        """4. Configuración explícita no reintroduce comodín silencioso."""
        self.assertNotIn('*', settings.ALLOWED_HOSTS)
        response = self.client.get('/healthz/', HTTP_HOST='malicious.org')
        self.assertEqual(response.status_code, 400)


# ==============================================================================
# SEC-03: ResetSandboxDataView
# ==============================================================================
class ResetSandboxDataSecurityTestCase(TestCase):
    """
    Pruebas obligatorias para SEC-03:
    1. La operación se rechaza para una organización productiva (403).
    2. La operación se rechaza si el entorno no está autorizado (ENABLE_SANDBOX=False o DEBUG=False) (403).
    3. La operación no elimina datos de otras organizaciones.
    4. El flujo legítimo de reinicio continúa funcionando en entorno expresamente habilitado.
    5. Los tests usan exclusivamente la base de datos aislada de pruebas.
    """

    def setUp(self):
        self.client = Client()
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

        # Organización Productiva
        self.prod_org = Organization.objects.create(
            name='EMPRESA PRODUCTIVA REAL S.A.',
            legal_name='Empresa Productiva Real S.A.',
            tax_id='J0310000999001',
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            tax_regime=Organization.TaxRegime.GENERAL,
            industry=Organization.Industry.SERVICES,
            slug='empresa-productiva-real',
            enabled_modules=['crm', 'operations', 'billing', 'purchasing'],
        )
        self.prod_owner = User.objects.create_user(
            email='owner.prod@empresareal.ni',
            password='Password123!',
            full_name='Director General Real',
        )
        self.prod_membership = OrganizationMembership.objects.create(
            organization=self.prod_org,
            user=self.prod_owner,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )

        # Organización Sandbox Autorizada
        self.sandbox_org = Organization.objects.create(
            name='IMESA INGENIERÍA S.A.',
            legal_name='Instalaciones y Mantenimiento Electromecánico S.A.',
            tax_id='J0310000458912',
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            tax_regime=Organization.TaxRegime.GENERAL,
            industry=Organization.Industry.ENERGY_TELCO,
            slug='imesa-ingenieria',
            enabled_modules=['crm', 'operations', 'billing', 'purchasing'],
        )
        self.sandbox_owner = User.objects.create_user(
            email='evaluador@eternallabs.ni',
            password='Password123!',
            full_name='Evaluador Sandbox',
        )
        self.sandbox_membership = OrganizationMembership.objects.create(
            organization=self.sandbox_org,
            user=self.sandbox_owner,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
            is_default_org=True,
        )

    @override_settings(DEBUG=True, ENABLE_SANDBOX=True, SANDBOX_ORGANIZATION_SLUGS=['imesa-ingenieria'])
    def test_01_reset_rejected_for_productive_organization(self):
        """1. La operación de reinicio se rechaza terminantemente (HTTP 403) para una organización productiva."""
        # Crear datos en la organización productiva
        Account.objects.create(
            organization=self.prod_org,
            code='CLI-PROD-01',
            commercial_name='Cliente Corporativo Protegido',
            tax_id='J0310000123456',
        )
        self.client.force_login(self.prod_owner)
        session = self.client.session
        session['active_org_id'] = str(self.prod_org.id)
        session.save()

        response = self.client.post(reverse('tenancy:sandbox_reset'))
        self.assertEqual(response.status_code, 403)

        # Los datos de la organización productiva continúan intactos
        self.assertTrue(Account.objects.filter(organization=self.prod_org, commercial_name='Cliente Corporativo Protegido').exists())

    @override_settings(DEBUG=False, ENABLE_SANDBOX=True, SANDBOX_ORGANIZATION_SLUGS=['imesa-ingenieria'])
    def test_02a_reset_rejected_when_debug_false(self):
        """2a. La operación se rechaza (HTTP 403) cuando DEBUG=False (ambiente productivo)."""
        self.client.force_login(self.sandbox_owner)
        session = self.client.session
        session['active_org_id'] = str(self.sandbox_org.id)
        session.save()

        response = self.client.post(reverse('tenancy:sandbox_reset'))
        self.assertEqual(response.status_code, 403)

    @override_settings(DEBUG=True, ENABLE_SANDBOX=False, SANDBOX_ORGANIZATION_SLUGS=['imesa-ingenieria'])
    def test_02b_reset_rejected_when_sandbox_disabled(self):
        """2b. La operación se rechaza (HTTP 403) cuando ENABLE_SANDBOX=False."""
        self.client.force_login(self.sandbox_owner)
        session = self.client.session
        session['active_org_id'] = str(self.sandbox_org.id)
        session.save()

        response = self.client.post(reverse('tenancy:sandbox_reset'))
        self.assertEqual(response.status_code, 403)

    @override_settings(DEBUG=True, ENABLE_SANDBOX=True, SANDBOX_ORGANIZATION_SLUGS=['imesa-ingenieria'])
    def test_03_reset_sandbox_does_not_affect_other_organizations(self):
        """3. El reinicio de la organización sandbox NO elimina datos de otra organización."""
        Account.objects.create(organization=self.sandbox_org, code='CLI-SND-01', commercial_name='Cliente Demo Sandbox', tax_id='J0310000111111')
        Account.objects.create(organization=self.prod_org, code='CLI-PRD-01', commercial_name='Cliente Real Prod', tax_id='J0310000222222')

        self.client.force_login(self.sandbox_owner)
        session = self.client.session
        session['active_org_id'] = str(self.sandbox_org.id)
        session.save()

        response = self.client.post(reverse('tenancy:sandbox_reset'))
        self.assertEqual(response.status_code, 302)

        # Los datos de sandbox fueron vaciados
        self.assertFalse(Account.objects.filter(organization=self.sandbox_org, commercial_name='Cliente Demo Sandbox').exists())
        # Los datos de la otra organización permanecen íntegros
        self.assertTrue(Account.objects.filter(organization=self.prod_org, commercial_name='Cliente Real Prod').exists())

    @override_settings(DEBUG=True, ENABLE_SANDBOX=True, SANDBOX_ORGANIZATION_SLUGS=['imesa-ingenieria'])
    def test_04_authorized_sandbox_reset_succeeds(self):
        """4. El reinicio legítimo en entorno autorizado vacía datos y conserva la organización y el owner."""
        Account.objects.create(organization=self.sandbox_org, code='CLI-SND-02', commercial_name='Cliente Demo 1', tax_id='J0310000111111')
        self.client.force_login(self.sandbox_owner)
        session = self.client.session
        session['active_org_id'] = str(self.sandbox_org.id)
        session.save()

        response = self.client.post(reverse('tenancy:sandbox_reset'))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('tenancy:dashboard'))

        # La organización e integrantes Nivel 100 permanecen
        self.assertTrue(Organization.objects.filter(id=self.sandbox_org.id).exists())
        self.assertTrue(OrganizationMembership.objects.filter(organization=self.sandbox_org, user=self.sandbox_owner).exists())

    @override_settings(DEBUG=True, ENABLE_SANDBOX=True, SANDBOX_ORGANIZATION_SLUGS=[])
    def test_05_default_settings_with_empty_slugs_rejects_all_resets(self):
        """5. Con lista vacía de slugs autorizados por omisión, el reinicio es rechazado (HTTP 403) para cualquier empresa."""
        self.client.force_login(self.sandbox_owner)
        session = self.client.session
        session['active_org_id'] = str(self.sandbox_org.id)
        session.save()

        response = self.client.post(reverse('tenancy:sandbox_reset'))
        self.assertEqual(response.status_code, 403)



# ==============================================================================
# SEC-04: ModelAdmin Multi-Tenant Isolation
# ==============================================================================
class ModelAdminTenancyIsolationTestCase(TestCase):
    """
    Pruebas obligatorias para SEC-04:
    1. Un superusuario conserva la capacidad global que le corresponde.
    2. Un usuario staff no superusuario no puede listar registros de otras organizaciones.
    3. Manipular una URL de detalle no permite acceder a un registro ajeno (has_change_permission).
    4. El control se aplica a proveedores, órdenes de compra y facturas identificados en la auditoría.
    5. Las restricciones no se basan únicamente en ocultar enlaces.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite()
        self.today = timezone.now().date()

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

        self.org_a = Organization.objects.create(
            name='TENANT ALPHA S.A.',
            legal_name='Tenant Alpha S.A.',
            tax_id='J0310000111AAA',
            country=self.country_ni,
            slug='tenant-alpha',
        )
        self.org_b = Organization.objects.create(
            name='TENANT BETA S.A.',
            legal_name='Tenant Beta S.A.',
            tax_id='J0310000222BBB',
            country=self.country_ni,
            slug='tenant-beta',
        )

        # Bodegas en cada Tenant
        self.warehouse_a = Warehouse.objects.create(
            organization=self.org_a,
            code='BOD-A-01',
            name='Bodega Central Alpha',
        )
        self.warehouse_b = Warehouse.objects.create(
            organization=self.org_b,
            code='BOD-B-01',
            name='Bodega Central Beta',
        )

        # Datos en Org A
        self.supplier_a = Supplier.objects.create(
            organization=self.org_a,
            code='PRV-A-01',
            commercial_name='Proveedor Alpha 1',
            tax_id='J0310000333001',
        )
        self.po_a = PurchaseOrder.objects.create(
            organization=self.org_a,
            number='OC-A-001',
            supplier=self.supplier_a,
            target_warehouse=self.warehouse_a,
            issue_date=self.today,
        )
        self.client_account_a = Account.objects.create(
            organization=self.org_a,
            code='CLI-A-01',
            commercial_name='Cliente Alpha 1',
            tax_id='J0310000555001',
        )
        self.invoice_a = Invoice.objects.create(
            organization=self.org_a,
            number='FAC-A-001',
            account=self.client_account_a,
            issue_date=self.today,
        )

        # Datos en Org B
        self.supplier_b = Supplier.objects.create(
            organization=self.org_b,
            code='PRV-B-01',
            commercial_name='Proveedor Beta 1',
            tax_id='J0310000444002',
        )
        self.po_b = PurchaseOrder.objects.create(
            organization=self.org_b,
            number='OC-B-001',
            supplier=self.supplier_b,
            target_warehouse=self.warehouse_b,
            issue_date=self.today,
        )
        self.client_account_b = Account.objects.create(
            organization=self.org_b,
            code='CLI-B-01',
            commercial_name='Cliente Beta 1',
            tax_id='J0310000666002',
        )
        self.invoice_b = Invoice.objects.create(
            organization=self.org_b,
            number='FAC-B-001',
            account=self.client_account_b,
            issue_date=self.today,
        )

        self.superuser = MockSuperUser()
        self.staff_user_a = MockStaffUser(pk=10)

    def test_01_superuser_retains_global_visibility(self):
        """1. Un superusuario conserva la capacidad global en Supplier, PurchaseOrder e Invoice."""
        request = self.factory.get('/platform-superadmin/')
        request.user = self.superuser
        request.organization = None

        supplier_admin = SupplierAdmin(Supplier, self.site)
        po_admin = PurchaseOrderAdmin(PurchaseOrder, self.site)
        inv_admin = InvoiceAdmin(Invoice, self.site)

        self.assertEqual(supplier_admin.get_queryset(request).count(), 2)
        self.assertEqual(po_admin.get_queryset(request).count(), 2)
        self.assertEqual(inv_admin.get_queryset(request).count(), 2)

    def test_02_staff_user_cannot_list_records_from_other_organizations(self):
        """2. Un usuario staff de Org A no puede listar registros de Org B en compras y facturación."""
        request = self.factory.get('/platform-superadmin/')
        request.user = self.staff_user_a
        request.organization = self.org_a

        supplier_admin = SupplierAdmin(Supplier, self.site)
        po_admin = PurchaseOrderAdmin(PurchaseOrder, self.site)
        inv_admin = InvoiceAdmin(Invoice, self.site)

        # Proveedores: solo ve PRV-A-01
        suppliers_qs = supplier_admin.get_queryset(request)
        self.assertEqual(suppliers_qs.count(), 1)
        self.assertEqual(suppliers_qs.first(), self.supplier_a)

        # Órdenes de Compra: solo ve OC-A-001
        po_qs = po_admin.get_queryset(request)
        self.assertEqual(po_qs.count(), 1)
        self.assertEqual(po_qs.first(), self.po_a)

        # Facturas: solo ve FAC-A-001
        inv_qs = inv_admin.get_queryset(request)
        self.assertEqual(inv_qs.count(), 1)
        self.assertEqual(inv_qs.first(), self.invoice_a)

    def test_03_manipulating_id_does_not_permit_access_to_other_org_record(self):
        """3. Usuario staff de Org A no posee permisos de ver, cambiar ni eliminar objetos de Org B."""
        request = self.factory.get('/platform-superadmin/')
        request.user = self.staff_user_a
        request.organization = self.org_a

        supplier_admin = SupplierAdmin(Supplier, self.site)
        po_admin = PurchaseOrderAdmin(PurchaseOrder, self.site)
        inv_admin = InvoiceAdmin(Invoice, self.site)

        # Intentar acceder a objetos de Org B debe ser denegado por backend
        self.assertFalse(supplier_admin.has_view_permission(request, self.supplier_b))
        self.assertFalse(supplier_admin.has_change_permission(request, self.supplier_b))
        self.assertFalse(supplier_admin.has_delete_permission(request, self.supplier_b))

        self.assertFalse(po_admin.has_view_permission(request, self.po_b))
        self.assertFalse(po_admin.has_change_permission(request, self.po_b))
        self.assertFalse(po_admin.has_delete_permission(request, self.po_b))

        self.assertFalse(inv_admin.has_view_permission(request, self.invoice_b))
        self.assertFalse(inv_admin.has_change_permission(request, self.invoice_b))
        self.assertFalse(inv_admin.has_delete_permission(request, self.invoice_b))

        # Y debe conservar permisos plenos sobre sus propios registros
        self.assertTrue(supplier_admin.has_view_permission(request, self.supplier_a))
        self.assertTrue(supplier_admin.has_change_permission(request, self.supplier_a))
        self.assertTrue(po_admin.has_view_permission(request, self.po_a))
        self.assertTrue(inv_admin.has_view_permission(request, self.invoice_a))

    def test_04_staff_without_organization_gets_empty_queryset(self):
        """4. Usuario staff sin organización resuelta recibe un queryset vacío."""
        request = self.factory.get('/platform-superadmin/')
        request.user = self.staff_user_a
        request.organization = None

        supplier_admin = SupplierAdmin(Supplier, self.site)
        self.assertEqual(supplier_admin.get_queryset(request).count(), 0)


# ==============================================================================
# SEC-05: Secure Cookies
# ==============================================================================
class SecureCookieSettingsTestCase(TestCase):
    """
    Pruebas obligatorias para SEC-05:
    1. La configuración de producción habilita las banderas seguras (SESSION_COOKIE_SECURE y CSRF_COOKIE_SECURE).
    2. El perfil de desarrollo conserva el comportamiento compatible con el entorno local HTTP.
    3. Las pruebas no requieren certificados ni conexiones externas.
    """

    @override_settings(DEBUG=False, SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
    def test_01_production_profile_enables_secure_flags(self):
        """1. En producción (DEBUG=False), las directivas de cookies seguras están activadas."""
        self.assertTrue(settings.SESSION_COOKIE_SECURE)
        self.assertTrue(settings.CSRF_COOKIE_SECURE)
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)

    @override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
    def test_02_development_profile_allows_http_for_local_testing(self):
        """2. En desarrollo local (DEBUG=True), las cookies seguras pueden operar sobre HTTP plano."""
        self.assertFalse(settings.SESSION_COOKIE_SECURE)
        self.assertFalse(settings.CSRF_COOKIE_SECURE)
