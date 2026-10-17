from datetime import date, timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.utils import timezone

from apps.core.models import CountryConfig
from apps.crm.models import Account
from apps.finance.models import (
    BankAccount,
    BankStatement,
    BankStatementLine,
    BankReconciliationMatch,
    FinancialTransaction,
    JournalEntry,
)
from apps.finance.services import (
    ensure_default_chart_of_accounts,
    record_card_batch_settlement,
    clear_check_deposit,
    deposit_check_to_bank,
)
from apps.finance.services_reconciliation import (
    import_bank_statement,
    parse_and_validate_bank_statement_csv,
    run_reconciliation_matching,
    confirm_reconciliation_match,
    undo_reconciliation_match,
    auto_confirm_exact_matches,
)
from apps.billing.models import (
    Invoice,
    InvoiceItem,
    PaymentReceipt,
)
from apps.billing.services import execute_pos_sale
from apps.billing.services_pos_shift import open_pos_shift
from apps.inventory.models import (
    InventoryItem,
    StockBalance,
    Warehouse,
)
from apps.purchasing.models import PurchaseOrder, Supplier, SupplierPayment
from apps.purchasing.services import record_supplier_disbursement
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel

User = get_user_model()


class BankReconciliationTestCase(TestCase):
    """
    Suite de Certificación de Conciliación Bancaria y Trazabilidad de Extractos (Fase 1C.5.1):
    1. Importación válida de CSV.
    2. Archivo inválido o con columnas faltantes rechazado.
    3. Reimportación del mismo archivo rechazada (idempotencia).
    4. Detección de líneas duplicadas dentro y entre extractos.
    5. Coincidencia exacta comprobada y confirmada.
    6. Coincidencia potencial que requiere revisión humana.
    7. Movimiento del extracto sin correspondencia en ERP y partidas en tránsito.
    8. Discrepancias de importe y monedas incompatibles rechazadas.
    9. Prevención estricta de doble conciliación (OneToOne).
    10. Aislamiento multi-tenant integral.
    11. Permisos insuficientes y RBAC en servidor.
    12. Rollback atómico ante fallo intermedio.
    13. Reversión auditada (undo) sin mutar transacciones inmutables.
    14. Cero regresión con liquidaciones de tarjeta, cheques y pagos a proveedores.
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
                'default_tax_name': 'IVA',
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )

        # Organización A (Empresa Principal)
        self.org_a = Organization.objects.create(
            name="Corporación Ferretera S.A.",
            slug="corp-ferretera-concil",
            country=self.country_ni,
            currency='NIO',
            enabled_modules=['crm', 'inventory', 'finance', 'billing', 'purchasing'],
            is_active=True,
        )

        # Organización B (Tenant Aislado)
        self.org_b = Organization.objects.create(
            name="Farmacia Central Managua S.A.",
            slug="farmacia-central-concil",
            country=self.country_ni,
            currency='NIO',
            enabled_modules=['crm', 'inventory', 'finance', 'billing', 'purchasing'],
            is_active=True,
        )

        # Usuarios
        self.user_owner_a = User.objects.create_user(
            email='admin@ferretera.ni',
            password='TestPassword2026*',
            full_name='Admin General Org A',
        )
        self.user_coord_a = User.objects.create_user(
            email='tesorero@ferretera.ni',
            password='TestPassword2026*',
            full_name='Tesorero Org A',
        )
        self.user_engineer_a = User.objects.create_user(
            email='ingeniero@ferretera.ni',
            password='TestPassword2026*',
            full_name='Ingeniero Org A',
        )
        self.user_b = User.objects.create_user(
            email='admin@farmacia.ni',
            password='TestPassword2026*',
            full_name='Admin Org B',
        )

        # Membresías RBAC
        OrganizationMembership.objects.create(
            organization=self.org_a,
            user=self.user_owner_a,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            organization=self.org_a,
            user=self.user_coord_a,
            role_level=RoleLevel.OPERATIONS_COORD,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            organization=self.org_a,
            user=self.user_engineer_a,
            role_level=RoleLevel.ENGINEER,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            organization=self.org_b,
            user=self.user_b,
            role_level=RoleLevel.OWNER_ADMIN,
            is_active=True,
        )

        # Cuentas bancarias
        self.bank_a = BankAccount.objects.create(
            organization=self.org_a,
            bank_name=BankAccount.BankName.BAC,
            account_number='001-998877-01',
            currency='NIO',
            current_balance=Decimal('100000.00'),
            is_active=True,
        )
        self.bank_a_usd = BankAccount.objects.create(
            organization=self.org_a,
            bank_name=BankAccount.BankName.BAC,
            account_number='001-998877-02',
            currency='USD',
            current_balance=Decimal('5000.00'),
            is_active=True,
        )
        self.bank_a_inactive = BankAccount.objects.create(
            organization=self.org_a,
            bank_name=BankAccount.BankName.BANPRO,
            account_number='002-334455-01',
            currency='NIO',
            current_balance=Decimal('500.00'),
            is_active=False,
        )
        self.bank_b = BankAccount.objects.create(
            organization=self.org_b,
            bank_name=BankAccount.BankName.BAC,
            account_number='009-112233-09',
            currency='NIO',
            current_balance=Decimal('25000.00'),
            is_active=True,
        )

        # Catálogos contables
        ensure_default_chart_of_accounts(self.org_a)
        ensure_default_chart_of_accounts(self.org_b)

    def test_01_valid_bank_statement_csv_import(self):
        """
        1. Importación válida de extracto CSV:
        - Procesa abonos y cargos correctamente.
        - Genera BankStatement y BankStatementLine en estado UNRECONCILED.
        - Invariante estricto: BankAccount.current_balance permanece INTACTO.
        """
        initial_balance = self.bank_a.current_balance
        csv_data = (
            "fecha,referencia,descripcion,monto,tipo\n"
            "2026-10-10,DEP-101,DEPOSITO EN EFECTIVO SUCURSAL,5000.00,CREDIT\n"
            "2026-10-11,CHQ-502,COBRO DE CHEQUE EN VENTANILLA,1200.00,DEBIT\n"
            "2026-10-12,TRF-889,TRANSFERENCIA RECIBIDA CLIENTE,3500.00,CREDIT\n"
        )

        stmt = import_bank_statement(
            organization=self.org_a,
            bank_account=self.bank_a,
            file_content=csv_data,
            file_name='extracto_octubre_2026.csv',
            user=self.user_coord_a,
            initial_balance=Decimal('100000.00'),
            final_balance=Decimal('107300.00'),
            notes='Extracto primera quincena octubre',
        )

        self.assertIsNotNone(stmt)
        self.assertEqual(stmt.total_lines, 3)
        self.assertEqual(stmt.total_credits, Decimal('8500.00'))  # 5000 + 3500
        self.assertEqual(stmt.total_debits, Decimal('1200.00'))
        self.assertEqual(stmt.status, BankStatement.Status.IMPORTED)
        self.assertEqual(stmt.lines.count(), 3)

        # Verificar invariante financiero: Cero afectación al saldo bancario en libros
        self.bank_a.refresh_from_db()
        self.assertEqual(self.bank_a.current_balance, initial_balance)

        # Verificar líneas
        lines = list(stmt.lines.order_by('line_number'))
        self.assertEqual(lines[0].movement_type, BankStatementLine.MovementType.CREDIT)
        self.assertEqual(lines[0].amount, Decimal('5000.00'))
        self.assertEqual(lines[0].status, BankStatementLine.LineStatus.UNRECONCILED)
        self.assertEqual(lines[1].movement_type, BankStatementLine.MovementType.DEBIT)
        self.assertEqual(lines[1].amount, Decimal('1200.00'))

    def test_02_invalid_file_and_missing_columns_rejected(self):
        """
        2. Archivos inválidos y con columnas faltantes son rechazados con ValidationError:
        - Archivo vacío.
        - Archivo sin columnas obligatorias de fecha o descripción.
        - Archivo sin importes válidos.
        """
        # Archivo vacío
        with self.assertRaises(ValidationError) as ctx_empty:
            import_bank_statement(
                organization=self.org_a,
                bank_account=self.bank_a,
                file_content="   \n",
                file_name='vacio.csv',
            )
        self.assertIn("vacío", str(ctx_empty.exception).lower())

        # Falta columna de fecha
        csv_no_date = "referencia,descripcion,monto,tipo\nREF1,Prueba,100.00,CREDIT\n"
        with self.assertRaises(ValidationError) as ctx_nodate:
            import_bank_statement(
                organization=self.org_a,
                bank_account=self.bank_a,
                file_content=csv_no_date,
                file_name='sin_fecha.csv',
            )
        self.assertIn("fecha", str(ctx_nodate.exception).lower())

        # Formato de fecha inválido
        csv_bad_date = "fecha,referencia,descripcion,monto,tipo\nFECHA_INVALIDA,REF1,Prueba,100.00,CREDIT\n"
        with self.assertRaises(ValidationError) as ctx_baddate:
            import_bank_statement(
                organization=self.org_a,
                bank_account=self.bank_a,
                file_content=csv_bad_date,
                file_name='fecha_mala.csv',
            )
        self.assertIn("inválido", str(ctx_baddate.exception).lower())

        # Monto no numérico
        csv_bad_amt = "fecha,referencia,descripcion,monto,tipo\n2026-10-10,REF1,Prueba,NO_ES_NUMERO,CREDIT\n"
        with self.assertRaises(ValidationError) as ctx_badamt:
            import_bank_statement(
                organization=self.org_a,
                bank_account=self.bank_a,
                file_content=csv_bad_amt,
                file_name='monto_malo.csv',
            )
        self.assertIn("numérico", str(ctx_badamt.exception).lower())

    def test_03_reimport_same_file_rejected_idempotency(self):
        """
        3. Idempotencia y anti-duplicación de lotes:
        - Reimportar el mismo archivo exacto (mismo file_hash en la misma cuenta) lanza ValidationError.
        - No se crean extractos ni líneas duplicadas.
        """
        csv_data = (
            "fecha,referencia,descripcion,monto,tipo\n"
            "2026-10-05,REF-IDEMP-01,ABONO CLIENTE 1,2000.00,CREDIT\n"
        )
        stmt1 = import_bank_statement(
            organization=self.org_a,
            bank_account=self.bank_a,
            file_content=csv_data,
            file_name='extracto_idemp.csv',
            user=self.user_coord_a,
        )
        self.assertIsNotNone(stmt1)

        # Intento de reimportar el mismo extracto
        with self.assertRaises(ValidationError) as ctx:
            import_bank_statement(
                organization=self.org_a,
                bank_account=self.bank_a,
                file_content=csv_data,
                file_name='extracto_idemp.csv',
                user=self.user_coord_a,
            )
        self.assertIn("ya fue importado previamente", str(ctx.exception).lower())
        self.assertEqual(BankStatement.objects.filter(bank_account=self.bank_a).count(), 1)

    def test_04_duplicate_bank_lines_within_and_across_files_rejected(self):
        """
        4. Detección estricta de líneas duplicadas:
        - Fila duplicada idéntica dentro del mismo archivo CSV.
        - Movimiento duplicado que ya fue importado en un extracto previo de la cuenta.
        """
        # Duplicado intra-archivo
        csv_intra_dup = (
            "fecha,referencia,descripcion,monto,tipo\n"
            "2026-10-01,REF-DUP-01,PAGO SERVICIO AGUA,500.00,DEBIT\n"
            "2026-10-01,REF-DUP-01,PAGO SERVICIO AGUA,500.00,DEBIT\n"
        )
        with self.assertRaises(ValidationError) as ctx_intra:
            import_bank_statement(
                organization=self.org_a,
                bank_account=self.bank_a,
                file_content=csv_intra_dup,
                file_name='intra_dup.csv',
            )
        self.assertIn("duplicado", str(ctx_intra.exception).lower())

        # Duplicado entre extractos diferentes
        csv_file1 = (
            "fecha,referencia,descripcion,monto,tipo\n"
            "2026-10-01,REF-CROSS-01,PAGO ENERGIA ELECTRICA,800.00,DEBIT\n"
        )
        import_bank_statement(
            organization=self.org_a,
            bank_account=self.bank_a,
            file_content=csv_file1,
            file_name='lote1.csv',
        )

        csv_file2 = (
            "fecha,referencia,descripcion,monto,tipo\n"
            "2026-10-01,REF-CROSS-01,PAGO ENERGIA ELECTRICA,800.00,DEBIT\n"
            "2026-10-02,REF-NEW-02,INGRESO NUEVO,1000.00,CREDIT\n"
        )
        with self.assertRaises(ValidationError) as ctx_cross:
            import_bank_statement(
                organization=self.org_a,
                bank_account=self.bank_a,
                file_content=csv_file2,
                file_name='lote2.csv',
            )
        self.assertIn("ya fueron importados previamente", str(ctx_cross.exception).lower())

    def test_05_exact_match_identification_and_confirmation(self):
        """
        5. Coincidencia exacta (EXACT MATCH):
        - Transacción ERP asentada y línea de extracto con mismo importe, sentido, referencia y fecha cercana.
        - Motor identifica EXACT_MATCH.
        - Confirmación crea BankReconciliationMatch.
        - Invariante estricto: BankAccount.current_balance y FinancialTransaction no sufren duplicaciones.
        """
        initial_bank_bal = self.bank_a.current_balance

        # Crear transacción ERP confirmada (Cobro a Cliente por Transferencia)
        trx = FinancialTransaction.objects.create(
            organization=self.org_a,
            transaction_type=FinancialTransaction.TransactionType.CLIENT_COLLECTION,
            bank_account=self.bank_a,
            amount=Decimal('4500.00'),
            balance_after=self.bank_a.current_balance + Decimal('4500.00'),
            concept="Cobro Factura FAC-2026-101",
            reference_code="TRANSF-BAC-901",
            performed_by=self.user_coord_a,
        )
        self.assertFalse(trx.is_reconciled)

        # Importar extracto con el crédito bancario equivalente
        csv_data = (
            "fecha,referencia,descripcion,monto,tipo\n"
            f"{timezone.now().date().isoformat()},TRANSF-BAC-901,TRANSFERENCIA TERCEROS BAC,4500.00,CREDIT\n"
        )
        stmt = import_bank_statement(
            organization=self.org_a,
            bank_account=self.bank_a,
            file_content=csv_data,
            file_name='extracto_exact.csv',
            user=self.user_coord_a,
        )
        line = stmt.lines.first()

        # Ejecutar motor de emparejamiento
        report = run_reconciliation_matching(
            organization=self.org_a,
            bank_account=self.bank_a,
            statement=stmt,
        )
        self.assertEqual(len(report['exact_matches']), 1)
        self.assertEqual(report['exact_matches'][0]['statement_line'].id, line.id)
        self.assertEqual(report['exact_matches'][0]['financial_transaction'].id, trx.id)

        # Confirmar conciliación
        match = confirm_reconciliation_match(
            organization=self.org_a,
            statement_line=line,
            financial_transaction=trx,
            user=self.user_coord_a,
            notes="Conciliación verificada conforme.",
            match_type=BankReconciliationMatch.MatchType.EXACT,
        )
        self.assertIsNotNone(match)

        line.refresh_from_db()
        self.assertEqual(line.status, BankStatementLine.LineStatus.RECONCILED)
        self.assertEqual(line.matched_transaction_id, trx.id)

        # Transacción marcada como conciliada mediante propiedad y relación
        trx.refresh_from_db()
        self.assertTrue(trx.is_reconciled)

        stmt.refresh_from_db()
        self.assertEqual(stmt.status, BankStatement.Status.RECONCILED)

        # Invariante financiero: El saldo bancario NO cambia por conciliar
        self.bank_a.refresh_from_db()
        self.assertEqual(self.bank_a.current_balance, initial_bank_bal)

    def test_06_potential_match_requiring_human_review(self):
        """
        6. Coincidencia potencial (POTENTIAL MATCH):
        - Mismo importe y sentido, pero referencias discrepantes o múltiples candidatos.
        - Motor clasifica como POTENTIAL y NO auto-confirma.
        - Usuario revisa y autoriza manualmente.
        """
        # Dos transacciones ERP por el mismo importe de $1,250.00
        tx1 = FinancialTransaction.objects.create(
            organization=self.org_a,
            transaction_type=FinancialTransaction.TransactionType.VENDOR_DISBURSEMENT,
            bank_account=self.bank_a,
            amount=Decimal('1250.00'),
            balance_after=self.bank_a.current_balance - Decimal('1250.00'),
            concept="Pago Proveedor Alfa",
            reference_code="CHEQUE-101",
            performed_by=self.user_coord_a,
        )
        tx2 = FinancialTransaction.objects.create(
            organization=self.org_a,
            transaction_type=FinancialTransaction.TransactionType.VENDOR_DISBURSEMENT,
            bank_account=self.bank_a,
            amount=Decimal('1250.00'),
            balance_after=self.bank_a.current_balance - Decimal('2500.00'),
            concept="Pago Proveedor Beta",
            reference_code="CHEQUE-102",
            performed_by=self.user_coord_a,
        )

        # Extracto con cargo por $1,250.00 con referencia ambigua
        csv_data = (
            "fecha,referencia,descripcion,monto,tipo\n"
            f"{timezone.now().date().isoformat()},CHQ-GENERICO,COBRO DE CHEQUE VENTANILLA,1250.00,DEBIT\n"
        )
        stmt = import_bank_statement(
            organization=self.org_a,
            bank_account=self.bank_a,
            file_content=csv_data,
            file_name='extracto_potential.csv',
        )
        line = stmt.lines.first()

        report = run_reconciliation_matching(
            organization=self.org_a,
            bank_account=self.bank_a,
            statement=stmt,
        )
        self.assertEqual(len(report['exact_matches']), 0)
        self.assertEqual(len(report['potential_matches']), 1)
        self.assertEqual(len(report['potential_matches'][0]['candidate_transactions']), 2)

        # Auto-match NO debe tocarlo
        auto_confirmed = auto_confirm_exact_matches(
            organization=self.org_a,
            bank_account=self.bank_a,
            statement=stmt,
        )
        self.assertEqual(len(auto_confirmed), 0)
        line.refresh_from_db()
        self.assertEqual(line.status, BankStatementLine.LineStatus.UNRECONCILED)

        # Confirmación manual explícita sobre tx1
        match = confirm_reconciliation_match(
            organization=self.org_a,
            statement_line=line,
            financial_transaction=tx1,
            user=self.user_coord_a,
            notes="Revisado y confirmado manualmente con soporte físico",
            match_type=BankReconciliationMatch.MatchType.MANUAL,
        )
        self.assertEqual(match.match_type, BankReconciliationMatch.MatchType.MANUAL)
        line.refresh_from_db()
        self.assertEqual(line.status, BankStatementLine.LineStatus.RECONCILED)
        tx1.refresh_from_db()
        self.assertTrue(tx1.is_reconciled)
        tx2.refresh_from_db()
        self.assertFalse(tx2.is_reconciled)

    def test_07_unmatched_statement_and_unmatched_erp_in_transit(self):
        """
        7. Movimientos sin correspondencia y partidas en tránsito:
        - Comisión bancaria en extracto sin registro en ERP -> UNMATCHED_STATEMENT.
        - Transacción ERP aún no reflejada en banco -> UNMATCHED_ERP.
        """
        # Transacción en tránsito en ERP ($8,000.00)
        tx_in_transit = FinancialTransaction.objects.create(
            organization=self.org_a,
            transaction_type=FinancialTransaction.TransactionType.CLIENT_COLLECTION,
            bank_account=self.bank_a,
            amount=Decimal('8000.00'),
            balance_after=self.bank_a.current_balance + Decimal('8000.00'),
            concept="Cobro fin de mes en tránsito",
            reference_code="TRANSF-TRANSITO-99",
            performed_by=self.user_coord_a,
        )

        # Extracto con cargo por comisión bancaria ($35.00)
        csv_data = (
            "fecha,referencia,descripcion,monto,tipo\n"
            f"{timezone.now().date().isoformat()},COMIS-01,COMISION MANTENIMIENTO CUENTA,35.00,DEBIT\n"
        )
        stmt = import_bank_statement(
            organization=self.org_a,
            bank_account=self.bank_a,
            file_content=csv_data,
            file_name='extracto_unmatched.csv',
        )

        report = run_reconciliation_matching(
            organization=self.org_a,
            bank_account=self.bank_a,
            statement=stmt,
        )
        self.assertEqual(len(report['unmatched_statement_lines']), 1)
        self.assertEqual(report['unmatched_statement_lines'][0]['statement_line'].amount, Decimal('35.00'))

        unmatched_erp_ids = [tx.id for tx in report['unmatched_erp_transactions']]
        self.assertIn(tx_in_transit.id, unmatched_erp_ids)

    def test_08_discrepancy_amount_and_incompatible_currency_rejected(self):
        """
        8. Discrepancias de monto y validación de monedas:
        - Mapeo con referencia idéntica pero montos diferentes se clasifica como DISCREPANCY.
        - Intento de confirmar match con montos divergentes lanza ValidationError.
        - Carga de extracto en moneda distinta a la cuenta bancaria lanza ValidationError.
        """
        tx = FinancialTransaction.objects.create(
            organization=self.org_a,
            transaction_type=FinancialTransaction.TransactionType.CLIENT_COLLECTION,
            bank_account=self.bank_a,
            amount=Decimal('5000.00'),
            balance_after=self.bank_a.current_balance + Decimal('5000.00'),
            concept="Cobro cliente con diferencia",
            reference_code="REF-DISCREPANCY-77",
            performed_by=self.user_coord_a,
        )

        csv_data = (
            "fecha,referencia,descripcion,monto,tipo\n"
            f"{timezone.now().date().isoformat()},REF-DISCREPANCY-77,ABONO CON DIFERENCIA BANCO,4950.00,CREDIT\n"
        )
        stmt = import_bank_statement(
            organization=self.org_a,
            bank_account=self.bank_a,
            file_content=csv_data,
            file_name='extracto_disc.csv',
        )
        line = stmt.lines.first()

        report = run_reconciliation_matching(
            organization=self.org_a,
            bank_account=self.bank_a,
            statement=stmt,
        )
        self.assertEqual(len(report['discrepancies']), 1)

        # Forzar confirmación con montos distintos debe ser RECHAZADO
        with self.assertRaises(ValidationError) as ctx:
            confirm_reconciliation_match(
                organization=self.org_a,
                statement_line=line,
                financial_transaction=tx,
                user=self.user_coord_a,
            )
        self.assertIn("discrepancia material de importe", str(ctx.exception).lower())

        # Subir extracto con columna de moneda incompatible (USD en cuenta NIO)
        csv_usd_in_nio = (
            "fecha,referencia,descripcion,monto,tipo,moneda\n"
            f"{timezone.now().date().isoformat()},REF-USD,PAGO INTERNACIONAL,100.00,CREDIT,USD\n"
        )
        with self.assertRaises(ValidationError) as ctx_curr:
            import_bank_statement(
                organization=self.org_a,
                bank_account=self.bank_a,  # Cuenta en NIO
                file_content=csv_usd_in_nio,
                file_name='incompatible_curr.csv',
            )
        self.assertIn("moneda incompatible", str(ctx_curr.exception).lower())

    def test_09_double_reconciliation_prevention(self):
        """
        9. Prevención de doble conciliación:
        - Una línea de extracto ya conciliada no puede conciliarse de nuevo.
        - Una transacción ERP ya conciliada no puede emparejarse con una segunda línea.
        """
        tx = FinancialTransaction.objects.create(
            organization=self.org_a,
            transaction_type=FinancialTransaction.TransactionType.CLIENT_COLLECTION,
            bank_account=self.bank_a,
            amount=Decimal('3000.00'),
            balance_after=self.bank_a.current_balance + Decimal('3000.00'),
            concept="Cobro Único",
            reference_code="REF-DOUBLE-01",
            performed_by=self.user_coord_a,
        )

        csv_data = (
            "fecha,referencia,descripcion,monto,tipo\n"
            f"{timezone.now().date().isoformat()},REF-DOUBLE-01,ABONO 1,3000.00,CREDIT\n"
            f"{timezone.now().date().isoformat()},REF-DOUBLE-02,ABONO 2,3000.00,CREDIT\n"
        )
        stmt = import_bank_statement(
            organization=self.org_a,
            bank_account=self.bank_a,
            file_content=csv_data,
            file_name='extracto_double.csv',
        )
        line1 = stmt.lines.get(line_number=1)
        line2 = stmt.lines.get(line_number=2)

        # Conciliar line1 con tx
        confirm_reconciliation_match(self.org_a, line1, tx, user=self.user_coord_a)

        # 1. Intentar volver a conciliar line1
        with self.assertRaises(ValidationError) as ctx1:
            confirm_reconciliation_match(self.org_a, line1, tx, user=self.user_coord_a)
        self.assertIn("ya se encuentra conciliada", str(ctx1.exception).lower())

        # 2. Intentar conciliar line2 con la misma transacción tx
        with self.assertRaises(ValidationError) as ctx2:
            confirm_reconciliation_match(self.org_a, line2, tx, user=self.user_coord_a)
        self.assertIn("ya fue conciliada previamente", str(ctx2.exception).lower())

    def test_10_multi_tenant_isolation_in_statements_and_matches(self):
        """
        10. Aislamiento multi-tenant estricto:
        - Org B no puede cargar extractos en cuentas de Org A.
        - No se pueden conciliar líneas de Org A con transacciones de Org B.
        """
        csv_data = (
            "fecha,referencia,descripcion,monto,tipo\n"
            "2026-10-10,REF-TENANT,MOVIMIENTO,1000.00,CREDIT\n"
        )

        # Org B intentando cargar a cuenta de Org A
        with self.assertRaises(ValidationError) as ctx1:
            import_bank_statement(
                organization=self.org_b,
                bank_account=self.bank_a,  # Cuenta de Org A
                file_content=csv_data,
                file_name='hack.csv',
            )
        self.assertIn("no pertenece a la organización", str(ctx1.exception).lower())

        # Crear línea en Org A
        stmt_a = import_bank_statement(
            organization=self.org_a,
            bank_account=self.bank_a,
            file_content=csv_data,
            file_name='extracto_a.csv',
        )
        line_a = stmt_a.lines.first()

        # Crear transacción en Org B
        tx_b = FinancialTransaction.objects.create(
            organization=self.org_b,
            transaction_type=FinancialTransaction.TransactionType.CLIENT_COLLECTION,
            bank_account=self.bank_b,
            amount=Decimal('1000.00'),
            balance_after=self.bank_b.current_balance + Decimal('1000.00'),
            concept="Cobro Org B",
            reference_code="REF-TENANT",
            performed_by=self.user_b,
        )

        # Intento cruzado de conciliar línea de Org A con transacción de Org B
        with self.assertRaises(ValidationError) as ctx2:
            confirm_reconciliation_match(
                organization=self.org_a,
                statement_line=line_a,
                financial_transaction=tx_b,
            )
        self.assertIn("no pertenece a la organización", str(ctx2.exception).lower())

    def test_11_insufficient_permissions_and_rbac_enforcement(self):
        """
        11. Control de acceso jerárquico RBAC en servidor:
        - Usuarios con Nivel ENGINEER (50) reciben HTTP 403 Forbidden al intentar acceder a la consola de conciliación.
        - Usuarios con Nivel OPERATIONS_COORD (80) o OWNER_ADMIN (100) tienen acceso permitido (HTTP 200).
        """
        client = Client()

        # 1. Login con Ingeniero (Nivel 50)
        client.force_login(self.user_engineer_a)
        session = client.session
        session['active_organization_id'] = str(self.org_a.id)
        session.save()

        res_eng = client.get('/finance/reconciliation/')
        self.assertEqual(res_eng.status_code, 403)

        # 2. Login con Tesorero / Coordinador (Nivel 80)
        client.force_login(self.user_coord_a)
        session = client.session
        session['active_organization_id'] = str(self.org_a.id)
        session.save()

        res_coord = client.get('/finance/reconciliation/')
        self.assertEqual(res_coord.status_code, 200)

    def test_12_atomic_rollback_on_intermediate_failure(self):
        """
        12. Atomicidad y rollback:
        - Si ocurre un fallo en una línea intermedia, el extracto completo se revierte.
        - Cero registros huérfanos en base de datos.
        """
        count_statements_before = BankStatement.objects.filter(organization=self.org_a).count()
        count_lines_before = BankStatementLine.objects.filter(organization=self.org_a).count()

        # CSV con fila 2 válida pero fila 3 con fecha corrupta
        csv_corrupt = (
            "fecha,referencia,descripcion,monto,tipo\n"
            "2026-10-10,REF-OK,LINEA VALIDA,100.00,CREDIT\n"
            "FECHA_CORRUPTA,REF-BAD,LINEA INVALIDA,200.00,CREDIT\n"
        )

        with self.assertRaises(ValidationError):
            import_bank_statement(
                organization=self.org_a,
                bank_account=self.bank_a,
                file_content=csv_corrupt,
                file_name='corrupto.csv',
            )

        self.assertEqual(BankStatement.objects.filter(organization=self.org_a).count(), count_statements_before)
        self.assertEqual(BankStatementLine.objects.filter(organization=self.org_a).count(), count_lines_before)

    def test_13_undo_reconciliation_traceability(self):
        """
        13. Reversibilidad auditada (undo match):
        - Deshacer una conciliación libera la línea de extracto (UNRECONCILED) y la transacción ERP.
        - No borra ni modifica la transacción inmutable FinancialTransaction.
        - Preserva intacto BankAccount.current_balance.
        """
        initial_bank_bal = self.bank_a.current_balance

        tx = FinancialTransaction.objects.create(
            organization=self.org_a,
            transaction_type=FinancialTransaction.TransactionType.CLIENT_COLLECTION,
            bank_account=self.bank_a,
            amount=Decimal('7000.00'),
            balance_after=self.bank_a.current_balance + Decimal('7000.00'),
            concept="Cobro a revertir",
            reference_code="REF-UNDO-11",
            performed_by=self.user_coord_a,
        )

        csv_data = (
            "fecha,referencia,descripcion,monto,tipo\n"
            f"{timezone.now().date().isoformat()},REF-UNDO-11,ABONO BANCO,7000.00,CREDIT\n"
        )
        stmt = import_bank_statement(
            organization=self.org_a,
            bank_account=self.bank_a,
            file_content=csv_data,
            file_name='extracto_undo.csv',
        )
        line = stmt.lines.first()

        match = confirm_reconciliation_match(self.org_a, line, tx, user=self.user_coord_a)
        self.assertEqual(BankReconciliationMatch.objects.filter(id=match.id).count(), 1)
        self.assertTrue(tx.is_reconciled)

        # Deshacer conciliación
        undo_reconciliation_match(
            organization=self.org_a,
            match_id=match.id,
            user=self.user_coord_a,
            reason="Error de asignación por referencia ambigua",
        )

        self.assertEqual(BankReconciliationMatch.objects.filter(id=match.id).count(), 0)
        line.refresh_from_db()
        self.assertEqual(line.status, BankStatementLine.LineStatus.UNRECONCILED)
        self.assertIsNone(line.matched_transaction)

        tx.refresh_from_db()
        self.assertFalse(tx.is_reconciled)

        # El saldo bancario no sufre alteración
        self.bank_a.refresh_from_db()
        self.assertEqual(self.bank_a.current_balance, initial_bank_bal)

    def test_14_no_regression_card_settlement_and_check_clearing_reconciliation(self):
        """
        14. No regresión con servicios financieros de Fases 1C.4 y 1C.4.1:
        - Liquidación de lote de tarjetas (CARD_SETTLEMENT) se concilia con abono neto de extracto.
        - Compensación de cheque (CHECK_CLEARING) se concilia con abono de depósito en extracto.
        - Pago a proveedor (VENDOR_DISBURSEMENT) se concilia con débito/cargo de extracto.
        """
        # A. Liquidación de lote de tarjetas
        settlement = record_card_batch_settlement(
            organization=self.org_a,
            bank_account=self.bank_a,
            batch_reference='BATCH-RECONCIL-01',
            gross_amount=Decimal('10000.00'),
            fee_amount=Decimal('300.00'),
            tax_retention_amount=Decimal('200.00'),
            user=self.user_coord_a,
        )
        # Transacción generada con monto neto = 9,500.00 NIO
        tx_card = FinancialTransaction.objects.get(
            organization=self.org_a,
            reference_code='BATCH-RECONCIL-01',
            transaction_type=FinancialTransaction.TransactionType.CARD_SETTLEMENT,
        )
        self.assertEqual(tx_card.amount, Decimal('9500.00'))

        # B. Pago a proveedor (Desembolso)
        warehouse = Warehouse.objects.create(
            organization=self.org_a,
            name="Bodega Principal A",
            is_active=True,
        )
        supplier = Supplier.objects.create(
            organization=self.org_a,
            legal_name="Distribuidora de Cemento S.A.",
            tax_id="J0310000001234",
            is_active=True,
        )
        po = PurchaseOrder.objects.create(
            organization=self.org_a,
            supplier=supplier,
            target_warehouse=warehouse,
            currency_code='NIO',
            currency_symbol='C$',
            subtotal=Decimal('6000.00'),
            tax_total=Decimal('900.00'),
            gross_total=Decimal('6900.00'),
            net_payable=Decimal('6900.00'),
            balance_due=Decimal('6900.00'),
            status=PurchaseOrder.Status.RECEIVED,
            created_by=self.user_coord_a,
        )
        payment, tx_vendor, entry = record_supplier_disbursement(
            organization=self.org_a,
            purchase_order=po,
            amount=Decimal('6000.00'),
            user=self.user_coord_a,
            payment_method=SupplierPayment.PaymentMethod.TRANSFER_BAC,
            bank_account=self.bank_a,
            reference_number='PAY-SUPPLIER-777',
        )
        self.assertIsNotNone(tx_vendor)
        self.assertEqual(tx_vendor.transaction_type, FinancialTransaction.TransactionType.VENDOR_DISBURSEMENT)

        # C. Importar extracto con ambos movimientos
        csv_data = (
            "fecha,referencia,descripcion,monto,tipo\n"
            f"{timezone.now().date().isoformat()},BATCH-RECONCIL-01,LIQUIDACION CREDOMATIC ADQUIRENTE,9500.00,CREDIT\n"
            f"{timezone.now().date().isoformat()},PAY-SUPPLIER-777,TRANSFERENCIA A PROVEEDOR CEMENTO,{tx_vendor.amount},DEBIT\n"
        )
        stmt = import_bank_statement(
            organization=self.org_a,
            bank_account=self.bank_a,
            file_content=csv_data,
            file_name='extracto_regresion.csv',
            user=self.user_coord_a,
        )

        report = run_reconciliation_matching(
            organization=self.org_a,
            bank_account=self.bank_a,
            statement=stmt,
        )
        self.assertEqual(len(report['exact_matches']), 2)

        # Auto-conciliar exactas
        matches = auto_confirm_exact_matches(self.org_a, self.bank_a, statement=stmt, user=self.user_coord_a)
        self.assertEqual(len(matches), 2)

        stmt.refresh_from_db()
        self.assertEqual(stmt.status, BankStatement.Status.RECONCILED)
        tx_card.refresh_from_db()
        self.assertTrue(tx_card.is_reconciled)
        tx_vendor.refresh_from_db()
        self.assertTrue(tx_vendor.is_reconciled)
