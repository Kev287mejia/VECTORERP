from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from apps.core.models import CountryConfig
from apps.core.services.sandbox_seeder import SandboxScenarioSeeder
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel

User = get_user_model()


class Command(BaseCommand):
    help = "Prepara el entorno sandbox con escenario industrial real para Nicaragua (Eternal Labs)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Elimina datos previos y organizaciones huérfanas antes de sembrar.',
        )

    def handle(self, *args, **options):
        if not getattr(settings, 'ENABLE_SANDBOX', False):
            raise CommandError(
                "El sembrado del entorno sandbox está estrictamente deshabilitado en este entorno. "
                "Para autorizar el sembrado, configure ENABLE_SANDBOX=True en la configuración del entorno."
            )

        self.stdout.write("[SANDBOX] 1/4 Aplicando migraciones de base de datos...")
        call_command('migrate', interactive=False)

        self.stdout.write("[SANDBOX] 2/4 Sembrando configuración fiscal de Centroamérica...")
        call_command('seed_countries')

        country_ni = CountryConfig.objects.filter(code='NI').first()
        if not country_ni:
            self.stderr.write("[ERROR] No se encontró la configuración fiscal para Nicaragua (NI).")
            return

        self.stdout.write("[SANDBOX] 3/4 Creando usuario evaluador y organización demo...")
        evaluator_user, created_user = User.objects.get_or_create(
            email='evaluador@eternallabs.ni',
            defaults={
                'full_name': 'Ing. Evaluador de Producto & UX',
                'personal_id_number': '001-120590-0001Z',
                'phone': '+505 8888-0000',
            }
        )
        if created_user or not evaluator_user.check_password('Eternal2026*'):
            evaluator_user.set_password('Eternal2026*')
            evaluator_user.save()

        org, created_org = Organization.objects.get_or_create(
            slug='imesa-ingenieria',
            defaults={
                'name': 'IMESA INGENIERÍA S.A.',
                'legal_name': 'Instalaciones y Mantenimiento Electromecánico Sociedad Anónima',
                'tax_id': 'J0310000458912',
                'country': country_ni,
                'currency': Organization.Currency.NIO,
                'tax_regime': Organization.TaxRegime.GENERAL,
                'industry': Organization.Industry.ENERGY_TELCO,
                'accounting_template': Organization.AccountingTemplate.NIIF_PYMES,
                'enabled_modules': ['crm', 'operations', 'sst', 'inventory', 'finance'],
                'onboarding_completed': True,
                'is_active': True,
            }
        )

        OrganizationMembership.objects.update_or_create(
            user=evaluator_user,
            organization=org,
            defaults={
                'role_level': RoleLevel.OWNER_ADMIN,
                'job_title': 'Director General / Evaluador de Producto',
                'is_active': True,
                'is_default_org': True,
            }
        )

        self.stdout.write("[SANDBOX] 4/4 Sembrando escenario industrial real (CRM, OPS, SST, INV, FIN)...")
        stats = SandboxScenarioSeeder.populate_for_organization(org, evaluator_user)

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write("ENTORNO SANDBOX PREPARADO CORRECTAMENTE - ETERNAL LABS")
        self.stdout.write("=" * 72)
        self.stdout.write(f"Organización Demo: {org.name} [RUC: {org.tax_id}]")
        self.stdout.write(f"Usuario Evaluador: {evaluator_user.email} (Clave: Eternal2026*)")
        self.stdout.write(f"Módulos Activos  : {', '.join(org.enabled_modules).upper()}")
        self.stdout.write(f"Colaboradores    : {stats['members_count']} en cadena de mando RBAC")
        self.stdout.write(f"Cuentas B2B      : {stats['accounts_count']} cuentas corporativas e institucionales")
        self.stdout.write(f"Cotizaciones     : {stats['quotes_count']} bimonetarias (NIO y USD)")
        self.stdout.write(f"Órdenes Trabajo  : {stats['work_orders_count']} en diversas fases operativas")
        self.stdout.write(f"Checklists SST   : {stats['checklists_count']} inspecciones pre-operacionales Ley 618")
        self.stdout.write(f"Bodegas & Kardex : {stats['warehouses_count']} bodegas y {stats['inventory_items_count']} ítems industriales")
        self.stdout.write(f"Tesorería        : {stats['bank_accounts_count']} cuentas bancarias y {stats['petty_cash_funds_count']} caja chica")
        self.stdout.write("=" * 72)
        self.stdout.write("Acceso Rápido:")
        self.stdout.write("1. Servidor local : python manage.py runserver")
        self.stdout.write("2. Registro manual: http://127.0.0.1:8000/auth/register/")
        self.stdout.write("3. Login 1-Clic   : Botón instantáneo en http://127.0.0.1:8000/auth/login/")
        self.stdout.write("=" * 72)
