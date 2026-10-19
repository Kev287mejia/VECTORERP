from apps.tenancy.constants import BusinessVertical, VERTICAL_PRESETS_CONFIG
from apps.tenancy.models import OrganizationMembership, RoleLevel


def tenancy_context(request):
    """
    Context Processor Multi-Tenant, RBAC Jerárquico y Simulador de Rol.
    Expone variables de contexto seguras y reactivas para todos los templates del ERP.
    """
    user = getattr(request, 'user', None)
    organization = getattr(request, 'organization', None)
    membership = getattr(request, 'membership', None)
    role_level = getattr(request, 'role_level', 0)
    real_role_level = getattr(request, 'real_role_level', role_level)
    is_simulating_role = getattr(request, 'is_simulating_role', False)

    # Identificación del país de operación
    current_country = getattr(organization, 'country', None) if organization else None

    # Vertical de Industria y Presets Operativos (Paso 12.1)
    current_vertical = getattr(organization, 'business_vertical', BusinessVertical.ENGINEERING_SERVICES) if organization else BusinessVertical.ENGINEERING_SERVICES
    vertical_preset = VERTICAL_PRESETS_CONFIG.get(current_vertical, VERTICAL_PRESETS_CONFIG[BusinessVertical.ENGINEERING_SERVICES])
    current_vertical_label = vertical_preset.get('name', 'Ingeniería, Construcción y Servicios de Campo')
    current_vertical_short = vertical_preset.get('short_name', 'INGENIERÍA')

    vertical_features = {
        'enable_pos': vertical_preset.get('enable_pos', False),
        'enable_batches_expiry': vertical_preset.get('enable_batches_expiry', False),
        'enable_tiered_pricing': vertical_preset.get('enable_tiered_pricing', False),
        'enable_workorders_sst': vertical_preset.get('enable_workorders_sst', True),
    }

    available_verticals = [
        {
            'code': code,
            'name': config['name'],
            'short_name': config['short_name'],
            'is_active': (code == current_vertical),
        }
        for code, config in VERTICAL_PRESETS_CONFIG.items()
    ]

    # Etiqueta del rol activo (mostrando el rol simulado si la simulación está activa)
    if role_level in RoleLevel.values:
        current_role_label = RoleLevel(role_level).label
    elif membership:
        current_role_label = membership.get_role_level_display()
    elif user and user.is_authenticated and user.is_superuser:
        current_role_label = "SUPERADMIN PLATAFORMA"
    else:
        current_role_label = "SIN ASIGNAR"

    # Membresías activas del usuario (para el switcher de organización)
    user_memberships = []
    if user and user.is_authenticated:
        user_memberships = list(
            OrganizationMembership.objects
            .select_related('organization', 'organization__country')
            .filter(
                user=user,
                is_active=True,
                organization__is_active=True
            )
            .order_by('-role_level', 'organization__name')
        )

    # Matriz booleana de navegación calculada en tiempo real
    # Nota clave: se pasa effective_role_level=role_level para que la barra lateral y los accesos
    # reaccionen inmediatamente cuando el Owner simula otro rol operativo.
    nav_access = {
        'can_view_crm': membership.has_module_access('crm', effective_role_level=role_level) if membership else False,
        'can_manage_crm': (role_level in [RoleLevel.SALES_ADVISOR, RoleLevel.COMMERCIAL_MGR, RoleLevel.OWNER_ADMIN]) if membership else False,
        'can_approve_quotes': (role_level >= RoleLevel.COMMERCIAL_MGR) if membership else False,
        'can_view_billing': membership.has_module_access('billing', effective_role_level=role_level) if membership else False,
        'can_manage_billing': (role_level >= RoleLevel.COMMERCIAL_MGR) if membership else False,
        'can_view_purchasing': membership.has_module_access('purchasing', effective_role_level=role_level) if membership else False,
        'can_pay_suppliers': (role_level >= RoleLevel.COMMERCIAL_MGR) if membership else False,
        'can_view_operations': membership.has_module_access('operations', effective_role_level=role_level) if membership else False,
        'can_view_sst': membership.has_module_access('sst', effective_role_level=role_level) if membership else False,
        'can_view_inventory': membership.has_module_access('inventory', effective_role_level=role_level) if membership else False,
        'can_view_finance': membership.has_module_access('finance', effective_role_level=role_level) if membership else False,
        'can_view_tech': membership.has_module_access('tech', effective_role_level=role_level) if membership else False,
        'can_view_marketing': membership.has_module_access('marketing', effective_role_level=role_level) if membership else False,
        'can_view_repository': membership.has_module_access('repository', effective_role_level=role_level) if membership else False,
        'can_view_tickets': membership.has_module_access('tickets', effective_role_level=role_level) if membership else False,
        'can_view_ai_mcp': membership.has_module_access('ai_agent', effective_role_level=role_level) if membership else False,
        'can_view_payroll': membership.has_module_access('payroll', effective_role_level=role_level) if membership else False,
        'can_manage_payroll': role_level >= RoleLevel.COMMERCIAL_MGR,
        'can_view_own_payslips': role_level >= RoleLevel.TECHNICIAN,
        'can_manage_org': role_level >= RoleLevel.OWNER_ADMIN,
        'can_approve_sst': role_level >= RoleLevel.SST_OFFICER,
        'can_plan_crews': role_level >= RoleLevel.OPERATIONS_COORD,
        'can_view_financials': (role_level >= RoleLevel.OPERATIONS_COORD) if membership else False,
        'can_view_financial_statements': (role_level >= RoleLevel.COMMERCIAL_MGR) if membership else False,
        'is_field_tech_only': role_level == RoleLevel.TECHNICIAN,
    }

    # Radar de Alertas Activas en Tiempo Real
    active_alerts_count = 0
    critical_alerts_count = 0
    if user and user.is_authenticated and organization and membership:
        try:
            from apps.core.services.alerts_engine import get_active_alerts_for_user
            alerts_data = get_active_alerts_for_user(organization, user, role_level)
            active_alerts_count = alerts_data['counts']['total']
            critical_alerts_count = alerts_data['counts']['critical']
        except Exception:
            active_alerts_count = 0
            critical_alerts_count = 0

    return {
        'current_org': organization,
        'current_country': current_country,
        'current_membership': membership,
        'current_role_level': role_level,
        'real_role_level': real_role_level,
        'is_simulating_role': is_simulating_role,
        'current_role_label': current_role_label,
        'user_memberships': user_memberships,
        'nav_access': nav_access,
        'can_view_financial_statements': (role_level >= RoleLevel.COMMERCIAL_MGR) if membership else False,
        'RoleLevel': RoleLevel,
        'role_choices': RoleLevel.choices,
        'role_choices_desc': sorted(RoleLevel.choices, key=lambda x: x[0], reverse=True),
        'current_vertical': current_vertical,
        'current_vertical_label': current_vertical_label,
        'current_vertical_short': current_vertical_short,
        'vertical_features': vertical_features,
        'available_verticals': available_verticals,
        'active_alerts_count': active_alerts_count,
        'critical_alerts_count': critical_alerts_count,
    }
