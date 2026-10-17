from decimal import Decimal
import threading
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, connections, DatabaseError, IntegrityError, models, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.core.models import CountryConfig
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel
from apps.finance.models import (
    AccountingAccount,
    BankAccount,
    CardBatchSettlement,
    JournalEntry,
    JournalEntryLine,
    PettyCashFund,
)
from apps.finance.services import (
    ensure_default_chart_of_accounts,
    record_card_batch_settlement,
)

User = get_user_model()


class Phase1C83CDatabaseIntegrityTestCase(TestCase):
    """
    VECTOR ERP — FASE 1C.8.3C: Cierre de Migración e Integridad a Nivel de Base de Datos.
    Suite de Verificación de Integridad en PostgreSQL:
    - H-10: Migración y protección referencial de JournalEntryLine.entry (models.PROTECT).
    - H-11: Triggers PostgreSQL para sellado hermético contra bypass de ORM (bulk_create, QuerySet.update/delete).
    - H-12: CheckConstraints a nivel de motor para límite autorizado y saldo no negativo en PettyCashFund.
    """

    def setUp(self):
        # 1. Configuración Regional
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
                'is_active': True,
                'default_tax_rate': Decimal('15.00'),
                'default_tax_name': 'IVA',
            }
        )

        # 2. Organización
        self.org = Organization.objects.create(
            name="Constructora DB Shield S.A.",
            legal_name="Constructora DB Shield S.A.",
            tax_id="J0310000009999",
            country=self.country_ni,
            is_active=True,
        )

        # 3. Usuario Auditor
        self.auditor = User.objects.create_user(
            email='db_auditor@dbshield.com',
            password='audit_secure_password_2026',
            full_name='Auditor de Base de Datos',
        )
        OrganizationMembership.objects.create(
            user=self.auditor,
            organization=self.org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )

        # 4. Cuentas Contables Básicas
        ensure_default_chart_of_accounts(self.org)
        self.acct_cash = AccountingAccount.objects.get(organization=self.org, code='1101-01')
        self.acct_bank = AccountingAccount.objects.get(organization=self.org, code='1102-01')
        self.acct_ap = AccountingAccount.objects.get(organization=self.org, code='2101-01')

    # =========================================================================
    # H-10: INTEGRIDAD REFERENCIAL Y MIGRACIÓN (models.PROTECT)
    # =========================================================================

    def test_h10_01_journal_entry_line_foreign_key_is_protected(self):
        """Verifica que el campo entry en JournalEntryLine tenga on_delete=models.PROTECT."""
        field = JournalEntryLine._meta.get_field('entry')
        self.assertEqual(field.remote_field.on_delete, models.PROTECT)

    def test_h10_02_deletion_of_entry_with_lines_triggers_protected_error(self):
        """Verifica que borrar una póliza que tiene líneas sea bloqueado por ProtectedError en Django."""
        entry = JournalEntry.objects.create(
            organization=self.org,
            number="POL-TEST-PROT-01",
            date=timezone.now().date(),
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
            concept="Póliza para prueba de borrado protegido",
            is_balanced=False,
            created_by=self.auditor,
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acct_cash,
            concept="Línea 1",
            debit=Decimal('100.00'),
            credit=Decimal('0.00'),
        )

        with self.assertRaises(ProtectedError):
            JournalEntry.objects.filter(id=entry.id).delete()

    # =========================================================================
    # H-11: TRIGGERS POSTGRESQL DE INMUTABILIDAD CONTABLE
    # =========================================================================

    def _create_balanced_entry(self, number="POL-BAL-01"):
        """Helper para crear una póliza balanceada legítima."""
        entry = JournalEntry.objects.create(
            organization=self.org,
            number=number,
            date=timezone.now().date(),
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
            concept="Póliza balanceada legítima",
            is_balanced=False,
            created_by=self.auditor,
        )
        line1 = JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acct_cash,
            concept="Cargo",
            debit=Decimal('5000.00'),
            credit=Decimal('0.00'),
        )
        line2 = JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acct_bank,
            concept="Abono",
            debit=Decimal('0.00'),
            credit=Decimal('5000.00'),
        )
        entry.is_balanced = True
        entry.total_debit = Decimal('5000.00')
        entry.total_credit = Decimal('5000.00')
        entry.save()
        return entry, line1, line2

    def test_h11_01_queryset_update_on_balanced_entry_is_blocked_by_trigger(self):
        """1. QuerySet.update() sobre una póliza contabilizada debe ser bloqueado por PostgreSQL."""
        if connection.vendor != 'postgresql':
            self.skipTest("Prueba específica de triggers nativos PostgreSQL")

        entry, _, _ = self._create_balanced_entry("POL-TRG-01")

        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                JournalEntry.objects.filter(id=entry.id).update(concept="Concepto alterado maliciosamente")

        entry.refresh_from_db()
        self.assertEqual(entry.concept, "Póliza balanceada legítima")

    def test_h11_02_queryset_delete_on_balanced_entry_is_blocked(self):
        """2. QuerySet.delete() sobre una póliza contabilizada debe ser bloqueado."""
        entry, _, _ = self._create_balanced_entry("POL-TRG-02")

        with self.assertRaises((ProtectedError, DatabaseError)):
            with transaction.atomic():
                JournalEntry.objects.filter(id=entry.id).delete()

        self.assertTrue(JournalEntry.objects.filter(id=entry.id).exists())

    def test_h11_03_queryset_update_on_balanced_line_is_blocked_by_trigger(self):
        """3. QuerySet.update() sobre una línea contabilizada debe ser bloqueado por el trigger PostgreSQL."""
        if connection.vendor != 'postgresql':
            self.skipTest("Prueba específica de triggers nativos PostgreSQL")

        entry, line1, _ = self._create_balanced_entry("POL-TRG-03")

        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                JournalEntryLine.objects.filter(id=line1.id).update(debit=Decimal('9999.00'))

        line1.refresh_from_db()
        self.assertEqual(line1.debit, Decimal('5000.00'))

    def test_h11_04_queryset_delete_on_balanced_line_is_blocked_by_trigger(self):
        """4. QuerySet.delete() sobre una línea contabilizada debe ser bloqueado por el trigger PostgreSQL."""
        if connection.vendor != 'postgresql':
            self.skipTest("Prueba específica de triggers nativos PostgreSQL")

        entry, line1, _ = self._create_balanced_entry("POL-TRG-04")

        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                JournalEntryLine.objects.filter(id=line1.id).delete()

        self.assertTrue(JournalEntryLine.objects.filter(id=line1.id).exists())

    def test_h11_05_bulk_create_lines_on_balanced_entry_is_blocked_by_trigger(self):
        """5. bulk_create() de líneas sobre una póliza contabilizada debe ser bloqueado por el trigger PostgreSQL."""
        if connection.vendor != 'postgresql':
            self.skipTest("Prueba específica de triggers nativos PostgreSQL")

        entry, _, _ = self._create_balanced_entry("POL-TRG-05")

        new_line = JournalEntryLine(
            organization=self.org,
            entry=entry,
            account=self.acct_cash,
            concept="Línea inyectada por bulk_create",
            debit=Decimal('100.00'),
            credit=Decimal('0.00'),
        )

        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                JournalEntryLine.objects.bulk_create([new_line])

        self.assertEqual(entry.lines.count(), 2)

    def test_h11_06_legitimate_draft_modification_is_permitted(self):
        """6. Modificaciones legítimas sobre borradores no balanceados están plenamente permitidas."""
        draft = JournalEntry.objects.create(
            organization=self.org,
            number="POL-DRAFT-01",
            date=timezone.now().date(),
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
            concept="Borrador en edición",
            is_balanced=False,
            created_by=self.auditor,
        )

        # 6.a update() en cabecera de borrador
        JournalEntry.objects.filter(id=draft.id).update(concept="Borrador editado legítimamente")
        draft.refresh_from_db()
        self.assertEqual(draft.concept, "Borrador editado legítimamente")

        # 6.b bulk_create() en líneas de borrador
        l1 = JournalEntryLine(
            organization=self.org,
            entry=draft,
            account=self.acct_cash,
            concept="Línea Borrador 1",
            debit=Decimal('200.00'),
            credit=Decimal('0.00'),
        )
        l2 = JournalEntryLine(
            organization=self.org,
            entry=draft,
            account=self.acct_bank,
            concept="Línea Borrador 2",
            debit=Decimal('0.00'),
            credit=Decimal('200.00'),
        )
        JournalEntryLine.objects.bulk_create([l1, l2])
        self.assertEqual(draft.lines.count(), 2)

        # 6.c update() en línea de borrador
        line_to_edit = draft.lines.first()
        JournalEntryLine.objects.filter(id=line_to_edit.id).update(concept="Concepto corregido")
        line_to_edit.refresh_from_db()
        self.assertEqual(line_to_edit.concept, "Concepto corregido")

        # 6.d delete() en línea de borrador
        JournalEntryLine.objects.filter(id=line_to_edit.id).delete()
        self.assertEqual(draft.lines.count(), 1)

    def test_h11_07_legitimate_closure_and_balancing_is_permitted(self):
        """7. El cierre legítimo pasando is_balanced de False a True es permitido por el trigger."""
        draft = JournalEntry.objects.create(
            organization=self.org,
            number="POL-CLOSE-01",
            date=timezone.now().date(),
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
            concept="Borrador a cerrar",
            is_balanced=False,
            created_by=self.auditor,
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=draft,
            account=self.acct_cash,
            concept="Debe",
            debit=Decimal('1000.00'),
            credit=Decimal('0.00'),
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=draft,
            account=self.acct_bank,
            concept="Haber",
            debit=Decimal('0.00'),
            credit=Decimal('1000.00'),
        )

        # Transición legítima de False a True
        draft.is_balanced = True
        draft.total_debit = Decimal('1000.00')
        draft.total_credit = Decimal('1000.00')
        draft.save()

        draft.refresh_from_db()
        self.assertTrue(draft.is_balanced)

    def test_h11_08_reversal_entry_creation_is_permitted(self):
        """8. La creación de una póliza de reversión independiente es totalmente permitida."""
        entry, _, _ = self._create_balanced_entry("POL-ORIG-01")

        # Crear reversión independiente
        rev_entry = JournalEntry.objects.create(
            organization=self.org,
            number="POL-REV-01",
            date=timezone.now().date(),
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
            concept=f"Reversión contable de {entry.number}",
            reference=entry.number,
            is_balanced=False,
            created_by=self.auditor,
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=rev_entry,
            account=self.acct_cash,
            concept=f"Reverso Debe {entry.number}",
            debit=Decimal('0.00'),
            credit=Decimal('5000.00'),
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=rev_entry,
            account=self.acct_bank,
            concept=f"Reverso Haber {entry.number}",
            debit=Decimal('5000.00'),
            credit=Decimal('0.00'),
        )
        rev_entry.is_balanced = True
        rev_entry.total_debit = Decimal('5000.00')
        rev_entry.total_credit = Decimal('5000.00')
        rev_entry.save()

        rev_entry.refresh_from_db()
        entry.refresh_from_db()

        self.assertTrue(rev_entry.is_balanced)
        self.assertTrue(entry.is_balanced)
        self.assertEqual(entry.lines.count(), 2)
        self.assertEqual(rev_entry.lines.count(), 2)

    def test_h11_09_prohibited_operation_aborts_transaction_and_rolls_back(self):
        """9. Rollback de una transacción que intente una operación prohibida por trigger."""
        if connection.vendor != 'postgresql':
            self.skipTest("Prueba específica de triggers nativos PostgreSQL")

        entry, _, _ = self._create_balanced_entry("POL-ROLL-01")

        account_before = AccountingAccount.objects.create(
            organization=self.org,
            code='9999-01',
            name='Cuenta Temporal Rollback',
            account_type=AccountingAccount.AccountType.ASSET,
        )

        try:
            with transaction.atomic():
                # 1. Operación que modifica un registro
                account_before.name = 'Nombre Modificado en TX'
                account_before.save()

                # 2. Operación que viola el trigger
                JournalEntry.objects.filter(id=entry.id).update(concept='Mutación prohibida')
        except DatabaseError:
            pass

        account_before.refresh_from_db()
        self.assertEqual(account_before.name, 'Cuenta Temporal Rollback')

    def test_h11_11_raw_sql_update_on_card_settlement_balanced_entry_is_blocked_by_trigger(self):
        """
        11. Validación de trigger PostgreSQL mediante SQL directo (cursor.execute) sobre la póliza
        cerrada y balanceada generada por el servicio real record_card_batch_settlement() (H-13).
        """
        if connection.vendor != 'postgresql':
            self.skipTest("Prueba específica de triggers nativos PostgreSQL")

        bank = BankAccount.objects.create(
            organization=self.org,
            bank_name=BankAccount.BankName.BAC,
            account_number='000-SQL-SETTLE-01',
            account_type=BankAccount.AccountType.CHECKING,
            currency='NIO',
            current_balance=Decimal('20000.00'),
            is_active=True,
        )

        settlement = record_card_batch_settlement(
            organization=self.org,
            bank_account=bank,
            batch_reference='BATCH-SQL-TEST-01',
            gross_amount=Decimal('4000.00'),
            fee_amount=Decimal('120.00'),
            tax_retention_amount=Decimal('60.00'),
            user=self.auditor,
            notes='Liquidación para prueba SQL directo H-13',
            idempotency_key='IDEMP-SQL-H13-001',
        )

        entry = settlement.journal_entry
        self.assertIsNotNone(entry)
        self.assertTrue(entry.is_balanced)

        original_concept = entry.concept
        original_total_debit = entry.total_debit
        original_total_credit = entry.total_credit

        # Ejecutar UPDATE directo con cursor SQL nativo dentro de subtransacción controlada (savepoint)
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE finance_journalentry SET concept = %s WHERE id = %s",
                        ['Concepto alterado mediante SQL directo', entry.id]
                    )

        # Confirmar que la póliza conserva su valor original y que la transacción de prueba sigue activa
        entry.refresh_from_db()
        self.assertEqual(entry.concept, original_concept)
        self.assertEqual(entry.total_debit, original_total_debit)
        self.assertEqual(entry.total_credit, original_total_credit)
        self.assertTrue(entry.is_balanced)

    # =========================================================================
    # H-12: LÍMITE AUTORIZADO DE CAJA CHICA (CHECK CONSTRAINTS)
    # =========================================================================

    def test_h12_01_check_constraint_rejects_balance_exceeding_allocated(self):
        """Verifica que PostgreSQL rechace current_balance > allocated_amount vía CheckConstraint."""
        fund = PettyCashFund.objects.create(
            organization=self.org,
            code="CAJ-H12-01",
            name="Caja Chica H12",
            custodian=self.auditor,
            allocated_amount=Decimal('10000.00'),
            current_balance=Decimal('5000.00'),
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                # Intento directo por ORM update bypassing validation
                PettyCashFund.objects.filter(id=fund.id).update(current_balance=Decimal('15000.00'))

        fund.refresh_from_db()
        self.assertEqual(fund.current_balance, Decimal('5000.00'))

    def test_h12_02_check_constraint_rejects_negative_balance(self):
        """Verifica que PostgreSQL rechace current_balance < 0.00 vía CheckConstraint."""
        fund = PettyCashFund.objects.create(
            organization=self.org,
            code="CAJ-H12-02",
            name="Caja Chica Negativo",
            custodian=self.auditor,
            allocated_amount=Decimal('10000.00'),
            current_balance=Decimal('0.00'),
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PettyCashFund.objects.filter(id=fund.id).update(current_balance=Decimal('-1.00'))

        fund.refresh_from_db()
        self.assertEqual(fund.current_balance, Decimal('0.00'))

    def test_h12_03_valid_balance_within_bounds_is_accepted(self):
        """Verifica que saldos dentro de [0.00, allocated_amount] sean perfectamente aceptados."""
        fund = PettyCashFund.objects.create(
            organization=self.org,
            code="CAJ-H12-03",
            name="Caja Chica Válida",
            custodian=self.auditor,
            allocated_amount=Decimal('10000.00'),
            current_balance=Decimal('10000.00'),  # Exactamente el límite
        )
        self.assertEqual(fund.current_balance, Decimal('10000.00'))

        PettyCashFund.objects.filter(id=fund.id).update(current_balance=Decimal('0.00'))
        fund.refresh_from_db()
        self.assertEqual(fund.current_balance, Decimal('0.00'))


class Phase1C83CConcurrencyTestCase(TransactionTestCase):
    """
    10. Comportamiento de los triggers y bloqueos ante operaciones concurrentes relevantes.
    Hereda de TransactionTestCase para permitir múltiples conexiones de hilos a la BD.
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
                'tax_id_label': 'RUC',
                'tax_id_regex': r'^[A-Z]\d{13}$',
                'is_active': True,
                'default_tax_rate': Decimal('15.00'),
                'default_tax_name': 'IVA',
            }
        )
        self.org = Organization.objects.create(
            name="Constructora Concurrencia S.A.",
            legal_name="Constructora Concurrencia S.A.",
            tax_id="J0310000008888",
            country=self.country_ni,
            is_active=True,
        )
        self.auditor = User.objects.create_user(
            email='concurrency_auditor@dbshield.com',
            password='audit_secure_password_2026',
            full_name='Auditor Concurrente',
        )
        OrganizationMembership.objects.create(
            user=self.auditor,
            organization=self.org,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )
        ensure_default_chart_of_accounts(self.org)
        self.acct_cash = AccountingAccount.objects.get(organization=self.org, code='1101-01')
        self.acct_bank = AccountingAccount.objects.get(organization=self.org, code='1102-01')

    def test_h11_10_concurrent_operations_on_balanced_entry_are_consistently_blocked(self):
        """Verifica que bajo múltiples conexiones concurrentes los triggers bloqueen de forma consistente."""
        if connection.vendor != 'postgresql':
            self.skipTest("Prueba específica de PostgreSQL")

        entry = JournalEntry.objects.create(
            organization=self.org,
            number="POL-CONC-01",
            date=timezone.now().date(),
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
            concept="Póliza Balanceada para Concurrencia",
            is_balanced=False,
            created_by=self.auditor,
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acct_cash,
            concept="Debe",
            debit=Decimal('100.00'),
            credit=Decimal('0.00'),
        )
        JournalEntryLine.objects.create(
            organization=self.org,
            entry=entry,
            account=self.acct_bank,
            concept="Haber",
            debit=Decimal('0.00'),
            credit=Decimal('100.00'),
        )
        entry.is_balanced = True
        entry.total_debit = Decimal('100.00')
        entry.total_credit = Decimal('100.00')
        entry.save()

        errors = []

        def worker_attempt_update(thread_id):
            try:
                # Cada hilo abre una conexión independiente a PostgreSQL
                connections.close_all()
                with transaction.atomic():
                    JournalEntry.objects.filter(id=entry.id).update(concept=f"Hack Thread {thread_id}")
            except DatabaseError as e:
                errors.append((thread_id, type(e).__name__))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker_attempt_update, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Ambos hilos deben haber sido bloqueados por el trigger
        self.assertEqual(len(errors), 2)
        entry.refresh_from_db()
        self.assertEqual(entry.concept, "Póliza Balanceada para Concurrencia")
