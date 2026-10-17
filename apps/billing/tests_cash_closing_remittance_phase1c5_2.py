import datetime
from decimal import Decimal
import hashlib
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.billing.models import (
    Invoice,
    InvoiceItem,
    PaymentReceipt,
    PosRegisterShift,
    CashMovement,
    CashRemittance,
)
from apps.billing.services import execute_pos_sale
from apps.billing.services_pos_shift import (
    open_pos_shift,
    compute_shift_x_cut_metrics,
    close_pos_shift_z_cut,
)
from apps.billing.services_cash import (
    record_cash_movement,
    recalculate_shift_cash_metrics,
    prepare_cash_remittance,
    dispatch_cash_remittance,
    record_remittance_bank_deposit,
    confirm_remittance_deposit,
    reject_cash_remittance,
    calculate_denomination_total,
)
from apps.core.models import CountryConfig
from apps.crm.models import Account
from apps.finance.models import (
    AccountingAccount,
    BankAccount,
    BankStatement,
    BankStatementLine,
    FinancialTransaction,
    JournalEntry,
    JournalEntryLine,
)
from apps.finance.services import (
    ensure_default_chart_of_accounts,
    record_client_collection,
)
from apps.inventory.models import (
    InventoryBatch,
    InventoryItem,
    StockBalance,
    StockMovement,
    Warehouse,
)
from apps.purchasing.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
    SupplierInvoice,
    SupplierPayment,
)
from apps.purchasing.services import record_supplier_disbursement
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel

User = get_user_model()


class CashClosingAndRemittanceTestCase(TestCase):
    """
    Suite de Certificación Integral Fase 1C.5.2:
    Cierre de Caja, Cierre Z Operativo y Ciclo de Remesas de Efectivo a Bancos.
    Entorno de prueba obligatorio: PostgreSQL 14.
    """

    def setUp(self):
        # 1. Configuración de País
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

        # 2. Organización Principal Tenant A
        self.org_a = Organization.objects.create(
            name="Comercial Vector SA",
            slug="comercial-vector-sa",
            country=self.country_ni,
            currency="NIO",
            is_active=True,
        )

        # 3. Organización Tenant B para Pruebas de Aislamiento
        self.org_b = Organization.objects.create(
            name="Distribuidora Norte SA",
            slug="distribuidora-norte-sa",
            country=self.country_ni,
            currency="NIO",
            is_active=True,
        )

        # Catálogo de Cuentas Tenant A y Tenant B
        self.chart_a = ensure_default_chart_of_accounts(self.org_a)
        self.chart_b = ensure_default_chart_of_accounts(self.org_b)

        # 4. Usuarios
        self.cashier_user = User.objects.create_user(
            email='cajero@vector.com',
            password='password123',
            full_name='Cajero POS Vector',
            is_active=True,
        )
        OrganizationMembership.objects.create(
            user=self.cashier_user,
            organization=self.org_a,
            role_level=RoleLevel.TECHNICIAN,
            is_active=True,
        )

        self.supervisor_user = User.objects.create_user(
            email='supervisor@vector.com',
            password='password123',
            full_name='Supervisor POS Vector',
            is_active=True,
        )
        OrganizationMembership.objects.create(
            user=self.supervisor_user,
            organization=self.org_a,
            role_level=RoleLevel.OPERATIONS_COORD,
            is_active=True,
        )

        self.user_tenant_b = User.objects.create_user(
            email='cajero@norte.com',
            password='password123',
            full_name='Cajero Norte Tenant B',
            is_active=True,
        )
        OrganizationMembership.objects.create(
            user=self.user_tenant_b,
            organization=self.org_b,
            role_level=RoleLevel.TECHNICIAN,
            is_active=True,
        )

        # 5. Cuentas Bancarias
        self.bank_bac_a = BankAccount.objects.create(
            organization=self.org_a,
            bank_name=BankAccount.BankName.BAC,
            account_number='000-BAC-NIO-01',
            account_type=BankAccount.AccountType.CHECKING,
            currency='NIO',
            current_balance=Decimal('50000.00'),
            is_active=True,
        )
        self.bank_lafise_b = BankAccount.objects.create(
            organization=self.org_b,
            bank_name=BankAccount.BankName.LAFISE,
            account_number='999-LAFISE-NIO-01',
            account_type=BankAccount.AccountType.CHECKING,
            currency='NIO',
            current_balance=Decimal('20000.00'),
            is_active=True,
        )

        # 6. Cliente y Bodega para ventas POS
        self.customer = Account.objects.create(
            organization=self.org_a,
            account_type=Account.AccountType.CORPORATE,
            tax_id="J0310000001234",
            legal_name="Cliente Mostrador Final",
            commercial_name="Consumidor Final",
            lifecycle_stage=Account.LifecycleStage.ACTIVE,
        )
        self.warehouse = Warehouse.objects.create(
            organization=self.org_a,
            code="BOD-POS",
            name="Bodega de Mostrador",
            is_active=True,
        )
        self.product = InventoryItem.objects.create(
            organization=self.org_a,
            sku="PROD-POS-01",
            name="Artículo Mostrador A",
            unit_cost=Decimal('80.00'),
            is_active=True,
        )
        StockBalance.objects.create(
            organization=self.org_a,
            warehouse=self.warehouse,
            item=self.product,
            quantity=Decimal('100.00'),
        )

    # --------------------------------------------------------------------------
    # 1. Apertura de Sesión y Saldo Inicial
    # --------------------------------------------------------------------------
    def test_open_shift_and_initial_balance(self):
        shift = open_pos_shift(
            organization=self.org_a,
            cashier=self.cashier_user,
            opening_cash_nio=Decimal('1500.00'),
            register_name='CAJA-01',
        )

        self.assertEqual(shift.status, PosRegisterShift.Status.OPEN)
        self.assertEqual(shift.opening_cash_nio, Decimal('1500.00'))
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('1500.00'))
        self.assertTrue(shift.shift_number.startswith(f"TURNO-{timezone.now().year}-"))

        # Impedir abrir un segundo turno concurrente para el mismo cajero
        with self.assertRaises(ValidationError) as ctx:
            open_pos_shift(
                organization=self.org_a,
                cashier=self.cashier_user,
                opening_cash_nio=Decimal('500.00'),
            )
        self.assertIn("Ya existe un turno de caja abierto", str(ctx.exception))

        # Impedir apertura con fondo inicial negativo
        with self.assertRaises(ValidationError):
            open_pos_shift(
                organization=self.org_a,
                cashier=self.supervisor_user,
                opening_cash_nio=Decimal('-100.00'),
            )

    # --------------------------------------------------------------------------
    # 2. Entradas y Salidas Manuales de Efectivo
    # --------------------------------------------------------------------------
    def test_record_cash_in_and_cash_out_movements(self):
        shift = open_pos_shift(
            organization=self.org_a,
            cashier=self.cashier_user,
            opening_cash_nio=Decimal('1000.00'),
        )

        # Entrada adicional (ej: sencillo provisto a medio turno)
        mov_in = record_cash_movement(
            shift=shift,
            movement_type=CashMovement.MovementType.CASH_IN,
            amount=Decimal('500.00'),
            concept="Aporte adicional de sencillo en monedas y billetes",
            performed_by=self.cashier_user,
            reference="SENCILLO-01",
        )
        self.assertEqual(mov_in.amount, Decimal('500.00'))
        shift.refresh_from_db()
        self.assertEqual(shift.total_cash_inflow_nio, Decimal('500.00'))
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('1500.00'))

        # Salida menor (ej: compra de insumos de limpieza urgentes)
        mov_out = record_cash_movement(
            shift=shift,
            movement_type=CashMovement.MovementType.CASH_OUT,
            amount=Decimal('200.00'),
            concept="Compra de agua purificada y papel para mostrador",
            performed_by=self.cashier_user,
            reference="REC-098",
        )
        self.assertEqual(mov_out.amount, Decimal('200.00'))
        shift.refresh_from_db()
        self.assertEqual(shift.total_cash_outflow_nio, Decimal('200.00'))
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('1300.00'))

        # Intento de salida que supera la liquidez disponible en gaveta
        with self.assertRaises(ValidationError) as ctx:
            record_cash_movement(
                shift=shift,
                movement_type=CashMovement.MovementType.CASH_OUT,
                amount=Decimal('1500.00'),
                concept="Salida que excede fondos",
                performed_by=self.cashier_user,
            )
        self.assertIn("Fondos insuficientes en gaveta", str(ctx.exception))

    # --------------------------------------------------------------------------
    # 3. Venta POS en Efectivo Contabilizada una Sola Vez y con Movimiento
    # --------------------------------------------------------------------------
    def test_pos_cash_sale_creates_cash_movement_and_updates_shift(self):
        shift = open_pos_shift(
            organization=self.org_a,
            cashier=self.cashier_user,
            opening_cash_nio=Decimal('1000.00'),
        )

        sale_items = [{'item_id': str(self.product.id), 'quantity': 2, 'unit_price': 100.00}]
        invoice = execute_pos_sale(
            organization=self.org_a,
            account=self.customer,
            warehouse=self.warehouse,
            items_data=sale_items,
            payment_method=PaymentReceipt.PaymentMethod.CASH,
            user=self.cashier_user,
        )

        shift.refresh_from_db()
        # Venta = 200 Subtotal + 30 IVA = 230 C$
        expected_sale_amt = Decimal('230.00')
        self.assertEqual(invoice.net_receivable, expected_sale_amt)
        self.assertEqual(shift.system_cash_sales_nio, expected_sale_amt)
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('1230.00'))

        # Verificar que se creó el CashMovement(POS_SALE) granular
        mov = CashMovement.objects.filter(shift=shift, invoice=invoice).first()
        self.assertIsNotNone(mov)
        self.assertEqual(mov.movement_type, CashMovement.MovementType.POS_SALE)
        self.assertEqual(mov.amount, expected_sale_amt)
        self.assertEqual(mov.reference, invoice.number)

        # Verificar que no tocó las cuentas bancarias institucionales
        self.bank_bac_a.refresh_from_db()
        self.assertEqual(self.bank_bac_a.current_balance, Decimal('50000.00'))

    # --------------------------------------------------------------------------
    # 4. Venta con Tarjeta no Afecta Efectivo en Gaveta
    # --------------------------------------------------------------------------
    def test_pos_card_sale_does_not_affect_drawer_cash(self):
        shift = open_pos_shift(
            organization=self.org_a,
            cashier=self.cashier_user,
            opening_cash_nio=Decimal('1000.00'),
        )

        sale_items = [{'item_id': str(self.product.id), 'quantity': 1, 'unit_price': 100.00}]
        invoice = execute_pos_sale(
            organization=self.org_a,
            account=self.customer,
            warehouse=self.warehouse,
            items_data=sale_items,
            payment_method=PaymentReceipt.PaymentMethod.CARD,
            user=self.cashier_user,
        )

        shift.refresh_from_db()
        expected_card_amt = Decimal('115.00')
        self.assertEqual(shift.system_card_transfer_sales_nio, expected_card_amt)
        # El efectivo esperado en gaveta sigue siendo 1000.00 C$
        self.assertEqual(shift.system_cash_sales_nio, Decimal('0.00'))
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('1000.00'))

        # No debe haber ningún CashMovement generado
        self.assertEqual(CashMovement.objects.filter(shift=shift, invoice=invoice).count(), 0)

    # --------------------------------------------------------------------------
    # 5. Pago a Proveedor en Efectivo Afecta Gaveta y Resuelve la Brecha
    # --------------------------------------------------------------------------
    def test_supplier_cash_payment_affects_shift_drawer(self):
        shift = open_pos_shift(
            organization=self.org_a,
            cashier=self.cashier_user,
            opening_cash_nio=Decimal('2000.00'),
        )

        supplier = Supplier.objects.create(
            organization=self.org_a,
            legal_name="Ferretería La Económica SA",
            commercial_name="La Económica",
            tax_id="J031000001",
            is_active=True,
        )
        po = PurchaseOrder.objects.create(
            organization=self.org_a,
            supplier=supplier,
            target_warehouse=self.warehouse,
            status=PurchaseOrder.Status.APPROVED,
            currency_code="NIO",
            apply_tax=False,
            subtotal=Decimal('500.00'),
            tax_total=Decimal('0.00'),
            gross_total=Decimal('500.00'),
            net_payable=Decimal('500.00'),
            balance_due=Decimal('500.00'),
            created_by=self.cashier_user,
        )

        egr, trx, pol = record_supplier_disbursement(
            organization=self.org_a,
            purchase_order=po,
            amount=Decimal('500.00'),
            user=self.cashier_user,
            payment_method=SupplierPayment.PaymentMethod.CASH,
            pos_shift=shift,
        )

        shift.refresh_from_db()
        self.assertEqual(shift.total_supplier_payments_nio, Decimal('500.00'))
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('1500.00'))

        # Se creó el CashMovement(SUPPLIER_PAYMENT)
        mov = CashMovement.objects.filter(shift=shift, supplier_payment=egr).first()
        self.assertIsNotNone(mov)
        self.assertEqual(mov.movement_type, CashMovement.MovementType.SUPPLIER_PAYMENT)
        self.assertEqual(mov.amount, Decimal('500.00'))
        self.assertIsNone(trx)  # Efectivo no crea FinancialTransaction bancaria

    # --------------------------------------------------------------------------
    # 6. Cálculo Matemático del Saldo Esperado (Fórmula Multivariable)
    # --------------------------------------------------------------------------
    def test_expected_cash_formula_integration(self):
        # Fórmula: Saldo esperado = Fondo inicial + Ventas efvo + Entradas - Salidas - Remesas - Pagos prov - Devoluciones
        shift = open_pos_shift(organization=self.org_a, cashier=self.cashier_user, opening_cash_nio=Decimal('1000.00'))

        # 1. Entrada manual +300
        record_cash_movement(shift=shift, movement_type=CashMovement.MovementType.CASH_IN, amount=Decimal('300.00'), concept="Entrada", performed_by=self.cashier_user)
        # 2. Salida manual -100
        record_cash_movement(shift=shift, movement_type=CashMovement.MovementType.CASH_OUT, amount=Decimal('100.00'), concept="Salida", performed_by=self.cashier_user)
        # 3. Devolución a cliente en efectivo -50
        record_cash_movement(shift=shift, movement_type=CashMovement.MovementType.CUSTOMER_REFUND, amount=Decimal('50.00'), concept="Devolución", performed_by=self.cashier_user)

        metrics = compute_shift_x_cut_metrics(shift)
        # 1000 + 300 - 100 - 50 = 1150
        self.assertEqual(metrics['expected_cash_in_drawer_nio'], Decimal('1150.00'))
        shift.refresh_from_db()
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('1150.00'))

    # --------------------------------------------------------------------------
    # 7. Arqueo Ciego: Cuadre Exacto vs Discrepancia con Justificación Obligatoria
    # --------------------------------------------------------------------------
    def test_blind_cash_count_exact_and_discrepancy(self):
        shift = open_pos_shift(organization=self.org_a, cashier=self.cashier_user, opening_cash_nio=Decimal('1000.00'))

        # Intento de cierre con discrepancia (declarado 950 vs esperado 1000) SIN justificación: DEBE FALLAR
        with self.assertRaises(ValidationError) as ctx:
            close_pos_shift_z_cut(
                shift=shift,
                declared_cash_nio=Decimal('950.00'),
                user=self.cashier_user,
                difference_justification='',
            )
        self.assertIn("Se requiere obligatoriamente una justificación formal", str(ctx.exception))

        # Cierre con justificación válida: DEBE ÉXITO
        closed_shift = close_pos_shift_z_cut(
            shift=shift,
            declared_cash_nio=Decimal('950.00'),
            user=self.cashier_user,
            supervisor=self.supervisor_user,
            difference_justification="Faltante de C$ 50.00 por vuelto mal entregado en factura FAC-001",
            notes="Arqueo verificado con supervisor",
        )
        self.assertEqual(closed_shift.status, PosRegisterShift.Status.CLOSED_Z)
        self.assertEqual(closed_shift.cash_difference_nio, Decimal('-50.00'))
        self.assertEqual(closed_shift.supervisor, self.supervisor_user)
        self.assertTrue(len(closed_shift.audit_hash) == 64)

    # --------------------------------------------------------------------------
    # 8. Arqueo con Desglose por Denominación
    # --------------------------------------------------------------------------
    def test_denomination_breakdown_validation(self):
        shift = open_pos_shift(organization=self.org_a, cashier=self.cashier_user, opening_cash_nio=Decimal('1850.00'))

        # Desglose: 1x1000 + 1x500 + 1x200 + 1x100 + 1x50 = 1850 C$
        breakdown = {
            '1000': 1,
            '500': 1,
            '200': 1,
            '100': 1,
            '50': 1,
        }
        total_calc = calculate_denomination_total(breakdown)
        self.assertEqual(total_calc, Decimal('1850.00'))

        # Si se declara un monto que choca con la suma del desglose: DEBE FALLAR
        with self.assertRaises(ValidationError) as ctx:
            close_pos_shift_z_cut(
                shift=shift,
                declared_cash_nio=Decimal('1900.00'),
                user=self.cashier_user,
                denomination_breakdown=breakdown,
            )
        self.assertIn("no coincide con la suma del desglose", str(ctx.exception))

        # Cuadre exacto mediante desglose: DEBE ÉXITO
        closed_shift = close_pos_shift_z_cut(
            shift=shift,
            declared_cash_nio=Decimal('1850.00'),
            user=self.cashier_user,
            denomination_breakdown=breakdown,
        )
        self.assertEqual(closed_shift.status, PosRegisterShift.Status.CLOSED_Z)
        self.assertEqual(closed_shift.cash_difference_nio, Decimal('0.00'))
        self.assertEqual(closed_shift.denomination_breakdown, breakdown)

    # --------------------------------------------------------------------------
    # 9. Inmutabilidad y Bloqueo de Movimientos Ordinarios en Turnos Cerrados
    # --------------------------------------------------------------------------
    def test_closed_shift_immutability_and_rejects_new_movements(self):
        shift = open_pos_shift(organization=self.org_a, cashier=self.cashier_user, opening_cash_nio=Decimal('1000.00'))
        close_pos_shift_z_cut(shift=shift, declared_cash_nio=Decimal('1000.00'), user=self.cashier_user)

        # 1. Prohibido agregar nuevos CashMovement
        with self.assertRaises(ValidationError) as ctx:
            record_cash_movement(
                shift=shift,
                movement_type=CashMovement.MovementType.CASH_IN,
                amount=Decimal('100.00'),
                concept="Movimiento posterior prohibido",
                performed_by=self.cashier_user,
            )
        self.assertIn("No se pueden registrar movimientos de efectivo en un turno cerrado", str(ctx.exception))

        # 2. Prohibido preparar nuevas remesas
        with self.assertRaises(ValidationError):
            prepare_cash_remittance(
                shift=shift,
                destination_bank_account=self.bank_bac_a,
                amount=Decimal('500.00'),
                user=self.cashier_user,
            )

        # 3. Prohibido modificar el turno sellado
        shift.opening_cash_nio = Decimal('9999.00')
        with self.assertRaises(ValidationError) as ctx:
            shift.save()
        self.assertIn("Un turno con Corte Z sellado es inmutable", str(ctx.exception))

        # 4. Prohibido eliminar el turno sellado
        with self.assertRaises(ValidationError) as ctx:
            shift.delete()
        self.assertIn("Un turno con Corte Z sellado es inmutable", str(ctx.exception))

    # --------------------------------------------------------------------------
    # 10. Aislamiento Multiempresa (Multi-Tenant Scoping)
    # --------------------------------------------------------------------------
    def test_multi_tenant_isolation_in_shifts_and_movements(self):
        shift_a = open_pos_shift(organization=self.org_a, cashier=self.cashier_user, opening_cash_nio=Decimal('1000.00'))
        shift_b = open_pos_shift(organization=self.org_b, cashier=self.user_tenant_b, opening_cash_nio=Decimal('2000.00'))

        # Usuario Tenant B no puede registrar movimientos en el turno del Tenant A
        with self.assertRaises(ValidationError) as ctx:
            record_cash_movement(
                shift=shift_a,
                movement_type=CashMovement.MovementType.CASH_IN,
                amount=Decimal('100.00'),
                concept="Intento de cruce",
                performed_by=self.user_tenant_b,
            )
        self.assertIn("Aislamiento de tenant", str(ctx.exception))

        # No se puede preparar una remesa hacia una cuenta bancaria de otra organización
        with self.assertRaises(ValidationError) as ctx:
            prepare_cash_remittance(
                shift=shift_a,
                destination_bank_account=self.bank_lafise_b,
                amount=Decimal('500.00'),
                user=self.cashier_user,
            )
        self.assertIn("La cuenta bancaria destino no pertenece a la misma organización", str(ctx.exception))

    # --------------------------------------------------------------------------
    # 11. Ciclo Completo de Remesas de Efectivo a Bancos (Fases 1, 2, 3 y 4)
    # --------------------------------------------------------------------------
    def test_remittance_full_lifecycle(self):
        shift = open_pos_shift(organization=self.org_a, cashier=self.cashier_user, opening_cash_nio=Decimal('5000.00'))
        initial_bank_bal = self.bank_bac_a.current_balance

        # Fase 1: PREPARED (La remesa se arma dentro de la gaveta)
        rem = prepare_cash_remittance(
            shift=shift,
            destination_bank_account=self.bank_bac_a,
            amount=Decimal('3000.00'),
            user=self.cashier_user,
            notes="Remesa valija 01",
        )
        self.assertEqual(rem.status, CashRemittance.Status.PREPARED)
        self.assertEqual(rem.amount, Decimal('3000.00'))
        # Efectivo en gaveta sigue intacto (aún no se entrega al custodio)
        shift.refresh_from_db()
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('5000.00'))
        # Saldo bancario sigue intacto
        self.bank_bac_a.refresh_from_db()
        self.assertEqual(self.bank_bac_a.current_balance, initial_bank_bal)

        # Fase 2: IN_TRANSIT (Entrega física al custodio / mensajero)
        dispatched_rem = dispatch_cash_remittance(
            remittance=rem,
            user=self.cashier_user,
            carrier_name="Juan Pérez (Custodio UltraVal)",
            carrier_id="001-010190-0001A",
        )
        self.assertEqual(dispatched_rem.status, CashRemittance.Status.IN_TRANSIT)
        # El dinero SALIÓ físicamente de la gaveta: saldo esperado baja a 2000.00 C$
        shift.refresh_from_db()
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('2000.00'))
        self.assertEqual(shift.total_remittances_nio, Decimal('3000.00'))

        # Se generó la póliza contable de Efectivo en Tránsito
        self.assertIsNotNone(dispatched_rem.transit_journal_entry)
        transit_entry = dispatched_rem.transit_journal_entry
        self.assertEqual(transit_entry.lines.filter(account__code='1101-04', debit=Decimal('3000.00')).count(), 1)
        self.assertEqual(transit_entry.lines.filter(account__code='1101-01', credit=Decimal('3000.00')).count(), 1)

        # Saldo bancario NO debe haberse incrementado aún
        self.bank_bac_a.refresh_from_db()
        self.assertEqual(self.bank_bac_a.current_balance, initial_bank_bal)

        # Fase 3: DEPOSITED (Asentamiento de la minuta de depósito bancario)
        deposited_rem = record_remittance_bank_deposit(
            remittance=dispatched_rem,
            deposit_slip_number="MINUTA-BAC-2026-999",
            deposit_date=timezone.now().date(),
            user=self.cashier_user,
            notes="Entregada en ventanilla Sucursal Metrocentro",
        )
        self.assertEqual(deposited_rem.status, CashRemittance.Status.DEPOSITED)
        self.assertEqual(deposited_rem.deposit_slip_number, "MINUTA-BAC-2026-999")

        # Saldo bancario sigue intacto hasta confirmación
        self.bank_bac_a.refresh_from_db()
        self.assertEqual(self.bank_bac_a.current_balance, initial_bank_bal)

        # Fase 4: CONFIRMED (Acreditación bancaria confirmada)
        confirmed_rem = confirm_remittance_deposit(
            remittance=deposited_rem,
            user=self.supervisor_user,
        )
        self.assertEqual(confirmed_rem.status, CashRemittance.Status.CONFIRMED)

        # Saldo bancario se incrementa exactamente una sola vez (+3000 C$)
        self.bank_bac_a.refresh_from_db()
        self.assertEqual(self.bank_bac_a.current_balance, initial_bank_bal + Decimal('3000.00'))

        # Se generó la FinancialTransaction inmutable para el extracto
        self.assertIsNotNone(confirmed_rem.financial_transaction)
        trx = confirmed_rem.financial_transaction
        self.assertEqual(trx.transaction_type, FinancialTransaction.TransactionType.CASH_BANK_DEPOSIT)
        self.assertEqual(trx.amount, Decimal('3000.00'))
        self.assertEqual(trx.reference_code, "MINUTA-BAC-2026-999")

        # Se generó la póliza definitiva de cancelación de tránsito contra bancos
        self.assertIsNotNone(confirmed_rem.confirmation_journal_entry)
        conf_entry = confirmed_rem.confirmation_journal_entry
        self.assertEqual(conf_entry.lines.filter(account__code='1102-01', debit=Decimal('3000.00')).count(), 1)
        self.assertEqual(conf_entry.lines.filter(account__code='1101-04', credit=Decimal('3000.00')).count(), 1)

    # --------------------------------------------------------------------------
    # 12. Prevención de Doble Depósito y Referencias Duplicadas
    # --------------------------------------------------------------------------
    def test_remittance_duplicate_deposit_slip_prevention(self):
        shift = open_pos_shift(organization=self.org_a, cashier=self.cashier_user, opening_cash_nio=Decimal('4000.00'))

        rem1 = prepare_cash_remittance(shift=shift, destination_bank_account=self.bank_bac_a, amount=Decimal('1000.00'), user=self.cashier_user)
        dispatch_cash_remittance(remittance=rem1, user=self.cashier_user)
        record_remittance_bank_deposit(remittance=rem1, deposit_slip_number="MINUTA-DUPLICADA-01", deposit_date=timezone.now().date(), user=self.cashier_user)
        confirm_remittance_deposit(remittance=rem1, user=self.supervisor_user)

        # Intentar asentar la misma boleta en una segunda remesa para la misma cuenta bancaria: DEBE FALLAR
        rem2 = prepare_cash_remittance(shift=shift, destination_bank_account=self.bank_bac_a, amount=Decimal('1000.00'), user=self.cashier_user)
        dispatch_cash_remittance(remittance=rem2, user=self.cashier_user)

        with self.assertRaises(ValidationError) as ctx:
            record_remittance_bank_deposit(
                remittance=rem2,
                deposit_slip_number="MINUTA-DUPLICADA-01",
                deposit_date=timezone.now().date(),
                user=self.cashier_user,
            )
        self.assertIn("ya fue registrada previamente", str(ctx.exception))

    # --------------------------------------------------------------------------
    # 13. Rechazo de Remesa y Reversión Contable de Efectivo en Tránsito
    # --------------------------------------------------------------------------
    def test_remittance_rejection_and_transit_reversal(self):
        shift = open_pos_shift(organization=self.org_a, cashier=self.cashier_user, opening_cash_nio=Decimal('3000.00'))

        rem = prepare_cash_remittance(shift=shift, destination_bank_account=self.bank_bac_a, amount=Decimal('1500.00'), user=self.cashier_user)
        dispatch_cash_remittance(remittance=rem, user=self.cashier_user, carrier_name="Mensajero")

        shift.refresh_from_db()
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('1500.00'))

        # Rechazo formal de la remesa (ej: ventanilla cerrada / billetes no admitidos)
        rejected_rem = reject_cash_remittance(
            remittance=rem,
            user=self.supervisor_user,
            rejection_reason="Ventanilla bancaria cerrada y valija devuelta íntegra a caja",
        )
        self.assertEqual(rejected_rem.status, CashRemittance.Status.REJECTED)

        # Fondos reintegrados a caja
        shift.refresh_from_db()
        self.assertEqual(shift.expected_cash_in_drawer_nio, Decimal('3000.00'))

        # Saldo bancario nunca fue alterado
        self.bank_bac_a.refresh_from_db()
        self.assertEqual(self.bank_bac_a.current_balance, Decimal('50000.00'))

    # --------------------------------------------------------------------------
    # 14. Integración con Conciliación Bancaria (Fase 1C.5.1)
    # --------------------------------------------------------------------------
    def test_reconciliation_integration_with_phase1c5_1(self):
        shift = open_pos_shift(organization=self.org_a, cashier=self.cashier_user, opening_cash_nio=Decimal('3000.00'))
        rem = prepare_cash_remittance(shift=shift, destination_bank_account=self.bank_bac_a, amount=Decimal('2000.00'), user=self.cashier_user)
        dispatch_cash_remittance(remittance=rem, user=self.cashier_user)
        record_remittance_bank_deposit(remittance=rem, deposit_slip_number="MINUTA-CONCILIAR-777", deposit_date=timezone.now().date(), user=self.cashier_user)
        rem = confirm_remittance_deposit(remittance=rem, user=self.supervisor_user)
        rem.refresh_from_db()

        trx = rem.financial_transaction
        self.assertIsNotNone(trx)

        # Crear extracto bancario conteniendo la línea de crédito por el depósito
        statement = BankStatement.objects.create(
            organization=self.org_a,
            bank_account=self.bank_bac_a,
            statement_number="EXT-BAC-OCT-2026-01",
            file_name="extracto_bac_octubre.csv",
            file_hash=hashlib.sha256(b"statement-hash-rem-1").hexdigest(),
            period_start=timezone.now().date(),
            period_end=timezone.now().date(),
            initial_balance=Decimal('50000.00'),
            final_balance=Decimal('52000.00'),
            imported_by=self.supervisor_user,
        )

        line = BankStatementLine.objects.create(
            organization=self.org_a,
            statement=statement,
            bank_account=self.bank_bac_a,
            line_number=1,
            post_date=timezone.now().date(),
            movement_type=BankStatementLine.MovementType.CREDIT,
            amount=Decimal('2000.00'),
            currency='NIO',
            reference="MINUTA-CONCILIAR-777",
            description="DEPÓSITO REMESA EFECTIVO MINUTA-CONCILIAR-777",
            line_hash=hashlib.sha256(b"line-hash-rem-1").hexdigest(),
        )

        # Emparejar la línea del extracto con la transacción de la remesa
        line.matched_transaction = trx
        line.status = BankStatementLine.LineStatus.RECONCILED
        line.save()

        # Verificar conciliación sin duplicación
        line.refresh_from_db()
        self.assertEqual(line.status, BankStatementLine.LineStatus.RECONCILED)
        self.assertEqual(line.matched_transaction, trx)
        self.assertTrue(trx.is_reconciled)

    # --------------------------------------------------------------------------
    # 15. Rollback Transaccional Atómico ante Errores
    # --------------------------------------------------------------------------
    def test_transactional_rollback_on_failure(self):
        shift = open_pos_shift(organization=self.org_a, cashier=self.cashier_user, opening_cash_nio=Decimal('2000.00'))

        # Intentar despacho con monto que supera el saldo en caja (debe abortar sin crear movimientos residuales)
        rem = prepare_cash_remittance(shift=shift, destination_bank_account=self.bank_bac_a, amount=Decimal('1000.00'), user=self.cashier_user)

        # Forzar alteración externa para reducir saldo
        shift.opening_cash_nio = Decimal('500.00')
        super(PosRegisterShift, shift).save(update_fields=['opening_cash_nio'])
        compute_shift_x_cut_metrics(shift)

        with self.assertRaises(ValidationError):
            dispatch_cash_remittance(remittance=rem, user=self.cashier_user)

        # Comprobar que remesa sigue PREPARED y no se generó ningún JournalEntry ni CashMovement de despacho
        rem.refresh_from_db()
        self.assertEqual(rem.status, CashRemittance.Status.PREPARED)
        self.assertEqual(CashMovement.objects.filter(remittance=rem).count(), 0)
