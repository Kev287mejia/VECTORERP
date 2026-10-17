from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from apps.core.models import CountryConfig
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel
from apps.crm.models import Account
from apps.operations.models import Crew, WorkOrder, WorkOrderTimelineLog
from apps.finance.models import (
    BankAccount,
    PettyCashFund,
    PettyCashExpense,
    PettyCashReplenishment,
    FinancialTransaction,
    AccountingAccount,
    JournalEntry,
    JournalEntryLine,
)
from apps.finance.services import (
    allocate_petty_cash_fund,
    record_petty_cash_expense,
    submit_petty_cash_replenishment,
    disburse_petty_cash_replenishment,
    ensure_default_chart_of_accounts,
)
from apps.finance.services_statements import FinancialStatementsService
from apps.finance.services_dgi_tax import NicaraguaDgiTaxService
from apps.billing.models import Invoice
from apps.payroll.models import PayrollPeriod
from apps.payroll.services import ensure_payroll_accounting_accounts
from apps.purchasing.models import Supplier, PurchaseOrder
from apps.inventory.models import Warehouse

User = get_user_model()


class FinanceModuleEndToEndTestCase(TestCase):
    """
    Suite de Pruebas End-to-End para el Módulo de Finanzas, Tesorería y Fondos de Caja Chica (apps/finance).
    Verifica:
    1. Ciclo Financiero Completo:
       Apertura Bancaria -> Dotación de Fondo Fijo -> Registro de Comprobantes de Campo vinculados a OT
       -> Arqueo y Solicitud de Reposición -> Aprobación y Desembolso Bancario -> Restauración de Saldo.
    2. Prevención Estricta de Sobregiros y Saldos Negativos en Cajas Chicas y Bancos.
    3. Inmutabilidad Estricta del Libro Mayor (FinancialTransaction) y Comprobantes Liquidados.
    4. Matriz de Autoridad Jerárquica RBAC y Confidencialidad Bancaria:
       - Nivel 20 (Técnico de Campo): Acceso Prohibido (HTTP 403).
       - Nivel 50 (Ingeniero Residente / Custodio): Acceso a su caja asignada, ocultamiento total de cuentas bancarias.
       - Nivel 80 (Coordinador Operativo): Visibilidad global, aprobación y desembolso bancario.
       - Nivel 100 (Owner / Administrador): Acceso irrestricto y apertura de cuentas bancarias.
    5. Aislamiento Multi-Tenant Estricto por Organización.
    """

    def setUp(self):
        # 1. Configuración de Jurisdicción Fiscal Nicaragua
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
            name="Vector Soluciones Industriales, S.A.",
            legal_name="Vector Soluciones Industriales de Nicaragua, S.A.",
            slug="vector-ni-finance",
            tax_id="J0310000199991",
            country=self.country_ni,
            enabled_modules=['crm', 'operations', 'sst', 'inventory', 'finance'],
            is_active=True,
        )

        self.org_intruder = Organization.objects.create(
            name="Constructora del Norte, S.A.",
            legal_name="Constructora del Norte S.A.",
            slug="norte-ni-finance",
            tax_id="J0310000188882",
            country=self.country_ni,
            enabled_modules=['finance'],
            is_active=True,
        )

        # 3. Usuarios de la Matriz RBAC
        # Nivel 100: Owner / Administrador General
        self.user_owner = User.objects.create_user(
            email='owner@vector.com.ni',
            full_name='Ing. Roberto Meza (Owner)',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            user=self.user_owner,
            organization=self.org,
            role_level=RoleLevel.OWNER_ADMIN,
            job_title='Director General',
            is_active=True,
            is_default_org=True,
        )

        # Nivel 80: Coordinador Operativo
        self.user_coord = User.objects.create_user(
            email='coordinador@vector.com.ni',
            full_name='Ing. Carlos Pineda (Coordinador)',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            user=self.user_coord,
            organization=self.org,
            role_level=RoleLevel.OPERATIONS_COORD,
            job_title='Coordinador de Proyectos',
            is_active=True,
            is_default_org=True,
        )

        # Nivel 50: Ingeniero Residente (Custodio de Caja 1)
        self.user_engineer = User.objects.create_user(
            email='ingeniero@vector.com.ni',
            full_name='Ing. Andrea Salgado (Residente Alfa)',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            user=self.user_engineer,
            organization=self.org,
            role_level=RoleLevel.ENGINEER,
            job_title='Ingeniero Residente',
            is_active=True,
            is_default_org=True,
        )

        # Nivel 50: Segundo Ingeniero Residente (Custodio de Caja 2)
        self.user_engineer_beta = User.objects.create_user(
            email='ing.beta@vector.com.ni',
            full_name='Ing. Denis Morales (Residente Beta)',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            user=self.user_engineer_beta,
            organization=self.org,
            role_level=RoleLevel.ENGINEER,
            job_title='Ingeniero Residente Beta',
            is_active=True,
            is_default_org=True,
        )

        # Nivel 20: Técnico de Campo
        self.user_tech = User.objects.create_user(
            email='tecnico@vector.com.ni',
            full_name='Armando Solís (Técnico)',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            user=self.user_tech,
            organization=self.org,
            role_level=RoleLevel.TECHNICIAN,
            job_title='Técnico Electricista',
            is_active=True,
            is_default_org=True,
        )

        # Usuario Organización Intrusora
        self.user_intruder = User.objects.create_user(
            email='owner@norte.com.ni',
            full_name='Mario Espinoza (Intruso)',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            user=self.user_intruder,
            organization=self.org_intruder,
            role_level=RoleLevel.OWNER_ADMIN,
            job_title='Gerente Intruso',
            is_active=True,
            is_default_org=True,
        )

        # 4. Datos Operativos Base (Cliente, OT y Cuadrilla)
        self.client_account = Account.objects.create(
            organization=self.org,
            commercial_name="Consorcio Minero de Nicaragua, S.A.",
            legal_name="Consorcio Minero S.A.",
            tax_id="J0310000223344",
        )

        self.crew_alfa = Crew.objects.create(
            organization=self.org,
            code="CRW-ALFA",
            name="Cuadrilla Alfa - Electromecánica",
            vehicle_plate="M-301294",
        )

        self.work_order = WorkOrder.objects.create(
            organization=self.org,
            number="OT-2026-0001",
            account=self.client_account,
            title="Mantenimiento Subestación Mina San Albino",
            phase=WorkOrder.Phase.IN_PROGRESS,
            assigned_crew=self.crew_alfa,
        )

        # 5. Cuenta Bancaria Inicial
        self.bank_bac = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            currency='NIO',
            account_number='001-884920-001',
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('200000.00'),
            description='Cuenta Maestra Operativa BAC Credomatic Managua',
            is_active=True,
        )

        # 6. Caja Chica Asignada a Ingeniero Alfa
        self.fund_alfa = PettyCashFund.objects.create(
            organization=self.org,
            code="CAJ-01",
            name="Fondo Fijo Hilux Cuadrilla Alfa",
            custodian=self.user_engineer,
            linked_crew=self.crew_alfa,
            currency='NIO',
            allocated_amount=Decimal('10000.00'),
            current_balance=Decimal('0.00'),
            is_active=True,
        )

    # =========================================================================
    # 1. PRUEBA: CICLO FINANCIERO COMPLETO
    # =========================================================================
    def test_complete_petty_cash_lifecycle(self):
        """
        Valida el flujo end-to-end de tesorería y fondos de campo:
        1. Dotación inicial del fondo desde banco -> Saldos actualizados y TRX creada.
        2. Registro de gastos de campo vinculados a OT -> Balance disminuye, timeline de OT actualizado.
        3. Arqueo y solicitud formal de reposición -> Lote REP creado con estado SUBMITTED.
        4. Desembolso bancario ejecutado por Coordinador -> Fondos restaurados, gastos marcados is_replenished.
        """
        # Paso 1: Dotar el fondo fijo desde BAC Credomatic
        trx_dotacion = allocate_petty_cash_fund(
            organization=self.org,
            fund=self.fund_alfa,
            bank_account=self.bank_bac,
            amount=Decimal('10000.00'),
            user=self.user_owner,
        )
        self.fund_alfa.refresh_from_db()
        self.bank_bac.refresh_from_db()

        self.assertEqual(self.fund_alfa.current_balance, Decimal('10000.00'))
        self.assertEqual(self.bank_bac.current_balance, Decimal('190000.00'))
        self.assertEqual(trx_dotacion.transaction_type, FinancialTransaction.TransactionType.PETTY_CASH_ALLOCATION)
        self.assertEqual(trx_dotacion.balance_after, Decimal('190000.00'))  # H-01: Saldo bancario posterior

        # Paso 2: Registrar Gasto 1 (Combustible en ruta a mina)
        exp1 = record_petty_cash_expense(
            organization=self.org,
            fund=self.fund_alfa,
            expense_category=PettyCashExpense.Category.FUEL,
            vendor_name="Puma Tipitapa",
            amount=Decimal('2800.00'),
            description="35 Galones Diesel para Hilux Cuadrilla Alfa",
            user=self.user_engineer,
            work_order=self.work_order,
            vendor_tax_id="J0310000012345",
            invoice_number="FACT-9921",
        )
        self.fund_alfa.refresh_from_db()
        self.assertEqual(self.fund_alfa.current_balance, Decimal('7200.00'))
        self.assertEqual(self.fund_alfa.spent_amount, Decimal('2800.00'))
        self.assertEqual(self.fund_alfa.spent_percentage, 28)
        self.assertTrue(exp1.voucher_number.startswith("REC-"))
        self.assertFalse(exp1.is_replenished)

        # Verificar Bitácora Técnica en la Orden de Trabajo
        ot_log = WorkOrderTimelineLog.objects.filter(
            work_order=self.work_order,
            event_type='PETTY_CASH_EXPENSE'
        ).first()
        self.assertIsNotNone(ot_log)
        self.assertIn("FACT-9921", ot_log.details)
        self.assertIn("Puma Tipitapa", ot_log.details)

        # Paso 3: Registrar Gasto 2 (Viáticos y comida de cuadrilla)
        exp2 = record_petty_cash_expense(
            organization=self.org,
            fund=self.fund_alfa,
            expense_category=PettyCashExpense.Category.PER_DIEM,
            vendor_name="Restaurante La Casona Sebaco",
            amount=Decimal('1200.00'),
            description="Alimentación 4 técnicos despliegue montaje",
            user=self.user_engineer,
            work_order=self.work_order,
            invoice_number="REC-041",
        )
        self.fund_alfa.refresh_from_db()
        self.assertEqual(self.fund_alfa.current_balance, Decimal('6000.00'))
        self.assertEqual(self.fund_alfa.spent_amount, Decimal('4000.00'))

        # Paso 4: Ingeniero realiza Arqueo y Solicita Reposición
        rep = submit_petty_cash_replenishment(
            organization=self.org,
            fund=self.fund_alfa,
            notes="Arqueo físico semanal cuadrilla alfa. Todos los recibos adjuntos.",
            user=self.user_engineer,
        )
        self.assertEqual(rep.status, PettyCashReplenishment.Status.SUBMITTED)
        self.assertEqual(rep.total_expenses_amount, Decimal('4000.00'))
        self.assertEqual(rep.requested_amount, Decimal('4000.00'))
        self.assertTrue(rep.code.startswith("REP-"))
        self.assertEqual(rep.expenses.count(), 2)

        # Paso 5: Coordinador Operativo aprueba y desembolsa desde BAC
        trx_disburse = disburse_petty_cash_replenishment(
            organization=self.org,
            replenishment=rep,
            bank_account=self.bank_bac,
            user=self.user_coord,
        )
        self.fund_alfa.refresh_from_db()
        self.bank_bac.refresh_from_db()
        rep.refresh_from_db()
        exp1.refresh_from_db()
        exp2.refresh_from_db()

        # Validaciones de Estado Final
        self.assertEqual(rep.status, PettyCashReplenishment.Status.DISBURSED)
        self.assertEqual(rep.approved_by, self.user_coord)
        self.assertEqual(rep.disbursed_from_bank, self.bank_bac)
        self.assertTrue(exp1.is_replenished)
        self.assertTrue(exp2.is_replenished)

        # Saldo del Fondo Fijo debe haber retornado a su límite autorizado de C$ 10,000.00
        self.assertEqual(self.fund_alfa.current_balance, Decimal('10000.00'))
        self.assertEqual(self.fund_alfa.spent_amount, Decimal('0.00'))

        # Saldo bancario debe reflejar el débito de los C$ 4,000.00 reembolsados
        self.assertEqual(self.bank_bac.current_balance, Decimal('186000.00'))
        self.assertEqual(trx_disburse.transaction_type, FinancialTransaction.TransactionType.PETTY_CASH_REPLENISH)
        self.assertEqual(trx_disburse.balance_after, Decimal('186000.00'))  # H-01: Saldo bancario posterior

    # =========================================================================
    # 2. PRUEBA: PREVENCIÓN DE SOBREGIRO Y SALDO NEGATIVO
    # =========================================================================
    def test_negative_balance_prevention(self):
        """
        Verifica que el sistema rechace transacciones que generen saldos negativos,
        tanto en Cajas Chicas como en Cuentas Bancarias.
        """
        # 1. Rechazo de dotación bancaria por fondos insuficientes
        bank_pobre = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BANPRO,
            currency='NIO',
            account_number='002-111111-001',
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('500.00'),
            is_active=True,
        )
        with self.assertRaises(ValidationError) as ctx:
            allocate_petty_cash_fund(
                organization=self.org,
                fund=self.fund_alfa,
                bank_account=bank_pobre,
                amount=Decimal('1000.00'),
                user=self.user_owner,
            )
        self.assertIn("insuficiente", str(ctx.exception).lower())
        self.assertEqual(bank_pobre.current_balance, Decimal('500.00'))

        # 2. Rechazo de gasto en caja chica que excede el efectivo disponible
        # Caja tiene 0.00 de saldo disponible
        with self.assertRaises(ValidationError) as ctx:
            record_petty_cash_expense(
                organization=self.org,
                fund=self.fund_alfa,
                expense_category=PettyCashExpense.Category.FUEL,
                vendor_name="Puma",
                amount=Decimal('150.00'),
                description="Compra de combustible sin saldo",
                user=self.user_engineer,
            )
        self.assertIn("insuficiente", str(ctx.exception).lower())
        self.assertEqual(self.fund_alfa.current_balance, Decimal('0.00'))

    # =========================================================================
    # 3. PRUEBA: INMUTABILIDAD ESTRICTA DEL LIBRO MAYOR FINANCIERO
    # =========================================================================
    def test_ledger_immutability(self):
        """
        Garantiza que ningún registro de FinancialTransaction pueda ser alterado ni borrado.
        Garantiza que comprobantes ya liquidados (is_replenished=True) no puedan ser adulterados.
        """
        # Crear transacción válida
        trx = FinancialTransaction.objects.create(
            organization=self.org,
            transaction_type=FinancialTransaction.TransactionType.PETTY_CASH_ALLOCATION,
            bank_account=self.bank_bac,
            petty_cash_fund=self.fund_alfa,
            amount=Decimal('5000.00'),
            balance_after=Decimal('205000.00'),
            concept="Inyección de capital inicial",
            performed_by=self.user_owner,
        )

        # Intento de modificación debe lanzar ValidationError
        trx.concept = "Modificación fraudulenta de concepto"
        with self.assertRaises(ValidationError):
            trx.save()

        # Intento de eliminación debe lanzar ValidationError
        with self.assertRaises(ValidationError):
            trx.delete()

        # Validar inmutabilidad de comprobante liquidado
        self.fund_alfa.current_balance = Decimal('1000.00')
        self.fund_alfa.save()

        expense = record_petty_cash_expense(
            organization=self.org,
            fund=self.fund_alfa,
            expense_category=PettyCashExpense.Category.TOLL_PARKING,
            vendor_name="Peaje San Benito",
            amount=Decimal('100.00'),
            description="Peaje cuadrilla",
            user=self.user_engineer,
        )
        expense.is_replenished = True
        expense.save()

        # Intentar cambiar monto una vez reembolsado
        expense.amount = Decimal('500.00')
        with self.assertRaises(ValidationError):
            expense.clean()

    # =========================================================================
    # 4. PRUEBA: MATRIZ DE AUTORIDAD RBAC Y CONFIDENCIALIDAD BANCARIA
    # =========================================================================
    def test_rbac_and_financial_confidentiality(self):
        """
        Verifica:
        - Nivel 20 (Técnico de Campo) tiene acceso denegado (HTTP 403).
        - Nivel 50 (Ingeniero Custodio) puede ver su caja, pero NO ve cuentas bancarias ni saldos de bancos.
        - Nivel 50 no puede gastar ni rendir cajas asignadas a otro custodio.
        - Nivel 80 (Coordinador) ve cuentas bancarias y aprueba desembolsos.
        """
        # Dotar caja para pruebas de interfaz
        self.fund_alfa.current_balance = Decimal('5000.00')
        self.fund_alfa.save()

        # Caja asignada al Ingeniero Beta
        fund_beta = PettyCashFund.objects.create(
            organization=self.org,
            code="CAJ-02",
            name="Fondo Fijo Hilux Cuadrilla Beta",
            custodian=self.user_engineer_beta,
            currency='NIO',
            allocated_amount=Decimal('8000.00'),
            current_balance=Decimal('2000.00'),
            is_active=True,
        )

        # 1. Nivel 20: Técnico de Campo -> 403 Forbidden
        client_tech = Client()
        client_tech.force_login(self.user_tech)
        resp = client_tech.get(reverse('finance:dashboard'))
        self.assertEqual(resp.status_code, 403)

        # 2. Nivel 50: Ingeniero Alfa -> Dashboard
        client_eng = Client()
        client_eng.force_login(self.user_engineer)
        resp = client_eng.get(reverse('finance:dashboard'))
        self.assertEqual(resp.status_code, 200)

        # Confidencialidad Bancaria: can_view_bank_accounts debe ser False
        self.assertFalse(resp.context['can_view_bank_accounts'])
        # Las cuentas bancarias no deben estar presentes en el contexto del ingeniero
        self.assertEqual(len(resp.context['bank_accounts']), 0)
        self.assertEqual(resp.context['total_bank_nio'], Decimal('0.00'))

        # Solo debe ver su propia caja (CAJ-01), no la de Beta (CAJ-02)
        funds_visible = [f.code for f in resp.context['funds']]
        self.assertIn("CAJ-01", funds_visible)
        self.assertNotIn("CAJ-02", funds_visible)

        # Ingeniero Alfa intenta registrar gasto en CAJ-02 (de Beta) -> 403
        post_data = {
            'fund': str(fund_beta.pk),
            'amount': '500.00',
            'expense_category': 'FUEL',
            'vendor_name': 'Puma Tipitapa',
            'description': 'Intento de gasto en caja ajena',
            'spent_at': str(timezone.now().date()),
        }
        resp_post = client_eng.post(reverse('finance:expense_create'), data=post_data)
        self.assertEqual(resp_post.status_code, 403)

        # 3. Nivel 80: Coordinador Operativo -> Ve todo y tiene can_view_bank_accounts
        client_coord = Client()
        client_coord.force_login(self.user_coord)
        resp_coord = client_coord.get(reverse('finance:dashboard'))
        self.assertEqual(resp_coord.status_code, 200)
        self.assertTrue(resp_coord.context['can_view_bank_accounts'])
        self.assertTrue(resp_coord.context['can_manage_funds'])
        self.assertGreater(len(resp_coord.context['bank_accounts']), 0)
        # Debe ver ambas cajas
        coord_funds = [f.code for f in resp_coord.context['funds']]
        self.assertIn("CAJ-01", coord_funds)
        self.assertIn("CAJ-02", coord_funds)

    # =========================================================================
    # 5. PRUEBA: AISLAMIENTO MULTI-TENANT ESTRICTO
    # =========================================================================
    def test_multi_tenant_isolation(self):
        """
        Garantiza que un usuario de otra organización (Tenant B), incluso con rol Owner (100),
        no pueda consultar, listar ni operar cuentas bancarias, cajas chicas o transacciones de Tenant A.
        """
        # Crear cuenta bancaria y caja en la organización intrusa
        bank_intruder = BankAccount.objects.create(
            organization=self.org_intruder,
            bank_name=BankAccount.BankName.LAFISE,
            currency='USD',
            account_number='110-994821-001',
            account_type=BankAccount.AccountType.CHECKING,
            current_balance=Decimal('50000.00'),
            is_active=True,
        )
        fund_intruder = PettyCashFund.objects.create(
            organization=self.org_intruder,
            code="CAJ-01",
            name="Caja Intruso Norte",
            custodian=self.user_intruder,
            currency='NIO',
            allocated_amount=Decimal('5000.00'),
            current_balance=Decimal('5000.00'),
            is_active=True,
        )

        client_intruder = Client()
        client_intruder.force_login(self.user_intruder)

        # Dashboard del intruso: solo ve sus recursos
        resp = client_intruder.get(reverse('finance:dashboard'))
        self.assertEqual(resp.status_code, 200)

        bank_ids = [b.id for b in resp.context['bank_accounts']]
        fund_ids = [f.id for f in resp.context['funds']]

        self.assertIn(bank_intruder.id, bank_ids)
        self.assertNotIn(self.bank_bac.id, bank_ids)

        self.assertIn(fund_intruder.id, fund_ids)
        self.assertNotIn(self.fund_alfa.id, fund_ids)

        # Intento de consultar detalle de caja de Tenant A -> 404
        resp_detail = client_intruder.get(reverse('finance:fund_detail', kwargs={'pk': self.fund_alfa.pk}))
        self.assertEqual(resp_detail.status_code, 404)


class FinancialStatementsAndDgiTaxTestCase(TestCase):
    """
    Suite de Pruebas Unitarias y de Integración para el Motor de Estados Financieros
    y Consola Tributaria DGI VET (Paso 17.1).
    Valida:
    1. FinancialStatementsService:
       - Balanza de Comprobación: sum_debits == sum_credits, is_balanced == True.
       - Estado de Resultados P&L: desglose de ingresos, costos, gastos de nómina, utilidad neta y margen %.
       - Balance General: Activo == Pasivo + Patrimonio + Resultado del Ejercicio (is_balanced == True).
    2. NicaraguaDgiTaxService:
       - Liquidación IVA 15% (Débito vs Crédito).
       - Retenciones IR e IMI, Retención Salarial Ley 822 y Cargas Sociales INSS/INATEC.
       - Generación y descarga CSV formato VET DGI (HTTP 200, headers oficiales técnicos).
    3. Vista de Impresión Oficial (/finance/statements/print/) con firmas CPA y Representante Legal.
    4. Matriz de Autoridad Jerárquica RBAC:
       - Niveles 20 (Técnico), 50 (Ingeniero), 70 (Oficial SST) y 80 (Coordinador) -> HTTP 403 Forbidden.
       - Niveles 85 (Gerente Comercial) y 100 (Owner / Administrador) -> HTTP 200 OK.
    """

    def setUp(self):
        # 1. Configuración de Jurisdicción Fiscal Nicaragua
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

        # 2. Organización
        self.org = Organization.objects.create(
            name="SERMANSA Ingeniería S.A.",
            legal_name="Servicios y Mantenimiento Electromecánico S.A.",
            slug="sermansa-audit-finance",
            tax_id="J0310000311223",
            country=self.country_ni,
            enabled_modules=['crm', 'operations', 'sst', 'inventory', 'finance', 'billing', 'payroll', 'purchasing'],
            is_active=True,
        )

        # Catálogo de Cuentas Contables Oficial
        ensure_default_chart_of_accounts(self.org)
        ensure_payroll_accounting_accounts(self.org)
        self.acc_bank = AccountingAccount.objects.get(organization=self.org, code='1102-01')
        self.acc_ar = AccountingAccount.objects.get(organization=self.org, code='1103-01')
        self.acc_tax_credit = AccountingAccount.objects.get(organization=self.org, code='1104-01')
        self.acc_ap = AccountingAccount.objects.get(organization=self.org, code='2101-01')
        self.acc_tax_payable = AccountingAccount.objects.get(organization=self.org, code='2102-01')
        self.acc_salaries_payable = AccountingAccount.objects.get(organization=self.org, code='2104-01')
        self.acc_inss_payable = AccountingAccount.objects.get(organization=self.org, code='2104-02')
        self.acc_ir_withheld = AccountingAccount.objects.get(organization=self.org, code='2104-03')
        self.acc_provisions_payable = AccountingAccount.objects.get(organization=self.org, code='2104-04')
        self.acc_capital = AccountingAccount.objects.get(organization=self.org, code='3101-01')
        self.acc_sales = AccountingAccount.objects.get(organization=self.org, code='4101-01')
        self.acc_cost_services = AccountingAccount.objects.get(organization=self.org, code='5101-01')
        self.acc_exp_salaries = AccountingAccount.objects.get(organization=self.org, code='5201-01')
        self.acc_exp_inss = AccountingAccount.objects.get(organization=self.org, code='5201-02')
        self.acc_exp_provisions = AccountingAccount.objects.get(organization=self.org, code='5201-03')

        # 3. Usuarios de la Matriz RBAC
        # Nivel 100: Owner / Admin
        self.user_owner = User.objects.create_user(
            email='owner.fin@sermansa.com.ni',
            full_name='Ing. Roberto Meza (Owner)',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            user=self.user_owner,
            organization=self.org,
            role_level=RoleLevel.OWNER_ADMIN,
            job_title='Director General',
            is_active=True,
            is_default_org=True,
        )

        # Nivel 85: Gerente Comercial
        self.user_comm_mgr = User.objects.create_user(
            email='comercial.fin@sermansa.com.ni',
            full_name='Lic. Sofia Valenzuela (Gerente Comercial)',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            user=self.user_comm_mgr,
            organization=self.org,
            role_level=RoleLevel.COMMERCIAL_MGR,
            job_title='Gerente Comercial',
            is_active=True,
            is_default_org=True,
        )

        # Nivel 80: Coordinador Operativo
        self.user_coord = User.objects.create_user(
            email='coord.fin@sermansa.com.ni',
            full_name='Ing. Carlos Pineda (Coordinador)',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            user=self.user_coord,
            organization=self.org,
            role_level=RoleLevel.OPERATIONS_COORD,
            job_title='Coordinador de Proyectos',
            is_active=True,
            is_default_org=True,
        )

        # Nivel 70: Oficial SST
        self.user_sst = User.objects.create_user(
            email='sst.fin@sermansa.com.ni',
            full_name='Lic. Valeria Solís (Oficial SST)',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            user=self.user_sst,
            organization=self.org,
            role_level=RoleLevel.SST_OFFICER,
            job_title='Oficial SST',
            is_active=True,
            is_default_org=True,
        )

        # Nivel 50: Ingeniero Residente
        self.user_engineer = User.objects.create_user(
            email='eng.fin@sermansa.com.ni',
            full_name='Ing. Andrea Salgado (Residente)',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            user=self.user_engineer,
            organization=self.org,
            role_level=RoleLevel.ENGINEER,
            job_title='Ingeniero Residente',
            is_active=True,
            is_default_org=True,
        )

        # Nivel 20: Técnico
        self.user_tech = User.objects.create_user(
            email='tech.fin@sermansa.com.ni',
            full_name='Armando Solís (Técnico)',
            password='Password123!',
        )
        OrganizationMembership.objects.create(
            user=self.user_tech,
            organization=self.org,
            role_level=RoleLevel.TECHNICIAN,
            job_title='Técnico Electricista',
            is_active=True,
            is_default_org=True,
        )

        # 4. Asientos Contables de Partida Doble Cuadrados
        today = timezone.now().date()
        self.today = today

        # Asiento 1: Aporte de Capital Inicial (C$ 500,000.00)
        entry_cap = JournalEntry.objects.create(
            organization=self.org,
            number="POL-2026-CAPITAL",
            date=today,
            concept="Aporte inicial de capital de los socios",
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
            created_by=self.user_owner,
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_cap,
            account=self.acc_bank,
            debit=Decimal('500000.00'),
            credit=Decimal('0.00'),
            concept="Ingreso a cuenta bancaria",
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_cap,
            account=self.acc_capital,
            debit=Decimal('0.00'),
            credit=Decimal('500000.00'),
            concept="Constitución de Capital Social",
        )
        entry_cap.verify_and_save_balance()

        # Asiento 2: Facturación de Servicios de Montaje (C$ 200,000.00 + IVA C$ 30,000.00)
        entry_sales = JournalEntry.objects.create(
            organization=self.org,
            number="POL-2026-VENTAS",
            date=today,
            concept="Facturación de servicios de montaje electromecánico",
            entry_type=JournalEntry.EntryType.SALES,
            created_by=self.user_owner,
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_sales,
            account=self.acc_ar,
            debit=Decimal('230000.00'),
            credit=Decimal('0.00'),
            concept="Cuentas por cobrar comerciales cliente",
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_sales,
            account=self.acc_sales,
            debit=Decimal('0.00'),
            credit=Decimal('200000.00'),
            concept="Ingresos por servicios de montaje",
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_sales,
            account=self.acc_tax_payable,
            debit=Decimal('0.00'),
            credit=Decimal('30000.00'),
            concept="IVA por pagar 15% débito fiscal",
        )
        entry_sales.verify_and_save_balance()

        # Asiento 3: Costos Operativos de Subcontrato (C$ 30,000.00 + IVA Acreditable C$ 4,500.00)
        entry_cost = JournalEntry.objects.create(
            organization=self.org,
            number="POL-2026-COSTOS",
            date=today,
            concept="Servicios de grúa y logística de subcontratista",
            entry_type=JournalEntry.EntryType.PURCHASES,
            created_by=self.user_owner,
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_cost,
            account=self.acc_cost_services,
            debit=Decimal('30000.00'),
            credit=Decimal('0.00'),
            concept="Costo directo por servicio de grúas",
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_cost,
            account=self.acc_tax_credit,
            debit=Decimal('4500.00'),
            credit=Decimal('0.00'),
            concept="IVA Crédito fiscal acreditable 15%",
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_cost,
            account=self.acc_ap,
            debit=Decimal('0.00'),
            credit=Decimal('34500.00'),
            concept="Cuentas por pagar proveedores",
        )
        entry_cost.verify_and_save_balance()

        # Asiento 4: Póliza de Nómina (Sueldos C$ 60,000.00, Cargas Patronales C$ 14,700.00, Provisiones C$ 10,000.00)
        entry_payroll = JournalEntry.objects.create(
            organization=self.org,
            number="POL-2026-NOMINA",
            date=today,
            concept="Póliza contable devengado quincenal nómina",
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
            created_by=self.user_owner,
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_payroll,
            account=self.acc_exp_salaries,
            debit=Decimal('60000.00'),
            credit=Decimal('0.00'),
            concept="Gasto de sueldos y salarios quincenales",
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_payroll,
            account=self.acc_exp_inss,
            debit=Decimal('14700.00'),
            credit=Decimal('0.00'),
            concept="Cargas sociales INSS patronal 22.5% e INATEC 2%",
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_payroll,
            account=self.acc_exp_provisions,
            debit=Decimal('10000.00'),
            credit=Decimal('0.00'),
            concept="Provisiones laborales aguinaldo, indemnización y vacaciones",
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_payroll,
            account=self.acc_salaries_payable,
            debit=Decimal('0.00'),
            credit=Decimal('51800.00'),
            concept="Sueldos netos por transferir",
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_payroll,
            account=self.acc_inss_payable,
            debit=Decimal('0.00'),
            credit=Decimal('18900.00'),
            concept="Retenciones y cargas INSS/INATEC por enterar",
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_payroll,
            account=self.acc_ir_withheld,
            debit=Decimal('0.00'),
            credit=Decimal('4000.00'),
            concept="Retenciones IR salarial Art. 23 Ley 822",
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry_payroll,
            account=self.acc_provisions_payable,
            debit=Decimal('0.00'),
            credit=Decimal('10000.00'),
            concept="Pasivo provisiones laborales acumuladas MITRAB",
        )
        entry_payroll.verify_and_save_balance()

        # 5. Documentos Comerciales y de Nómina para el Motor Tributario DGI
        self.client_acc = Account.objects.create(
            organization=self.org,
            commercial_name="Minera de Nicaragua S.A.",
            legal_name="Minera de Nicaragua S.A.",
            tax_id="J0310000223344",
        )
        self.invoice = Invoice.objects.create(
            organization=self.org,
            number="FAC-2026-0001",
            account=self.client_acc,
            status=Invoice.Status.ISSUED,
            issue_date=today,
            subtotal=Decimal('200000.00'),
            apply_tax=True,
            tax_total=Decimal('30000.00'),
            gross_total=Decimal('230000.00'),
            ir_retention_amount=Decimal('4000.00'),
            imi_retention_amount=Decimal('2000.00'),
            net_receivable=Decimal('224000.00'),
        )
        self.payroll_run = PayrollPeriod.objects.create(
            organization=self.org,
            number="NOM-2026-0001",
            period_name="Planilla Quincenal Octubre 2026",
            status=PayrollPeriod.Status.CLOSED,
            start_date=today,
            end_date=today,
            payment_date=today,
            total_gross_salary=Decimal('60000.00'),
            total_inss_employee=Decimal('4200.00'),
            total_ir_tax=Decimal('4000.00'),
            total_net_salary=Decimal('51800.00'),
            total_inss_employer=Decimal('13500.00'),
            total_inatec=Decimal('1200.00'),
            total_provisions=Decimal('10000.00'),
            total_company_cost=Decimal('84700.00'),
        )

        self.supplier = Supplier.objects.create(
            organization=self.org,
            commercial_name="Suministros Eléctricos S.A.",
            legal_name="Suministros Eléctricos S.A.",
            tax_id="J0310000445566",
        )
        self.warehouse = Warehouse.objects.create(
            organization=self.org,
            code="BOD-01",
            name="Bodega Central Managua",
        )
        self.purchase_order = PurchaseOrder.objects.create(
            organization=self.org,
            number="OC-2026-0001",
            supplier=self.supplier,
            target_warehouse=self.warehouse,
            supplier_invoice_number="FAC-SUM-1002",
            status=PurchaseOrder.Status.RECEIVED,
            issue_date=today,
            apply_tax=True,
            subtotal=Decimal('30000.00'),
            tax_total=Decimal('4500.00'),
            gross_total=Decimal('34500.00'),
            ir_withholding_rate=Decimal('2.00'),
            ir_withheld_amount=Decimal('600.00'),
            imi_withholding_rate=Decimal('1.00'),
            imi_withheld_amount=Decimal('300.00'),
            net_payable=Decimal('33600.00'),
        )

    def test_financial_statements_service_trial_balance(self):
        """
        Verifica que la Balanza de Comprobación sume exactamente débitos y créditos
        y reporte is_balanced == True sin descuadre.
        """
        tb = FinancialStatementsService.get_trial_balance(self.org)
        self.assertTrue(tb['is_balanced'])
        self.assertEqual(tb['difference'], Decimal('0.00'))
        self.assertEqual(tb['sum_debits'], Decimal('849200.00'))
        self.assertEqual(tb['sum_credits'], Decimal('849200.00'))
        self.assertGreater(len(tb['accounts']), 0)

    def test_financial_statements_service_income_statement(self):
        """
        Verifica el cálculo del Estado de Resultados (P&L):
        Ventas - Costos Operativos = Utilidad Bruta
        Utilidad Bruta - Gastos Nómina = Utilidad Neta
        """
        pnl = FinancialStatementsService.get_income_statement(self.org)
        self.assertEqual(pnl['total_revenue'], Decimal('200000.00'))
        self.assertEqual(pnl['total_operational_costs'], Decimal('30000.00'))
        self.assertEqual(pnl['gross_profit'], Decimal('170000.00'))
        self.assertEqual(pnl['total_payroll_expenses'], Decimal('84700.00'))
        self.assertEqual(pnl['total_operating_expenses'], Decimal('114700.00'))
        self.assertEqual(pnl['net_income'], Decimal('85300.00'))
        self.assertEqual(pnl['net_margin_pct'], Decimal('42.65'))

    def test_financial_statements_service_balance_sheet(self):
        """
        Verifica que el Balance General cumpla estrictamente la Ecuación Contable:
        Activo (C$ 734,500.00) == Pasivo (C$ 149,200.00) + Patrimonio (C$ 585,300.00)
        """
        bs = FinancialStatementsService.get_balance_sheet(self.org)
        self.assertEqual(bs['total_assets'], Decimal('734500.00'))
        self.assertEqual(bs['total_liabilities'], Decimal('149200.00'))
        self.assertEqual(bs['total_equity_and_results'], Decimal('585300.00'))
        self.assertEqual(bs['accounting_equation_diff'], Decimal('0.00'))
        self.assertTrue(bs['is_balanced'])

    def test_nicaragua_dgi_tax_service_consolidation(self):
        """
        Verifica la consolidación tributaria DGI Nicaragua:
        - IVA Débito de facturación
        - Retención IR Salarial (Ley 822)
        - Cargas Sociales INSS e INATEC
        """
        tax = NicaraguaDgiTaxService.get_tax_summary(
            self.org,
            year=self.today.year,
            month=self.today.month,
        )
        self.assertEqual(tax['iva_debit_sales'], Decimal('30000.00'))
        self.assertEqual(tax['iva_credit_purchases'], Decimal('4500.00'))
        self.assertEqual(tax['net_iva_payable'], Decimal('25500.00'))
        self.assertEqual(tax['ir_professional_and_goods'], Decimal('600.00'))
        self.assertEqual(tax['imi_municipal_tax'], Decimal('300.00'))
        self.assertEqual(tax['ir_salary_withholding'], Decimal('4000.00'))
        self.assertEqual(tax['inss_inatec_payable'], Decimal('18900.00'))
        self.assertEqual(tax['total_dgi_payable'], Decimal('30100.00'))
        self.assertEqual(tax['invoices_count'], 1)
        self.assertEqual(tax['purchases_count'], 1)
        self.assertEqual(tax['payrolls_count'], 1)

    def test_dgi_vet_csv_export_format_and_response(self):
        """
        Verifica la exportación formal CSV para la Ventanilla Electrónica Tributaria (VET):
        - Header Content-Type text/csv
        - Content-Disposition attachment con RUC
        - Encabezados técnicos oficiales
        """
        client = Client()
        client.force_login(self.user_comm_mgr)
        resp = client.get(reverse('finance:dgi_vet_export_csv'))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp['Content-Type'].startswith('text/csv'))
        self.assertIn('SABANA_VET_DGI_J0310000311223', resp['Content-Disposition'])

        csv_content = resp.content.decode('utf-8')
        # Encabezados
        self.assertIn('RUC_CONTRIBUYENTE', csv_content)
        self.assertIn('RAZON_SOCIAL', csv_content)
        self.assertIn('FECHA_DOCUMENTO', csv_content)
        self.assertIn('TIPO_OPERACION', csv_content)
        self.assertIn('FOLIO_FACTURA_O_NOMINA', csv_content)
        self.assertIn('SUBTOTAL_GRAVADO_NIO', csv_content)
        self.assertIn('IVA_15_NIO', csv_content)
        self.assertIn('RETENCION_IR_NIO', csv_content)
        self.assertIn('RETENCION_IMI_NIO', csv_content)
        self.assertIn('TOTAL_NETO_NIO', csv_content)
        # Registros
        self.assertIn('FAC-2026-0001', csv_content)
        self.assertIn('FAC-SUM-1002', csv_content)
        self.assertIn('NOM-2026-0001', csv_content)
        self.assertIn('J0310000311223', csv_content)

    def test_financial_statements_print_view(self):
        """
        Verifica que la vista oficial de impresión (/finance/statements/print/)
        responda HTTP 200 y contenga los bloques de certificación CPA y Representante Legal.
        """
        client = Client()
        client.force_login(self.user_owner)
        resp = client.get(reverse('finance:statements_print'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "SERMANSA Ingeniería S.A.")
        self.assertContains(resp, "Estado de Resultados Integral (P&L)")
        self.assertContains(resp, "Balance General (Estado de Situación Financiera)")
        self.assertContains(resp, "Contador Público Autorizado")
        self.assertContains(resp, "Representante Legal")

    def test_financial_and_tax_views_rbac_security_matrix(self):
        """
        Verifica la Matriz de Seguridad y Confidencialidad RBAC:
        - Roles Nivel < 85 (Técnico 20, Ingeniero 50, Oficial SST 70, Coordinador 80) -> HTTP 403 Forbidden.
        - Roles Nivel >= 85 (Gerente Comercial 85, Owner/Admin 100) -> HTTP 200 OK.
        """
        endpoints = [
            reverse('finance:statements'),
            reverse('finance:statements_print'),
            reverse('finance:dgi_vet'),
            reverse('finance:dgi_vet_export_csv'),
        ]

        forbidden_users = [
            self.user_tech,       # Nivel 20
            self.user_engineer,   # Nivel 50
            self.user_sst,        # Nivel 70
            self.user_coord,      # Nivel 80
        ]

        authorized_users = [
            self.user_comm_mgr,   # Nivel 85
            self.user_owner,      # Nivel 100
        ]

        # Validar 403 Forbidden para roles sin autoridad
        for user in forbidden_users:
            client = Client()
            client.force_login(user)
            for url in endpoints:
                resp = client.get(url)
                self.assertEqual(
                    resp.status_code,
                    403,
                    f"Fallo de seguridad RBAC: Usuario {user.email} con rol inferior a 85 pudo acceder a {url}",
                )

        # Validar 200 OK para roles autorizados
        for user in authorized_users:
            client = Client()
            client.force_login(user)
            for url in endpoints:
                resp = client.get(url)
                self.assertEqual(
                    resp.status_code,
                    200,
                    f"Fallo de acceso: Usuario {user.email} con rol {user.memberships.first().role_level} no pudo acceder a {url}",
                )

