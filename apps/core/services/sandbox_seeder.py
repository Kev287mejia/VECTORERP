"""
Motor Transaccional de Escenario Industrial - Eternal Labs.
Generador e inicializador idempotente de datos operacionales de demostración
para evaluación de producto, diseño y experiencia de usuario (UX).
"""
import datetime
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

import hashlib
from apps.billing.models import Invoice, InvoiceItem, PaymentReceipt, CashRegisterSession, PosRegisterShift
from apps.core.models import CountryConfig
from apps.crm.models import Account, AccountContact, Opportunity, Quote, QuoteItem
from apps.purchasing.models import PurchaseOrder, PurchaseOrderItem, Supplier, SupplierPayment
from apps.repository.models import RepositoryFolder, CorporateDocument, DocumentVersion
from apps.repository.services import publish_new_document_version
from apps.tickets.models import SupportTicket, TicketComment
from apps.ai_agent.models import McpAccessToken, McpAuditLog
from apps.ai_agent.services import generate_mcp_token
from apps.finance.models import (
    BankAccount,
    FinancialTransaction,
    PettyCashExpense,
    PettyCashFund,
    PettyCashReplenishment,
    AccountingAccount,
    JournalEntry,
    JournalEntryLine,
)
from apps.finance.services import (
    sync_commercial_and_purchasing_to_ledger,
    ensure_default_chart_of_accounts,
)
from apps.inventory.models import InventoryItem, StockBalance, StockMovement, Warehouse, InventoryBatch
from apps.inventory.services import record_stock_entry
from apps.operations.models import (
    Crew,
    WorkOrder,
    WorkOrderEvidence,
    WorkOrderItem,
    WorkOrderTimelineLog,
)
from apps.sst.models import EPPDelivery, LeaveRequest, SafetyChecklist
from apps.tenancy.models import (
    BloodType,
    ContractType,
    CorporateEvent,
    Department,
    Organization,
    OrganizationMembership,
    RoleLevel,
    SaasSubscriptionInvoice,
)
from apps.payroll.models import EmployeeContract, PayrollPeriod, Payslip
from apps.payroll.services import close_and_account_payroll_period

User = get_user_model()


class SandboxScenarioSeeder:
    """
    Servicio corporativo de poblamiento y limpieza de escenarios industriales para Nicaragua.
    Diseñado para pruebas de producto, evaluación de marketing y auditoría de interfaz.
    """

    @classmethod
    @transaction.atomic
    def reset_organization_data(cls, organization: Organization):
        """
        Elimina de forma limpia y segura todos los registros transaccionales y operativos
        de la organización indicada, preservando únicamente la entidad Organization
        y la membresía Nivel 100 del Owner/Admin actual.
        """
        # 0. Servidor MCP y Agentes IA (apps/ai_agent)
        McpAuditLog.objects.filter(organization=organization).delete()
        McpAccessToken.objects.filter(organization=organization).delete()

        # 0.1 Repositorio Documental y Mesa de Ayuda (apps/repository & apps/tickets)
        TicketComment.objects.filter(organization=organization).delete()
        SupportTicket.objects.filter(organization=organization).delete()
        DocumentVersion.objects.filter(organization=organization).delete()
        CorporateDocument.objects.filter(organization=organization).delete()
        RepositoryFolder.objects.filter(organization=organization).delete()

        # 1. Asientos de Partida Doble en Borrador y Catálogo Contable (apps/finance)
        JournalEntryLine.objects.filter(organization=organization).delete()
        JournalEntry.objects.filter(organization=organization).delete()
        AccountingAccount.objects.filter(organization=organization).delete()

        # 1.1 Compras, Proveedores y Cuentas por Pagar (apps/purchasing)
        SupplierPayment.objects.filter(organization=organization).delete()
        PurchaseOrderItem.objects.filter(organization=organization).delete()
        PurchaseOrder.objects.filter(organization=organization).delete()
        Supplier.objects.filter(organization=organization).delete()

        # 2. Facturación Fiscal, Retenciones DGI, Recibos Oficiales de Caja (ROC) y Arqueos Z
        PosRegisterShift.objects.filter(organization=organization).update(status=PosRegisterShift.Status.OPEN, audit_hash='')
        PosRegisterShift.objects.filter(organization=organization).delete()
        CashRegisterSession.objects.filter(organization=organization).update(status=CashRegisterSession.Status.OPEN, audit_hash='')
        CashRegisterSession.objects.filter(organization=organization).delete()
        PaymentReceipt.objects.filter(organization=organization).delete()
        InvoiceItem.objects.filter(organization=organization).delete()
        Invoice.objects.filter(organization=organization).delete()

        # 3. Finanzas y Tesorería
        FinancialTransaction.objects.filter(organization=organization).delete()
        PettyCashExpense.objects.filter(organization=organization).delete()
        PettyCashReplenishment.objects.filter(organization=organization).delete()
        PettyCashFund.objects.filter(organization=organization).delete()
        BankAccount.objects.filter(organization=organization).delete()

        # 4. Inventario, Bodegas y Kardex
        StockMovement.objects.filter(organization=organization).delete()
        InventoryBatch.objects.filter(organization=organization).delete()
        StockBalance.objects.filter(organization=organization).delete()
        InventoryItem.objects.filter(organization=organization).delete()
        Warehouse.objects.filter(organization=organization).delete()

        # 5. Seguridad y Salud en el Trabajo (SST Ley 618)
        SafetyChecklist.objects.filter(organization=organization).delete()
        EPPDelivery.objects.filter(organization=organization).delete()
        LeaveRequest.objects.filter(organization=organization).delete()

        # 6. Operaciones, Cuadrillas y Órdenes de Trabajo
        WorkOrderEvidence.objects.filter(organization=organization).delete()
        WorkOrderTimelineLog.objects.filter(organization=organization).delete()
        WorkOrderItem.objects.filter(organization=organization).delete()
        WorkOrder.objects.filter(organization=organization).delete()
        Crew.objects.filter(organization=organization).delete()

        # 7. Comercial, Pipeline y Cotizador Bimonetario
        QuoteItem.objects.filter(organization=organization).delete()
        Quote.objects.filter(organization=organization).delete()
        Opportunity.objects.filter(organization=organization).delete()
        AccountContact.objects.filter(organization=organization).delete()
        Account.objects.filter(organization=organization).delete()

        # 7.5 Agenda Corporativa y Calendario Fiscal (apps/tenancy / apps/core)
        CorporateEvent.objects.filter(organization=organization).delete()

        # 7.6 Facturación de Suscripción SaaS HQ
        SaasSubscriptionInvoice.objects.filter(organization=organization).delete()

        # 7.7 Nómina Laboral, Colillas y Provisiones (apps/payroll)
        PayrollPeriod.objects.filter(organization=organization).update(status=PayrollPeriod.Status.DRAFT, audit_hash='')
        Payslip.objects.filter(organization=organization).delete()
        PayrollPeriod.objects.filter(organization=organization).delete()
        EmployeeContract.objects.filter(organization=organization).delete()

        # 8. Miembros subordinados del equipo
        OrganizationMembership.objects.filter(
            organization=organization
        ).exclude(role_level=RoleLevel.OWNER_ADMIN).delete()

    @classmethod
    @transaction.atomic
    def populate_for_organization(cls, organization: Organization, owner_user=None):
        """
        Siembra un escenario operativo industrial completo para Nicaragua en una organización dada.
        La ejecución es estrictamente idempotente.
        """
        # Habilitar los módulos operativos contratados incluyendo Facturación DGI, Compras, Repositorio, Mesa de Ayuda y MCP
        all_modules = ['crm', 'operations', 'sst', 'inventory', 'finance', 'billing', 'purchasing', 'repository', 'tickets', 'ai_agent']
        organization.enabled_modules = all_modules
        organization.business_profile = Organization.BusinessProfile.ENGINEERING_SERVICES
        organization.onboarding_completed = True
        organization.subscription_plan = Organization.SubscriptionPlan.ENTERPRISE
        organization.subscription_status = Organization.SubscriptionStatus.ACTIVE
        organization.monthly_fee_usd = Decimal('199.00')
        organization.max_users_quota = 50
        organization.max_users = 50
        organization.next_billing_date = datetime.date(2026, 11, 1)
        organization.subscription_renews_at = datetime.date(2026, 11, 1)
        organization.save(update_fields=[
            'enabled_modules', 'business_profile', 'onboarding_completed',
            'subscription_plan', 'subscription_status', 'monthly_fee_usd',
            'max_users_quota', 'max_users', 'next_billing_date', 'subscription_renews_at',
        ])

        # Limpiar datos previos manteniendo solo el Owner
        cls.reset_organization_data(organization)

        if not owner_user:
            owner_membership = OrganizationMembership.objects.filter(
                organization=organization,
                role_level=RoleLevel.OWNER_ADMIN,
                is_active=True,
            ).first()
            if owner_membership:
                owner_user = owner_membership.user
            else:
                owner_user = User.objects.filter(is_active=True).first()

        # =========================================================================
        # 1. PERSONAL Y CADENA DE MANDO RBAC (apps/tenancy)
        # =========================================================================
        team_specs = [
            {
                'email': 'vjarquin@imesa.ni',
                'full_name': 'Lic. Valentina Jarquín Rivas',
                'personal_id_number': '001-140288-0004Y',
                'phone': '+505 8844-1234',
                'role_level': RoleLevel.COMMERCIAL_MGR,
                'job_title': 'Gerente de Licitaciones & Contratos',
                'department': Department.COMMERCIAL,
                'contract_type': ContractType.PERMANENT,
                'social_security_number': '4589123-1',
                'hire_date': datetime.date(2024, 1, 15),
                'blood_type': BloodType.O_POS,
                'emergency_contact_name': 'Ernesto Jarquín (Padre)',
                'emergency_contact_phone': '+505 8877-2233',
            },
            {
                'email': 'mfonseca@imesa.ni',
                'full_name': 'Ing. Marcos Antonio Fonseca',
                'personal_id_number': '001-220785-0002A',
                'phone': '+505 8455-9876',
                'role_level': RoleLevel.OPERATIONS_COORD,
                'job_title': 'Coordinador General de Operaciones',
                'department': Department.OPERATIONS,
                'contract_type': ContractType.PERMANENT,
                'social_security_number': '3891456-8',
                'hire_date': datetime.date(2023, 6, 1),
                'blood_type': BloodType.A_POS,
                'emergency_contact_name': 'Lucía Fonseca (Esposa)',
                'emergency_contact_phone': '+505 8622-4411',
            },
            {
                'email': 'hgomez@imesa.ni',
                'full_name': 'Lic. Hazel Gómez Blandón',
                'personal_id_number': '081-190490-0003K',
                'phone': '+505 8933-7722',
                'role_level': RoleLevel.SST_OFFICER,
                'job_title': 'Oficial de Seguridad Industrial & Salud MITRAB',
                'department': Department.SST,
                'contract_type': ContractType.PERMANENT,
                'social_security_number': '5123984-4',
                'hire_date': datetime.date(2024, 3, 10),
                'blood_type': BloodType.O_NEG,
                'emergency_contact_name': 'Ramiro Blandón (Hermano)',
                'emergency_contact_phone': '+505 8711-5588',
            },
            {
                'email': 'cchamorro@imesa.ni',
                'full_name': 'Ing. Carlos Eduardo Chamorro',
                'personal_id_number': '001-051189-0006L',
                'phone': '+505 8366-2244',
                'role_level': RoleLevel.ENGINEER,
                'job_title': 'Ingeniero Residente de Subestaciones',
                'department': Department.OPERATIONS,
                'contract_type': ContractType.PERMANENT,
                'social_security_number': '4712039-2',
                'hire_date': datetime.date(2024, 2, 1),
                'blood_type': BloodType.B_POS,
                'emergency_contact_name': 'Silvia Morales (Cónyuge)',
                'emergency_contact_phone': '+505 8833-9900',
            },
            {
                'email': 'cruiz@imesa.ni',
                'full_name': 'Téc. Carlos Ruiz Martínez',
                'personal_id_number': '001-300895-0008F',
                'phone': '+505 8511-3322',
                'role_level': RoleLevel.TECHNICIAN,
                'job_title': 'Técnico Electromecánico & Liniero',
                'department': Department.OPERATIONS,
                'contract_type': ContractType.PROJECT,
                'social_security_number': '6294810-7',
                'hire_date': datetime.date(2024, 5, 15),
                'blood_type': BloodType.O_POS,
                'emergency_contact_name': 'Marta Ruiz (Madre)',
                'emergency_contact_phone': '+505 8422-7711',
            },
            {
                'email': 'rruiz@auditoria-externa.ni',
                'full_name': 'Aud. Rodolfo Ruiz Mendoza',
                'personal_id_number': '001-180378-0001P',
                'phone': '+505 8922-6633',
                'role_level': RoleLevel.OBSERVER,
                'job_title': 'Auditor Externo de Cumplimiento Financiero & SST',
                'department': Department.MANAGEMENT,
                'contract_type': ContractType.SERVICES,
                'social_security_number': '2109438-5',
                'hire_date': datetime.date(2024, 6, 1),
                'blood_type': BloodType.AB_POS,
                'emergency_contact_name': 'Oficina Central Ruiz & Asociados',
                'emergency_contact_phone': '+505 2270-1122',
            },
        ]

        created_users = {}
        for spec in team_specs:
            user, created = User.objects.get_or_create(
                email=spec['email'],
                defaults={
                    'full_name': spec['full_name'],
                    'personal_id_number': spec['personal_id_number'],
                    'phone': spec['phone'],
                }
            )
            if created or not user.check_password('Eternal2026*'):
                user.set_password('Eternal2026*')
                user.save()

            OrganizationMembership.objects.update_or_create(
                user=user,
                organization=organization,
                defaults={
                    'role_level': spec['role_level'],
                    'job_title': spec['job_title'],
                    'department': spec['department'],
                    'contract_type': spec['contract_type'],
                    'social_security_number': spec['social_security_number'],
                    'hire_date': spec['hire_date'],
                    'blood_type': spec['blood_type'],
                    'emergency_contact_name': spec['emergency_contact_name'],
                    'emergency_contact_phone': spec['emergency_contact_phone'],
                    'is_active': True,
                }
            )
            created_users[spec['role_level']] = user

        commercial_mgr = created_users[RoleLevel.COMMERCIAL_MGR]
        ops_coord = created_users[RoleLevel.OPERATIONS_COORD]
        sst_officer = created_users[RoleLevel.SST_OFFICER]
        resident_eng = created_users[RoleLevel.ENGINEER]
        field_tech = created_users[RoleLevel.TECHNICIAN]

        # =========================================================================
        # 2. DIRECTORIO COMERCIAL, PIPELINE Y COTIZACIONES (apps/crm)
        # =========================================================================
        accounts_data = [
            {
                'code': 'CLI-001',
                'commercial_name': 'ENATREL - Transmisión Eléctrica',
                'legal_name': 'Empresa Nacional de Transmisión Eléctrica',
                'account_type': Account.AccountType.GOVERNMENT,
                'tax_id': 'J0310000000018',
                'email': 'licitaciones@enatrel.gob.ni',
                'phone': '+505 2222-4455',
                'address': 'Rotonda El Güegüense 2c abajo, Managua',
                'city': 'Managua',
                'default_currency': 'NIO',
                'payment_terms_days': 30,
                'contact_name': 'Ing. Fernando Rivas',
                'contact_title': 'Gerente de Subestaciones y Líneas',
            },
            {
                'commercial_name': 'CEMEX Nicaragua S.A.',
                'code': 'CLI-002',
                'legal_name': 'CEMEX de Nicaragua Sociedad Anónima',
                'account_type': Account.AccountType.CORPORATE,
                'tax_id': 'J0310000012459',
                'email': 'adquisiciones@cemex.com',
                'phone': '+505 2269-8000',
                'address': 'Km 35 Carretera a San Rafael del Sur',
                'city': 'San Rafael del Sur',
                'default_currency': 'USD',
                'payment_terms_days': 30,
                'contact_name': 'Ing. Gabriela Somarriba',
                'contact_title': 'Jefa de Mantenimiento de Planta',
            },
            {
                'commercial_name': 'Ingenio San Antonio',
                'code': 'CLI-003',
                'legal_name': 'Nicaragua Sugar Estates Limited',
                'account_type': Account.AccountType.CORPORATE,
                'tax_id': 'J0310000005423',
                'email': 'compras.industriales@nicaraguasugar.com',
                'phone': '+505 2342-9100',
                'address': 'Plantel Industrial Ingenio San Antonio',
                'city': 'Chichigalpa',
                'default_currency': 'USD',
                'payment_terms_days': 15,
                'contact_name': 'Ing. Alejandro Pellas Chamorro',
                'contact_title': 'Director de Operaciones Energéticas',
            },
            {
                'commercial_name': 'Hospital Metropolitano Vivian Pellas',
                'code': 'CLI-004',
                'legal_name': 'Hospital Metropolitano Vivian Pellas S.A.',
                'account_type': Account.AccountType.CORPORATE,
                'tax_id': 'J0310000078129',
                'email': 'ingenieria@metropolitano.com.ni',
                'phone': '+505 2255-6900',
                'address': 'Km 9.8 Carretera a Masaya 250m al este',
                'city': 'Managua',
                'default_currency': 'USD',
                'payment_terms_days': 30,
                'contact_name': 'Lic. Carlos Lacayo',
                'contact_title': 'Director de Infraestructura y Biomédica',
            },
            {
                'commercial_name': 'Holcim Nicaragua S.A.',
                'code': 'CLI-005',
                'legal_name': 'Holcim (Nicaragua) Sociedad Anónima',
                'account_type': Account.AccountType.CORPORATE,
                'tax_id': 'J0310000034901',
                'email': 'compras@holcim.com.ni',
                'phone': '+505 2311-2244',
                'address': 'Km 42 Carretera Nueva a León',
                'city': 'Nagarote',
                'default_currency': 'NIO',
                'payment_terms_days': 45,
                'contact_name': 'Ing. Mauricio Baltodano',
                'contact_title': 'Superintendente Eléctrico',
            },
        ]

        accounts = {}
        for acc_spec in accounts_data:
            contact_name = acc_spec.pop('contact_name')
            contact_title = acc_spec.pop('contact_title')
            acc = Account.objects.create(
                organization=organization,
                lifecycle_stage=Account.LifecycleStage.ACTIVE,
                assigned_to=commercial_mgr,
                **acc_spec
            )
            AccountContact.objects.create(
                organization=organization,
                account=acc,
                full_name=contact_name,
                job_title=contact_title,
                email=acc.email,
                phone=acc.phone,
                is_primary=True,
            )
            accounts[acc.code] = acc

        # Oportunidades en distintas fases del pipeline
        opp_specs = [
            {
                'code': 'OP-2026-0001',
                'account': accounts['CLI-001'],
                'title': 'Modernización de Transformador de Potencia 230kV Subestación Tipitapa',
                'currency': 'USD',
                'estimated_value': Decimal('85000.00'),
                'stage': Opportunity.Stage.NEGOTIATION,
                'expected_close_date': timezone.now().date() + datetime.timedelta(days=20),
            },
            {
                'code': 'OP-2026-0002',
                'account': accounts['CLI-002'],
                'title': 'Mantenimiento Predictivo Termográfico de Tableros Planta San Rafael',
                'currency': 'NIO',
                'estimated_value': Decimal('345000.00'),
                'stage': Opportunity.Stage.PROPOSAL,
                'expected_close_date': timezone.now().date() + datetime.timedelta(days=15),
            },
            {
                'code': 'OP-2026-0003',
                'account': accounts['CLI-003'],
                'title': 'Automatización SCADA de Turbogenerador Zafra 2026',
                'currency': 'USD',
                'estimated_value': Decimal('120000.00'),
                'stage': Opportunity.Stage.QUALIFIED,
                'expected_close_date': timezone.now().date() + datetime.timedelta(days=45),
            },
            {
                'code': 'OP-2026-0004',
                'account': accounts['CLI-004'],
                'title': 'Suministro de Sistema Ininterrumpido UPS Grado Quirófanos',
                'currency': 'USD',
                'estimated_value': Decimal('48000.00'),
                'stage': Opportunity.Stage.WON,
                'expected_close_date': timezone.now().date() - datetime.timedelta(days=5),
            },
            {
                'code': 'OP-2026-0005',
                'account': accounts['CLI-005'],
                'title': 'Instalación de Banco de Capacitores 13.8kV Planta Molinos',
                'currency': 'NIO',
                'estimated_value': Decimal('580000.00'),
                'stage': Opportunity.Stage.WON,
                'expected_close_date': timezone.now().date() - datetime.timedelta(days=10),
            },
            {
                'code': 'OP-2026-0006',
                'account': accounts['CLI-001'],
                'title': 'Inspección de Línea de Transmisión 138kV Corredor Norte',
                'currency': 'NIO',
                'estimated_value': Decimal('420000.00'),
                'stage': Opportunity.Stage.NEW,
                'expected_close_date': timezone.now().date() + datetime.timedelta(days=60),
            },
        ]

        opportunities = []
        for opp in opp_specs:
            o = Opportunity.objects.create(
                organization=organization,
                assigned_to=commercial_mgr,
                **opp
            )
            opportunities.append(o)

        # Cotizaciones Bimonetarias con Desglose de Partidas e IVA 15%
        # 1. COT-2026-0001 (USD - Aprobada - Hospital Vivian Pellas)
        q1 = Quote.objects.create(
            organization=organization,
            number='COT-2026-0001',
            account=accounts['CLI-004'],
            opportunity=opportunities[3],
            title='Suministro e Instalación de Sistema UPS Grado Quirófano 40kVA',
            status=Quote.Status.APPROVED,
            currency_code='USD',
            currency_symbol='$',
            exchange_rate=Decimal('36.6243'),
            tax_rate=Decimal('15.00'),
            apply_tax=True,
            valid_until=timezone.now().date() + datetime.timedelta(days=30),
            created_by=commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=organization,
            quote=q1,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-ING-01',
            description='Montaje, conexionado de fuerza y puesta en marcha de UPS modular',
            unit='GLB',
            quantity=Decimal('1.00'),
            unit_price=Decimal('8500.00'),
        )
        QuoteItem.objects.create(
            organization=organization,
            quote=q1,
            item_type=QuoteItem.ItemType.MATERIAL,
            code='MAT-BAT-01',
            description='Banco de baterías selladas AGM de alta descarga 12V 100Ah',
            unit='UND',
            quantity=Decimal('32.00'),
            unit_price=Decimal('350.00'),
        )
        QuoteItem.objects.create(
            organization=organization,
            quote=q1,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-PRU-02',
            description='Pruebas de conmutación en carga y certificación de onda senoidal',
            unit='GLB',
            quantity=Decimal('1.00'),
            unit_price=Decimal('2500.00'),
        )

        # 2. COT-2026-0002 (NIO - Aprobada - Holcim Nicaragua)
        q2 = Quote.objects.create(
            organization=organization,
            number='COT-2026-0002',
            account=accounts['CLI-005'],
            opportunity=opportunities[4],
            title='Suministro y Montaje de Banco de Capacitores Automático 13.8kV',
            status=Quote.Status.APPROVED,
            currency_code='NIO',
            currency_symbol='C$',
            exchange_rate=Decimal('36.6243'),
            tax_rate=Decimal('15.00'),
            apply_tax=True,
            valid_until=timezone.now().date() + datetime.timedelta(days=25),
            created_by=commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=organization,
            quote=q2,
            item_type=QuoteItem.ItemType.MATERIAL,
            code='MAT-CAP-01',
            description='Celdas capacitivas trifásicas de media tensión 300 kVAR 13.8kV',
            unit='UND',
            quantity=Decimal('4.00'),
            unit_price=Decimal('85000.00'),
        )
        QuoteItem.objects.create(
            organization=organization,
            quote=q2,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-EST-01',
            description='Fabricación de estructura metálica galvanizada y barras colectoras',
            unit='GLB',
            quantity=Decimal('1.00'),
            unit_price=Decimal('65000.00'),
        )
        QuoteItem.objects.create(
            organization=organization,
            quote=q2,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-IZA-01',
            description='Maniobra de izaje con grúa y conexión en celda de media tensión',
            unit='HRS',
            quantity=Decimal('2.00'),
            unit_price=Decimal('42000.00'),
        )

        # 3. COT-2026-0003 (USD - Enviada - CEMEX)
        q3 = Quote.objects.create(
            organization=organization,
            number='COT-2026-0003',
            account=accounts['CLI-002'],
            opportunity=opportunities[1],
            title='Servicio de Termografía Infrarroja y Análisis de Calidad de Energía',
            status=Quote.Status.SENT,
            currency_code='USD',
            currency_symbol='$',
            exchange_rate=Decimal('36.6243'),
            tax_rate=Decimal('15.00'),
            apply_tax=True,
            valid_until=timezone.now().date() + datetime.timedelta(days=15),
            created_by=commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=organization,
            quote=q3,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-TER-01',
            description='Inspección termográfica FLIR en 48 CCMs de planta y transformadores',
            unit='GLB',
            quantity=Decimal('1.00'),
            unit_price=Decimal('4800.00'),
        )
        QuoteItem.objects.create(
            organization=organization,
            quote=q3,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-ARM-01',
            description='Monitoreo de armónicos y factor de potencia con analizador Fluke',
            unit='JRN',
            quantity=Decimal('3.00'),
            unit_price=Decimal('1200.00'),
        )

        # 4. COT-2026-0004 (NIO - Borrador - ENATREL)
        q4 = Quote.objects.create(
            organization=organization,
            number='COT-2026-0004',
            account=accounts['CLI-001'],
            opportunity=opportunities[0],
            title='Reemplazo de Aisladores de Porcelana en Pórtico 138kV Subestación León',
            status=Quote.Status.DRAFT,
            currency_code='NIO',
            currency_symbol='C$',
            exchange_rate=Decimal('36.6243'),
            tax_rate=Decimal('15.00'),
            apply_tax=True,
            valid_until=timezone.now().date() + datetime.timedelta(days=30),
            created_by=commercial_mgr,
        )
        QuoteItem.objects.create(
            organization=organization,
            quote=q4,
            item_type=QuoteItem.ItemType.MATERIAL,
            code='MAT-AIS-01',
            description='Cadena de aisladores poliméricos tipo suspensión 138kV ANSI 52-3',
            unit='UND',
            quantity=Decimal('18.00'),
            unit_price=Decimal('8200.00'),
        )
        QuoteItem.objects.create(
            organization=organization,
            quote=q4,
            item_type=QuoteItem.ItemType.SERVICE,
            code='SRV-LIN-01',
            description='Cuadrilla técnica liniera especializada con camión canasta aislada',
            unit='HRS',
            quantity=Decimal('40.00'),
            unit_price=Decimal('2100.00'),
        )

        # =========================================================================
        # 3. OPERACIONES, CUADRILLAS Y ÓRDENES DE TRABAJO (apps/operations)
        # =========================================================================
        crew1 = Crew.objects.create(
            organization=organization,
            code='BRIG-01',
            name='Cuadrilla Alfa · Alta Tensión & Subestaciones',
            specialty='Media y Alta Tensión, Subestaciones 138/230kV',
            leader=field_tech,
            vehicle_plate='M 312-890',
            vehicle_description='Toyota Hilux 4x4 Doble Cabina 2024 con Winche',
            is_active=True,
        )
        crew2 = Crew.objects.create(
            organization=organization,
            code='BRIG-02',
            name='Cuadrilla Beta · Mantenimiento Electromecánico Industrial',
            specialty='Mantenimiento de Motores, Transformadores y Tableros',
            leader=resident_eng,
            vehicle_plate='M 298-114',
            vehicle_description='Isuzu D-Max 4x4 2023 con Caja de Herramientas Aislada',
            is_active=True,
        )
        crew3 = Crew.objects.create(
            organization=organization,
            code='BRIG-03',
            name='Cuadrilla Gamma · Servicios de Emergencia & Calidad Eléctrica',
            specialty='Termografía, Calidad de Energía y Análisis de Vibraciones',
            leader=ops_coord,
            vehicle_plate='M 345-671',
            vehicle_description='Ford Ranger 4x4 2023 con Laboratorio Móvil',
            is_active=True,
        )

        # Órdenes de Trabajo en distintas fases
        # OT-1: En Ejecución (desde COT-2026-0001 Vivian Pellas)
        wo1 = WorkOrder.objects.create(
            organization=organization,
            number='OT-2026-0001',
            account=accounts['CLI-004'],
            source_quote=q1,
            title='Instalación y Pruebas en Vacío Sistema UPS Quirófano Central',
            technical_scope='Montaje mecánico de gabinete UPS, conexión de barraje DC y banco de 32 baterías AGM.',
            site_address='Hospital Vivian Pellas, Módulo Quirúrgico Torre B, Sótano Eléctrico',
            priority=WorkOrder.Priority.CRITICAL,
            phase=WorkOrder.Phase.IN_PROGRESS,
            is_high_risk=True,
            sst_cleared=True,
            assigned_engineer=resident_eng,
            assigned_crew=crew2,
            scheduled_start=timezone.now().date() - datetime.timedelta(days=2),
            scheduled_end=timezone.now().date() + datetime.timedelta(days=3),
            created_by=ops_coord,
        )
        WorkOrderItem.objects.create(
            organization=organization,
            work_order=wo1,
            item_type=WorkOrderItem.ItemType.SERVICE,
            code='SRV-ING-01',
            description='Montaje mecánico y anclaje antisísmico de módulo UPS',
            unit='GLB',
            planned_quantity=Decimal('1.00'),
            executed_quantity=Decimal('1.00'),
            is_completed=True,
        )
        WorkOrderItem.objects.create(
            organization=organization,
            work_order=wo1,
            item_type=WorkOrderItem.ItemType.MATERIAL,
            code='MAT-BAT-01',
            description='Instalación de banco de baterías selladas 12V 100Ah',
            unit='UND',
            planned_quantity=Decimal('32.00'),
            executed_quantity=Decimal('32.00'),
            is_completed=True,
        )
        WorkOrderItem.objects.create(
            organization=organization,
            work_order=wo1,
            item_type=WorkOrderItem.ItemType.SERVICE,
            code='SRV-PRU-02',
            description='Pruebas de transferencia y descarga con banco resistivo',
            unit='GLB',
            planned_quantity=Decimal('1.00'),
            executed_quantity=Decimal('0.50'),
            is_completed=False,
        )
        WorkOrderTimelineLog.objects.create(
            organization=organization,
            work_order=wo1,
            event_type='CREATED',
            summary='Orden de trabajo generada desde cotización aprobada COT-2026-0001',
            details='Asignación inicial a Cuadrilla Beta liderada por Ing. Carlos Chamorro.',
            actor=ops_coord,
        )
        WorkOrderTimelineLog.objects.create(
            organization=organization,
            work_order=wo1,
            event_type='SST_CLEARANCE',
            summary='Aval técnico emitido por Oficial SST Ley 618 MITRAB',
            details='Checklist CHK-2026-0001 aprobado sin observaciones.',
            actor=sst_officer,
        )
        WorkOrderEvidence.objects.create(
            organization=organization,
            work_order=wo1,
            evidence_type=WorkOrderEvidence.EvidenceType.PHOTO_PROGRESS,
            title='Conexión de terminales de potencia en gabinete de baterías',
            notes='Torque verificado según especificación de fabricante a 12 Nm.',
            file_reference='evidencias/ot_001_baterias.jpg',
            uploaded_by=resident_eng,
        )

        # Firma digital de conformidad del cliente (Hospital Vivian Pellas)
        sample_signature_base64 = (
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAASwAAABkCAYAAACp0JBwAAAABmJLR0QA/wD/AP+gvaeTAAAACXBIWXMA"
            "AAsTAAALEwEAmpwYAAAAB3RJTUUH6AoODQwbk9rD+AAAAB1pVFh0Q29tbWVudAAAAAAAQ3JlYXRlZCB3aXRoIEdJTVBkLmUH"
            "AAAZhklEQVR42u2de3xU1bX/P3vO3Jm5v5NMSAgkIQkQIKBEqFWotbW2VmuxtvVSq7X2/npr7bWtt16vt729v/b+bK19bX2t"
            "9dr62qp9ba3Wam191FoVhVpeCiqXkIQEknnP3Jk58/vjHJJMQjITCEmQd3+fz0eSOWfP2fucfX577bX23mshhBBCCCGEEEII"
            "IYQQQgghhBBCCCGEEEIIIYQQQgghhBBCCCGEEEIIIYQQQgghhBBCCCGEEEIIIYQQQgghhBBCCCGEEEIIIYQQQgghhBBCCCGE"
            "EEIIIYQQQgghhBBCCCGEEEIIIYQQQgghhBBCCCGEEEIIIYQQQgghhBBCCCGEEEIIIYQQQgghhBBCCCGEEEIIIYQQQgghhBBC"
            "CCGEEEIIIYQQQgghhBBCCCGEEEIIIYQQQgghhBBCCCGEEEIIIYQQQgghhBBCCCGEEEIIIYQQQgghhBBCCCGEEEIIIYQQQggh"
            "xKHHgA0QQgghhBDSl+j2GgghhBBCCDlsYLAIIYQQQghx2MBgEUIIIYQQ4rCBwSKEEEIIIcRhA4NFCCGEEEKIwwYGi1BfEY1G"
            "8/YaCCGEEEII2aUj9hoIIV6i1WrP22sghBBCCCHksIDBIuSgwOFw1O61EEIIIYQQcsjAYBFyUGAymZq210IIIYQQQshhAYNF"
            "yEEBh8Ph2GshhBBCCCDksIDBIuSgwOFw1O61EEIIIYQQcsjAYBFyUGAymZq210IIIYQQQshhAYNFyEEBh8Ph2GshhBBCCCDk"
            "sIDBIuSgwOFw1O61EEIIIYQQcsjAYBFyUGAymZq210IIIYQQQshhAYNFyEEBh8Ph2GshhBBCCCDksIDBIuSg4L8BAyXq0l3q"
            "LgAAAABJRU5ErkJggg=="
        )
        wo1.record_client_signature(
            signer_name='Ing. Fernando Morales Silva',
            signer_title='Director de Infraestructura Hospitalaria',
            signer_id='001-240879-0002A',
            signature_base64=sample_signature_base64,
            actor=resident_eng,
        )

        # Evidencia fotográfica de entrega comprimida en cliente (4,120 KB -> 138 KB)
        sample_jpeg_base64 = (
            "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////"
            "/////////////////////////////////////////////////wgALCAAEAAQA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/a"
            "AAgBAQABPxA="
        )
        ev_after = WorkOrderEvidence.objects.create(
            organization=organization,
            work_order=wo1,
            evidence_type=WorkOrderEvidence.EvidenceType.PHOTO_AFTER,
            title='Inspección final de banco de baterías y módulo UPS energizado',
            notes='Prueba de conmutación 0ms completada satisfactoriamente ante comitiva hospitalaria.',
            file_reference='evidencias/ot_001_entrega_final.jpg',
            compressed_image_data=sample_jpeg_base64,
            original_size_kb=4120,
            compressed_size_kb=138,
            uploaded_by=resident_eng,
        )
        WorkOrderTimelineLog.objects.create(
            organization=organization,
            work_order=wo1,
            event_type='EVIDENCE_ADDED',
            summary=f"Nueva evidencia técnica: {ev_after.get_evidence_type_display()} · {ev_after.title}",
            details=f"{ev_after.notes}\nEvidencia fotográfica optimizada: 4,120 KB -> 138 KB",
            actor=resident_eng,
        )

        # OT-2: Borrador (desde COT-2026-0002 Holcim)
        wo2 = WorkOrder.objects.create(
            organization=organization,
            number='OT-2026-0002',
            account=accounts['CLI-005'],
            source_quote=q2,
            title='Obras Civiles y Anclaje de Banco de Capacitores 13.8kV',
            technical_scope='Fundación de losa de concreto, anclaje de celdas capacitivas e interconexión.',
            site_address='Holcim Planta Nagarote, Celda de Molienda #03',
            priority=WorkOrder.Priority.HIGH,
            phase=WorkOrder.Phase.DRAFT,
            is_high_risk=True,
            sst_cleared=False,
            assigned_engineer=resident_eng,
            assigned_crew=crew1,
            scheduled_start=timezone.now().date() + datetime.timedelta(days=5),
            scheduled_end=timezone.now().date() + datetime.timedelta(days=12),
            created_by=ops_coord,
        )

        # OT-3: Bloqueada por SST (ENATREL)
        wo3 = WorkOrder.objects.create(
            organization=organization,
            number='OT-2026-0003',
            account=accounts['CLI-001'],
            title='Maniobra de Mantenimiento en Seccionadores de Línea 138kV',
            technical_scope='Desconexión y apertura de cuchillas, limpieza criogénica y ajuste de contactos.',
            site_address='Subestación Tipitapa 230/138kV, Bahía de Línea Masaya',
            priority=WorkOrder.Priority.CRITICAL,
            phase=WorkOrder.Phase.SCHEDULED,
            is_high_risk=True,
            sst_cleared=False,
            assigned_engineer=resident_eng,
            assigned_crew=crew1,
            scheduled_start=timezone.now().date() + datetime.timedelta(days=2),
            scheduled_end=timezone.now().date() + datetime.timedelta(days=4),
            created_by=ops_coord,
        )
        WorkOrderTimelineLog.objects.create(
            organization=organization,
            work_order=wo3,
            event_type='SST_LOCK',
            summary='Frente de obra retenido: falta validación de permiso LOTO en seccionador',
            details='Oficial SST detectó que la línea paralela no cuenta con puesta a tierra temporal.',
            actor=sst_officer,
        )

        # OT-4: Finalizada (CEMEX)
        wo4 = WorkOrder.objects.create(
            organization=organization,
            number='OT-2026-0004',
            account=accounts['CLI-002'],
            title='Inspección Termográfica y Corrección de Puntos Calientes en CCM Molino #2',
            technical_scope='Termografía con cámara FLIR T540, reapriete con torquímetro aislado y entrega de informe.',
            site_address='CEMEX Planta San Rafael del Sur, Sala Eléctrica #4',
            priority=WorkOrder.Priority.NORMAL,
            phase=WorkOrder.Phase.COMPLETED,
            is_high_risk=False,
            sst_cleared=True,
            assigned_engineer=ops_coord,
            assigned_crew=crew3,
            scheduled_start=timezone.now().date() - datetime.timedelta(days=8),
            scheduled_end=timezone.now().date() - datetime.timedelta(days=6),
            completed_at=timezone.now() - datetime.timedelta(days=6),
            created_by=ops_coord,
        )
        WorkOrderTimelineLog.objects.create(
            organization=organization,
            work_order=wo4,
            event_type='COMPLETED',
            summary='Obra finalizada y entregada a satisfacción de la jefatura de planta CEMEX',
            details='Informe técnico firmado por Ing. Gabriela Somarriba.',
            actor=ops_coord,
        )

        # =========================================================================
        # 4. SEGURIDAD LABORAL LEY 618 MITRAB (apps/sst)
        # =========================================================================
        chk1 = SafetyChecklist.objects.create(
            organization=organization,
            number='CHK-2026-0001',
            work_order=wo1,
            crew=crew2,
            submitted_by=resident_eng,
            work_type=SafetyChecklist.WorkType.ELECTRICAL,
            epp_verified=True,
            loto_verified=True,
            tools_inspected=True,
            area_signaled=True,
            risk_briefing_done=True,
            hazard_notes='Riesgo de arco eléctrico mitigado con traje de protección Arc Flash 40 cal/cm2.',
            status=SafetyChecklist.Status.APPROVED,
            reviewed_by=sst_officer,
            reviewed_at=timezone.now() - datetime.timedelta(days=2),
            review_comments='Checklist verificado conforme a Ley 618. Labor autorizada.',
        )

        chk2 = SafetyChecklist.objects.create(
            organization=organization,
            number='CHK-2026-0002',
            work_order=wo3,
            crew=crew1,
            submitted_by=field_tech,
            work_type=SafetyChecklist.WorkType.HEIGHTS,
            epp_verified=True,
            loto_verified=False,
            tools_inspected=True,
            area_signaled=True,
            risk_briefing_done=True,
            hazard_notes='Pendiente confirmación de consignación formal de línea por despacho de carga.',
            status=SafetyChecklist.Status.PENDING,
            reviewed_by=None,
        )

        # Dotaciones de EPP
        EPPDelivery.objects.create(
            organization=organization,
            code='EPP-2026-0001',
            collaborator=field_tech,
            category=EPPDelivery.Category.HEAD,
            item_name='Casco Dieléctrico Tipo II Clase E 20kV MSA V-Gard',
            serial_or_batch='LOT-MSA-2026-01',
            quantity=1,
            delivery_date=timezone.now().date() - datetime.timedelta(days=30),
            expiration_date=timezone.now().date() + datetime.timedelta(days=335),
            status=EPPDelivery.Status.ACTIVE,
            acknowledged_by_worker=True,
            delivered_by=sst_officer,
        )
        EPPDelivery.objects.create(
            organization=organization,
            code='EPP-2026-0002',
            collaborator=field_tech,
            category=EPPDelivery.Category.HANDS,
            item_name='Guantes Dieléctricos de Alta Tensión Salisbury Clase 2 17kV',
            serial_or_batch='LOT-SAL-8891',
            quantity=1,
            delivery_date=timezone.now().date() - datetime.timedelta(days=30),
            expiration_date=timezone.now().date() + datetime.timedelta(days=150),
            status=EPPDelivery.Status.ACTIVE,
            acknowledged_by_worker=True,
            delivered_by=sst_officer,
        )
        EPPDelivery.objects.create(
            organization=organization,
            code='EPP-2026-0003',
            collaborator=resident_eng,
            category=EPPDelivery.Category.FALL,
            item_name='Arnés de Cuerpo Completo Miller Titan con Doble Eslinga Dieléctrica',
            serial_or_batch='LOT-MIL-9421',
            quantity=1,
            delivery_date=timezone.now().date() - datetime.timedelta(days=45),
            expiration_date=timezone.now().date() + datetime.timedelta(days=320),
            status=EPPDelivery.Status.ACTIVE,
            acknowledged_by_worker=True,
            delivered_by=sst_officer,
        )

        # Solicitudes de Permiso / Reposo Laboral
        LeaveRequest.objects.create(
            organization=organization,
            number='PER-2026-0001',
            requester=field_tech,
            requester_role_level=RoleLevel.TECHNICIAN,
            leave_type=LeaveRequest.LeaveType.MEDICAL,
            start_date=timezone.now().date() - datetime.timedelta(days=12),
            end_date=timezone.now().date() - datetime.timedelta(days=10),
            days_requested=3,
            reason='Constancia de reposo médico por lumbago emitida por Policlínico Central INSS.',
            status=LeaveRequest.Status.APPROVED,
            reviewed_by=ops_coord,
            reviewed_at=timezone.now() - datetime.timedelta(days=12),
            resolution_notes='Subsidio INSS justificado. Reingreso con aptitud médica confirmada.',
        )
        LeaveRequest.objects.create(
            organization=organization,
            number='PER-2026-0002',
            requester=resident_eng,
            requester_role_level=RoleLevel.ENGINEER,
            leave_type=LeaveRequest.LeaveType.TRAINING,
            start_date=timezone.now().date() + datetime.timedelta(days=15),
            end_date=timezone.now().date() + datetime.timedelta(days=16),
            days_requested=2,
            reason='Certificación Oficial NFPA 70E sobre Seguridad Eléctrica en Lugares de Trabajo.',
            status=LeaveRequest.Status.APPROVED,
            reviewed_by=owner_user,
            reviewed_at=timezone.now() - datetime.timedelta(days=1),
            resolution_notes='Capacitación estratégica autorizada para el frente de subestaciones.',
        )

        # =========================================================================
        # 5. ALMACÉN, BODEGAS Y KARDEX (apps/inventory)
        # =========================================================================
        bod1 = Warehouse.objects.create(
            organization=organization,
            code='BOD-01',
            name='Bodega Central Managua (Km 7.5 Carretera Norte)',
            warehouse_type=Warehouse.WarehouseType.CENTRAL,
            address='Parque Industrial Las Mercedes, Módulo 4',
            is_active=True,
        )
        bod2 = Warehouse.objects.create(
            organization=organization,
            code='BOD-02',
            name='Unidad Móvil Hilux #04 (Cuadrilla Alfa)',
            warehouse_type=Warehouse.WarehouseType.MOBILE_CREW,
            linked_crew=crew1,
            address='Móvil asignado a brigada de alta tensión',
            is_active=True,
        )
        bod3 = Warehouse.objects.create(
            organization=organization,
            code='BOD-03',
            name='Frente de Obra Subestación Tipitapa',
            warehouse_type=Warehouse.WarehouseType.PROJECT,
            address='Subestación Tipitapa ENATREL',
            is_active=True,
        )

        items_data = [
            {
                'sku': 'MAT-CAB-01',
                'name': 'Cable de Cobre THHN Calibre 4 AWG 600V Negro',
                'category': InventoryItem.Category.ELECTRICAL,
                'unit': 'MTS',
                'min_stock_level': Decimal('100.00'),
                'unit_cost': Decimal('85.00'),
            },
            {
                'sku': 'MAT-CAB-02',
                'name': 'Cable de Potencia XLP 15kV Calibre 2/0 AWG Apantallado',
                'category': InventoryItem.Category.ELECTRICAL,
                'unit': 'MTS',
                'min_stock_level': Decimal('50.00'),
                'unit_cost': Decimal('420.00'),
            },
            {
                'sku': 'MAT-AIS-01',
                'name': 'Aislador Polimérico tipo Suspensión 138kV ANSI 52-3',
                'category': InventoryItem.Category.ELECTRICAL,
                'unit': 'UND',
                'min_stock_level': Decimal('12.00'),
                'unit_cost': Decimal('3850.00'),
            },
            {
                'sku': 'MAT-TER-01',
                'name': 'Terminal Termocontráctil para Media Tensión 15kV Exterior 3M',
                'category': InventoryItem.Category.ELECTRICAL,
                'unit': 'JGO',
                'min_stock_level': Decimal('8.00'),
                'unit_cost': Decimal('2450.00'),
            },
            {
                'sku': 'HER-MUL-01',
                'name': 'Multímetro Digital Industrial True-RMS Fluke 87V Calibrado',
                'category': InventoryItem.Category.TOOL,
                'unit': 'UND',
                'min_stock_level': Decimal('2.00'),
                'unit_cost': Decimal('18900.00'),
            },
            {
                'sku': 'SEG-CAS-01',
                'name': 'Casco Dieléctrico con Barboquejo Clase E 20kV MSA V-Gard',
                'category': InventoryItem.Category.SAFETY_EPP,
                'unit': 'UND',
                'min_stock_level': Decimal('10.00'),
                'unit_cost': Decimal('750.00'),
            },
            {
                'sku': 'SEG-GUA-01',
                'name': 'Guantes Dieléctricos de Alta Tensión Clase 2 17kV Salisbury',
                'category': InventoryItem.Category.SAFETY_EPP,
                'unit': 'PAR',
                'min_stock_level': Decimal('6.00'),
                'unit_cost': Decimal('4200.00'),
            },
            {
                'sku': 'MAT-CON-01',
                'name': 'Cinta Autofundente de Goma Alta Tensión 3M Scotch 23',
                'category': InventoryItem.Category.CONSUMABLE,
                'unit': 'RLL',
                'min_stock_level': Decimal('20.00'),
                'unit_cost': Decimal('380.00'),
            },
        ]

        items = {}
        for item_spec in items_data:
            item = InventoryItem.objects.create(
                organization=organization,
                is_active=True,
                **item_spec
            )
            items[item.sku] = item

        # Saldos Físicos de Stock en Bodega Central y Móvil
        StockBalance.objects.create(
            organization=organization,
            warehouse=bod1,
            item=items['MAT-CAB-01'],
            quantity=Decimal('450.00'),
        )
        StockBalance.objects.create(
            organization=organization,
            warehouse=bod1,
            item=items['MAT-CAB-02'],
            quantity=Decimal('180.00'),
        )
        StockBalance.objects.create(
            organization=organization,
            warehouse=bod1,
            item=items['MAT-AIS-01'],
            quantity=Decimal('24.00'),
        )
        StockBalance.objects.create(
            organization=organization,
            warehouse=bod1,
            item=items['HER-MUL-01'],
            quantity=Decimal('4.00'),
        )
        StockBalance.objects.create(
            organization=organization,
            warehouse=bod2,
            item=items['MAT-CON-01'],
            quantity=Decimal('15.00'),
        )
        StockBalance.objects.create(
            organization=organization,
            warehouse=bod2,
            item=items['SEG-GUA-01'],
            quantity=Decimal('4.00'),
        )
        StockBalance.objects.create(
            organization=organization,
            warehouse=bod3,
            item=items['MAT-TER-01'],
            quantity=Decimal('6.00'),
        )
        StockBalance.objects.create(
            organization=organization,
            warehouse=bod1,
            item=items['SEG-CAS-01'],
            quantity=Decimal('10.00'),
        )

        # Movimientos de Kardex
        StockMovement.objects.create(
            organization=organization,
            number='KDX-2026-0001',
            movement_type=StockMovement.MovementType.IN_PURCHASE,
            warehouse=bod1,
            item=items['MAT-CAB-01'],
            quantity=Decimal('500.00'),
            unit_cost_snapshot=Decimal('85.00'),
            balance_after=Decimal('500.00'),
            reference_note='Recepción de lote según Factura Comercial Ferretería Roberto Morales #4891',
            performed_by=ops_coord,
        )
        StockMovement.objects.create(
            organization=organization,
            number='KDX-2026-0002',
            movement_type=StockMovement.MovementType.TRANSFER_OUT,
            warehouse=bod1,
            target_warehouse=bod2,
            item=items['MAT-CON-01'],
            quantity=Decimal('15.00'),
            unit_cost_snapshot=Decimal('380.00'),
            balance_after=Decimal('35.00'),
            reference_note='Dotación inicial de consumibles a Unidad Móvil Hilux #04',
            performed_by=ops_coord,
        )
        StockMovement.objects.create(
            organization=organization,
            number='KDX-2026-0003',
            movement_type=StockMovement.MovementType.OUT_WORKORDER,
            warehouse=bod1,
            item=items['MAT-CAB-01'],
            quantity=Decimal('50.00'),
            unit_cost_snapshot=Decimal('85.00'),
            balance_after=Decimal('450.00'),
            work_order=wo1,
            reference_note='Despacho de cable de fuerza para interconexión de tablero UPS',
            performed_by=resident_eng,
        )
        StockMovement.objects.create(
            organization=organization,
            number='KDX-2026-0004',
            movement_type=StockMovement.MovementType.OUT_WORKORDER,
            warehouse=bod3,
            item=items['MAT-TER-01'],
            quantity=Decimal('2.00'),
            unit_cost_snapshot=Decimal('2450.00'),
            balance_after=Decimal('6.00'),
            work_order=wo3,
            reference_note='Imputación de terminales termocontráctiles al transformador de potencia T-01',
            performed_by=resident_eng,
        )

        # =========================================================================
        # 6. TESORERÍA, CAJAS CHICAS Y BANCOS (apps/finance)
        # =========================================================================
        bank_nio = BankAccount.objects.create(
            organization=organization,
            bank_name=BankAccount.BankName.BAC,
            account_number='001-348921-04',
            currency='NIO',
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('485250.00'),
            description='Cuenta Principal de Operaciones y Nómina - BAC Credomatic',
        )
        bank_usd = BankAccount.objects.create(
            organization=organization,
            bank_name=BankAccount.BankName.BANPRO,
            account_number='100-204589-12',
            currency='USD',
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('34800.00'),
            description='Cuenta Corporativa en Dólares para Suministros Importados - Banpro',
        )

        petty_fund = PettyCashFund.objects.create(
            organization=organization,
            code='CAJ-01',
            name='Caja Chica Frentes de Obra Managua & Tipitapa',
            custodian=resident_eng,
            linked_crew=crew1,
            currency='NIO',
            allocated_amount=Decimal('25000.00'),
            current_balance=Decimal('16850.00'),
        )

        # Asiento inicial de asignación de fondo fijo
        FinancialTransaction.objects.create(
            organization=organization,
            transaction_type=FinancialTransaction.TransactionType.PETTY_CASH_ALLOCATION,
            bank_account=bank_nio,
            petty_cash_fund=petty_fund,
            amount=Decimal('25000.00'),
            balance_after=petty_fund.current_balance,
            concept='Apertura y dotación de fondo fijo para Ing. Carlos Chamorro',
            reference_code='CHQ-BAC-4812',
            performed_by=owner_user,
        )
        FinancialTransaction.objects.create(
            organization=organization,
            transaction_type=FinancialTransaction.TransactionType.CLIENT_COLLECTION,
            bank_account=bank_nio,
            amount=Decimal('185000.00'),
            balance_after=bank_nio.current_balance,
            concept='Cobro anticipo 30% Licitación ENATREL según Factura #0129',
            reference_code='TRANSF-ACH-98214',
            performed_by=commercial_mgr,
        )
        FinancialTransaction.objects.create(
            organization=organization,
            transaction_type=FinancialTransaction.TransactionType.PETTY_CASH_REPLENISH,
            bank_account=bank_nio,
            petty_cash_fund=petty_fund,
            amount=Decimal('8150.00'),
            balance_after=petty_fund.allocated_amount,
            concept='Desembolso por reposición y liquidación de arqueo REP-2026-0001',
            reference_code='DEB-BAC-7741',
            performed_by=owner_user,
        )

        # Comprobantes de Gastos de Campo
        exp1 = PettyCashExpense.objects.create(
            organization=organization,
            voucher_number='REC-2026-0001',
            fund=petty_fund,
            work_order=wo1,
            expense_category=PettyCashExpense.Category.FUEL,
            vendor_name='Estación Puma Energy Tipitapa',
            vendor_tax_id='J0310000041289',
            invoice_number='FAC-89124',
            amount=Decimal('3450.00'),
            description='Abastecimiento de Diesel 85 galones para vehículo Hilux M 312-890 frente Tipitapa',
            spent_at=timezone.now().date() - datetime.timedelta(days=2),
            created_by=resident_eng,
            is_replenished=False,
        )
        exp2 = PettyCashExpense.objects.create(
            organization=organization,
            voucher_number='REC-2026-0002',
            fund=petty_fund,
            work_order=wo1,
            expense_category=PettyCashExpense.Category.EMERGENCY_HARDWARE,
            vendor_name='Ferretería Richardson S.A.',
            vendor_tax_id='J0310000011456',
            invoice_number='FAC-12498',
            amount=Decimal('2800.00'),
            description='Compra de pernos de anclaje de acero inoxidable y brocas SDS para concreto',
            spent_at=timezone.now().date() - datetime.timedelta(days=1),
            created_by=resident_eng,
            is_replenished=False,
        )
        exp3 = PettyCashExpense.objects.create(
            organization=organization,
            voucher_number='REC-2026-0003',
            fund=petty_fund,
            work_order=wo1,
            expense_category=PettyCashExpense.Category.PER_DIEM,
            vendor_name='Restaurante El Cortijo Tipitapa',
            vendor_tax_id='001-150682-0003N',
            invoice_number='FAC-4412',
            amount=Decimal('1900.00'),
            description='Alimentación y viáticos para cuadrilla de 6 técnicos en maniobra nocturna',
            spent_at=timezone.now().date() - datetime.timedelta(days=1),
            created_by=resident_eng,
            is_replenished=False,
        )

        # Solicitud de Reposición (Arqueo de Caja)
        rep = PettyCashReplenishment.objects.create(
            organization=organization,
            code='REP-2026-0001',
            fund=petty_fund,
            status=PettyCashReplenishment.Status.APPROVED,
            total_expenses_amount=Decimal('8150.00'),
            requested_amount=Decimal('8150.00'),
            notes='Arqueo quincenal verificado. Comprobantes fiscales válidos adjuntos.',
            submitted_by=resident_eng,
            approved_by=ops_coord,
            approved_at=timezone.now() - datetime.timedelta(hours=4),
        )

        # =========================================================================
        # 8. FACTURACIÓN FISCAL DGI, RETENCIONES LEY 822 Y CAJA ROC (apps/billing)
        # =========================================================================
        cli_enatrel = Account.objects.get(organization=organization, code='CLI-001')
        inv1 = Invoice.objects.create(
            organization=organization,
            number='FAC-2026-0001',
            account=cli_enatrel,
            source_work_order=wo1,
            source_quote=wo1.source_quote if wo1 else None,
            status=Invoice.Status.PARTIAL,
            payment_condition=Invoice.PaymentCondition.CREDIT_30,
            issue_date=timezone.now().date() - datetime.timedelta(days=10),
            due_date=timezone.now().date() + datetime.timedelta(days=20),
            currency_code='NIO',
            currency_symbol='C$',
            exchange_rate=Decimal('36.6243'),
            apply_tax=True,
            tax_name='IVA',
            tax_rate=Decimal('15.00'),
            ir_retention_rate=Decimal('2.00'),
            imi_retention_rate=Decimal('1.00'),
            retention_certificate_number='RET-ENATREL-2026-0089',
            created_by=commercial_mgr,
            notes='Facturación correspondiente al Hito 1 de Mantenimiento Preventivo Subestación Tipitapa.',
        )

        InvoiceItem.objects.create(
            organization=organization,
            invoice=inv1,
            item_type='SERVICE',
            code='SRV-ING-001',
            description='Servicio de pruebas dieléctricas y aislamiento a transformador de potencia 50MVA',
            unit='SRV',
            quantity=Decimal('1.00'),
            unit_price=Decimal('65000.00'),
            discount_percent=Decimal('0.00'),
        )
        InvoiceItem.objects.create(
            organization=organization,
            invoice=inv1,
            item_type='SERVICE',
            code='SRV-ING-002',
            description='Calibración y termografía infrarroja en banco de interruptores de potencia SF6',
            unit='SRV',
            quantity=Decimal('1.00'),
            unit_price=Decimal('30000.00'),
            discount_percent=Decimal('0.00'),
        )
        inv1.recalculate_totals()
        inv1.save()

        # Recibo Oficial de Caja (ROC) bimonetario inmutable
        PaymentReceipt.objects.create(
            organization=organization,
            number='ROC-2026-0001',
            invoice=inv1,
            payment_date=timezone.now().date() - datetime.timedelta(days=5),
            payment_method=PaymentReceipt.PaymentMethod.TRANSFER_BAC,
            reference_number='TRF-BAC-992014',
            currency_paid='NIO',
            amount_in_payment_currency=Decimal('50000.00'),
            exchange_rate_applied=inv1.exchange_rate,
            amount_applied_to_invoice=Decimal('50000.00'),
            received_by=commercial_mgr,
            notes='Abono inicial 50,000.00 C$ mediante transferencia directa BAC de ENATREL.',
        )

        # Factura 2: Venta de Insumos / Medicamentos Exentos de IVA (Ley 822)
        cli_claro = Account.objects.get(organization=organization, code='CLI-002')
        inv2 = Invoice.objects.create(
            organization=organization,
            number='FAC-2026-0002',
            account=cli_claro,
            status=Invoice.Status.PAID,
            payment_condition=Invoice.PaymentCondition.CASH,
            issue_date=timezone.now().date() - datetime.timedelta(days=2),
            due_date=timezone.now().date() - datetime.timedelta(days=2),
            currency_code='NIO',
            currency_symbol='C$',
            exchange_rate=Decimal('36.6243'),
            apply_tax=False,
            tax_name='EXENTO LEY 822',
            tax_rate=Decimal('0.00'),
            ir_retention_rate=Decimal('0.00'),
            imi_retention_rate=Decimal('0.00'),
            created_by=commercial_mgr,
            notes='Venta de insumos y medicamentos de botiquín industrial exentos de IVA según Ley 822.',
        )
        InvoiceItem.objects.create(
            organization=organization,
            invoice=inv2,
            item_type='PRODUCT',
            code='MED-AMX-01',
            description='Amoxicilina + Ácido Clavulánico 500/125mg Caja x 30 tabs (Exento Ley 822)',
            unit='CJ',
            quantity=Decimal('20.00'),
            unit_price=Decimal('450.00'),
            discount_percent=Decimal('0.00'),
        )
        inv2.recalculate_totals()
        inv2.save()

        # Recibo Oficial de Caja (ROC) al contado en efectivo para la factura 2
        PaymentReceipt.objects.create(
            organization=organization,
            number='ROC-2026-0002',
            invoice=inv2,
            payment_date=timezone.now().date() - datetime.timedelta(days=2),
            payment_method=PaymentReceipt.PaymentMethod.CASH,
            reference_number='REC-CAJA-002',
            currency_paid='NIO',
            amount_in_payment_currency=inv2.net_receivable,
            exchange_rate_applied=inv2.exchange_rate,
            amount_applied_to_invoice=inv2.net_receivable,
            received_by=commercial_mgr,
            notes='Pago de contado en efectivo en mostrador.',
        )

        # Factura 3: En Mora (Overdue CxC) para Radar de Alertas
        inv3 = Invoice.objects.create(
            organization=organization,
            number='FAC-2026-0003',
            account=cli_claro,
            status=Invoice.Status.ISSUED,
            payment_condition=Invoice.PaymentCondition.CREDIT_30,
            issue_date=timezone.now().date() - datetime.timedelta(days=40),
            due_date=timezone.now().date() - datetime.timedelta(days=10),
            currency_code='NIO',
            currency_symbol='C$',
            exchange_rate=Decimal('36.6243'),
            apply_tax=True,
            tax_name='IVA',
            tax_rate=Decimal('15.00'),
            ir_retention_rate=Decimal('2.00'),
            imi_retention_rate=Decimal('1.00'),
            created_by=commercial_mgr,
            notes='Factura en mora por servicios técnicos de mantenimiento.',
        )
        InvoiceItem.objects.create(
            organization=organization,
            invoice=inv3,
            item_type='SERVICE',
            code='SRV-ING-003',
            description='Servicio especializado de mantenimiento preventivo correctivo',
            unit='SRV',
            quantity=Decimal('1.00'),
            unit_price=Decimal('45000.00'),
            discount_percent=Decimal('0.00'),
        )
        inv3.recalculate_totals()
        inv3.save()

        # -------------------------------------------------------------------------
        # ARQUEO DE CAJA Z BIMONETARIO (apps/billing)
        # -------------------------------------------------------------------------
        # 1. Sesión de Caja Z Cerrada y Sellada con Hash SHA-256 (ARQ-2026-0001)
        arq1 = CashRegisterSession.objects.create(
            organization=organization,
            number='ARQ-2026-0001',
            cashier=commercial_mgr,
            status=CashRegisterSession.Status.OPEN,
            opened_at=timezone.now() - datetime.timedelta(days=2, hours=8),
            opening_cash_nio=Decimal('5000.00'),
            opening_cash_usd=Decimal('100.00'),
            exchange_rate=Decimal('36.6243'),
            notes='Turno inicial de apertura comercial con fondo en Córdobas y Dólares.',
        )
        arq1.close_and_seal_session(
            declared_nio=Decimal('14000.00'),  # 5,000 inicial + 9,000 de inv2
            declared_usd=Decimal('100.00'),
            notes='Corte Z sellado sin discrepancias en caja física.',
        )

        # 2. Sesión de Caja Activa en Línea para pruebas interactivas de mostrador
        CashRegisterSession.objects.create(
            organization=organization,
            number='ARQ-2026-0002',
            cashier=commercial_mgr,
            status=CashRegisterSession.Status.OPEN,
            opened_at=timezone.now() - datetime.timedelta(hours=3),
            opening_cash_nio=Decimal('5000.00'),
            opening_cash_usd=Decimal('100.00'),
            exchange_rate=Decimal('36.6243'),
            notes='Turno activo de operaciones en mostrador y ventas rápidas POS.',
        )

        # -------------------------------------------------------------------------
        # TURNOS DE CAJA POS RETAIL (apps/billing - PosRegisterShift)
        # -------------------------------------------------------------------------
        # 1. Turno Histórico Cerrado con Corte Z Cuadrado y Sello SHA-256
        shift_open_t = timezone.now() - datetime.timedelta(days=1, hours=8)
        shift_close_t = timezone.now() - datetime.timedelta(days=1)
        raw_hash_main = f"{organization.id}:TURNO-2026-0001:2000.00:3450.00:5450.00:5450.00:0.00:CLOSED_Z"
        sha_seal_main = hashlib.sha256(raw_hash_main.encode('utf-8')).hexdigest()

        shift_z_main = PosRegisterShift.objects.create(
            organization=organization,
            shift_number='TURNO-2026-0001',
            cashier=commercial_mgr,
            register_name='CAJA-PRINCIPAL-01',
            status=PosRegisterShift.Status.OPEN,
            opened_at=shift_open_t,
            opening_cash_nio=Decimal('2000.00'),
            system_cash_sales_nio=Decimal('3450.00'),
            system_card_transfer_sales_nio=Decimal('1200.00'),
            expected_cash_in_drawer_nio=Decimal('5450.00'),
            declared_cash_nio=Decimal('5450.00'),
            cash_difference_nio=Decimal('0.00'),
            transactions_count=8,
            closing_notes='Cierre de turno jornada anterior. Gaveta cuadrada al 100% sin discrepancias.',
        )
        shift_z_main.status = PosRegisterShift.Status.CLOSED_Z
        shift_z_main.closed_at = shift_close_t
        shift_z_main.audit_hash = sha_seal_main
        shift_z_main.save()

        # 2. Turno Actualmente Abierto para Terminal POS
        PosRegisterShift.objects.create(
            organization=organization,
            shift_number='TURNO-2026-0002',
            cashier=commercial_mgr,
            register_name='CAJA-PRINCIPAL-01',
            status=PosRegisterShift.Status.OPEN,
            opened_at=timezone.now() - datetime.timedelta(hours=2),
            opening_cash_nio=Decimal('2000.00'),
            system_cash_sales_nio=Decimal('0.00'),
            system_card_transfer_sales_nio=Decimal('0.00'),
            expected_cash_in_drawer_nio=Decimal('2000.00'),
            declared_cash_nio=Decimal('0.00'),
            cash_difference_nio=Decimal('0.00'),
            transactions_count=0,
            closing_notes='',
        )

        # =========================================================================
        # 8. COMPRAS, PROVEEDORES Y CUENTAS POR PAGAR (apps/purchasing)
        # =========================================================================
        supplier_sinsa = Supplier.objects.create(
            organization=organization,
            code='PRV-0001',
            commercial_name='SINSA Industrial S.A.',
            legal_name='SERVICIOS INDUSTRIALES S.A.',
            tax_id='J0310000556677',
            contact_name='Ing. Roberto Somarriba',
            email='ventas.industriales@sinsa.com.ni',
            phone='+505 2270-1000',
            address='Carretera a Masaya Km 4.5',
            city='Managua',
            credit_days=30,
            default_ir_rate=Decimal('1.00'),
            default_imi_rate=Decimal('1.00'),
            is_active=True,
        )

        supplier_comtech = Supplier.objects.create(
            organization=organization,
            code='PRV-0002',
            commercial_name='Comtech de Nicaragua S.A.',
            legal_name='COMPUTADORAS Y TECNOLOGIA S.A.',
            tax_id='J0310000112233',
            contact_name='Lic. Maritza Morales',
            email='corporativo@comtech.com.ni',
            phone='+505 2251-5000',
            address='Rotonda El Periodista 200m al Sur',
            city='Managua',
            credit_days=15,
            default_ir_rate=Decimal('1.00'),
            default_imi_rate=Decimal('1.00'),
            is_active=True,
        )

        # Orden de Compra 1: Gravada con IVA 15% y Retenciones RET-2026-0001
        po1 = PurchaseOrder.objects.create(
            organization=organization,
            number='OC-2026-0001',
            supplier=supplier_sinsa,
            target_warehouse=bod1,
            supplier_invoice_number='FAC-SIN-99120',
            status=PurchaseOrder.Status.RECEIVED,
            issue_date=timezone.now().date() - datetime.timedelta(days=7),
            due_date=timezone.now().date() + datetime.timedelta(days=23),
            received_at=timezone.now() - datetime.timedelta(days=6),
            currency_code='NIO',
            currency_symbol='C$',
            exchange_rate=Decimal('36.6243'),
            apply_tax=True,
            tax_rate=Decimal('15.00'),
            ir_withholding_rate=Decimal('1.00'),
            imi_withholding_rate=Decimal('1.00'),
            withholding_certificate_number='RET-2026-0001',
            stock_received=True,
            created_by=commercial_mgr,
            received_by=ops_coord,
        )

        PurchaseOrderItem.objects.create(
            organization=organization,
            purchase_order=po1,
            inventory_item=items['MAT-CAB-02'],
            description='Cable de Potencia XLP 15kV Calibre 2/0 AWG Apantallado',
            unit='MTS',
            quantity=Decimal('5.00'),
            unit_cost=Decimal('20000.00'),
            line_total=Decimal('100000.00'),
        )
        po1.recalculate_totals()
        po1.withholding_certificate_number = 'RET-2026-0001'
        po1.save()

        # Entrada física en Bodega Central y generación de folio inmutable en Kardex
        record_stock_entry(
            organization=organization,
            warehouse=bod1,
            item=items['MAT-CAB-02'],
            quantity=Decimal('5.00'),
            unit_cost=Decimal('20000.00'),
            reference_note=f"Recepción {po1.number} · Factura {po1.supplier_invoice_number}",
            user=ops_coord,
        )

        # Orden de Compra 2: Servicios de Conectividad y Licenciamiento Exento de IVA con RET-2026-0002
        po2 = PurchaseOrder.objects.create(
            organization=organization,
            number='OC-2026-0002',
            supplier=supplier_comtech,
            target_warehouse=bod1,
            supplier_invoice_number='FAC-COM-8832',
            status=PurchaseOrder.Status.RECEIVED,
            issue_date=timezone.now().date() - datetime.timedelta(days=4),
            due_date=timezone.now().date() + datetime.timedelta(days=11),
            received_at=timezone.now() - datetime.timedelta(days=3),
            currency_code='NIO',
            currency_symbol='C$',
            exchange_rate=Decimal('36.6243'),
            apply_tax=False,
            tax_rate=Decimal('0.00'),
            ir_withholding_rate=Decimal('1.00'),
            imi_withholding_rate=Decimal('1.00'),
            withholding_certificate_number='RET-2026-0002',
            stock_received=True,
            created_by=commercial_mgr,
            received_by=ops_coord,
        )
        PurchaseOrderItem.objects.create(
            organization=organization,
            purchase_order=po2,
            inventory_item=items.get('MAT-CON-01'),
            description='Suministro de conectores y accesorios de comunicación (Exento)',
            unit='SRV',
            quantity=Decimal('2.00'),
            unit_cost=Decimal('15000.00'),
            line_total=Decimal('30000.00'),
        )
        po2.recalculate_totals()
        po2.withholding_certificate_number = 'RET-2026-0002'
        po2.save()

        # Asiento 1: Aporte de Capital Social Inicial (Borrador Cuadrado para Evaluación)
        accounts_map = ensure_default_chart_of_accounts(organization)
        if not JournalEntry.objects.filter(organization=organization, concept__icontains='Capital Social').exists():
            entry_capital = JournalEntry.objects.create(
                organization=organization,
                date=timezone.now().date() - datetime.timedelta(days=60),
                entry_type=JournalEntry.EntryType.ADJUSTMENT,
                concept="Aporte Inicial de Capital Social de Socios Fundadores",
                reference="ACT-CONST-2026",
                created_by=owner_user,
            )
            # DEBE: Bancos Institucionales (BAC)
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry_capital,
                account=accounts_map['1102-01'],
                concept="Depósito BAC Aporte Capital Fundadores",
                debit=Decimal('500000.00'),
                credit=Decimal('0.00'),
            )
            # HABER: Capital Social Pagado
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry_capital,
                account=accounts_map['3101-01'],
                concept="Capital Social Autorizado y Pagado",
                debit=Decimal('0.00'),
                credit=Decimal('500000.00'),
            )
            JournalEntry.objects.filter(pk=entry_capital.pk).update(
                total_debit=Decimal('500000.00'),
                total_credit=Decimal('500000.00'),
                is_balanced=False,
                updated_at=timezone.now(),
            )

        # Asiento 2: Reconocimiento de Ingresos por Servicios Mayores de Ingeniería (Borrador Cuadrado)
        if not JournalEntry.objects.filter(organization=organization, concept__icontains='Hito Mayor Subestación').exists():
            entry_income = JournalEntry.objects.create(
                organization=organization,
                date=timezone.now().date() - datetime.timedelta(days=15),
                entry_type=JournalEntry.EntryType.SALES,
                concept="Facturación Hito Mayor Subestación Geotérmica San Jacinto",
                reference="FAC-2026-0004",
                created_by=owner_user,
            )
            # DEBE: Clientes por Cobrar CxC (Neto)
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry_income,
                account=accounts_map['1103-01'],
                concept="CxC Polaris Energy Nicaragua - Hito Mayor",
                debit=Decimal('224000.00'),
                credit=Decimal('0.00'),
            )
            # DEBE: Anticipo Retención IR 2% Sufrida
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry_income,
                account=accounts_map['1105-01'],
                concept="Retención IR 2% Polaris Energy",
                debit=Decimal('4000.00'),
                credit=Decimal('0.00'),
            )
            # DEBE: Anticipo Retención IMI 1% Sufrida
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry_income,
                account=accounts_map['1105-02'],
                concept="Retención IMI 1% Alcaldía de Telica",
                debit=Decimal('2000.00'),
                credit=Decimal('0.00'),
            )
            # HABER: Ingresos por Servicios Electromecánicos
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry_income,
                account=accounts_map['4101-01'],
                concept="Ingresos por Pruebas de Alta Tensión y Montaje",
                debit=Decimal('0.00'),
                credit=Decimal('200000.00'),
            )
            # HABER: IVA Débito Fiscal 15%
            JournalEntryLine.objects.create(
                organization=organization,
                entry=entry_income,
                account=accounts_map['2102-01'],
                concept="IVA 15% Débito Fiscal Hito Mayor",
                debit=Decimal('0.00'),
                credit=Decimal('30000.00'),
            )
            JournalEntry.objects.filter(pk=entry_income.pk).update(
                total_debit=Decimal('230000.00'),
                total_credit=Decimal('230000.00'),
                is_balanced=False,
                updated_at=timezone.now(),
            )

        # =========================================================================
        # 8. REPOSITORIO DOCUMENTAL INSTITUCIONAL (apps/repository)
        # =========================================================================
        fld_ing = RepositoryFolder.objects.create(
            organization=organization,
            code='FLD-ING-01',
            name='Planos y Manuales de Campo',
            department=RepositoryFolder.Department.ENGINEERING,
            min_role_level_view=RoleLevel.TECHNICIAN,
            min_role_level_upload=RoleLevel.ENGINEER,
        )

        fld_fin = RepositoryFolder.objects.create(
            organization=organization,
            code='FLD-FIN-01',
            name='Contratos y Solvencias Fiscales DGI',
            department=RepositoryFolder.Department.FINANCE_LEGAL,
            min_role_level_view=RoleLevel.COMMERCIAL_MGR,
            min_role_level_upload=RoleLevel.COMMERCIAL_MGR,
        )

        # Documento 1: Diagrama Unifilar v1 -> v2 (en FLD-ING-01)
        doc1 = CorporateDocument.objects.create(
            organization=organization,
            code='DOC-2026-0001',
            folder=fld_ing,
            work_order=wo1,
            title='Diagrama Unifilar Subestación 15kV - Ingenio San Antonio',
            document_type=CorporateDocument.DocumentType.CAD_PLAN,
            current_version_number=1,
            latest_filename='UNIFILAR-ISA-15KV-V1.CAD',
            latest_checksum='',
            is_locked=False,
            created_by=resident_eng,
        )
        payload_v1 = "Esquema unifilar 15kV preliminar aprobado para inicio de montaje."
        raw_v1 = f"{doc1.code}|1|UNIFILAR-ISA-15KV-V1.CAD|{payload_v1}".encode('utf-8')
        sha_v1 = hashlib.sha256(raw_v1).hexdigest()
        doc1.latest_checksum = sha_v1
        doc1.save(update_fields=['latest_checksum'])

        DocumentVersion.objects.create(
            organization=organization,
            document=doc1,
            version_number=1,
            filename='UNIFILAR-ISA-15KV-V1.CAD',
            file_size_kb=2450,
            content_summary_or_payload=payload_v1,
            checksum_sha256=sha_v1,
            change_log='Emisión preliminar de ingeniería aprobada para procura y montaje.',
            uploaded_by=resident_eng,
        )

        # Publicar versión v2 concurrente con servicio ISO
        publish_new_document_version(
            organization=organization,
            document_id=doc1.pk,
            filename='UNIFILAR-ISA-15KV-V2.PDF',
            file_size_kb=3120,
            change_log='Revisión v2 aprobada para construcción: ajuste de coordinación de protecciones y relevador diferencial conforme observaciones DGI/ENATREL.',
            content_payload='Plano ejecutivo final con firma de residente y visto bueno de interconexión ENATREL.',
            user=resident_eng,
            role_level=RoleLevel.ENGINEER,
        )

        # Documento 2: Solvencia Fiscal DGI (en FLD-FIN-01)
        doc2 = CorporateDocument.objects.create(
            organization=organization,
            code='DOC-2026-0002',
            folder=fld_fin,
            title='Constancia de Inscripción y Solvencia Fiscal DGI 2026',
            document_type=CorporateDocument.DocumentType.TAX_LEGAL,
            current_version_number=1,
            latest_filename='SOLVENCIA-FISCAL-DGI-IMESA-2026.PDF',
            latest_checksum='',
            is_locked=True,
            created_by=commercial_mgr,
        )
        payload_doc2 = "Constancia DGI con validez para licitaciones públicas del Estado de Nicaragua."
        raw_doc2 = f"{doc2.code}|1|SOLVENCIA-FISCAL-DGI-IMESA-2026.PDF|{payload_doc2}".encode('utf-8')
        sha_doc2 = hashlib.sha256(raw_doc2).hexdigest()
        doc2.latest_checksum = sha_doc2
        doc2.save(update_fields=['latest_checksum'])

        DocumentVersion.objects.create(
            organization=organization,
            document=doc2,
            version_number=1,
            filename='SOLVENCIA-FISCAL-DGI-IMESA-2026.PDF',
            file_size_kb=480,
            content_summary_or_payload=payload_doc2,
            checksum_sha256=sha_doc2,
            change_log='Emisión fiscal trimestral emitida por Administración de Rentas Managua.',
            uploaded_by=commercial_mgr,
        )

        # =========================================================================
        # 9. MESA DE AYUDA Y CONTROL DE SLA (apps/tickets)
        # =========================================================================
        # Ticket 1: Crítico en Curso (SLA 4h) reportado por Técnico Carlos Ruiz
        tck1 = SupportTicket.objects.create(
            organization=organization,
            number='TCK-2026-0001',
            title='Mantenimiento urgente camioneta Hilux M 345-890',
            category=SupportTicket.Category.VEHICLE_FLEET,
            priority=SupportTicket.Priority.CRITICAL,
            status=SupportTicket.Status.OPEN,
            related_work_order=wo1,
            reported_by=field_tech,
            assigned_to=ops_coord,
            description='Durante el retorno de campo de la cuadrilla A en Carretera Vieja a León, la camioneta Toyota Hilux placa M 345-890 presentó vibración severa en eje delantero y pérdida de presión en frenos. Requiere revisión mecánica inmediata para no paralizar salida de mañana a Subestación San Antonio.',
        )
        TicketComment.objects.create(
            organization=organization,
            ticket=tck1,
            author=field_tech,
            message='Se dejó la camioneta estacionada en el patio de maniobras de Bodega Central. Llaves entregadas en garita de vigilancia.',
        )

        # Ticket 2: Resuelto dentro de plazo
        tck2 = SupportTicket.objects.create(
            organization=organization,
            number='TCK-2026-0002',
            title='Calibración y certificación anual de multímetro industrial Fluke 87V',
            category=SupportTicket.Category.TOOL_EQUIPMENT,
            priority=SupportTicket.Priority.NORMAL,
            status=SupportTicket.Status.RESOLVED,
            reported_by=field_tech,
            assigned_to=ops_coord,
            description='El equipo multímetro Fluke 87V serie FLK-9921 requiere calibración y emisión de certificado metrológico trazable para auditoría de calidad.',
            resolved_at=timezone.now() - datetime.timedelta(hours=6),
            resolution_summary='Equipo calibrado con patrón trazable de laboratorio metrológico. Certificado CERT-MET-2026-088 archivado en repositorio técnico. Apto para uso en campo.',
        )
        TicketComment.objects.create(
            organization=organization,
            ticket=tck2,
            author=ops_coord,
            message='Calibración completada satisfactoriamente. Equipo entregado a la cuadrilla de instrumentación.',
        )

        # =========================================================================
        # 10. SERVIDOR MODEL CONTEXT PROTOCOL (MCP) Y AGENTES IA (apps/ai_agent)
        # =========================================================================
        # Token 1: Claude Desktop - Dirección Operativa (Nivel 80)
        mcp_token_ops, _raw1 = generate_mcp_token(
            organization=organization,
            name="Claude Desktop - Dirección Operativa",
            scoped_role_level=RoleLevel.OPERATIONS_COORD,
            allowed_tools=[
                'query_workorders',
                'query_inventory_stock',
                'query_sst_compliance',
                'query_sla_tickets',
                'query_financial_summary',
            ],
            created_by=owner_user,
            expires_at=timezone.now() + datetime.timedelta(days=90),
        )
        mcp_token_ops.total_calls = 2
        mcp_token_ops.last_used_at = timezone.now() - datetime.timedelta(minutes=15)
        mcp_token_ops.save(update_fields=['total_calls', 'last_used_at'])

        # Token 2: Agente Auditoría SST Ley 618 (Nivel 70)
        mcp_token_sst, _raw2 = generate_mcp_token(
            organization=organization,
            name="Agente Auditoría SST Ley 618",
            scoped_role_level=RoleLevel.SST_OFFICER,
            allowed_tools=[
                'query_workorders',
                'query_sst_compliance',
                'query_sla_tickets',
            ],
            created_by=owner_user,
            expires_at=timezone.now() + datetime.timedelta(days=60),
        )
        mcp_token_sst.total_calls = 2
        mcp_token_sst.last_used_at = timezone.now() - datetime.timedelta(minutes=5)
        mcp_token_sst.save(update_fields=['total_calls', 'last_used_at'])

        # 3 Registros de Auditoría Inmutables
        # Log 1: Consulta exitosa de OTs activas por Dirección Operativa
        McpAuditLog.objects.create(
            organization=organization,
            token=mcp_token_ops,
            tool_name='query_workorders',
            request_payload={'phase': 'IN_PROGRESS'},
            response_status=McpAuditLog.ResponseStatus.SUCCESS,
            execution_ms=45,
            ip_address='192.168.1.50',
            error_message='',
        )

        # Log 2: Consulta exitosa de cumplimiento SST por Agente SST Ley 618
        McpAuditLog.objects.create(
            organization=organization,
            token=mcp_token_sst,
            tool_name='query_sst_compliance',
            request_payload={'status': 'BLOCKED'},
            response_status=McpAuditLog.ResponseStatus.SUCCESS,
            execution_ms=38,
            ip_address='192.168.1.105',
            error_message='',
        )

        # Log 3: Intento DENEGADO por RBAC (Agente SST intentó consultar saldos financieros Nivel 80+)
        McpAuditLog.objects.create(
            organization=organization,
            token=mcp_token_sst,
            tool_name='query_financial_summary',
            request_payload={},
            response_status=McpAuditLog.ResponseStatus.DENIED,
            execution_ms=12,
            ip_address='192.168.1.105',
            error_message='Violación RBAC: scoped_role_level 70 insuficiente para invocar query_financial_summary (mínimo 80)',
        )

        # -------------------------------------------------------------------------
        # 10. AGENDA CORPORATIVA UNIFICADA & HITOS REGULATORIOS (Paso 14.1)
        # -------------------------------------------------------------------------
        now_dt = timezone.now()
        current_year = now_dt.year
        current_month = now_dt.month
        tz = timezone.get_current_timezone()

        # Hito 1: DGI IVA y Retenciones Ley 822 (Día 05)
        dgi_date = timezone.make_aware(datetime.datetime(current_year, current_month, 5, 8, 0), tz)
        CorporateEvent.objects.get_or_create(
            organization=organization,
            code='EVT-2026-0001',
            defaults={
                'title': 'Declaración Mensual IVA y Retenciones en la Fuente VET-DGI Ley 822',
                'event_type': CorporateEvent.EventType.TAX_DEADLINE,
                'priority': CorporateEvent.Priority.CRITICAL,
                'status': CorporateEvent.Status.SCHEDULED,
                'start_datetime': dgi_date,
                'end_datetime': dgi_date + datetime.timedelta(hours=4),
                'location': 'Ventanilla Electrónica Tributaria (VET-DGI)',
                'description': 'Presentación y liquidación obligatoria de IVA mensual y retenciones del 2% IR / 1% IMI.',
                'min_role_level': RoleLevel.OPERATIONS_COORD,
                'created_by': owner_user,
            }
        )

        # Hito 2: Alcaldía IMI (Día 15)
        imi_date = timezone.make_aware(datetime.datetime(current_year, current_month, 15, 8, 0), tz)
        CorporateEvent.objects.get_or_create(
            organization=organization,
            code='EVT-2026-0002',
            defaults={
                'title': 'Declaración y Pago de Impuesto Municipal sobre Ingresos (IMI Alcaldía)',
                'event_type': CorporateEvent.EventType.TAX_DEADLINE,
                'priority': CorporateEvent.Priority.HIGH,
                'status': CorporateEvent.Status.SCHEDULED,
                'start_datetime': imi_date,
                'end_datetime': imi_date + datetime.timedelta(hours=4),
                'location': 'Alcaldía de Managua (ALMA) / Ventanilla Virtual',
                'description': 'Liquidación y pago mensual del 1% de Impuesto Municipal sobre Ingresos.',
                'min_role_level': RoleLevel.OPERATIONS_COORD,
                'created_by': owner_user,
            }
        )

        # Hito 3: INSS / INATEC (Día 20)
        inss_date = timezone.make_aware(datetime.datetime(current_year, current_month, 20, 8, 0), tz)
        CorporateEvent.objects.get_or_create(
            organization=organization,
            code='EVT-2026-0003',
            defaults={
                'title': 'Pago de Facturación y Planilla Mensual INSS / INATEC',
                'event_type': CorporateEvent.EventType.TAX_DEADLINE,
                'priority': CorporateEvent.Priority.HIGH,
                'status': CorporateEvent.Status.SCHEDULED,
                'start_datetime': inss_date,
                'end_datetime': inss_date + datetime.timedelta(hours=4),
                'location': 'Portal Patronal INSS / Bancos del Sistema Financiero',
                'description': 'Pago patronal y retención laboral conforme a factura mensual emitida por el INSS.',
                'min_role_level': RoleLevel.OPERATIONS_COORD,
                'created_by': owner_user,
            }
        )

        # Hito 4: Mantenimiento Técnico Preventivo en OT-2026-0001
        maint_date = now_dt + datetime.timedelta(days=2)
        CorporateEvent.objects.get_or_create(
            organization=organization,
            code='EVT-2026-0004',
            defaults={
                'title': 'Mantenimiento Preventivo Subestación 15kV y Banco de Baterías AGM',
                'event_type': CorporateEvent.EventType.TECHNICAL_MAINTENANCE,
                'priority': CorporateEvent.Priority.CRITICAL,
                'status': CorporateEvent.Status.SCHEDULED,
                'start_datetime': maint_date,
                'end_datetime': maint_date + datetime.timedelta(hours=6),
                'location': 'Hospital Vivian Pellas, Módulo Quirúrgico Torre B',
                'description': 'Montaje mecánico de banco de baterías y pruebas de aislamiento dieléctrico.',
                'min_role_level': RoleLevel.TECHNICIAN,
                'related_work_order': wo1,
                'related_account': accounts.get('CLI-004') if 'accounts' in locals() else None,
                'assigned_to': resident_eng,
                'created_by': ops_coord,
            }
        )

        # Hito 5: Auditoría FEFO en Bodega Central
        audit_date = now_dt + datetime.timedelta(days=7)
        CorporateEvent.objects.get_or_create(
            organization=organization,
            code='EVT-2026-0005',
            defaults={
                'title': 'Conteo Físico y Auditoría Sanitaria FEFO de Lotes Farmacéuticos',
                'event_type': CorporateEvent.EventType.INVENTORY_AUDIT,
                'priority': CorporateEvent.Priority.HIGH,
                'status': CorporateEvent.Status.SCHEDULED,
                'start_datetime': audit_date,
                'end_datetime': audit_date + datetime.timedelta(hours=5),
                'location': 'Bodega Central Tipitapa (BOD-01)',
                'description': 'Inspección de lotes de antibióticos y sueros conforme a normativa MINSA.',
                'min_role_level': RoleLevel.ENGINEER,
                'assigned_to': resident_eng,
                'created_by': ops_coord,
            }
        )

        # =========================================================================
        # 16. HISTORIAL DE FACTURACIÓN DE SUSCRIPCIÓN SAAS HQ (apps/tenancy)
        # =========================================================================
        SaasSubscriptionInvoice.objects.get_or_create(
            organization=organization,
            number='SAAS-2026-0001',
            defaults={
                'plan_snapshot': 'ENTERPRISE',
                'billing_period': '2026-09',
                'amount_usd': Decimal('249.00'),
                'exchange_rate': Decimal('36.6243'),
                'amount_nio': Decimal('9119.45'),
                'status': SaasSubscriptionInvoice.Status.PAID,
                'payment_reference': 'STRIPE-NI-88412 / BAC-USD',
                'issued_at': datetime.date(2026, 9, 1),
            }
        )
        SaasSubscriptionInvoice.objects.get_or_create(
            organization=organization,
            number='SAAS-2026-0002',
            defaults={
                'plan_snapshot': 'ENTERPRISE',
                'billing_period': '2026-10',
                'amount_usd': Decimal('249.00'),
                'exchange_rate': Decimal('36.6243'),
                'amount_nio': Decimal('9119.45'),
                'status': SaasSubscriptionInvoice.Status.PAID,
                'payment_reference': 'STRIPE-NI-99231 / BAC-USD',
                'issued_at': datetime.date(2026, 10, 1),
            }
        )

        # =========================================================================
        # 12. PLANILLA Y NÓMINA LABORAL NICARAGUA LEY 822 / MITRAB (apps/payroll)
        # =========================================================================
        contracts_specs = [
            {
                'code': 'EMP-0001',
                'user': owner_user,
                'full_name': 'Ing. Alejandro Vega',
                'national_id': '001-150882-0003K',
                'inss_number': '2458901-4',
                'job_title': 'Director General & Socio Fundador',
                'department': EmployeeContract.Department.MANAGEMENT,
                'monthly_base_salary': Decimal('65000.00'),
                'bank_name': 'BAC Credomatic',
                'bank_account_number': '360-128491-0',
            },
            {
                'code': 'EMP-0002',
                'user': ops_coord,
                'full_name': 'Ing. Marcos Silva',
                'national_id': '001-220785-0002A',
                'inss_number': '3891456-8',
                'job_title': 'Coordinador General de Operaciones',
                'department': EmployeeContract.Department.OPERATIONS,
                'monthly_base_salary': Decimal('35000.00'),
                'bank_name': 'BAC Credomatic',
                'bank_account_number': '360-845591-2',
            },
            {
                'code': 'EMP-0003',
                'user': resident_eng,
                'full_name': 'Ing. Ricardo Blandón',
                'national_id': '001-051189-0006L',
                'inss_number': '4712039-2',
                'job_title': 'Ingeniero Residente de Media Tensión',
                'department': EmployeeContract.Department.OPERATIONS,
                'monthly_base_salary': Decimal('25000.00'),
                'bank_name': 'BAC Credomatic',
                'bank_account_number': '360-471203-9',
            },
            {
                'code': 'EMP-0004',
                'user': field_tech,
                'full_name': 'Téc. Carlos Ruiz',
                'national_id': '001-300895-0008F',
                'inss_number': '6294810-7',
                'job_title': 'Técnico Electromecánico & Liniero',
                'department': EmployeeContract.Department.OPERATIONS,
                'monthly_base_salary': Decimal('9500.00'),
                'bank_name': 'BAC Credomatic',
                'bank_account_number': '360-629481-0',
            },
        ]

        seeded_contracts = []
        for cspec in contracts_specs:
            contract, _ = EmployeeContract.objects.update_or_create(
                organization=organization,
                code=cspec['code'],
                defaults={
                    'user': cspec['user'],
                    'full_name': cspec['full_name'],
                    'national_id': cspec['national_id'],
                    'inss_number': cspec['inss_number'],
                    'job_title': cspec['job_title'],
                    'department': cspec['department'],
                    'hire_date': datetime.date(2024, 1, 15),
                    'monthly_base_salary': cspec['monthly_base_salary'],
                    'bank_name': cspec['bank_name'],
                    'bank_account_number': cspec['bank_account_number'],
                    'is_active': True,
                }
            )
            seeded_contracts.append(contract)

        period_oct, _ = PayrollPeriod.objects.get_or_create(
            organization=organization,
            number='NOM-2026-0001',
            defaults={
                'period_name': 'Planilla Mensual Octubre 2026',
                'frequency': PayrollPeriod.Frequency.MONTHLY,
                'start_date': datetime.date(2026, 10, 1),
                'end_date': datetime.date(2026, 10, 31),
                'payment_date': datetime.date(2026, 10, 31),
                'employer_inss_rate': Decimal('22.50'),
                'inatec_rate': Decimal('2.00'),
                'status': PayrollPeriod.Status.DRAFT,
            }
        )
        # Mantener el período de nómina en borrador para permitir pruebas interactivas de cierre y vaciado limpio sin pólizas inmutables
        # if period_oct.status == PayrollPeriod.Status.DRAFT:
        #     close_and_account_payroll_period(period_oct, owner_user)

        return {
            'organization': organization,
            'members_count': 7,
            'accounts_count': 5,
            'quotes_count': 4,
            'crews_count': 3,
            'work_orders_count': 4,
            'checklists_count': 2,
            'warehouses_count': 3,
            'inventory_items_count': 8,
            'bank_accounts_count': 2,
            'petty_cash_funds_count': 1,
            'expenses_count': 3,
            'invoices_count': 1,
            'receipts_count': 1,
            'suppliers_count': 2,
            'purchase_orders_count': 1,
            'folders_count': 2,
            'documents_count': 2,
            'tickets_count': 2,
            'mcp_tokens_count': 2,
            'mcp_logs_count': 3,
            'contracts_count': len(seeded_contracts),
            'payroll_periods_count': 1,
        }


def ensure_vertical_demo_catalog(organization: Organization, vertical_code: str):
    """
    Garantiza que el catálogo de la organización posea los ítems de muestra de la vertical elegida
    (Ferretería con 3 listas de precios o Farmacia con lotes FEFO y candado sanitario).
    Idempotente: no duplica si los SKUs ya existen.
    """
    bod1 = Warehouse.objects.filter(organization=organization, is_active=True).first()
    if not bod1:
        bod1 = Warehouse.objects.create(
            organization=organization,
            code='BOD-01',
            name='Bodega Central Mostrador',
            warehouse_type=Warehouse.WarehouseType.CENTRAL,
        )

    today = timezone.now().date()

    if vertical_code == 'HARDWARE':
        hardware_items = [
            {
                'sku': 'FER-CEM-01',
                'name': 'Cemento Canal 42.5kg (UGC)',
                'category': InventoryItem.Category.MECHANICAL,
                'unit': 'BOL',
                'purchase_unit': 'BOLSA',
                'conversion_factor': Decimal('1.00'),
                'min_stock_level': Decimal('50.00'),
                'unit_cost': Decimal('260.00'),
                'price_retail': Decimal('340.00'),
                'price_contractor': Decimal('315.00'),
                'price_wholesale': Decimal('295.00'),
                'wholesale_min_qty': Decimal('20.00'),
            },
            {
                'sku': 'FER-VAR-01',
                'name': 'Varilla Corrugada 3/8" Grado 40',
                'category': InventoryItem.Category.MECHANICAL,
                'unit': 'UND',
                'purchase_unit': 'ATADO',
                'conversion_factor': Decimal('100.00'),
                'min_stock_level': Decimal('100.00'),
                'unit_cost': Decimal('200.00'),
                'price_retail': Decimal('285.00'),
                'price_contractor': Decimal('260.00'),
                'price_wholesale': Decimal('240.00'),
                'wholesale_min_qty': Decimal('50.00'),
            },
            {
                'sku': 'FER-PIN-01',
                'name': 'Pintura Anticorrosiva Lanco Galón',
                'category': InventoryItem.Category.CONSUMABLE,
                'unit': 'GAL',
                'purchase_unit': 'GALON',
                'conversion_factor': Decimal('1.00'),
                'min_stock_level': Decimal('12.00'),
                'unit_cost': Decimal('550.00'),
                'price_retail': Decimal('850.00'),
                'price_contractor': Decimal('780.00'),
                'price_wholesale': Decimal('710.00'),
                'wholesale_min_qty': Decimal('6.00'),
            },
        ]
        for spec in hardware_items:
            item, created = InventoryItem.objects.get_or_create(
                organization=organization,
                sku=spec['sku'],
                defaults={'is_active': True, **spec}
            )
            StockBalance.objects.get_or_create(
                organization=organization,
                warehouse=bod1,
                item=item,
                defaults={'quantity': Decimal('100.00')}
            )

    elif vertical_code == 'SME_RETAIL':
        sme_items = [
            {
                'sku': 'ART-OF-01',
                'name': 'Papel Bond Carta 75g (Resma x 500)',
                'category': InventoryItem.Category.CONSUMABLE,
                'unit': 'RES',
                'purchase_unit': 'CAJA',
                'conversion_factor': Decimal('10.00'),
                'min_stock_level': Decimal('20.00'),
                'unit_cost': Decimal('140.00'),
                'price_retail': Decimal('210.00'),
                'price_contractor': Decimal('195.00'),
                'price_wholesale': Decimal('180.00'),
                'wholesale_min_qty': Decimal('5.00'),
            },
            {
                'sku': 'ART-OF-02',
                'name': 'Archivador Palanca Tamaño Oficio',
                'category': InventoryItem.Category.CONSUMABLE,
                'unit': 'UND',
                'purchase_unit': 'DOCENA',
                'conversion_factor': Decimal('12.00'),
                'min_stock_level': Decimal('15.00'),
                'unit_cost': Decimal('65.00'),
                'price_retail': Decimal('115.00'),
                'price_contractor': Decimal('105.00'),
                'price_wholesale': Decimal('95.00'),
                'wholesale_min_qty': Decimal('12.00'),
            },
            {
                'sku': 'ART-TEC-01',
                'name': 'Teclado y Mouse Inalámbrico Logitech MK270',
                'category': InventoryItem.Category.CONSUMABLE,
                'unit': 'KIT',
                'purchase_unit': 'KIT',
                'conversion_factor': Decimal('1.00'),
                'min_stock_level': Decimal('10.00'),
                'unit_cost': Decimal('680.00'),
                'price_retail': Decimal('980.00'),
                'price_contractor': Decimal('920.00'),
                'price_wholesale': Decimal('850.00'),
                'wholesale_min_qty': Decimal('3.00'),
            },
        ]
        for spec in sme_items:
            item, created = InventoryItem.objects.get_or_create(
                organization=organization,
                sku=spec['sku'],
                defaults={'is_active': True, **spec}
            )
            StockBalance.objects.get_or_create(
                organization=organization,
                warehouse=bod1,
                item=item,
                defaults={'quantity': Decimal('60.00')}
            )

    elif vertical_code == 'PHARMACY':
        pharmacy_items = [
            {
                'sku': 'MED-AMX-01',
                'name': 'Amoxicilina + Ácido Clavulánico 500mg (Caja x 14)',
                'category': InventoryItem.Category.CONSUMABLE,
                'unit': 'CAJA',
                'purchase_unit': 'CAJA',
                'conversion_factor': Decimal('1.00'),
                'min_stock_level': Decimal('15.00'),
                'unit_cost': Decimal('320.00'),
                'price_retail': Decimal('480.00'),
                'price_contractor': Decimal('450.00'),
                'price_wholesale': Decimal('410.00'),
                'wholesale_min_qty': Decimal('12.00'),
                'requires_batch_control': True,
                'sanitary_registration': 'RS-NI-2026-0412',
                'active_ingredient': 'Amoxicilina 500mg + Ácido Clavulánico 125mg',
                'laboratory_brand': 'Laboratorios Ramos',
                'requires_prescription': True,
            },
            {
                'sku': 'MED-LOS-01',
                'name': 'Losartán Potásico 50mg MK',
                'category': InventoryItem.Category.CONSUMABLE,
                'unit': 'CAJA',
                'purchase_unit': 'CAJA',
                'conversion_factor': Decimal('1.00'),
                'min_stock_level': Decimal('20.00'),
                'unit_cost': Decimal('110.00'),
                'price_retail': Decimal('195.00'),
                'price_contractor': Decimal('180.00'),
                'price_wholesale': Decimal('160.00'),
                'wholesale_min_qty': Decimal('10.00'),
                'requires_batch_control': True,
                'sanitary_registration': 'RS-NI-2025-0199',
                'active_ingredient': 'Losartán Potásico 50mg',
                'laboratory_brand': 'MK / Tecnoquímicas',
                'requires_prescription': False,
            },
            {
                'sku': 'MED-SAL-01',
                'name': 'Solución Salina 0.9% 1000ml',
                'category': InventoryItem.Category.CONSUMABLE,
                'unit': 'FCO',
                'purchase_unit': 'FRASCO',
                'conversion_factor': Decimal('1.00'),
                'min_stock_level': Decimal('25.00'),
                'unit_cost': Decimal('65.00'),
                'price_retail': Decimal('120.00'),
                'price_contractor': Decimal('105.00'),
                'price_wholesale': Decimal('90.00'),
                'wholesale_min_qty': Decimal('24.00'),
                'requires_batch_control': True,
                'sanitary_registration': 'RS-NI-2024-0088',
                'active_ingredient': 'Cloruro de Sodio 0.9%',
                'laboratory_brand': 'Laboratorios Baxter',
                'requires_prescription': False,
            },
        ]
        for spec in pharmacy_items:
            item, created = InventoryItem.objects.get_or_create(
                organization=organization,
                sku=spec['sku'],
                defaults={'is_active': True, **spec}
            )
            StockBalance.objects.get_or_create(
                organization=organization,
                warehouse=bod1,
                item=item,
                defaults={'quantity': Decimal('125.00')}
            )

        amx_item = InventoryItem.objects.filter(organization=organization, sku='MED-AMX-01').first()
        if amx_item:
            InventoryBatch.objects.get_or_create(
                organization=organization,
                warehouse=bod1,
                item=amx_item,
                batch_number='LOT-2026-OPT1',
                defaults={
                    'manufacturing_date': today - datetime.timedelta(days=60),
                    'expiration_date': today + datetime.timedelta(days=365),
                    'quantity_available': Decimal('80.00'),
                }
            )
            InventoryBatch.objects.get_or_create(
                organization=organization,
                warehouse=bod1,
                item=amx_item,
                batch_number='LOT-2026-SOON',
                defaults={
                    'manufacturing_date': today - datetime.timedelta(days=300),
                    'expiration_date': today + datetime.timedelta(days=45),
                    'quantity_available': Decimal('30.00'),
                }
            )
            InventoryBatch.objects.get_or_create(
                organization=organization,
                warehouse=bod1,
                item=amx_item,
                batch_number='LOT-2025-EXP1',
                defaults={
                    'manufacturing_date': today - datetime.timedelta(days=700),
                    'expiration_date': today - datetime.timedelta(days=10),
                    'quantity_available': Decimal('15.00'),
                }
            )

        # Alerta de Stock Bajo (LOW_STOCK): Ajustar Solución Salina a 5 unidades (min_stock_level=25)
        sal_item = InventoryItem.objects.filter(organization=organization, sku='MED-SAL-01').first()
        if sal_item:
            StockBalance.objects.filter(organization=organization, warehouse=bod1, item=sal_item).update(quantity=Decimal('5.00'))


def seed_multi_vertical_demo_organizations(country, users):
    """
    Siembra 4 organizaciones independientes para evaluación limpia de cada vertical de negocio:
    1. SERMANSA Ingeniería S.A. (ENGINEERING) - Flujo industrial completo, OTs, SST, Nómina.
    2. Ferretería El Maestro S.A. (HARDWARE) - POS, 3 listas de precios, compras ferreteras.
    3. Farmacia Santa Lucía S.A. (PHARMACY) - POS, lotes FEFO, registro MINSA, dispensación médica.
    4. Comercial Masaya S.A. (SME_RETAIL) - Tienda PYME, facturación y caja ágil.
    """
    from apps.tenancy.constants import BusinessVertical
    from apps.finance.models import BankAccount, PettyCashFund

    org_definitions = [
        {
            'slug': 'ferreteria-el-maestro',
            'name': 'Ferretería El Maestro S.A.',
            'legal_name': 'Distribuidora y Ferretería El Maestro Sociedad Anónima',
            'tax_id': 'J0310000771234',
            'business_vertical': BusinessVertical.HARDWARE_STORE,
            'business_profile': Organization.BusinessProfile.COMMERCIAL_RETAIL,
            'subscription_plan': Organization.SubscriptionPlan.BUSINESS_PRO,
            'subscription_status': Organization.SubscriptionStatus.ACTIVE,
            'monthly_fee_usd': Decimal('89.00'),
            'max_users_quota': 15,
            'enabled_modules': ['crm', 'inventory', 'finance', 'billing', 'purchasing', 'repository', 'tickets', 'ai_agent', 'payroll'],
            'industry': Organization.Industry.COMMERCE,
            'warehouse_name': 'Bodega Ferretera Central',
            'bank_name': 'BAC Credomatic - Córdobas (Ferretería)',
            'fund_name': 'Caja Chica Mostrador Ferretería',
            'fund_amount': Decimal('15000.00'),
        },
        {
            'slug': 'farmacia-santa-lucia',
            'name': 'Farmacia Santa Lucía S.A.',
            'legal_name': 'Droguería y Farmacia Santa Lucía Sociedad Anónima',
            'tax_id': 'J0310000885678',
            'business_vertical': BusinessVertical.PHARMACY_HEALTH,
            'business_profile': Organization.BusinessProfile.COMMERCIAL_RETAIL,
            'subscription_plan': Organization.SubscriptionPlan.BUSINESS_PRO,
            'subscription_status': Organization.SubscriptionStatus.ACTIVE,
            'monthly_fee_usd': Decimal('89.00'),
            'max_users_quota': 15,
            'enabled_modules': ['crm', 'inventory', 'finance', 'billing', 'purchasing', 'repository', 'tickets', 'ai_agent', 'payroll'],
            'industry': Organization.Industry.COMMERCE,
            'warehouse_name': 'Bodega Farmacéutica Climatizada',
            'bank_name': 'Banco Lafise Bancentro - Córdobas (Farmacia)',
            'fund_name': 'Caja Chica Farmacia Turno A',
            'fund_amount': Decimal('10000.00'),
        },
        {
            'slug': 'comercial-masaya',
            'name': 'Comercial Masaya S.A.',
            'legal_name': 'Distribuciones Comerciales de Masaya Sociedad Anónima',
            'tax_id': 'J0310000994321',
            'business_vertical': BusinessVertical.SME_RETAIL,
            'business_profile': Organization.BusinessProfile.COMMERCIAL_RETAIL,
            'subscription_plan': Organization.SubscriptionPlan.PYME_STARTER,
            'subscription_status': Organization.SubscriptionStatus.ACTIVE,
            'monthly_fee_usd': Decimal('39.00'),
            'max_users_quota': 5,
            'enabled_modules': ['crm', 'inventory', 'finance', 'billing', 'purchasing', 'repository', 'tickets', 'payroll'],
            'industry': Organization.Industry.COMMERCE,
            'warehouse_name': 'Tienda y Almacén General',
            'bank_name': 'Banco Avanz - Córdobas (Comercial Masaya)',
            'fund_name': 'Caja Principal Tienda Masaya',
            'fund_amount': Decimal('8000.00'),
        },
    ]

    created_orgs = []
    for spec in org_definitions:
        org, _ = Organization.objects.get_or_create(
            slug=spec['slug'],
            defaults={
                'name': spec['name'],
                'legal_name': spec['legal_name'],
                'tax_id': spec['tax_id'],
                'country': country,
                'currency': Organization.Currency.NIO,
                'tax_regime': Organization.TaxRegime.GENERAL,
                'industry': spec['industry'],
                'business_vertical': spec['business_vertical'],
                'business_profile': spec['business_profile'],
                'accounting_template': Organization.AccountingTemplate.NIIF_PYMES,
                'subscription_plan': spec['subscription_plan'],
                'subscription_status': spec['subscription_status'],
                'monthly_fee_usd': spec['monthly_fee_usd'],
                'max_users_quota': spec['max_users_quota'],
                'max_users': spec['max_users_quota'],
                'enabled_modules': spec['enabled_modules'],
                'onboarding_completed': True,
                'is_active': True,
            }
        )
        org.name = spec['name']
        org.legal_name = spec['legal_name']
        org.tax_id = spec['tax_id']
        org.business_vertical = spec['business_vertical']
        org.business_profile = spec['business_profile']
        org.subscription_plan = spec['subscription_plan']
        org.subscription_status = spec['subscription_status']
        org.monthly_fee_usd = spec['monthly_fee_usd']
        org.max_users_quota = spec['max_users_quota']
        org.max_users = spec['max_users_quota']
        org.next_billing_date = datetime.date(2026, 11, 1)
        org.enabled_modules = spec['enabled_modules']
        org.onboarding_completed = True
        org.is_active = True
        org.save()

        for u in users:
            OrganizationMembership.objects.update_or_create(
                user=u,
                organization=org,
                defaults={
                    'role_level': RoleLevel.OWNER_ADMIN,
                    'job_title': 'Director General / Socio',
                    'is_active': True,
                }
            )

        Warehouse.objects.get_or_create(
            organization=org,
            code='BOD-01',
            defaults={
                'name': spec['warehouse_name'],
                'warehouse_type': Warehouse.WarehouseType.CENTRAL,
                'is_active': True,
            }
        )

        BankAccount.objects.get_or_create(
            organization=org,
            account_number=f"360-{org.tax_id[-6:]}-1",
            defaults={
                'bank_name': BankAccount.BankName.BAC,
                'description': spec['bank_name'],
                'currency': 'NIO',
                'account_type': BankAccount.AccountType.CHECKING,
                'current_balance': Decimal('250000.00'),
                'is_active': True,
            }
        )

        PettyCashFund.objects.get_or_create(
            organization=org,
            code='CAJA-01',
            defaults={
                'name': spec['fund_name'],
                'custodian': users[0],
                'currency': 'NIO',
                'allocated_amount': spec['fund_amount'],
                'current_balance': spec['fund_amount'],
                'is_active': True,
            }
        )

        ensure_vertical_demo_catalog(org, spec['business_vertical'])

        # Turnos de Caja POS demostrativos para organizaciones de comercio y mostrador
        PosRegisterShift.objects.filter(organization=org).update(status=PosRegisterShift.Status.OPEN, audit_hash='')
        PosRegisterShift.objects.filter(organization=org).delete()

        raw_hash_sat = f"{org.id}:TURNO-2026-0001:2000.00:3450.00:5450.00:5450.00:0.00:CLOSED_Z"
        sha_sat = hashlib.sha256(raw_hash_sat.encode('utf-8')).hexdigest()

        sat_shift_z = PosRegisterShift.objects.create(
            organization=org,
            shift_number='TURNO-2026-0001',
            cashier=users[0],
            register_name='CAJA-PRINCIPAL-01',
            status=PosRegisterShift.Status.OPEN,
            opened_at=timezone.now() - datetime.timedelta(days=1, hours=8),
            opening_cash_nio=Decimal('2000.00'),
            system_cash_sales_nio=Decimal('3450.00'),
            system_card_transfer_sales_nio=Decimal('1200.00'),
            expected_cash_in_drawer_nio=Decimal('5450.00'),
            declared_cash_nio=Decimal('5450.00'),
            cash_difference_nio=Decimal('0.00'),
            transactions_count=8,
            closing_notes='Arqueo de cierre de jornada regular. Gaveta cuadrada al 100% conforme a comprobantes.',
        )
        sat_shift_z.status = PosRegisterShift.Status.CLOSED_Z
        sat_shift_z.closed_at = timezone.now() - datetime.timedelta(days=1)
        sat_shift_z.audit_hash = sha_sat
        sat_shift_z.save()

        PosRegisterShift.objects.create(
            organization=org,
            shift_number='TURNO-2026-0002',
            cashier=users[0],
            register_name='CAJA-PRINCIPAL-01',
            status=PosRegisterShift.Status.OPEN,
            opened_at=timezone.now() - datetime.timedelta(hours=2),
            opening_cash_nio=Decimal('2000.00'),
            system_cash_sales_nio=Decimal('0.00'),
            system_card_transfer_sales_nio=Decimal('0.00'),
            expected_cash_in_drawer_nio=Decimal('2000.00'),
            declared_cash_nio=Decimal('0.00'),
            cash_difference_nio=Decimal('0.00'),
            transactions_count=0,
            closing_notes='',
        )

        created_orgs.append(org)

    return created_orgs




