from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from apps.core.models import TimeStampedModel
from apps.tenancy.constants import MODULE_THRESHOLD_MAP, BusinessVertical, VERTICAL_PRESETS_CONFIG


class CallableInt(int):
    """Permite evaluar una métrica tanto como valor entero como llamable sin romper retrocompatibilidad."""
    def __call__(self):
        return int(self)


class Organization(TimeStampedModel):
    """
    Representa el Tenant u Organización aislada en la plataforma SaaS.
    Cada cliente corporativo opera de manera aislada dentro de su organización.
    """
    class Currency(models.TextChoices):
        NIO = 'NIO', 'Córdoba Nicaragüense (C$)'
        USD = 'USD', 'Dólar Estadounidense ($)'

    class SubscriptionPlan(models.TextChoices):
        PYME_STARTER = 'PYME_STARTER', 'Plan PYME Starter ($39/mes · Hasta 5 Usuarios)'
        BUSINESS_PRO = 'BUSINESS_PRO', 'Plan Comercial Pro ($89/mes · Hasta 15 Usuarios)'
        ENTERPRISE = 'ENTERPRISE', 'Plan Industrial Enterprise ($199/mes · Usuarios Ilimitados / 50+)'
        # Aliases retrocompatibles
        STARTER = 'STARTER', 'Starter Comercial ($49 USD/mes · 5 Usuarios)'
        PYME_PRO = 'PYME_PRO', 'Pyme Profesional ($119 USD/mes · 15 Usuarios)'

    class SubscriptionStatus(models.TextChoices):
        TRIAL = 'TRIAL', 'Periodo de Evaluación'
        ACTIVE = 'ACTIVE', 'Suscripción Activa al Día'
        PAST_DUE = 'PAST_DUE', 'Pago Pendiente (Aviso de Cobro)'
        SUSPENDED = 'SUSPENDED', 'Servicio Suspendido por Mora'

    class TaxRegime(models.TextChoices):
        GENERAL = 'GENERAL', 'Régimen General DGI (IR Anual + Retenciones)'
        SIMPLIFIED = 'SIMPLIFIED', 'Régimen Simplificado / Cuota Fija'
        SPECIAL = 'SPECIAL', 'Régimen Especial / Gran Contribuyente / Zona Franca'

    class Industry(models.TextChoices):
        SERVICES = 'SERVICES', 'Servicios Industriales & Mantenimiento Especializado'
        CONSTRUCTION = 'CONSTRUCTION', 'Construcción, Ingeniería & Obras Civiles'
        ENERGY_TELCO = 'ENERGY_TELCO', 'Energía, Redes Eléctricas & Telecomunicaciones'
        COMMERCE = 'COMMERCE', 'Comercio, Distribución & Suministros'
        MANUFACTURING = 'MANUFACTURING', 'Manufactura, Agroindustria & Procesamiento'
        TECHNOLOGY = 'TECHNOLOGY', 'Tecnología, Software & Servicios IT'
        MARKETING = 'MARKETING', 'Marketing Digital, Medios & Publicidad'
        OTHER = 'OTHER', 'Otros Giros Comerciales / Corporativo'

    class BusinessProfile(models.TextChoices):
        ENGINEERING_SERVICES = 'ENGINEERING_SERVICES', 'Servicios Técnicos, Mantenimiento e Ingeniería'
        COMMERCIAL_RETAIL = 'COMMERCIAL_RETAIL', 'Comercio, Ferretería y Distribución'
        FIELD_CONTRACTOR = 'FIELD_CONTRACTOR', 'Contratista de Campo y Obras'
        HYBRID_ENTERPRISE = 'HYBRID_ENTERPRISE', 'Operación Integral Multi-Giro'

    BUSINESS_PROFILE_PRESETS = {
        'ENGINEERING_SERVICES': ['crm', 'operations', 'sst', 'inventory', 'billing', 'purchasing', 'repository', 'tickets', 'ai_agent', 'payroll'],
        'COMMERCIAL_RETAIL': ['crm', 'inventory', 'billing', 'purchasing', 'repository', 'tickets', 'payroll'],
        'FIELD_CONTRACTOR': ['operations', 'sst', 'inventory', 'purchasing', 'repository', 'tickets', 'payroll'],
        'HYBRID_ENTERPRISE': ['crm', 'operations', 'sst', 'inventory', 'billing', 'purchasing', 'repository', 'tickets', 'ai_agent', 'payroll'],
    }

    BusinessVertical = BusinessVertical

    class AccountingTemplate(models.TextChoices):
        NIIF_PYMES = 'NIIF_PYMES', 'Catálogo Estándar NIIF para PYMES (Nicaragua DGI)'
        SIMPLIFIED = 'SIMPLIFIED', 'Catálogo Simplificado de Servicios y Flujo de Fondos'
        BLANK = 'BLANK', 'Catálogo en Blanco (Configuración Manual)'

    name = models.CharField(
        max_length=150,
        verbose_name="Nombre Comercial",
        help_text="Nombre de marca o fantasía de la empresa (ej: Soluciones Industriales S.A.)"
    )
    legal_name = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="Razón Social",
        help_text="Nombre legal registrado ante las autoridades tributarias"
    )
    tax_id = models.CharField(
        max_length=50,
        db_index=True,
        verbose_name="Identificación Fiscal",
        help_text="Número de RUC, RTN, NIT o Cédula Jurídica según el país"
    )
    country = models.ForeignKey(
        'core.CountryConfig',
        on_delete=models.PROTECT,
        related_name='organizations',
        verbose_name="País de Operación Principal",
        help_text="Define la normativa fiscal, moneda y validaciones del Tenant"
    )
    currency = models.CharField(
        max_length=10,
        choices=Currency.choices,
        default=Currency.NIO,
        verbose_name="Moneda Principal Operativa",
        help_text="Moneda base para balances, cotizaciones y reportes"
    )
    tax_regime = models.CharField(
        max_length=30,
        choices=TaxRegime.choices,
        default=TaxRegime.GENERAL,
        verbose_name="Régimen Fiscal DGI",
        help_text="Régimen tributario de la empresa ante la DGI"
    )
    industry = models.CharField(
        max_length=40,
        choices=Industry.choices,
        default=Industry.SERVICES,
        verbose_name="Giro / Rubro Comercial",
        help_text="Sector o actividad económica principal"
    )
    business_profile = models.CharField(
        max_length=40,
        choices=BusinessProfile.choices,
        default=BusinessProfile.HYBRID_ENTERPRISE,
        verbose_name="Perfil de Giro de Negocio",
        help_text="Configuración predeterminada de arquitectura modular según el giro comercial"
    )
    business_vertical = models.CharField(
        max_length=40,
        choices=BusinessVertical.choices,
        default=BusinessVertical.ENGINEERING_SERVICES,
        verbose_name="Giro / Vertical de Industria",
        help_text="Vertical operativa que adapta módulos, flujos POS y catálogos"
    )
    accounting_template = models.CharField(
        max_length=30,
        choices=AccountingTemplate.choices,
        default=AccountingTemplate.NIIF_PYMES,
        verbose_name="Plan Contable Inicial",
        help_text="Plantilla de catálogo de cuentas y estructura financiera"
    )
    slug = models.SlugField(
        max_length=100,
        unique=True,
        db_index=True,
        verbose_name="Slug / Identificador URL",
        help_text="Identificador único en minúsculas y guiones para subdominios o URLs"
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name="Organización Activa",
        help_text="Permite deshabilitar el acceso completo a la organización por mora o suspensión"
    )
    onboarding_completed = models.BooleanField(
        default=False,
        verbose_name="Onboarding Completado",
        help_text="Indica si la organización culminó su configuración inicial de datos y módulos"
    )
    enabled_modules = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Módulos Habilitados",
        help_text="Lista de códigos de módulos contratados (ej: ['crm', 'operations', 'sst', 'inventory'])"
    )

    # Gobierno Comercial SaaS y Cuotas de Licenciamiento (Paso 19.1)
    subscription_plan = models.CharField(
        max_length=30,
        choices=SubscriptionPlan.choices,
        default=SubscriptionPlan.ENTERPRISE,
        verbose_name="Plan de Suscripción SaaS",
        help_text="Plan comercial activo del tenant"
    )
    subscription_status = models.CharField(
        max_length=20,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.ACTIVE,
        db_index=True,
        verbose_name="Estado de Suscripción",
        help_text="Estado de cobranza y operatividad del tenant en SaaS HQ"
    )
    monthly_fee_usd = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('199.00'),
        verbose_name="Tarifa Mensual (USD)",
        help_text="Canon mensual contratado en dólares"
    )
    max_users = models.PositiveIntegerField(
        default=50,
        verbose_name="Límite Máximo de Usuarios",
        help_text="Límite máximo de membresías activas permitidas por el plan"
    )
    max_users_quota = models.PositiveIntegerField(
        default=50,
        verbose_name="Cuota Máxima de Usuarios",
        help_text="Límite de usuarios asignados por la consola SaaS HQ"
    )
    next_billing_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Próxima Fecha de Facturación",
        help_text="Fecha de vencimiento o próximo corte de suscripción"
    )
    subscription_renews_at = models.DateField(
        null=True,
        blank=True,
        verbose_name="Fecha de Renovación / Próximo Cobro",
        help_text="Fecha de vencimiento o corte del periodo de suscripción"
    )

    class Meta:
        verbose_name = "Organización (Tenant)"
        verbose_name_plural = "Organizaciones (Tenants)"
        ordering = ['name']

    @property
    def currency_symbol(self) -> str:
        return 'C$' if self.currency == self.Currency.NIO else '$'

    def __str__(self) -> str:
        return f"{self.name} ({self.country.code})"

    def clean(self):
        super().clean()
        if self.tax_id and self.country_id:
            if hasattr(self, 'country') and not self.country.validate_tax_id(self.tax_id):
                raise ValidationError({
                    'tax_id': (
                        f"El formato del {self.country.tax_id_label} no es válido "
                        f"para {self.country.name}."
                    )
                })

    def is_module_enabled(self, module_code: str) -> bool:
        """Determina si un módulo específico está suscrito y habilitado para este Tenant."""
        if not self.is_active or not self.enabled_modules:
            return False
        return module_code.lower().strip() in [m.lower().strip() for m in self.enabled_modules]

    def apply_business_profile_preset(self, profile_code: str):
        """Aplica la configuración modular predefinida según el giro de negocio."""
        if profile_code in self.BusinessProfile.values:
            self.business_profile = profile_code
            self.enabled_modules = list(self.BUSINESS_PROFILE_PRESETS.get(profile_code, self.enabled_modules))
            self.save(update_fields=['business_profile', 'enabled_modules', 'updated_at'])

    def is_pharmacy_vertical(self) -> bool:
        """Indica si el tenant opera bajo la vertical de farmacia y salud."""
        return self.business_vertical == BusinessVertical.PHARMACY_HEALTH

    def is_hardware_vertical(self) -> bool:
        """Indica si el tenant opera bajo la vertical de ferretería y distribución."""
        return self.business_vertical == BusinessVertical.HARDWARE_STORE

    def is_sme_vertical(self) -> bool:
        """Indica si el tenant opera bajo la vertical de PYME comercial y tienda."""
        return self.business_vertical == BusinessVertical.SME_RETAIL

    def is_engineering_vertical(self) -> bool:
        """Indica si el tenant opera bajo la vertical de ingeniería y servicios de campo."""
        return self.business_vertical == BusinessVertical.ENGINEERING_SERVICES

    def apply_vertical_preset(self, vertical_code: str):
        """Aplica la configuración modular y operativa predefinida según la vertical de industria."""
        if vertical_code in BusinessVertical.values:
            self.business_vertical = vertical_code
            preset = VERTICAL_PRESETS_CONFIG.get(vertical_code)
            if preset and 'default_modules' in preset:
                self.enabled_modules = list(preset['default_modules'])
            self.save(update_fields=['business_vertical', 'enabled_modules', 'updated_at'])

    # Métodos Helper de Gobierno SaaS y Cuotas (Paso 19.1)
    def apply_subscription_plan(self, plan_code: str):
        """
        Ajusta cuotas y tarifas del plan SaaS.
        """
        code = str(plan_code).upper().strip()
        if code == 'STARTER':
            self.subscription_plan = self.SubscriptionPlan.STARTER
            self.monthly_fee_usd = Decimal('49.00')
            self.max_users = 5
            self.max_users_quota = 5
        elif code == self.SubscriptionPlan.PYME_STARTER:
            self.subscription_plan = self.SubscriptionPlan.PYME_STARTER
            self.monthly_fee_usd = Decimal('39.00')
            self.max_users = 5
            self.max_users_quota = 5
        elif code == 'PYME_PRO':
            self.subscription_plan = self.SubscriptionPlan.PYME_PRO
            self.monthly_fee_usd = Decimal('119.00')
            self.max_users = 15
            self.max_users_quota = 15
        elif code == self.SubscriptionPlan.BUSINESS_PRO:
            self.subscription_plan = self.SubscriptionPlan.BUSINESS_PRO
            self.monthly_fee_usd = Decimal('89.00')
            self.max_users = 15
            self.max_users_quota = 15
        elif code == self.SubscriptionPlan.ENTERPRISE:
            self.subscription_plan = self.SubscriptionPlan.ENTERPRISE
            self.monthly_fee_usd = Decimal('249.00')
            self.max_users = 999
            self.max_users_quota = 999
        else:
            return
        self.save(update_fields=['subscription_plan', 'monthly_fee_usd', 'max_users', 'max_users_quota', 'updated_at'])

    @property
    def active_members_count(self) -> CallableInt:
        """Retorna la cantidad de colaboradores activos en la organización."""
        return CallableInt(self.memberships.filter(is_active=True).count())

    @property
    def is_quota_exceeded(self) -> bool:
        """Indica si se ha alcanzado o superado la cuota de usuarios permitidos."""
        limit = self.max_users_quota or self.max_users or 50
        return int(self.active_members_count) >= limit

    def can_add_more_users(self) -> tuple[bool, str]:
        """Verifica si la organización dispone de cupos para incorporar nuevos colaboradores."""
        limit = self.max_users_quota or self.max_users or 50
        active = int(self.active_members_count)
        if active >= limit:
            return (
                False,
                f"Límite de asientos alcanzado / cuota ({active}/{limit} usuarios). El plan {self.get_subscription_plan_display()} no permite agregar más colaboradores. Actualice su suscripción en Eternal Labs HQ."
            )
        return (True, "OK")

    def is_write_blocked_by_saas_suspension(self) -> bool:
        """Determina si la organización se encuentra en modo Solo Lectura por suspensión comercial."""
        return self.subscription_status == self.SubscriptionStatus.SUSPENDED


class RoleLevel(models.IntegerChoices):
    """
    Escala jerárquica estricta de autoridad RBAC.
    Garantiza que la lógica de aprobación y permisos siempre siga una progresión numérica.
    Previene inversiones o colisiones entre roles operativos.
    """
    OBSERVER = 10, 'Observador / Auditor'
    SALES_ADVISOR = 15, 'Asesor Comercial'
    TECHNICIAN = 20, 'Técnico de Campo'
    ENGINEER = 50, 'Ingeniero Residente / Supervisor'
    SST_OFFICER = 70, 'Oficial SST (Seguridad y Salud)'
    OPERATIONS_COORD = 80, 'Coordinador Operativo'
    COMMERCIAL_MGR = 85, 'Gerente Comercial'
    OWNER_ADMIN = 100, 'Owner / Administrador General'


class Department(models.TextChoices):
    """
    Áreas o unidades operativas y funcionales de la organización.
    """
    OPERATIONS = 'OPERATIONS', 'Operaciones & Ingeniería'
    SST = 'SST', 'Seguridad y Salud en el Trabajo (SST)'
    LOGISTICS = 'LOGISTICS', 'Logística, Bodega & Inventario'
    COMMERCIAL = 'COMMERCIAL', 'Comercial & Licitaciones'
    FINANCE = 'FINANCE', 'Finanzas & Administración'
    MANAGEMENT = 'MANAGEMENT', 'Dirección General & Legal'
    TECH = 'TECH', 'Tecnología & Sistemas'
    OTHER = 'OTHER', 'Otros / General'


class ContractType(models.TextChoices):
    """
    Modalidades contractuales estándar corporativas.
    """
    PERMANENT = 'PERMANENT', 'Indeterminado / Fijo'
    PROJECT = 'PROJECT', 'Por Obra / Proyecto Determinado'
    SERVICES = 'SERVICES', 'Servicios Profesionales / Honorarios'
    INTERN = 'INTERN', 'Pasantía / Formación Técnica'


class BloodType(models.TextChoices):
    """
    Grupo sanguíneo para ficha médica y seguridad ocupacional en frentes de obra.
    """
    UNKNOWN = 'UNKNOWN', 'No Registrado'
    O_POS = 'O+', 'O Positivo (O+)'
    O_NEG = 'O-', 'O Negativo (O-)'
    A_POS = 'A+', 'A Positivo (A+)'
    A_NEG = 'A-', 'A Negativo (A-)'
    B_POS = 'B+', 'B Positivo (B+)'
    B_NEG = 'B-', 'B Negativo (B-)'
    AB_POS = 'AB+', 'AB Positivo (AB+)'
    AB_NEG = 'AB-', 'AB Negativo (AB-)'


class OrganizationMembership(TimeStampedModel):
    """
    Vínculo entre un Usuario y una Organización con un nivel de rol jerárquico asignado.
    Un usuario puede pertenecer a múltiples organizaciones pero con roles independientes.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='memberships',
        verbose_name="Usuario"
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='memberships',
        verbose_name="Organización"
    )
    role_level = models.IntegerField(
        choices=RoleLevel.choices,
        default=RoleLevel.OBSERVER,
        verbose_name="Nivel de Rol Jerárquico",
        help_text="Nivel numérico de autoridad (10=Observador ... 100=Owner/Admin)"
    )
    job_title = models.CharField(
        max_length=120,
        blank=True,
        verbose_name="Cargo / Título Profesional",
        help_text="Título descriptivo del colaborador en la organización (ej: Líder de Cuadrilla)"
    )
    department = models.CharField(
        max_length=30,
        choices=Department.choices,
        default=Department.OPERATIONS,
        verbose_name="Departamento / Área Operativa",
        help_text="Unidad organizativa o división funcional de asignación"
    )
    contract_type = models.CharField(
        max_length=30,
        choices=ContractType.choices,
        default=ContractType.PERMANENT,
        verbose_name="Modalidad Contractual",
        help_text="Régimen de contratación laboral"
    )
    social_security_number = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Número de Seguro Social (INSS)",
        help_text="Número de afiliación de seguro social del trabajador"
    )
    hire_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Fecha de Ingreso",
        help_text="Fecha oficial de inicio de labores en la organización"
    )
    work_location = models.CharField(
        max_length=120,
        blank=True,
        verbose_name="Ubicación / Base Operativa",
        help_text="Sede central, bodega o frente de obra asignado"
    )
    blood_type = models.CharField(
        max_length=10,
        choices=BloodType.choices,
        default=BloodType.UNKNOWN,
        verbose_name="Grupo Sanguíneo",
        help_text="Requerimiento de seguridad para personal en campo u obra"
    )
    emergency_contact_name = models.CharField(
        max_length=150,
        blank=True,
        verbose_name="Contacto de Emergencia (Nombre)",
        help_text="Familiar o contacto inmediato en caso de incidente operativo"
    )
    emergency_contact_phone = models.CharField(
        max_length=30,
        blank=True,
        verbose_name="Contacto de Emergencia (Teléfono)",
        help_text="Número telefónico del contacto de emergencia"
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="Membresía Activa",
        help_text="Permite revocar el acceso a este tenant específico sin eliminar al usuario"
    )
    is_default_org = models.BooleanField(
        default=False,
        verbose_name="Organización Predeterminada",
        help_text="Organización que se selecciona automáticamente al iniciar sesión"
    )

    class Meta:
        verbose_name = "Membresía en Organización"
        verbose_name_plural = "Membresías en Organizaciones"
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'organization'],
                name='unique_user_organization_membership'
            )
        ]
        ordering = ['-role_level', 'created_at']

    @property
    def role_display(self) -> str:
        return self.get_role_level_display()

    def __str__(self) -> str:
        return f"{self.user} @ {self.organization.name} [{self.get_role_level_display()}]"

    def can_approve(self, required_level: int) -> bool:
        """
        Evalúa si la membresía posee el nivel de autoridad jerárquico mínimo requerido
        para autorizar un documento, gasto, cambio de estado u orden de trabajo.
        """
        if not self.is_active or not self.organization.is_active:
            return False
        return self.role_level >= required_level

    def has_module_access(self, module_code: str, effective_role_level: int = None) -> bool:
        """
        Evalúa si el usuario tiene acceso a un módulo específico dentro de esta organización.
        1. Verifica que la membresía y la organización estén activas.
        2. Verifica que la organización tenga el módulo habilitado en 'enabled_modules'.
        3. Si se especifica 'effective_role_level', evalúa la autoridad contra ese nivel
           (utilizado por el Simulador de Rol en tiempo real para auditorías operativas).
        4. El Administrador General (OWNER_ADMIN 100) posee acceso irrestricto a todos los módulos contratados.
        5. Roles operativos validan su nivel de autoridad contra los umbrales específicos:
           - 'crm': Nivel 80 (OPERATIONS_COORD) o superior.
           - 'operations': Nivel 20 (TECHNICIAN) o superior.
           - 'sst': Nivel 20 (TECHNICIAN) o superior (técnicos registran; oficiales aprueban).
           - 'inventory': Nivel 50 (ENGINEER) o superior.
        """
        if not self.is_active or not self.organization.is_active:
            return False

        if not self.organization.is_module_enabled(module_code):
            return False

        eval_role_level = self.role_level if effective_role_level is None else effective_role_level

        if eval_role_level >= RoleLevel.OWNER_ADMIN:
            return True

        mod = module_code.lower().strip()
        min_required_role = MODULE_THRESHOLD_MAP.get(mod)
        if min_required_role is None:
            return False

        # Excepción localizada para CRM:
        # Los roles autorizados son SALES_ADVISOR (15) y roles de coordinación/gerencia >= 80.
        # Roles técnicos operativos (TECHNICIAN 20, ENGINEER 50, SST_OFFICER 70) y observadores (10)
        # no poseen acceso al módulo comercial.
        if mod == 'crm':
            return eval_role_level == RoleLevel.SALES_ADVISOR or eval_role_level >= RoleLevel.OPERATIONS_COORD

        return eval_role_level >= min_required_role


class TenantQuerySet(models.QuerySet):
    """
    QuerySet especializado para garantizar aislamiento multi-tenant en todas las consultas.
    """
    def for_org(self, organization):
        """
        Filtra el QuerySet por organización. Si organization es None o no tiene PK,
        retorna inmediatamente un conjunto vacío (none()) para evitar fugas de datos.
        """
        if not organization or not getattr(organization, 'pk', None):
            return self.none()
        return self.filter(organization=organization)


class TenantManager(models.Manager):
    """
    Manager base para todos los modelos multi-tenant.
    Provee el método .for_org(organization) de forma obligatoria y segura.
    """
    def get_queryset(self):
        return TenantQuerySet(self.model, using=self._db)

    def for_org(self, organization):
        """
        Retorna registros pertenecientes exclusivamente a la organización suministrada.
        Retorna .none() si organization es None o no es válida.
        """
        return self.get_queryset().for_org(organization)


class TenantScopedModel(TimeStampedModel):
    """
    Clase abstracta obligatoria para TODOS los modelos operativos del sistema.
    Garantiza:
    1. Aislamiento estricto por organización (Foreign Key a Organization).
    2. Indexación para alto rendimiento en consultas multi-tenant.
    3. Prevención de fugas de datos entre clientes mediante TenantManager.
    """
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        db_index=True,
        related_name="%(app_label)s_%(class)s_set",
        verbose_name="Organización / Tenant",
        help_text="Tenant propietario de este registro operativo"
    )

    objects = TenantManager()

    class Meta:
        abstract = True
        indexes = [
            models.Index(fields=['organization', 'created_at']),
        ]

    def save(self, *args, **kwargs):
        if not self.organization_id:
            raise ValidationError("Todo modelo derivado de TenantScopedModel requiere una organización asignada.")
        super().save(*args, **kwargs)


class CorporateEvent(TenantScopedModel):
    """
    Agenda Corporativa Unificada y Calendario de Hitos Fiscales/Operativos.
    Consolida vencimientos DGI/Alcaldía/INSS, mantenimientos técnicos en campo,
    inspecciones SST, visitas comerciales y auditorías de inventario FEFO.
    """
    class EventType(models.TextChoices):
        TECHNICAL_MAINTENANCE = 'TECHNICAL_MAINTENANCE', 'Mantenimiento / Ejecución Técnica'
        COMMERCIAL_VISIT = 'COMMERCIAL_VISIT', 'Visita Comercial / Licitación'
        SST_INSPECTION = 'SST_INSPECTION', 'Inspección / Capacitación SST'
        TAX_DEADLINE = 'TAX_DEADLINE', 'Vencimiento Fiscal DGI / Alcaldía / INSS'
        INVENTORY_AUDIT = 'INVENTORY_AUDIT', 'Conteo Físico / Auditoría FEFO'

    class Priority(models.TextChoices):
        LOW = 'LOW', 'Baja'
        NORMAL = 'NORMAL', 'Normal'
        HIGH = 'HIGH', 'Alta'
        CRITICAL = 'CRITICAL', 'Crítica'

    class Status(models.TextChoices):
        SCHEDULED = 'SCHEDULED', 'Programado'
        COMPLETED = 'COMPLETED', 'Completado'
        CANCELLED = 'CANCELLED', 'Cancelado'

    code = models.CharField(
        max_length=25,
        db_index=True,
        verbose_name="Código de Evento",
        help_text="Identificador único ej: EVT-2026-0001",
    )
    title = models.CharField(
        max_length=200,
        verbose_name="Título del Evento / Hito",
    )
    event_type = models.CharField(
        max_length=30,
        choices=EventType.choices,
        default=EventType.TECHNICAL_MAINTENANCE,
        db_index=True,
        verbose_name="Tipo de Evento",
    )
    priority = models.CharField(
        max_length=20,
        choices=Priority.choices,
        default=Priority.NORMAL,
        verbose_name="Prioridad",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SCHEDULED,
        db_index=True,
        verbose_name="Estado",
    )
    start_datetime = models.DateTimeField(
        verbose_name="Fecha y Hora de Inicio",
    )
    end_datetime = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Fecha y Hora de Fin",
    )
    location = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="Ubicación / Frente de Trabajo",
    )
    description = models.TextField(
        blank=True,
        verbose_name="Descripción / Instrucciones Técnicas",
    )
    min_role_level = models.IntegerField(
        choices=RoleLevel.choices,
        default=RoleLevel.ENGINEER,
        verbose_name="Nivel Jerárquico Mínimo de Visualización",
        help_text="Nivel jerárquico mínimo para visualizar este evento",
    )

    # Trazabilidad Operativa Multi-Módulo
    related_account = models.ForeignKey(
        'crm.Account',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='events',
        verbose_name="Cliente Vinculado",
    )
    related_work_order = models.ForeignKey(
        'operations.WorkOrder',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='events',
        verbose_name="Orden de Trabajo Vinculada",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_events',
        verbose_name="Responsable Asignado",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_events',
        verbose_name="Creado Por",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Evento Corporativo"
        verbose_name_plural = "Agenda Corporativa & Hitos"
        ordering = ['start_datetime']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'code'],
                name='unique_corporate_event_code_per_tenant',
            )
        ]

    def __str__(self) -> str:
        return f"{self.code} · {self.title} [{self.get_event_type_display()}]"

    def save(self, *args, **kwargs):
        # Regla de confidencialidad estricta comercial y fiscal:
        if self.event_type in (self.EventType.TAX_DEADLINE, self.EventType.COMMERCIAL_VISIT):
            if self.min_role_level < RoleLevel.OPERATIONS_COORD:
                self.min_role_level = RoleLevel.OPERATIONS_COORD

        # Autogenerador de código consecutivo por organización
        if not self.code and self.organization_id:
            year = self.start_datetime.year if self.start_datetime else timezone.now().year
            prefix = f"EVT-{year}-"
            count = (
                CorporateEvent.objects.filter(
                    organization_id=self.organization_id,
                    code__startswith=prefix,
                ).count()
                + 1
            )
            candidate = f"{prefix}{count:04d}"
            while CorporateEvent.objects.filter(
                organization_id=self.organization_id, code=candidate
            ).exists():
                count += 1
                candidate = f"{prefix}{count:04d}"
            self.code = candidate

        super().save(*args, **kwargs)


class SaasSubscriptionInvoice(TenantScopedModel):
    """
    Comprobante de Cobro y Facturación de Suscripción SaaS (SaaS HQ).
    Trazabilidad de pagos por licenciamiento recurrente B2B en USD y NIO.
    """
    class Status(models.TextChoices):
        PAID = 'PAID', 'Pagada'
        PENDING = 'PENDING', 'Pendiente'
        VOID = 'VOID', 'Anulada'

    number = models.CharField(
        max_length=25,
        db_index=True,
        verbose_name="Número de Factura SaaS",
        help_text="ej: SAAS-2026-0001",
    )
    plan_snapshot = models.CharField(
        max_length=25,
        default='ENTERPRISE',
        verbose_name="Plan Facturado",
    )
    billing_period = models.CharField(
        max_length=30,
        verbose_name="Periodo de Facturación",
        help_text="ej: 2026-10",
    )
    amount_usd = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('249.00'),
        verbose_name="Monto en USD",
    )
    exchange_rate = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal('36.6243'),
        verbose_name="Tipo de Cambio Oficial DGI",
    )
    amount_nio = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('9119.45'),
        verbose_name="Monto Equivalente en C$ (NIO)",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PAID,
        db_index=True,
        verbose_name="Estado de Cobro",
    )
    payment_reference = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Referencia de Pasarela / Transferencia",
        help_text="ej: STRIPE-NI-88412 / BAC-USD",
    )
    issued_at = models.DateField(
        default=timezone.now,
        verbose_name="Fecha de Emisión",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Factura de Suscripción SaaS"
        verbose_name_plural = "Facturas de Suscripción SaaS"
        ordering = ['-issued_at', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'number'],
                name='unique_saas_invoice_number_per_tenant',
            )
        ]

    def __str__(self) -> str:
        return f"{self.number} · {self.billing_period} [{self.plan_snapshot}] (${self.amount_usd} USD)"

    def save(self, *args, **kwargs):
        if not self.amount_nio and self.amount_usd and self.exchange_rate:
            self.amount_nio = (self.amount_usd * self.exchange_rate).quantize(Decimal('0.01'))

        if not self.number and self.organization_id:
            year = self.issued_at.year if self.issued_at else timezone.now().year
            prefix = f"SAAS-{year}-"
            count = (
                SaasSubscriptionInvoice.objects.filter(
                    organization_id=self.organization_id,
                    number__startswith=prefix,
                ).count()
                + 1
            )
            candidate = f"{prefix}{count:04d}"
            while SaasSubscriptionInvoice.objects.filter(
                organization_id=self.organization_id, number=candidate
            ).exists():
                count += 1
                candidate = f"{prefix}{count:04d}"
            self.number = candidate

        super().save(*args, **kwargs)


