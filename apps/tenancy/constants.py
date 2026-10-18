# ==============================================================================
# ETERNAL LABS - ENTERPRISE SOFTWARE DIVISION
# CATÁLOGO CENTRALIZADO DE SUBSISTEMAS MODULARES (constants.py)
# ==============================================================================

ERP_MODULES_CATALOG = [
    {
        'code': 'crm',
        'tag': 'MOD-CRM',
        'name': 'Gestión Comercial & Cotizaciones',
        'badge': 'RECOMENDADO',
        'description': 'Pipeline de licitaciones corporativas, directorio de clientes y cotizador bimonetario con desglose de impuestos.',
        'min_role_level': 15,
        'default_enabled': True,
    },
    {
        'code': 'billing',
        'tag': 'MOD-BIL',
        'name': 'Facturación DGI, Retenciones & Caja',
        'badge': 'FISCAL DGI',
        'description': 'Emisión de facturas fiscales, retenciones IR/IMI en la fuente, cuentas por cobrar y recibos oficiales de caja.',
        'min_role_level': 80,
        'default_enabled': True,
    },
    {
        'code': 'purchasing',
        'tag': 'MOD-PUR',
        'name': 'Compras, Proveedores & Cuentas por Pagar',
        'badge': 'ABASTECIMIENTO',
        'description': 'Directorio de proveedores, órdenes de compra locales, recepción en almacén, retenciones DGI y cuentas por pagar.',
        'min_role_level': 80,
        'default_enabled': True,
    },
    {
        'code': 'operations',
        'tag': 'MOD-OPS',
        'name': 'Ingeniería, Frentes & Órdenes de Trabajo',
        'badge': 'CRÍTICO',
        'description': 'Gestión de cuadrillas de campo, asignación de ingenieros residentes y seguimiento de folios OT.',
        'min_role_level': 20,
        'default_enabled': True,
    },
    {
        'code': 'sst',
        'tag': 'MOD-SST',
        'name': 'Seguridad Laboral & Normativa MITRAB',
        'badge': 'LEY 618',
        'description': 'Auditorías de campo, checklists obligatorios de seguridad industrial y avales formales de riesgo.',
        'min_role_level': 20,
        'default_enabled': True,
    },
    {
        'code': 'inventory',
        'tag': 'MOD-INV',
        'name': 'Control de Materiales, Almacén & Kardex',
        'badge': 'LOGÍSTICA',
        'description': 'Gestión de bodegas centrales y satélites, control de existencias para proyectos y requisición de materiales.',
        'min_role_level': 50,
        'default_enabled': False,
    },
    {
        'code': 'finance',
        'tag': 'MOD-FIN',
        'name': 'Tesorería, Cajas Chicas & Bancos',
        'badge': 'FINANZAS',
        'description': 'Arqueos de caja chica en campo, fondos de viáticos y comprobantes de gasto bimonetarios.',
        'min_role_level': 50,
        'default_enabled': False,
    },
    {
        'code': 'tech',
        'tag': 'MOD-TECH',
        'name': 'Tecnología, Soporte IT & Activos Digitales',
        'badge': 'TI / SISTEMAS',
        'description': 'Mesa de ayuda técnica, inventario de hardware y equipos de cómputo, gestión de licencias software y soporte a usuarios.',
        'min_role_level': 20,
        'default_enabled': False,
    },
    {
        'code': 'marketing',
        'tag': 'MOD-MKT',
        'name': 'Marketing Digital, Campañas & Growth',
        'badge': 'CRECIMIENTO',
        'description': 'Gestión de campañas publicitarias multicanal, captación de prospectos entrantes (inbound leads) y analítica de conversión.',
        'min_role_level': 80,
        'default_enabled': False,
    },
    {
        'code': 'repository',
        'tag': 'MOD-REP',
        'name': 'Repositorio Documental y Versionado ISO',
        'badge': 'GESTIÓN DOC',
        'description': 'Control jerárquico de carpetas, planos, contratos y versionado concurrente con sellado SHA-256.',
        'min_role_level': 20,
        'default_enabled': True,
    },
    {
        'code': 'tickets',
        'tag': 'MOD-TCK',
        'name': 'Mesa de Ayuda y Soporte SLA',
        'badge': 'SLA & SOPORTE',
        'description': 'Mesa de incidentes internos, fallas de flota, averías de equipos y seguimiento de tiempos de respuesta.',
        'min_role_level': 20,
        'default_enabled': True,
    },
    {
        'code': 'ai_agent',
        'tag': 'MOD-MCP',
        'name': 'Servidor MCP & Agentes IA',
        'badge': 'AGENTE IA',
        'description': 'Servidor Model Context Protocol con tokens revocables, auditoría inmutable e interoperabilidad con LLMs.',
        'min_role_level': 80,
        'default_enabled': True,
    },
    {
        'code': 'payroll',
        'tag': 'MOD-PAY',
        'name': 'Planilla, Nómina INSS/IR & Prestaciones MITRAB',
        'badge': 'LEY 822 / 185',
        'description': 'Nómina Nicaragüense, INSS Laboral 7%, INSS Patronal, INATEC 2%, Tabla Progresiva IR Salarial Ley 822 y Provisiones MITRAB.',
        'min_role_level': 20,
        'default_enabled': True,
    },
]

# Diccionario indexado por código para consultas directas O(1)
ERP_MODULES_BY_CODE = {mod['code']: mod for mod in ERP_MODULES_CATALOG}

# Lista de tuplas para Choices de formularios Django
AVAILABLE_MODULES = [(mod['code'], f"{mod['tag']} · {mod['name']}") for mod in ERP_MODULES_CATALOG]

# Alias de compatibilidad
MODULE_CHOICES = AVAILABLE_MODULES

# Lista de códigos habilitados por defecto en el flujo de inicialización
DEFAULT_ENABLED_MODULES = [mod['code'] for mod in ERP_MODULES_CATALOG if mod['default_enabled']]

# Mapeo de umbrales jerárquicos de acceso RBAC
MODULE_THRESHOLD_MAP = {mod['code']: mod['min_role_level'] for mod in ERP_MODULES_CATALOG}


# ==============================================================================
# VERTICALES DE INDUSTRIA & PRESETS OPERATIVOS (PASO 12.1)
# ==============================================================================

from django.db import models


class BusinessVertical(models.TextChoices):
    ENGINEERING_SERVICES = 'ENGINEERING', 'Ingeniería, Construcción y Servicios de Campo'
    HARDWARE_STORE = 'HARDWARE', 'Ferretería, Materiales y Distribución Mayorista'
    PHARMACY_HEALTH = 'PHARMACY', 'Farmacia, Droguería e Insumos Médicos (MINSA)'
    SME_RETAIL = 'SME_RETAIL', 'PYME Comercial, Tienda y Servicios Generales'


BusinessVertical.ENGINEERING = BusinessVertical.ENGINEERING_SERVICES
BusinessVertical.HARDWARE = BusinessVertical.HARDWARE_STORE
BusinessVertical.PHARMACY = BusinessVertical.PHARMACY_HEALTH


VERTICAL_PRESETS_CONFIG = {
    BusinessVertical.ENGINEERING_SERVICES: {
        'name': 'Ingeniería, Construcción y Servicios de Campo',
        'short_name': 'INGENIERÍA',
        'default_modules': ['crm', 'operations', 'sst', 'inventory', 'finance', 'billing', 'purchasing', 'repository', 'tickets', 'ai_agent', 'payroll'],
        'enable_pos': False,
        'enable_batches_expiry': False,
        'enable_tiered_pricing': False,
        'enable_workorders_sst': True,
    },
    BusinessVertical.HARDWARE_STORE: {
        'name': 'Ferretería, Materiales y Distribución Mayorista',
        'short_name': 'FERRETERÍA',
        'default_modules': ['crm', 'inventory', 'finance', 'billing', 'purchasing', 'repository', 'tickets', 'ai_agent', 'payroll'],
        'enable_pos': True,
        'enable_batches_expiry': False,
        'enable_tiered_pricing': True,
        'enable_workorders_sst': False,
    },
    BusinessVertical.PHARMACY_HEALTH: {
        'name': 'Farmacia, Droguería e Insumos Médicos (MINSA)',
        'short_name': 'FARMACIA',
        'default_modules': ['crm', 'inventory', 'finance', 'billing', 'purchasing', 'repository', 'tickets', 'ai_agent', 'payroll'],
        'enable_pos': True,
        'enable_batches_expiry': True,
        'enable_tiered_pricing': False,
        'enable_workorders_sst': False,
    },
    BusinessVertical.SME_RETAIL: {
        'name': 'PYME Comercial, Tienda y Servicios Generales',
        'short_name': 'PYME COMERCIAL',
        'default_modules': ['crm', 'inventory', 'finance', 'billing', 'purchasing', 'repository', 'tickets', 'ai_agent', 'payroll'],
        'enable_pos': True,
        'enable_batches_expiry': False,
        'enable_tiered_pricing': True,
        'enable_workorders_sst': False,
    },
}
