import json
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Count, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.decorators.http import require_GET

from apps.core.models import CountryConfig
from apps.core.services.sandbox_seeder import SandboxScenarioSeeder
from apps.crm.models import Quote
from apps.operations.models import WorkOrder
from apps.sst.models import SafetyChecklist, LeaveRequest
from apps.tenancy.constants import BusinessVertical, ERP_MODULES_CATALOG, VERTICAL_PRESETS_CONFIG
from django.conf import settings
from apps.tenancy.forms import OrganizationOnboardingForm
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel
from apps.tenancy.permissions import RoleLevelRequiredMixin, render_403_rbac


class OnboardingWizardView(LoginRequiredMixin, View):
    """
    Flujo de configuración en 4 fases para la inicialización y despliegue del Tenant.
    Fase 1: Jurisdicción Fiscal Centroamericana.
    Fase 2: Giro Principal del Negocio (Vertical Operativa Inmutable).
    Fase 3: Identidad Legal y RUC (DGI Nicaragua).
    Fase 4: Confirmación y Ajuste de Módulos Operativos.
    """
    template_name = 'tenancy/onboarding_wizard.html'

    def _get_wizard_context(self, form):
        countries = CountryConfig.objects.all().order_by('-is_active', 'name')
        active_country = countries.filter(is_active=True).first()

        countries_dict = {}
        for c in countries:
            countries_dict[c.code] = {
                'code': c.code,
                'name': c.name,
                'primary_currency_code': c.primary_currency_code,
                'primary_currency_symbol': c.primary_currency_symbol,
                'secondary_currency_code': c.secondary_currency_code,
                'secondary_currency_symbol': c.secondary_currency_symbol,
                'tax_id_label': c.tax_id_label,
                'tax_id_regex': c.tax_id_regex,
                'personal_id_label': c.personal_id_label,
                'default_tax_name': c.default_tax_name,
                'default_tax_rate': f"{c.default_tax_rate:.2f}",
                'timezone': c.timezone,
                'is_active': c.is_active,
            }

        verticals_list = [
            {
                'code': BusinessVertical.ENGINEERING_SERVICES,
                'name': 'Ingeniería, Construcción y Operaciones de Campo',
                'tag': 'INGENIERÍA',
                'description': 'Proyectos electromecánicos, obras civiles, OTs, cuadrillas de linieros y SST Ley 618.',
                'highlight': 'Pre-activa OTs, Cuadrillas, SST Ley 618, Kardex de Obra y Planilla.',
                'modules': VERTICAL_PRESETS_CONFIG[BusinessVertical.ENGINEERING_SERVICES]['default_modules'],
            },
            {
                'code': BusinessVertical.HARDWARE_STORE,
                'name': 'Ferretería, Materiales y Distribución Mayorista',
                'tag': 'FERRETERÍA',
                'description': 'Ventas de mostrador, materiales de construcción, 3 listas de precios y abastecimiento.',
                'highlight': 'Pre-activa Terminal POS, Listas Detalle/Contratista/Mayorista y Compras.',
                'modules': VERTICAL_PRESETS_CONFIG[BusinessVertical.HARDWARE_STORE]['default_modules'],
            },
            {
                'code': BusinessVertical.PHARMACY_HEALTH,
                'name': 'Farmacia, Droguería e Insumos Médicos MINSA',
                'tag': 'FARMACIA MINSA',
                'description': 'Dispensación farmacéutica, control de lotes y vencimientos FEFO con registro sanitario.',
                'highlight': 'Pre-activa Terminal POS, Lotes FEFO y Registro Sanitario; apaga OTs y SST.',
                'modules': VERTICAL_PRESETS_CONFIG[BusinessVertical.PHARMACY_HEALTH]['default_modules'],
            },
            {
                'code': BusinessVertical.SME_RETAIL,
                'name': 'PYME Comercial, Tienda y Servicios Generales',
                'tag': 'PYME COMERCIAL',
                'description': 'Comercio minorista, facturación ágil, inventario de tienda y cobranza inmediata.',
                'highlight': 'Pre-activa Terminal POS, Inventario, Facturación y Caja; apaga módulos industriales pesados.',
                'modules': VERTICAL_PRESETS_CONFIG[BusinessVertical.SME_RETAIL]['default_modules'],
            },
        ]
        vertical_presets_dict = {
            v['code']: v['modules'] for v in verticals_list
        }

        return {
            'countries': countries,
            'active_country': active_country,
            'countries_json': json.dumps(countries_dict),
            'countries_dict': countries_dict,
            'modules_catalog': ERP_MODULES_CATALOG,
            'business_verticals': verticals_list,
            'vertical_presets_json': json.dumps(vertical_presets_dict),
            'form': form,
        }

    def get(self, request, *args, **kwargs):
        countries = CountryConfig.objects.all().order_by('-is_active', 'name')
        active_country = countries.filter(is_active=True).first()

        form = OrganizationOnboardingForm(initial={
            'country_code': active_country.code if active_country else 'NI',
            'business_vertical': BusinessVertical.ENGINEERING_SERVICES,
            'enabled_modules': VERTICAL_PRESETS_CONFIG[BusinessVertical.ENGINEERING_SERVICES]['default_modules'],
        })

        return render(request, self.template_name, self._get_wizard_context(form))

    def post(self, request, *args, **kwargs):
        form = OrganizationOnboardingForm(request.POST)
        countries = CountryConfig.objects.all().order_by('-is_active', 'name')
        active_country = countries.filter(is_active=True).first()

        if form.is_valid():
            with transaction.atomic():
                country = form.cleaned_country
                name = form.cleaned_data['name'].strip()
                legal_name = form.cleaned_data['legal_name'].strip()
                tax_id = form.cleaned_data['tax_id'].strip().upper()
                currency = form.cleaned_data.get('currency', Organization.Currency.NIO)
                tax_regime = form.cleaned_data.get('tax_regime', Organization.TaxRegime.GENERAL)
                industry = form.cleaned_data.get('industry', Organization.Industry.SERVICES)
                business_vertical = form.cleaned_data.get('business_vertical') or BusinessVertical.ENGINEERING_SERVICES
                accounting_template = form.cleaned_data.get('accounting_template', Organization.AccountingTemplate.NIIF_PYMES)
                enabled_modules = form.cleaned_data['enabled_modules']
                slug = form.generate_unique_slug(name, tax_id)

                if business_vertical == BusinessVertical.ENGINEERING_SERVICES:
                    business_profile = Organization.BusinessProfile.ENGINEERING_SERVICES
                else:
                    business_profile = Organization.BusinessProfile.COMMERCIAL_RETAIL

                # 1. Crear Organización operativa con giro de negocio inmutable
                organization = Organization.objects.create(
                    country=country,
                    name=name,
                    legal_name=legal_name,
                    tax_id=tax_id,
                    currency=currency,
                    tax_regime=tax_regime,
                    industry=industry,
                    business_profile=business_profile,
                    business_vertical=business_vertical,
                    accounting_template=accounting_template,
                    slug=slug,
                    enabled_modules=enabled_modules,
                    onboarding_completed=True,
                    is_active=True,
                )

                try:
                    from apps.core.services.sandbox_seeder import ensure_vertical_demo_catalog
                    ensure_vertical_demo_catalog(organization, business_vertical)
                except Exception:
                    pass

                # 2. Desmarcar default previo
                OrganizationMembership.objects.filter(
                    user=request.user,
                    is_default_org=True
                ).update(is_default_org=False)

                # 3. Asignar al usuario como OWNER_ADMIN (Nivel 100)
                membership = OrganizationMembership.objects.create(
                    user=request.user,
                    organization=organization,
                    role_level=RoleLevel.OWNER_ADMIN,
                    job_title='Director General / Administrador',
                    is_active=True,
                    is_default_org=True,
                )

                # 4. Establecer en sesión HTTP
                request.session['active_org_id'] = str(organization.id)

            messages.success(
                request,
                f"Entorno ERP inicializado con éxito para '{organization.name}'. "
                f"Jurisdicción: {country.name} ({country.code}) · Moneda: {organization.get_currency_display()}."
            )
            return redirect('tenancy:dashboard')

        return render(request, self.template_name, self._get_wizard_context(form), status=422)


class SwitchOrganizationView(LoginRequiredMixin, View):
    """
    Permite conmutar la organización activa en la sesión del usuario previa validación
    estricta de membresía activa.
    """
    def post(self, request, *args, **kwargs):
        org_id = request.POST.get('org_id') or kwargs.get('org_id')
        membership = get_object_or_404(
            OrganizationMembership,
            user=request.user,
            organization_id=org_id,
            is_active=True,
            organization__is_active=True,
        )
        request.session['active_org_id'] = str(membership.organization.id)
        messages.info(
            request,
            f"Espacio de trabajo cambiado a: {membership.organization.name}"
        )
        return redirect('tenancy:dashboard')

    def get(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)


from apps.tenancy.permissions import TenantRequiredMixin


class DashboardWorkspaceView(TenantRequiredMixin, View):
    """
    Consola principal del Workspace Operativo.
    Protegido con TenantRequiredMixin: Si el usuario no tiene ninguna organización activa,
    lo redirige automáticamente al flujo de Onboarding.
    """
    template_name = 'tenancy/dashboard.html'

    def get(self, request, *args, **kwargs):
        org = request.organization
        country = org.country
        role_level = getattr(request, 'role_level', 0)

        curr_sym = org.currency_symbol

        # Métricas operativas técnicas calculadas STRICTAMENTE según los módulos suscritos
        kpi_cards = []

        # 1. MOD-CRM
        open_quotes_val = Decimal('0.00')
        if org.is_module_enabled('crm'):
            open_quotes_val = Quote.objects.for_org(org).filter(
                status__in=[Quote.Status.DRAFT, Quote.Status.SENT]
            ).aggregate(total=Sum('grand_total'))['total'] or Decimal('0.00')
            open_quotes_count = Quote.objects.for_org(org).filter(
                status__in=[Quote.Status.DRAFT, Quote.Status.SENT]
            ).count()
            kpi_cards.append({
                'id': 'crm',
                'title': 'LICITACIONES & COTIZACIONES',
                'badge': 'MOD-CRM',
                'badge_status': f"{open_quotes_count} ABIERTAS",
                'value': f"{curr_sym} {open_quotes_val:,.2f}",
                'subtitle': f"Moneda: {org.currency} ({curr_sym})",
                'tag': 'Pipeline Activo',
            })

        # 2. MOD-OPS
        active_wo_count = 0
        if org.is_module_enabled('operations'):
            active_wo_count = WorkOrder.objects.for_org(org).exclude(
                phase__in=[WorkOrder.Phase.COMPLETED, WorkOrder.Phase.CANCELLED]
            ).count()
            kpi_cards.append({
                'id': 'operations',
                'title': 'ÓRDENES EN CAMPO',
                'badge': 'MOD-OPS',
                'badge_status': 'DESPLEGADAS',
                'value': str(active_wo_count),
                'subtitle': 'Cuadrillas en frentes de obra',
                'tag': 'Occidente/MGA',
            })

        # 3. MOD-SST
        pending_sst_total = 0
        if org.is_module_enabled('sst'):
            pending_ots_sst = WorkOrder.objects.for_org(org).filter(
                is_high_risk=True,
                sst_cleared=False,
            ).exclude(phase__in=[WorkOrder.Phase.COMPLETED, WorkOrder.Phase.CANCELLED]).count()
            pending_checklists = SafetyChecklist.objects.for_org(org).filter(
                status=SafetyChecklist.Status.PENDING
            ).count()
            pending_leaves = LeaveRequest.objects.for_org(org).filter(
                status=LeaveRequest.Status.PENDING
            ).count()
            pending_sst_total = pending_ots_sst + pending_checklists + pending_leaves
            kpi_cards.append({
                'id': 'sst',
                'title': 'PERMISOS LEY 618 MITRAB',
                'badge': 'MOD-SST',
                'badge_status': 'AVAL REQUERIDO',
                'value': str(pending_sst_total),
                'subtitle': 'Maniobras 138kV & Altura',
                'tag': 'Inspección',
            })

        # 4. MOD-INV
        if org.is_module_enabled('inventory'):
            from apps.inventory.models import Warehouse
            wh_count = Warehouse.objects.for_org(org).filter(is_active=True).count()
            kpi_cards.append({
                'id': 'inventory',
                'title': 'BODEGAS & LOGÍSTICA',
                'badge': 'MOD-INV',
                'badge_status': 'ACTIVO',
                'value': str(wh_count),
                'subtitle': 'Bodegas Centrales y Móviles',
                'tag': 'Kardex',
            })

        # 5. MOD-FIN
        if org.is_module_enabled('finance'):
            from apps.finance.models import BankAccount, PettyCashFund
            bank_count = BankAccount.objects.for_org(org).filter(is_active=True).count()
            funds = PettyCashFund.objects.for_org(org).filter(is_active=True)
            petty_bal = funds.aggregate(total=Sum('current_balance'))['total'] or Decimal('0.00')
            kpi_cards.append({
                'id': 'finance',
                'title': 'TESORERÍA & CAJAS CHICAS',
                'badge': 'MOD-FIN',
                'badge_status': f"{bank_count} CUENTAS",
                'value': f"{curr_sym} {petty_bal:,.2f}",
                'subtitle': f"{funds.count()} Fondos Asignados",
                'tag': 'Caja Chica',
            })

        if org.is_module_enabled('tech'):
            kpi_cards.append({
                'id': 'tech',
                'title': 'TECNOLOGÍA & ACTIVOS TI',
                'badge': 'MOD-TECH',
                'badge_status': 'SOPORTE ACTIVO',
                'value': '100% OPERATIVO',
                'subtitle': 'Mesa de Ayuda & Licencias Cloud',
                'tag': 'Infraestructura',
            })

        if org.is_module_enabled('marketing'):
            kpi_cards.append({
                'id': 'marketing',
                'title': 'MARKETING & GROWTH',
                'badge': 'MOD-MKT',
                'badge_status': 'INBOUND READY',
                'value': 'CRECIMIENTO',
                'subtitle': 'Campañas Digitales & Leads',
                'tag': 'Pauta & Medios',
            })

        # Ficha Gobernanza & Perfil Fiscal (Siempre presente)
        kpi_cards.append({
            'id': 'governance',
            'title': 'PERFIL FISCAL & SUBSISTEMAS',
            'badge': f"DGI · {country.code}",
            'badge_status': org.get_tax_regime_display()[:15].upper(),
            'value': f"{len(org.enabled_modules or [])} / 7",
            'subtitle': f"Giro: {org.get_industry_display()[:25]}",
            'tag': org.currency,
        })

        kpis = {
            'active_work_orders': active_wo_count,
            'open_quotations': f"{curr_sym} {open_quotes_val:,.2f}",
            'pending_sst': pending_sst_total,
            'enabled_modules_count': len(org.enabled_modules or []),
            'total_modules_count': 7,
        }

        # Matriz de Autoridad Jerárquica y Delegación (Normativa DGI / MITRAB Nicaragua)
        rbac_matrix = [
            {
                'level': RoleLevel.OWNER_ADMIN,
                'name': 'Representante Legal / Gerente General (Owner)',
                'description': 'Gobierno corporativo del RUC, libros de actas, apoderado general ante DGI/MITRAB y administración de membresías.',
                'scope': 'GOBIERNO TOTAL',
            },
            {
                'level': RoleLevel.COMMERCIAL_MGR,
                'name': 'Gerente Comercial & Licitaciones',
                'description': 'Cuentas corporativas, licitaciones públicas/privadas, pliegos, ofertas bimonetarias (NIO/USD) y contratos comerciales.',
                'scope': 'COMERCIAL & CONTRATOS',
            },
            {
                'level': RoleLevel.OPERATIONS_COORD,
                'name': 'Jefe de Operaciones & Plantel',
                'description': 'Programación y despacho de cuadrillas de línea/subestación, logística de plantel central y conduces de almacén.',
                'scope': 'OPERACIONES & PLANTEL',
            },
            {
                'level': RoleLevel.SST_OFFICER,
                'name': 'Responsable de Higiene y Seguridad (SST)',
                'description': 'Fiscalización Ley 618 MITRAB, aval de permisos para trabajos de alto riesgo (altura/tensión) y actas de comisión mixta.',
                'scope': 'SEGURIDAD LEY 618',
            },
            {
                'level': RoleLevel.ENGINEER,
                'name': 'Ingeniero Residente / Supervisor de Obras',
                'description': 'Dirección técnica en frentes de obra, bitácora de ingeniería, descargo eléctrico y requisición de materiales.',
                'scope': 'INGENIERÍA & SITIO',
            },
            {
                'level': RoleLevel.TECHNICIAN,
                'name': 'Técnico Liniero / Electricista de Redes',
                'description': 'Ejecución de maniobras en sitio, reporte técnico de cuadrilla, inspección preoperacional y uso de EPP dieléctrico.',
                'scope': 'FRENTES DE CAMPO',
            },
            {
                'level': RoleLevel.OBSERVER,
                'name': 'Auditor Externo / Revisor Fiscal',
                'description': 'Inspección de libros contables, retenciones de ley DGI (IR/Alcaldía) y libro mayor de Kardex.',
                'scope': 'FISCALIZACIÓN DGI',
            },
        ]

        # Consultas reales a la base de datos para la organización activa
        total_members_count = OrganizationMembership.objects.filter(
            organization=org, is_active=True
        ).count()

        enabled_codes = set(org.enabled_modules or [])
        enabled_modules_list = [mod for mod in ERP_MODULES_CATALOG if mod['code'] in enabled_codes]

        membership = getattr(request, 'membership', None)
        if not membership and request.user.is_authenticated:
            membership = OrganizationMembership.objects.filter(
                user=request.user, organization=org, is_active=True
            ).first()

        accessible_modules_count = 0
        for mod in enabled_modules_list:
            if membership and membership.has_module_access(mod['code'], effective_role_level=role_level):
                accessible_modules_count += 1

        membership_counts = dict(
            OrganizationMembership.objects.filter(organization=org, is_active=True)
            .values('role_level')
            .annotate(cnt=Count('id'))
            .values_list('role_level', 'cnt')
        )

        rbac_distribution = []
        for level_val, label in sorted(RoleLevel.choices, key=lambda x: x[0], reverse=True):
            rbac_distribution.append({
                'level': level_val,
                'label': label,
                'members_count': membership_counts.get(level_val, 0),
                'is_current_session_level': (level_val == role_level),
                'can_current_user_approve': (role_level >= level_val),
            })

        recent_members = list(
            OrganizationMembership.objects.filter(organization=org, is_active=True)
            .select_related('user')
            .order_by('-created_at')[:5]
        )

        return render(request, self.template_name, {
            'kpis': kpis,
            'kpi_cards': kpi_cards,
            'rbac_matrix': rbac_matrix,
            'total_members_count': total_members_count,
            'enabled_modules_list': enabled_modules_list,
            'accessible_modules_count': accessible_modules_count,
            'rbac_distribution': rbac_distribution,
            'recent_members': recent_members,
            'modules_catalog': ERP_MODULES_CATALOG,
            'current_country': country,
            'current_role_level': role_level,
        })


@require_GET
def validate_ruc_htmx(request):
    """
    Endpoint reactivo HTMX para verificación asíncrona de sintaxis y unicidad del RUC.
    """
    country_code = request.GET.get('country_code', 'NI').upper().strip()
    tax_id = request.GET.get('tax_id', '').upper().strip()

    if not tax_id:
        return HttpResponse("")

    country = CountryConfig.objects.filter(code=country_code).first()
    if not country:
        return HttpResponse("")

    is_valid = country.validate_tax_id(tax_id)
    is_duplicate = Organization.objects.filter(country=country, tax_id=tax_id).exists()

    if is_duplicate:
        return HttpResponse(
            f'<div class="flex items-center gap-1.5 text-xs font-mono text-amber-700 mt-1">'
            f'<svg class="w-3.5 h-3.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"/></svg>'
            f'<span>{country.tax_id_label} ya registrado en el sistema.</span>'
            f'</div>'
        )

    if is_valid:
        return HttpResponse(
            f'<div class="flex items-center gap-1.5 text-xs font-mono text-emerald-700 mt-1">'
            f'<svg class="w-3.5 h-3.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>'
            f'<span>Estructura válida para {country.name} ({country.tax_id_label}).</span>'
            f'</div>'
        )
    else:
        return HttpResponse(
            f'<div class="flex items-center gap-1.5 text-xs font-mono text-rose-700 mt-1">'
            f'<svg class="w-3.5 h-3.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z"/></svg>'
            f'<span>Formato inválido (requiere 14 dígitos o cédula ej: 001-290590-0001A).</span>'
            f'</div>'
        )


class LoadSandboxDataView(RoleLevelRequiredMixin, View):
    """
    Carga interactiva de escenario industrial real para Nicaragua.
    Exclusivo para Owner / Administrador General (Nivel 100).
    """
    min_role_level = RoleLevel.OWNER_ADMIN

    def post(self, request, *args, **kwargs):
        if getattr(request, 'real_role_level', 0) < RoleLevel.OWNER_ADMIN:
            messages.error(request, "Solo el Administrador General puede ejecutar la carga del escenario demo.")
            return redirect('tenancy:dashboard')

        stats = SandboxScenarioSeeder.populate_for_organization(request.organization, request.user)
        messages.success(
            request,
            f"Escenario industrial de Nicaragua cargado con éxito: {stats['accounts_count']} cuentas, "
            f"{stats['quotes_count']} cotizaciones, {stats['work_orders_count']} OTs, "
            f"{stats['warehouses_count']} bodegas y cuentas bancarias actualizadas."
        )
        referer = request.META.get('HTTP_REFERER')
        return redirect(referer if referer else 'tenancy:dashboard')


class ResetSandboxDataView(RoleLevelRequiredMixin, View):
    """
    Vaciado de datos de prueba del tenant para pruebas en blanco.
    Exclusivo para Owner / Administrador General (Nivel 100).

    Salvaguarda estricta de seguridad multiempresa (SEC-03):
    1. El entorno debe tener DEBUG=True y ENABLE_SANDBOX=True.
    2. La organización debe estar expresamente listada en settings.SANDBOX_ORGANIZATION_SLUGS.
       Cualquier organización no listada es considerada estrictamente productiva y su reinicio es rechazado con HTTP 403.
    3. El usuario debe poseer autoridad real OWNER_ADMIN (Nivel 100).
    """
    min_role_level = RoleLevel.OWNER_ADMIN

    def post(self, request, *args, **kwargs):
        # 1. Comprobación del entorno de ejecución
        is_debug = getattr(settings, 'DEBUG', False)
        is_sandbox_enabled = getattr(settings, 'ENABLE_SANDBOX', False)
        if not (is_debug and is_sandbox_enabled):
            return render_403_rbac(
                request,
                reason="El reinicio de datos sandbox está estrictamente deshabilitado en este entorno de producción."
            )

        # 2. Comprobación de que la organización esté explícitamente autorizada como sandbox
        sandbox_slugs = getattr(settings, 'SANDBOX_ORGANIZATION_SLUGS', ['imesa-ingenieria'])
        org = getattr(request, 'organization', None)
        if not org or org.slug not in sandbox_slugs:
            return render_403_rbac(
                request,
                reason="La organización es de carácter productivo. El reinicio masivo de datos está terminantemente prohibido."
            )

        # 3. Comprobación de autoridad real Nivel 100
        if getattr(request, 'real_role_level', 0) < RoleLevel.OWNER_ADMIN:
            return render_403_rbac(
                request,
                min_role_level=RoleLevel.OWNER_ADMIN,
                reason="Solo el Administrador General (Owner) puede solicitar el reinicio de datos del tenant."
            )

        SandboxScenarioSeeder.reset_organization_data(org)
        messages.warning(
            request,
            "Datos operativos de prueba vaciados. La organización y su usuario administrador se conservan limpios."
        )
        referer = request.META.get('HTTP_REFERER')
        return redirect(referer if referer else 'tenancy:dashboard')
