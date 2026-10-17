"""
Suite de Pruebas de Regresión R-01 — Ciclo de Vida y Reseteo Seguro del Sandbox
Fase 1C.8.4.1: Diagnóstico y Corrección Controlada del Sandbox Seeder

Verifica:
1. Recreación e idempotencia del seeder sin colisión con triggers PostgreSQL.
2. Purga limpia sin datos huérfanos ni estados inconsistentes.
3. Preservación estricta de las garantías de inmutabilidad contable (H-07 a H-13).
"""
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.db import connection, IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import CountryConfig
from apps.core.services.sandbox_seeder import SandboxScenarioSeeder
from apps.crm.models import Account, Opportunity, Quote
from apps.finance.models import (
    BankAccount,
    FinancialTransaction,
    PettyCashExpense,
    PettyCashFund,
    PettyCashReplenishment,
    JournalEntry,
    JournalEntryLine,
    AccountingAccount,
)
from apps.inventory.models import InventoryItem, StockBalance, StockMovement, Warehouse
from apps.operations.models import Crew, WorkOrder
from apps.purchasing.models import PurchaseOrder, PurchaseOrderItem, Supplier
from apps.sst.models import EPPDelivery, LeaveRequest, SafetyChecklist
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel

User = get_user_model()


class SandboxLifecycleRegressionTestCase(TestCase):
    """
    Casos de prueba obligatorios de la Fase 1C.8.4.1 (R-01).
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
            email='evaluador.r01@eternallabs.ni',
            password='Password123!',
            full_name='Ing. Auditor R01',
        )
        self.organization = Organization.objects.create(
            name='SOLUCIONES SANDBOX R01 S.A.',
            legal_name='Soluciones Industriales Sandbox R01 S.A.',
            tax_id='J0310000777111',
            country=self.country_ni,
            currency=Organization.Currency.NIO,
            tax_regime=Organization.TaxRegime.GENERAL,
            industry=Organization.Industry.SERVICES,
            accounting_template=Organization.AccountingTemplate.NIIF_PYMES,
            enabled_modules=['crm', 'operations', 'sst', 'inventory', 'finance', 'purchasing', 'billing', 'payroll'],
        )
        self.membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.owner,
            role_level=RoleLevel.OWNER_ADMIN,
            job_title='Director General',
            is_active=True,
        )

    def test_r01_01_sandbox_seeder_lifecycle_populate_and_reset(self):
        """1. Una organización sandbox puede poblarse y vaciarse conforme al comportamiento previsto."""
        stats = SandboxScenarioSeeder.populate_for_organization(self.organization, self.owner)
        self.assertGreaterEqual(stats['accounts_count'], 5)
        self.assertGreaterEqual(stats['warehouses_count'], 3)
        self.assertGreaterEqual(PurchaseOrder.objects.filter(organization=self.organization).count(), 2)

        # Ejecución del reseteo
        SandboxScenarioSeeder.reset_organization_data(self.organization)

        # La organización y el Owner permanecen intactos
        self.assertTrue(Organization.objects.filter(id=self.organization.id).exists())
        self.assertEqual(OrganizationMembership.objects.filter(organization=self.organization).count(), 1)
        self.assertEqual(OrganizationMembership.objects.filter(organization=self.organization).first().user, self.owner)

    def test_r01_02_sandbox_idempotent_multiple_runs(self):
        """2. Ejecuciones sucesivas de populate_for_organization son estrictamente idempotentes."""
        SandboxScenarioSeeder.populate_for_organization(self.organization, self.owner)
        initial_accounts = Account.objects.filter(organization=self.organization).count()
        initial_wos = WorkOrder.objects.filter(organization=self.organization).count()
        initial_pos = PurchaseOrder.objects.filter(organization=self.organization).count()

        # Segunda y tercera ejecución consecutiva
        SandboxScenarioSeeder.populate_for_organization(self.organization, self.owner)
        SandboxScenarioSeeder.populate_for_organization(self.organization, self.owner)

        self.assertEqual(Account.objects.filter(organization=self.organization).count(), initial_accounts)
        self.assertEqual(WorkOrder.objects.filter(organization=self.organization).count(), initial_wos)
        self.assertEqual(PurchaseOrder.objects.filter(organization=self.organization).count(), initial_pos)

    def test_r01_03_no_orphaned_data_after_reset(self):
        """3. El vaciado no deja datos operativos huérfanos ni estados parcialmente guardados."""
        SandboxScenarioSeeder.populate_for_organization(self.organization, self.owner)
        SandboxScenarioSeeder.reset_organization_data(self.organization)

        self.assertEqual(Account.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(Opportunity.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(Quote.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(Crew.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(WorkOrder.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(SafetyChecklist.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(EPPDelivery.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(LeaveRequest.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(Warehouse.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(InventoryItem.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(StockBalance.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(StockMovement.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(PurchaseOrder.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(PurchaseOrderItem.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(Supplier.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(BankAccount.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(PettyCashFund.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(PettyCashExpense.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(PettyCashReplenishment.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(FinancialTransaction.objects.filter(organization=self.organization).count(), 0)

    def test_r01_04_production_accounting_invariants_remain_protected_against_tampering(self):
        """4. Demuestra que si se crea una póliza cerrada (is_balanced=True), los triggers PostgreSQL siguen protegiéndola."""
        if connection.vendor != 'postgresql':
            self.skipTest("Requiere motor PostgreSQL para triggers nativos de inmutabilidad.")

        entry = JournalEntry.objects.create(
            organization=self.organization,
            date=timezone.now().date(),
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
            concept="Póliza Cerrada Formal de Producción",
            reference="POL-PROD-99",
            created_by=self.owner,
            is_balanced=False,
        )
        account = AccountingAccount.objects.create(
            organization=self.organization,
            code="1101-99",
            name="Caja de Prueba",
            account_type=AccountingAccount.AccountType.ASSET,
        )
        line = JournalEntryLine.objects.create(
            organization=self.organization,
            entry=entry,
            account=account,
            concept="Línea Débito",
            debit=Decimal('100.00'),
            credit=Decimal('0.00'),
        )
        # Sellado como balanceado (cierre formal contable)
        entry.is_balanced = True
        entry.save(update_fields=['is_balanced'])

        # Intentar eliminar directamente la línea protegida debe ser rechazado por el trigger
        from django.db import DatabaseError, transaction
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM finance_journalentryline WHERE id = %s", [str(line.id)])

        # Intentar eliminar directamente la póliza protegida debe ser rechazado por el trigger
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM finance_journalentry WHERE id = %s", [str(entry.id)])

    def test_r01_05_interactive_reset_view_owner_succeeds(self):
        """5. La vista interactiva de reseteo sandbox vía HTTP POST se ejecuta sin errores para el Owner."""
        self.client.force_login(self.owner)
        session = self.client.session
        session['active_org_id'] = str(self.organization.id)
        session.save()

        # Primero cargar el escenario
        response_load = self.client.post(reverse('tenancy:sandbox_load'), follow=True)
        self.assertEqual(response_load.status_code, 200)

        # Ahora vaciar el escenario
        response_reset = self.client.post(reverse('tenancy:sandbox_reset'), follow=True)
        self.assertEqual(response_reset.status_code, 200)
        self.assertContains(response_reset, "Datos operativos de prueba vaciados")
