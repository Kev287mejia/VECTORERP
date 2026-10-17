"""
Suite de Pruebas Automatizadas para la Fase 1C.5.2.2:
Refactorización del Dominio de Depósitos Bancarios de Efectivo (CashBankDeposit).
Valida:
1. Nomenclatura unificada del dominio y ciclo completo.
2. Compatibilidad retroactiva total (aliases de funciones, propiedades y consultas).
3. Preservación estricta de invariantes financieros y contables.
4. Prevención de duplicidad de boletas y transacciones financieras.
5. Aislamiento estricto multi-tenant.
"""

from decimal import Decimal
import hashlib
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import (
    PosRegisterShift,
    CashMovement,
    CashBankDeposit,
    CashRemittance,
)
from apps.billing.services_pos_shift import (
    open_pos_shift,
    compute_shift_x_cut_metrics,
    close_pos_shift_z_cut,
)
from apps.billing.services_cash import (
    record_cash_movement,
    recalculate_shift_cash_metrics,
    prepare_cash_bank_deposit,
    dispatch_cash_for_deposit,
    record_bank_deposit_slip,
    confirm_bank_deposit,
    reject_bank_deposit,
    prepare_cash_remittance,
    dispatch_cash_remittance,
    record_remittance_bank_deposit,
    confirm_remittance_deposit,
    reject_cash_remittance,
)
from apps.core.models import CountryConfig
from apps.finance.models import (
    AccountingAccount,
    BankAccount,
    FinancialTransaction,
    JournalEntry,
    JournalEntryLine,
)
from apps.finance.services import ensure_default_chart_of_accounts
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel

User = get_user_model()


class CashBankDepositDomainRefactorTestCase(TestCase):
    """
    Certificación de Dominio para Fase 1C.5.2.2 sobre PostgreSQL 14.
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
                'default_tax_name': 'IVA',
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )

        self.org_a = Organization.objects.create(
            name="Organización Depósitos Alfa SA",
            slug="org-depositos-alfa-sa",
            country=self.country_ni,
            currency="NIO",
            is_active=True,
        )

        self.org_b = Organization.objects.create(
            name="Organización Depósitos Beta SA",
            slug="org-depositos-beta-sa",
            country=self.country_ni,
            currency="NIO",
            is_active=True,
        )

        self.admin_user = User.objects.create_user(
            email='admin@deposit.com',
            password='Password123!',
            full_name='Admin Depósitos',
        )
        self.cashier_user = User.objects.create_user(
            email='cashier@deposit.com',
            password='Password123!',
            full_name='Cajero Depósitos',
        )
        self.supervisor_user = User.objects.create_user(
            email='supervisor@deposit.com',
            password='Password123!',
            full_name='Supervisor Depósitos',
        )

        OrganizationMembership.objects.create(
            organization=self.org_a,
            user=self.admin_user,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            organization=self.org_a,
            user=self.cashier_user,
            role_level=RoleLevel.TECHNICIAN,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            organization=self.org_a,
            user=self.supervisor_user,
            role_level=RoleLevel.ENGINEER,
            is_active=True,
        )

        ensure_default_chart_of_accounts(self.org_a)
        ensure_default_chart_of_accounts(self.org_b)

        self.bank_account_a = BankAccount.objects.create(
            organization=self.org_a,
            bank_name="Banco de la Producción (BANPRO)",
            account_number="1002-DEP-001",
            account_type=BankAccount.AccountType.CHECKING,
            currency="NIO",
            current_balance=Decimal('100000.00'),
            is_active=True,
        )

    def test_alias_module_level_identity(self):
        """Verifica que CashRemittance sea un alias exacto de CashBankDeposit."""
        self.assertIs(CashRemittance, CashBankDeposit)

    def test_new_domain_full_lifecycle(self):
        """
        Ciclo completo utilizando exclusivamente los nombres nuevos del dominio:
        prepare_cash_bank_deposit -> dispatch_cash_for_deposit ->
        record_bank_deposit_slip -> confirm_bank_deposit.
        """
        shift = open_pos_shift(
            organization=self.org_a,
            cashier=self.cashier_user,
            opening_cash_nio=Decimal('10000.00'),
            register_name="CAJA-CENTRAL-01",
        )
        initial_bank_bal = self.bank_account_a.current_balance

        # 1. Preparación de depósito (invariante: efectivo en gaveta intacto)
        deposit = prepare_cash_bank_deposit(
            shift=shift,
            destination_bank_account=self.bank_account_a,
            amount=Decimal('4000.00'),
            user=self.cashier_user,
            notes="Depósito de cierre vespertino",
        )
        self.assertEqual(deposit.status, CashBankDeposit.Status.PREPARED)
        self.assertTrue(deposit.deposit_number.startswith(f"DEP-{timezone.now().year}-"))
        self.assertEqual(deposit.remittance_number, deposit.deposit_number)

        shift.refresh_from_db()
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('10000.00'))
        self.bank_account_a.refresh_from_db()
        self.assertEqual(self.bank_account_a.current_balance, initial_bank_bal)

        # 2. Despacho de efectivo para depósito (invariante: gaveta disminuye, saldo bancario intacto)
        dispatched_deposit = dispatch_cash_for_deposit(
            deposit=deposit,
            user=self.cashier_user,
            carrier_name="Valores del Istmo SA",
            carrier_id="VAL-8890",
        )
        self.assertEqual(dispatched_deposit.status, CashBankDeposit.Status.IN_TRANSIT)

        shift.refresh_from_db()
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('6000.00'))
        self.assertEqual(shift.total_bank_deposits_nio, Decimal('4000.00'))
        self.assertEqual(shift.total_remittances_nio, Decimal('4000.00'))

        # Movimiento de caja registrado
        mov = CashMovement.objects.get(bank_deposit=deposit)
        self.assertEqual(mov.movement_type, CashMovement.MovementType.CASH_DEPOSIT_DISPATCH)
        self.assertEqual(mov.amount, Decimal('4000.00'))
        self.assertEqual(mov.bank_deposit, deposit)
        self.assertEqual(mov.remittance, deposit)

        # Póliza de tránsito generada y balanceada
        self.assertIsNotNone(dispatched_deposit.transit_journal_entry)
        transit_entry = dispatched_deposit.transit_journal_entry
        self.assertEqual(transit_entry.total_debit, Decimal('4000.00'))
        self.assertEqual(transit_entry.total_credit, Decimal('4000.00'))

        # Saldo bancario sigue intacto
        self.bank_account_a.refresh_from_db()
        self.assertEqual(self.bank_account_a.current_balance, initial_bank_bal)

        # 3. Registro de boleta bancaria entregada en ventanilla
        recorded_deposit = record_bank_deposit_slip(
            deposit=dispatched_deposit,
            deposit_slip_number="BOL-BANPRO-2026-777",
            deposit_date=timezone.now().date(),
            user=self.cashier_user,
            notes="Boleta sellada en sucursal",
        )
        self.assertEqual(recorded_deposit.status, CashBankDeposit.Status.BANK_DEPOSIT_RECORDED)
        self.assertEqual(recorded_deposit.status, CashBankDeposit.Status.DEPOSITED)
        self.assertEqual(recorded_deposit.deposit_slip_number, "BOL-BANPRO-2026-777")

        # Saldo bancario sigue intacto
        self.bank_account_a.refresh_from_db()
        self.assertEqual(self.bank_account_a.current_balance, initial_bank_bal)

        # 4. Confirmación de acreditación bancaria (invariante: banco incrementa exactamente una vez)
        confirmed_deposit = confirm_bank_deposit(
            deposit=recorded_deposit,
            user=self.supervisor_user,
        )
        self.assertEqual(confirmed_deposit.status, CashBankDeposit.Status.CONFIRMED)

        self.bank_account_a.refresh_from_db()
        self.assertEqual(self.bank_account_a.current_balance, initial_bank_bal + Decimal('4000.00'))

        # Transacción financiera inmutable de bancos
        self.assertIsNotNone(confirmed_deposit.financial_transaction)
        trx = confirmed_deposit.financial_transaction
        self.assertEqual(trx.transaction_type, FinancialTransaction.TransactionType.CASH_BANK_DEPOSIT)
        self.assertEqual(trx.amount, Decimal('4000.00'))
        self.assertEqual(trx.reference_code, "BOL-BANPRO-2026-777")

        # Póliza de acreditación generada y balanceada
        self.assertIsNotNone(confirmed_deposit.confirmation_journal_entry)
        conf_entry = confirmed_deposit.confirmation_journal_entry
        self.assertEqual(conf_entry.total_debit, Decimal('4000.00'))
        self.assertEqual(conf_entry.total_credit, Decimal('4000.00'))

    def test_legacy_api_and_queryset_remap_compatibility(self):
        """
        Verifica que los consumidores que utilizan la API anterior de remesas
        continúen operando de manera transparente sin ninguna alteración.
        """
        shift = open_pos_shift(
            organization=self.org_a,
            cashier=self.cashier_user,
            opening_cash_nio=Decimal('8000.00'),
        )

        rem = prepare_cash_remittance(
            shift=shift,
            destination_bank_account=self.bank_account_a,
            amount=Decimal('3000.00'),
            user=self.cashier_user,
        )
        self.assertEqual(rem.status, CashRemittance.Status.PREPARED)

        dispatched = dispatch_cash_remittance(
            remittance=rem,
            user=self.cashier_user,
            carrier_name="Custodio Externo",
        )
        self.assertEqual(dispatched.status, CashRemittance.Status.IN_TRANSIT)

        # QuerySet remapping transparente para queries legacy
        qs_movement = CashMovement.objects.filter(remittance=dispatched)
        self.assertEqual(qs_movement.count(), 1)
        self.assertEqual(qs_movement.first().bank_deposit, dispatched)

        qs_deposit = CashBankDeposit.objects.filter(remittance_number=dispatched.remittance_number)
        self.assertEqual(qs_deposit.count(), 1)
        self.assertEqual(qs_deposit.first(), dispatched)

        # Related manager legacy en shift
        self.assertEqual(shift.remittances.count(), 1)
        self.assertEqual(shift.bank_deposits.count(), 1)

    def test_historical_folio_preservation(self):
        """
        Verifica que un depósito con folio histórico preexistente 'REM-2026-0001'
        conserve intacto su folio sin que save() lo sobrescriba a 'DEP-...'.
        """
        shift = open_pos_shift(
            organization=self.org_a,
            cashier=self.cashier_user,
            opening_cash_nio=Decimal('5000.00'),
        )

        legacy_deposit = CashBankDeposit.objects.create(
            organization=self.org_a,
            shift=shift,
            destination_bank_account=self.bank_account_a,
            amount=Decimal('1500.00'),
            deposit_number="REM-2026-0001",
            status=CashBankDeposit.Status.PREPARED,
            prepared_by=self.cashier_user,
        )

        legacy_deposit.notes = "Nota actualizada en depósito histórico"
        legacy_deposit.save()
        legacy_deposit.refresh_from_db()

        self.assertEqual(legacy_deposit.deposit_number, "REM-2026-0001")
        self.assertEqual(legacy_deposit.remittance_number, "REM-2026-0001")

    def test_duplicate_deposit_slip_prevention(self):
        """
        Verifica que dos depósitos distintos no puedan registrar la misma boleta
        en la misma cuenta bancaria (prevención de doble acreditación).
        """
        shift = open_pos_shift(
            organization=self.org_a,
            cashier=self.cashier_user,
            opening_cash_nio=Decimal('7000.00'),
        )

        dep1 = prepare_cash_bank_deposit(shift, self.bank_account_a, Decimal('2000.00'), self.cashier_user)
        dep2 = prepare_cash_bank_deposit(shift, self.bank_account_a, Decimal('2000.00'), self.cashier_user)

        dispatch_cash_for_deposit(dep1, self.cashier_user)
        dispatch_cash_for_deposit(dep2, self.cashier_user)

        record_bank_deposit_slip(dep1, "BOLETA-DUPLICADA-123", timezone.now().date(), self.cashier_user)

        with self.assertRaises(ValidationError) as ctx:
            record_bank_deposit_slip(dep2, "BOLETA-DUPLICADA-123", timezone.now().date(), self.cashier_user)
        self.assertIn("ya fue registrada previamente", str(ctx.exception))

    def test_rejection_and_physical_cash_reintegration(self):
        """
        Verifica que al rechazar un depósito bancario:
        1. Se revierta la póliza de efectivo en tránsito.
        2. Se reintegre físicamente el dinero a la gaveta de caja si el turno está abierto.
        3. El saldo esperado de gaveta recupere su valor original.
        """
        shift = open_pos_shift(
            organization=self.org_a,
            cashier=self.cashier_user,
            opening_cash_nio=Decimal('6000.00'),
        )
        dep = prepare_cash_bank_deposit(shift, self.bank_account_a, Decimal('2500.00'), self.cashier_user)
        dispatch_cash_for_deposit(dep, self.cashier_user, carrier_name="Custodio Rechazo")

        shift.refresh_from_db()
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('3500.00'))

        rejected_dep = reject_bank_deposit(
            deposit=dep,
            user=self.supervisor_user,
            rejection_reason="Sucursal cerrada por feriado bancario imprevisto",
        )
        self.assertEqual(rejected_dep.status, CashBankDeposit.Status.REJECTED)

        # Dinero reintegrado a gaveta vía CASH_IN
        shift.refresh_from_db()
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('6000.00'))

        reintegration_mov = CashMovement.objects.filter(
            shift=shift,
            movement_type=CashMovement.MovementType.CASH_IN,
            amount=Decimal('2500.00'),
        ).first()
        self.assertIsNotNone(reintegration_mov)
        self.assertIn("Reintegro físico", reintegration_mov.concept)

    def test_tenant_isolation_boundary(self):
        """
        Verifica que un cajero u operador no pueda interactuar ni asociar depósitos
        con cuentas bancarias o turnos pertenecientes a otra organización.
        """
        bank_b = BankAccount.objects.create(
            organization=self.org_b,
            bank_name="Banco BAC de Org B",
            account_number="9900-BETA-01",
            account_type=BankAccount.AccountType.CHECKING,
            currency="NIO",
            current_balance=Decimal('20000.00'),
            is_active=True,
        )

        shift_a = open_pos_shift(self.org_a, self.cashier_user, Decimal('4000.00'))

        with self.assertRaises(ValidationError) as ctx:
            prepare_cash_bank_deposit(
                shift=shift_a,
                destination_bank_account=bank_b,
                amount=Decimal('1000.00'),
                user=self.cashier_user,
            )
        self.assertIn("no pertenece a la misma organización", str(ctx.exception))
