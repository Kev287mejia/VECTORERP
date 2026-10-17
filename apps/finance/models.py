import uuid
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.tenancy.models import TenantScopedModel


class BankAccount(TenantScopedModel):
    """
    Cuenta Bancaria Institucional del Tenant (BAC, Banpro, Lafise, BDF, Ficohsa).
    Manejo bimonetario en Córdobas (NIO C$) y Dólares (USD $).
    """
    class AccountType(models.TextChoices):
        CHECKING = 'CHECKING', 'Cuenta Corriente'
        SAVINGS = 'SAVINGS', 'Cuenta de Ahorro'

    class BankName(models.TextChoices):
        BAC = 'BAC', 'BAC Credomatic Nicaragua'
        BANPRO = 'BANPRO', 'Banco de la Producción (Banpro)'
        LAFISE = 'LAFISE', 'Banco LAFISE Bancentro'
        BDF = 'BDF', 'Banco de Finanzas (BDF)'
        FICOHSA = 'FICOHSA', 'Banco Ficohsa Nicaragua'
        OTHER = 'OTHER', 'Otra Institución Financiera'

    bank_name = models.CharField(
        max_length=40,
        choices=BankName.choices,
        default=BankName.BAC,
        verbose_name="Institución Bancaria",
    )
    account_number = models.CharField(
        max_length=40,
        db_index=True,
        verbose_name="Número de Cuenta Bancaria",
        help_text="ej: 100-234567-89",
    )
    currency = models.CharField(
        max_length=10,
        default='NIO',
        verbose_name="Moneda de la Cuenta",
        help_text="NIO o USD",
    )
    account_type = models.CharField(
        max_length=20,
        choices=AccountType.choices,
        default=AccountType.CHECKING,
        verbose_name="Tipo de Cuenta",
    )
    current_balance = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Saldo en Libros",
    )
    description = models.CharField(
        max_length=120,
        blank=True,
        verbose_name="Descripción / Uso Operativo",
        help_text="ej: Cuenta Principal de Operaciones y Planilla",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="Cuenta Activa",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Cuenta Bancaria"
        verbose_name_plural = "Cuentas Bancarias"
        ordering = ['bank_name', 'account_number']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'account_number'],
                name='unique_bank_account_per_tenant',
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_bank_name_display()} · {self.account_number} [{self.currency}]"


class PettyCashFund(TenantScopedModel):
    """
    Caja Chica o Fondo Fijo Operativo asignado a un Custodio (Supervisor / Capataz de Cuadrilla).
    Permite cubrir gastos menores en frentes de obra y liquidar viáticos, combustible y compras de emergencia.
    """
    code = models.CharField(
        max_length=20,
        db_index=True,
        blank=True,
        verbose_name="Código de Caja Chica",
        help_text="ej: CAJ-01, FND-ALFA",
    )
    name = models.CharField(
        max_length=120,
        verbose_name="Nombre de la Caja / Fondo Fijo",
        help_text="ej: Fondo Operativo Cuadrilla Alfa, Caja Chica Plantel Managua",
    )
    custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='custodied_petty_cash_funds',
        verbose_name="Custodio Responsable (Supervisor / Ingeniero)",
    )
    linked_crew = models.ForeignKey(
        'operations.Crew',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='petty_cash_funds',
        verbose_name="Cuadrilla Vinculada",
    )
    currency = models.CharField(
        max_length=10,
        default='NIO',
        verbose_name="Moneda",
    )
    allocated_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('10000.00'),
        verbose_name="Fondo Fijo Asignado (C$)",
        help_text="Límite máximo formal autorizado para este fondo fijo",
    )
    current_balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Saldo Disponible en Mano (C$)",
        help_text="Efectivo físico disponible antes de reposición",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="Fondo Activo",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Caja Chica / Fondo Fijo"
        verbose_name_plural = "Cajas Chicas y Fondos Operativos"
        ordering = ['code']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'code'],
                name='unique_petty_cash_code_per_tenant',
            ),
            models.CheckConstraint(
                check=models.Q(current_balance__lte=models.F('allocated_amount')),
                name='petty_cash_fund_balance_lte_allocated',
            ),
            models.CheckConstraint(
                check=models.Q(current_balance__gte=Decimal('0.00')),
                name='petty_cash_fund_balance_gte_zero',
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} · {self.name} ({self.custodian.full_name or self.custodian.email})"

    @property
    def spent_amount(self) -> Decimal:
        """Monto ejecutado pendiente de reposición."""
        return max(Decimal('0.00'), self.allocated_amount - self.current_balance)

    @property
    def spent_percentage(self) -> int:
        """Porcentaje del fondo consumido."""
        if self.allocated_amount and self.allocated_amount > Decimal('0.00'):
            pct = int((self.spent_amount / self.allocated_amount) * Decimal('100'))
            return min(100, max(0, pct))
        return 0

    def save(self, *args, **kwargs):
        if not self.code and self.organization_id:
            count = PettyCashFund.objects.filter(organization_id=self.organization_id).count() + 1
            candidate = f"CAJ-{count:02d}"
            while PettyCashFund.objects.filter(organization_id=self.organization_id, code=candidate).exists():
                count += 1
                candidate = f"CAJ-{count:02d}"
            self.code = candidate
        super().save(*args, **kwargs)


class PettyCashReplenishment(TenantScopedModel):
    """
    Arqueo y Solicitud de Reposición de Fondo Fijo.
    Agrupa comprobantes de gastos de campo y solicita su desembolso desde cuenta bancaria.
    """
    class Status(models.TextChoices):
        SUBMITTED = 'SUBMITTED', 'Enviado / En Revisión'
        APPROVED = 'APPROVED', 'Aprobado para Pago'
        DISBURSED = 'DISBURSED', 'Reembolsado / Liquidado'
        REJECTED = 'REJECTED', 'Rechazado'

    code = models.CharField(
        max_length=25,
        db_index=True,
        blank=True,
        verbose_name="Folio de Reposición",
        help_text="ej: REP-2026-0001",
    )
    fund = models.ForeignKey(
        PettyCashFund,
        on_delete=models.PROTECT,
        related_name='replenishments',
        verbose_name="Caja Chica a Reembolsar",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SUBMITTED,
        db_index=True,
        verbose_name="Estado de Reposición",
    )
    total_expenses_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Suma de Gastos Comprobados (C$)",
    )
    requested_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Monto Solicitado a Desembolsar (C$)",
    )
    notes = models.TextField(
        blank=True,
        verbose_name="Observaciones de Liquidación / Auditoría",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='submitted_replenishments',
        verbose_name="Custodio Solicitante",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_replenishments',
        verbose_name="Aprobado Por (Coordinación / Gerencia)",
    )
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Fecha de Aprobación",
    )
    disbursed_from_bank = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='petty_cash_disbursements',
        verbose_name="Cuenta Bancaria Origen del Desembolso",
    )
    disbursed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Fecha de Desembolso / Transferencia",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Reposición de Caja Chica"
        verbose_name_plural = "Reposiciones de Fondo Fijo"
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"{self.code} · {self.fund.name} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if not self.code and self.organization_id:
            year = timezone.now().year
            count = PettyCashReplenishment.objects.filter(
                organization_id=self.organization_id,
                code__startswith=f"REP-{year}-"
            ).count() + 1
            candidate = f"REP-{year}-{count:04d}"
            while PettyCashReplenishment.objects.filter(organization_id=self.organization_id, code=candidate).exists():
                count += 1
                candidate = f"REP-{year}-{count:04d}"
            self.code = candidate
        super().save(*args, **kwargs)


class PettyCashExpense(TenantScopedModel):
    """
    Comprobante Individual de Gasto Operativo de Campo.
    Se imputa directamente a la Caja Chica y opcionalmente a una Orden de Trabajo (OT) de Operaciones.
    """
    class Category(models.TextChoices):
        FUEL = 'FUEL', 'Combustible y Lubricantes (Gasolina / Diesel)'
        PER_DIEM = 'PER_DIEM', 'Viáticos y Alimentación de Cuadrilla'
        TOLL_PARKING = 'TOLL_PARKING', 'Peajes y Estacionamientos'
        EMERGENCY_HARDWARE = 'EMERGENCY_HARDWARE', 'Ferretería y Compras de Emergencia en Obra'
        FREIGHT_LOCAL = 'FREIGHT_LOCAL', 'Fletes y Transporte Local'
        OTHER_OPERATIONAL = 'OTHER_OPERATIONAL', 'Otros Gastos Menores de Campo'

    voucher_number = models.CharField(
        max_length=25,
        db_index=True,
        blank=True,
        verbose_name="Folio de Comprobante",
        help_text="ej: REC-2026-0001",
    )
    fund = models.ForeignKey(
        PettyCashFund,
        on_delete=models.CASCADE,
        related_name='expenses',
        verbose_name="Caja Chica Afectada",
    )
    work_order = models.ForeignKey(
        'operations.WorkOrder',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='petty_cash_expenses',
        verbose_name="Orden de Trabajo (Frente de Obra Imputable)",
        help_text="Asigna este costo operativo directamente a la OT",
    )
    expense_category = models.CharField(
        max_length=30,
        choices=Category.choices,
        default=Category.FUEL,
        verbose_name="Rubro / Categoría de Gasto",
    )
    vendor_name = models.CharField(
        max_length=150,
        verbose_name="Comercio / Proveedor",
        help_text="ej: Gasolinera Puma Tipitapa, Ferretería El Halcón",
    )
    vendor_tax_id = models.CharField(
        max_length=30,
        blank=True,
        verbose_name="RUC / Cédula del Comercio",
        help_text="Identificación tributaria del proveedor",
    )
    invoice_number = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Nº Factura / Conduce / Recibo",
        help_text="Número fiscal impreso en el comprobante",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Monto del Gasto (C$)",
    )
    description = models.CharField(
        max_length=255,
        verbose_name="Concepto / Justificación Técnica",
        help_text="ej: 35 Galones de Diesel para Hilux Cuadrilla Alfa - Frentes Chinandega",
    )
    spent_at = models.DateField(
        default=timezone.now,
        verbose_name="Fecha del Comprobante",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='registered_petty_expenses',
        verbose_name="Registrado Por",
    )
    replenishment = models.ForeignKey(
        PettyCashReplenishment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expenses',
        verbose_name="Lote de Reposición / Arqueo",
    )
    is_replenished = models.BooleanField(
        default=False,
        verbose_name="Gasto Ya Reembolsado",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Comprobante de Caja Chica"
        verbose_name_plural = "Comprobantes de Caja Chica"
        ordering = ['-spent_at', '-created_at']

    def __str__(self) -> str:
        return f"{self.voucher_number} · {self.vendor_name} ({self.amount} C$)"

    def clean(self):
        super().clean()
        if self.is_replenished and not self._state.adding:
            original = PettyCashExpense.objects.filter(pk=self.pk).first()
            if original and original.is_replenished and (original.amount != self.amount or original.fund_id != self.fund_id):
                raise ValidationError("No se puede modificar un comprobante de gasto que ya fue liquidado y reembolsado.")

    def save(self, *args, **kwargs):
        if not self.voucher_number and self.organization_id:
            year = timezone.now().year
            count = PettyCashExpense.objects.filter(
                organization_id=self.organization_id,
                voucher_number__startswith=f"REC-{year}-"
            ).count() + 1
            candidate = f"REC-{year}-{count:04d}"
            while PettyCashExpense.objects.filter(organization_id=self.organization_id, voucher_number=candidate).exists():
                count += 1
                candidate = f"REC-{year}-{count:04d}"
            self.voucher_number = candidate
        super().save(*args, **kwargs)


class FinancialTransaction(TenantScopedModel):
    """
    Libro Mayor de Tesorería, Caja y Bancos (Kardex Financiero Inmutable).
    Garantiza trazabilidad legal: todo desembolso, reposición o transferencia
    queda asentado de forma no editable ni eliminable.
    """
    class TransactionType(models.TextChoices):
        PETTY_CASH_ALLOCATION = 'PETTY_CASH_ALLOCATION', 'Apertura / Dotación Inicial de Fondo Fijo'
        PETTY_CASH_EXPENSE = 'PETTY_CASH_EXPENSE', 'Egreso de Caja Chica por Gasto de Campo'
        PETTY_CASH_REPLENISH = 'PETTY_CASH_REPLENISH', 'Desembolso Bancario de Reposición'
        BANK_TRANSFER = 'BANK_TRANSFER', 'Transferencia entre Cuentas Bancarias'
        CLIENT_COLLECTION = 'CLIENT_COLLECTION', 'Cobro de Factura a Cliente'
        VENDOR_DISBURSEMENT = 'VENDOR_DISBURSEMENT', 'Pago a Proveedor con Retenciones'
        CARD_SETTLEMENT = 'CARD_SETTLEMENT', 'Liquidación de Lote de Tarjetas'
        CHECK_CLEARING = 'CHECK_CLEARING', 'Compensación y Acreditación de Cheque'
        CHECK_REJECTION = 'CHECK_REJECTION', 'Rechazo de Cheque'
        CASH_BANK_DEPOSIT = 'CASH_BANK_DEPOSIT', 'Depósito Bancario de Efectivo'
        CASH_REMITTANCE_DEPOSIT = 'CASH_REMITTANCE_DEPOSIT', 'Depósito de Remesa de Efectivo (Legacy)'

    number = models.CharField(
        max_length=25,
        db_index=True,
        blank=True,
        verbose_name="Folio de Transacción",
        help_text="ej: TRX-2026-0001",
    )
    transaction_type = models.CharField(
        max_length=35,
        choices=TransactionType.choices,
        verbose_name="Tipo de Transacción",
    )
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='transactions',
        verbose_name="Cuenta Bancaria Imputable",
    )
    petty_cash_fund = models.ForeignKey(
        PettyCashFund,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='transactions',
        verbose_name="Caja Chica Imputable",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        verbose_name="Monto Transado (C$)",
    )
    balance_after = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        verbose_name="Saldo Resultante en la Cuenta/Caja",
    )
    concept = models.CharField(
        max_length=255,
        verbose_name="Concepto / Detalle de la Operación",
    )
    reference_code = models.CharField(
        max_length=80,
        blank=True,
        verbose_name="Nº Referencia / Minuta Bancaria / Folio Recibo",
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        verbose_name="Operador Responsable",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Transacción Financiera"
        verbose_name_plural = "Libro Mayor de Tesorería (Transacciones)"
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"{self.number} · {self.get_transaction_type_display()} ({self.amount} C$)"

    def clean(self):
        super().clean()
        if not self._state.adding:
            raise ValidationError("El registro de auditoría financiera es inmutable. Prohibido modificar transacciones asentadas.")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("El registro de auditoría financiera es inmutable. Prohibido modificar transacciones asentadas.")

        if not self.number and self.organization_id:
            year = timezone.now().year
            count = FinancialTransaction.objects.filter(
                organization_id=self.organization_id,
                number__startswith=f"TRX-{year}-"
            ).count() + 1
            candidate = f"TRX-{year}-{count:04d}"
            while FinancialTransaction.objects.filter(organization_id=self.organization_id, number=candidate).exists():
                count += 1
                candidate = f"TRX-{year}-{count:04d}"
            self.number = candidate

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("El registro de auditoría financiera es inmutable. Prohibido eliminar transacciones históricas.")

    @property
    def is_reconciled(self) -> bool:
        """Indica si la transacción fue conciliada con una línea de extracto bancario."""
        try:
            if hasattr(self, 'reconciliation_match') and self.reconciliation_match is not None:
                return True
        except Exception:
            pass
        try:
            if hasattr(self, 'reconciled_statement_line') and self.reconciled_statement_line is not None:
                return True
        except Exception:
            pass
        return False


class AccountingAccount(TenantScopedModel):
    """
    Catálogo de Cuentas Contables NIIF / DGI Nicaragua.
    Estructura jerárquica para partida doble de compras, ventas, impuestos y cartera.
    """
    class AccountType(models.TextChoices):
        ASSET = 'ASSET', 'Activo'
        LIABILITY = 'LIABILITY', 'Pasivo'
        EQUITY = 'EQUITY', 'Patrimonio'
        INCOME = 'INCOME', 'Ingresos'
        EXPENSE = 'EXPENSE', 'Costos y Gastos'

    code = models.CharField(
        max_length=30,
        db_index=True,
        verbose_name="Código de Cuenta",
        help_text="ej: 1101-01, 2101-01",
    )
    name = models.CharField(
        max_length=150,
        verbose_name="Nombre de la Cuenta",
        help_text="ej: Caja General, Clientes por Cobrar",
    )
    account_type = models.CharField(
        max_length=20,
        choices=AccountType.choices,
        default=AccountType.ASSET,
        verbose_name="Tipo de Cuenta",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="Cuenta Activa",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Cuenta Contable"
        verbose_name_plural = "Catálogo de Cuentas Contables"
        ordering = ['code']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'code'],
                name='unique_accounting_account_code_per_tenant',
            )
        ]

    def __str__(self) -> str:
        return f"{self.code} · {self.name} [{self.get_account_type_display()}]"


class JournalEntry(TenantScopedModel):
    """
    Póliza Contable / Asiento de Partida Doble Cuadrada (Debe == Haber).
    Trazabilidad de operaciones comerciales, fiscales y de tesorería.
    """
    class EntryType(models.TextChoices):
        SALES = 'SALES', 'Póliza de Ventas / Facturación DGI'
        PURCHASES = 'PURCHASES', 'Póliza de Compras / Recepción'
        COLLECTIONS = 'COLLECTIONS', 'Póliza de Recaudación / Caja'
        PAYMENTS = 'PAYMENTS', 'Póliza de Egresos / Pagos'
        ADJUSTMENT = 'ADJUSTMENT', 'Póliza de Diario / Ajuste'

    number = models.CharField(
        max_length=25,
        db_index=True,
        verbose_name="N° Póliza Contable",
        help_text="Consecutivo oficial ej: POL-2026-0001",
    )
    date = models.DateField(
        default=timezone.now,
        verbose_name="Fecha Contable",
    )
    entry_type = models.CharField(
        max_length=20,
        choices=EntryType.choices,
        default=EntryType.SALES,
        verbose_name="Tipo de Póliza",
    )
    concept = models.CharField(
        max_length=255,
        verbose_name="Concepto de la Póliza",
    )
    reference = models.CharField(
        max_length=80,
        blank=True,
        verbose_name="Documento Fuente / Folio",
        help_text="ej: FAC-2026-0001, OC-2026-0001",
    )
    source_invoice = models.ForeignKey(
        'billing.Invoice',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='journal_entries',
        verbose_name="Factura Fuente",
    )
    source_purchase_order = models.ForeignKey(
        'purchasing.PurchaseOrder',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='journal_entries',
        verbose_name="Orden de Compra Fuente",
    )
    source_payment_receipt = models.ForeignKey(
        'billing.PaymentReceipt',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='journal_entries',
        verbose_name="Recibo de Cobro Fuente",
    )
    source_supplier_invoice = models.ForeignKey(
        'purchasing.SupplierInvoice',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='journal_entries',
        verbose_name="Factura de Proveedor Fuente",
    )
    source_supplier_payment = models.ForeignKey(
        'purchasing.SupplierPayment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='journal_entries',
        verbose_name="Egreso / Pago Fuente",
    )
    source_card_settlement = models.ForeignKey(
        'finance.CardBatchSettlement',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='journal_entries',
        verbose_name="Liquidación de Tarjetas Fuente",
    )
    total_debit = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total Debe (C$)",
    )
    total_credit = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total Haber (C$)",
    )
    is_balanced = models.BooleanField(
        default=False,
        verbose_name="Póliza Cuadrada",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Contador / Registrado Por",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Póliza Contable"
        verbose_name_plural = "Pólizas Contables (Libro Diario)"
        ordering = ['-date', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'number'],
                name='unique_journal_entry_number_per_tenant',
            ),
            models.UniqueConstraint(
                fields=['organization', 'source_invoice', 'entry_type'],
                condition=models.Q(source_invoice__isnull=False, entry_type='SALES'),
                name='unique_sales_journal_entry_per_invoice',
            ),
            models.UniqueConstraint(
                fields=['organization', 'source_payment_receipt', 'entry_type'],
                condition=models.Q(source_payment_receipt__isnull=False, entry_type='COLLECTIONS'),
                name='unique_collection_journal_entry_per_receipt',
            ),
            models.UniqueConstraint(
                fields=['organization', 'source_purchase_order', 'entry_type'],
                condition=models.Q(source_purchase_order__isnull=False, entry_type='PURCHASES'),
                name='unique_purchases_journal_entry_per_po',
            ),
            models.UniqueConstraint(
                fields=['organization', 'source_supplier_payment', 'entry_type'],
                condition=models.Q(source_supplier_payment__isnull=False, entry_type='PAYMENTS'),
                name='unique_payments_journal_entry_per_supplier_payment',
            ),
        ]

    def __str__(self) -> str:
        return f"{self.number} · {self.concept} ({self.total_debit} C$)"

    def verify_and_save_balance(self):
        totals = self.lines.aggregate(
            d=models.Sum('debit'),
            c=models.Sum('credit'),
        )
        self.total_debit = totals['d'] or Decimal('0.00')
        self.total_credit = totals['c'] or Decimal('0.00')
        diff = abs(self.total_debit - self.total_credit)
        self.is_balanced = (diff <= Decimal('0.01'))
        JournalEntry.objects.filter(pk=self.pk).update(
            total_debit=self.total_debit,
            total_credit=self.total_credit,
            is_balanced=self.is_balanced,
            updated_at=timezone.now(),
        )
        return self.is_balanced

    def delete(self, *args, **kwargs):
        if self.pk:
            in_db = JournalEntry.objects.filter(pk=self.pk).values('is_balanced').first()
            if (in_db and in_db['is_balanced']) or self.is_balanced or self.lines.exists():
                raise ValidationError(
                    "Las pólizas contables registradas en el Libro Diario son inmutables. "
                    "Prohibido eliminar asientos contables asentados. Utilice pólizas de reversión o ajuste."
                )
        super().delete(*args, **kwargs)

    def save(self, *args, **kwargs):
        if not self._state.adding and self.pk:
            in_db = JournalEntry.objects.filter(pk=self.pk).values('is_balanced').first()
            if in_db and in_db['is_balanced']:
                raise ValidationError(
                    "Las pólizas contables cuadradas y asentadas son inmutables. "
                    "Prohibido modificar una póliza contable cerrada."
                )

        if not self.number and self.organization_id:
            year = self.date.year if self.date else timezone.now().year
            prefix = f"POL-{year}-"
            count = (
                JournalEntry.objects.filter(
                    organization_id=self.organization_id,
                    number__startswith=prefix,
                ).count()
                + 1
            )
            candidate = f"{prefix}{count:04d}"
            while JournalEntry.objects.filter(
                organization_id=self.organization_id, number=candidate
            ).exists():
                count += 1
                candidate = f"{prefix}{count:04d}"
            self.number = candidate

        super().save(*args, **kwargs)


class JournalEntryLine(TenantScopedModel):
    """
    Partida Individual de Póliza Contable (Línea de Asiento).
    """
    entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        related_name='lines',
        verbose_name="Póliza Contable",
    )
    account = models.ForeignKey(
        AccountingAccount,
        on_delete=models.PROTECT,
        related_name='entry_lines',
        verbose_name="Cuenta Contable",
    )
    concept = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Detalle de la Línea",
    )
    debit = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Debe (C$)",
    )
    credit = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Haber (C$)",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Línea de Póliza"
        verbose_name_plural = "Líneas de Póliza"
        ordering = ['created_at']

    def __str__(self) -> str:
        return f"{self.entry.number} · {self.account.code} | D:{self.debit} C:{self.credit}"

    def delete(self, *args, **kwargs):
        if self.pk:
            raise ValidationError(
                "Las líneas de póliza contable son inmutables. Prohibido eliminar partidas asentadas."
            )
        super().delete(*args, **kwargs)

    def save(self, *args, **kwargs):
        if not self._state.adding and self.pk:
            raise ValidationError(
                "Las líneas de póliza contable son inmutables. Prohibido modificar partidas contables asentadas."
            )
        if self.entry_id:
            entry_balanced = False
            if hasattr(self, 'entry') and self.entry and self.entry.is_balanced:
                entry_balanced = True
            else:
                entry_balanced = JournalEntry.objects.filter(pk=self.entry_id, is_balanced=True).exists()
            if entry_balanced:
                raise ValidationError(
                    "No se pueden agregar partidas contables a una póliza que ya ha sido cuadrada y asentada."
                )
        super().save(*args, **kwargs)


class CardBatchSettlement(TenantScopedModel):
    """
    Liquidación y Conciliación de Lotes de Tarjetas de Crédito / Débito (Adquirente / BAC / Banpro / Red Enlace).
    Concilia el importe bruto pendiente en cuenta transitoria (1101-03), deduce la comisión bancaria
    y retenciones fiscales aplicables, y acredita los fondos netos líquidos en la cuenta bancaria.
    """
    class Status(models.TextChoices):
        SETTLED = 'SETTLED', 'Liquidado y Acreditado'
        VOID = 'VOID', 'Anulado'

    settlement_number = models.CharField(
        max_length=25,
        db_index=True,
        verbose_name="N° de Liquidación",
        help_text="Folio secuencial de liquidación ej: LIQ-2026-0001",
    )
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        related_name='card_settlements',
        verbose_name="Cuenta Bancaria Destino",
    )
    batch_reference = models.CharField(
        max_length=80,
        verbose_name="N° de Lote / Terminal / Cierre Adquirente",
        help_text="ej: LOTE-BAC-99823",
    )
    settlement_date = models.DateField(
        default=timezone.now,
        verbose_name="Fecha de Liquidación Bancaria",
    )
    currency = models.CharField(
        max_length=10,
        default='NIO',
        verbose_name="Moneda",
    )
    gross_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        verbose_name="Monto Bruto Liquidado (C$)",
        help_text="Monto total procesado en vouchers de tarjeta",
    )
    fee_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Comisión del Procesador / Adquirente (C$)",
    )
    tax_retention_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Retención Fiscal Aplicada (IR / IMI) (C$)",
    )
    net_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        verbose_name="Monto Neto Acreditado en Banco (C$)",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SETTLED,
        verbose_name="Estado de Liquidación",
    )
    idempotency_key = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        verbose_name="Clave de Idempotencia",
    )
    notes = models.TextField(
        blank=True,
        verbose_name="Observaciones / Comprobante de Liquidación",
    )
    financial_transaction = models.ForeignKey(
        FinancialTransaction,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='card_settlements',
        verbose_name="Transacción Financiera Bancaria",
    )
    journal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='card_settlements',
        verbose_name="Póliza Contable de Liquidación",
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        verbose_name="Operador / Contador",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Liquidación de Lote de Tarjetas"
        verbose_name_plural = "Liquidaciones de Lotes de Tarjetas"
        ordering = ['-settlement_date', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'settlement_number'],
                name='unique_card_settlement_number_per_tenant',
            ),
            models.UniqueConstraint(
                fields=['organization', 'idempotency_key'],
                condition=models.Q(idempotency_key__isnull=False) & ~models.Q(idempotency_key=''),
                name='unique_card_settlement_idempotency_key',
            ),
            models.UniqueConstraint(
                fields=['organization', 'bank_account', 'batch_reference'],
                name='unique_bank_batch_reference_per_tenant',
            ),
        ]

    def __str__(self) -> str:
        return f"{self.settlement_number} · Lote {self.batch_reference} -> {self.bank_account.account_number} ({self.net_amount} {self.currency})"

    def save(self, *args, **kwargs):
        if not self.settlement_number and self.organization_id:
            year = self.settlement_date.year if self.settlement_date else timezone.now().year
            prefix = f"LIQ-{year}-"
            count = CardBatchSettlement.objects.filter(
                organization_id=self.organization_id,
                settlement_number__startswith=prefix
            ).count() + 1
            candidate = f"{prefix}{count:04d}"
            while CardBatchSettlement.objects.filter(
                organization_id=self.organization_id, settlement_number=candidate
            ).exists():
                count += 1
                candidate = f"{prefix}{count:04d}"
            self.settlement_number = candidate
        super().save(*args, **kwargs)


class CheckReceiptRegister(TenantScopedModel):
    """
    Control y Custodia de Cheques Recibidos en Cartera / Mostrador.
    Trazabilidad del ciclo de vida del cheque:
    RECEPCIÓN (Caja) -> DEPÓSITO (Remesa bancaria) -> COMPENSACIÓN / ACREDITACIÓN (o RECHAZO).
    """
    class Status(models.TextChoices):
        RECEIVED = 'RECEIVED', 'Recibido en Cartera / Custodia'
        DEPOSITED = 'DEPOSITED', 'Depositado / En Compensación'
        CLEARED = 'CLEARED', 'Compensado y Acreditado en Banco'
        REJECTED = 'REJECTED', 'Rechazado / Rebotado'

    check_number = models.CharField(
        max_length=50,
        db_index=True,
        verbose_name="Número de Cheque",
    )
    drawer_bank = models.CharField(
        max_length=80,
        blank=True,
        verbose_name="Banco Librador",
        help_text="ej: BAC, Banpro, Lafise, etc.",
    )
    drawer_name = models.CharField(
        max_length=150,
        blank=True,
        verbose_name="Librador / Titular de la Cuenta",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        verbose_name="Monto del Cheque (C$)",
    )
    currency = models.CharField(
        max_length=10,
        default='NIO',
        verbose_name="Moneda",
    )
    received_date = models.DateField(
        default=timezone.now,
        verbose_name="Fecha de Recepción",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RECEIVED,
        db_index=True,
        verbose_name="Estado del Cheque",
    )
    payment_receipt = models.ForeignKey(
        'billing.PaymentReceipt',
        on_delete=models.CASCADE,
        related_name='check_records',
        verbose_name="Recibo de Cobro Asociado",
    )
    source_invoice = models.ForeignKey(
        'billing.Invoice',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='check_records',
        verbose_name="Factura Liquidada",
    )
    deposit_bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deposited_checks',
        verbose_name="Cuenta Bancaria de Depósito",
    )
    deposit_slip_number = models.CharField(
        max_length=80,
        blank=True,
        verbose_name="N° Boleta / Minuta de Depósito",
    )
    cleared_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Fecha y Hora de Acreditación",
    )
    rejection_reason = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Motivo de Rechazo (si aplica)",
    )
    financial_transaction = models.ForeignKey(
        FinancialTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cleared_checks',
        verbose_name="Transacción Financiera de Acreditación",
    )
    clearing_journal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='check_entries',
        verbose_name="Póliza Contable de Compensación o Reversión",
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='received_checks',
        verbose_name="Cajero Receptor",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Cheque en Cartera"
        verbose_name_plural = "Cheques en Cartera y Depósitos"
        ordering = ['-received_date', '-created_at']

    def __str__(self) -> str:
        return f"{self.check_number} · {self.drawer_name} ({self.amount} {self.currency}) [{self.get_status_display()}]"


class BankStatement(TenantScopedModel):
    """
    Lote de Extracto Bancario importado para una cuenta bancaria (BAC, Banpro, Lafise, BDF, Ficohsa).
    Permite contrastar los movimientos del banco contra el libro mayor de tesorería del ERP.
    """
    class Status(models.TextChoices):
        IMPORTED = 'IMPORTED', 'Importado'
        IN_PROGRESS = 'IN_PROGRESS', 'En Proceso de Conciliación'
        RECONCILED = 'RECONCILED', 'Conciliado'
        ARCHIVED = 'ARCHIVED', 'Archivado'

    statement_number = models.CharField(
        max_length=30,
        db_index=True,
        blank=True,
        verbose_name="Folio de Extracto",
        help_text="ej: EXT-2026-0001",
    )
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        related_name='bank_statements',
        verbose_name="Cuenta Bancaria",
    )
    file_name = models.CharField(
        max_length=255,
        verbose_name="Nombre del Archivo",
    )
    file_hash = models.CharField(
        max_length=64,
        db_index=True,
        verbose_name="Hash SHA-256 del Archivo",
    )
    period_start = models.DateField(
        null=True,
        blank=True,
        verbose_name="Inicio de Periodo",
    )
    period_end = models.DateField(
        null=True,
        blank=True,
        verbose_name="Fin de Periodo",
    )
    initial_balance = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Saldo Inicial del Extracto",
    )
    final_balance = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Saldo Final del Extracto",
    )
    total_lines = models.PositiveIntegerField(
        default=0,
        verbose_name="Total de Líneas",
    )
    total_debits = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total Cargos / Débitos",
    )
    total_credits = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total Abonos / Créditos",
    )
    status = models.CharField(
        max_length=25,
        choices=Status.choices,
        default=Status.IMPORTED,
        db_index=True,
        verbose_name="Estado de Conciliación",
    )
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='imported_bank_statements',
        verbose_name="Importado por",
    )
    notes = models.TextField(
        blank=True,
        verbose_name="Notas de Conciliación",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Extracto Bancario"
        verbose_name_plural = "Extractos Bancarios"
        ordering = ['-period_end', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'bank_account', 'file_hash'],
                name='unique_bank_statement_file_per_account',
            ),
            models.UniqueConstraint(
                fields=['organization', 'statement_number'],
                name='unique_bank_statement_number_per_tenant',
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.statement_number and self.organization_id:
            year = self.period_end.year if self.period_end else timezone.now().year
            prefix = f"EXT-{year}-"
            count = BankStatement.objects.filter(
                organization_id=self.organization_id,
                statement_number__startswith=prefix
            ).count() + 1
            candidate = f"{prefix}{count:04d}"
            while BankStatement.objects.filter(organization_id=self.organization_id, statement_number=candidate).exists():
                count += 1
                candidate = f"{prefix}{count:04d}"
            self.statement_number = candidate
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.statement_number} · {self.bank_account} ({self.file_name}) [{self.get_status_display()}]"


class BankStatementLine(TenantScopedModel):
    """
    Línea individual de un extracto bancario importado.
    Representa un movimiento asentado por la institución financiera (débito/cargo o crédito/abono).
    """
    class MovementType(models.TextChoices):
        DEBIT = 'DEBIT', 'Cargo / Débito (Salida del Banco)'
        CREDIT = 'CREDIT', 'Abono / Crédito (Entrada al Banco)'

    class LineStatus(models.TextChoices):
        UNRECONCILED = 'UNRECONCILED', 'Pendiente de Conciliación'
        RECONCILED = 'RECONCILED', 'Conciliado'
        DISCREPANCY = 'DISCREPANCY', 'Con Discrepancia'
        IGNORED = 'IGNORED', 'Ignorado / Excluido'

    statement = models.ForeignKey(
        BankStatement,
        on_delete=models.CASCADE,
        related_name='lines',
        verbose_name="Extracto Bancario",
    )
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        related_name='statement_lines',
        verbose_name="Cuenta Bancaria",
    )
    line_number = models.PositiveIntegerField(
        verbose_name="N° Línea",
    )
    post_date = models.DateField(
        db_index=True,
        verbose_name="Fecha de Operación",
    )
    value_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Fecha Valor",
    )
    movement_type = models.CharField(
        max_length=10,
        choices=MovementType.choices,
        verbose_name="Tipo de Movimiento",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        verbose_name="Importe",
    )
    currency = models.CharField(
        max_length=10,
        default='NIO',
        verbose_name="Moneda",
    )
    reference = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        verbose_name="Referencia Bancaria",
    )
    description = models.CharField(
        max_length=255,
        verbose_name="Descripción / Concepto Bancario",
    )
    line_hash = models.CharField(
        max_length=64,
        db_index=True,
        verbose_name="Hash Único de Línea",
    )
    status = models.CharField(
        max_length=25,
        choices=LineStatus.choices,
        default=LineStatus.UNRECONCILED,
        db_index=True,
        verbose_name="Estado",
    )
    matched_transaction = models.OneToOneField(
        FinancialTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reconciled_statement_line',
        verbose_name="Transacción ERP Conciliada",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Línea de Extracto Bancario"
        verbose_name_plural = "Líneas de Extractos Bancarios"
        ordering = ['post_date', 'line_number']
        constraints = [
            models.UniqueConstraint(
                fields=['statement', 'line_number'],
                name='unique_statement_line_number',
            ),
            models.UniqueConstraint(
                fields=['organization', 'bank_account', 'line_hash'],
                name='unique_statement_line_hash_per_account',
            ),
        ]

    def clean(self):
        super().clean()
        if self.matched_transaction_id:
            if self.matched_transaction.organization_id != self.organization_id:
                raise ValidationError("La transacción emparejada no pertenece a la misma organización.")
            if self.matched_transaction.bank_account_id != self.bank_account_id:
                raise ValidationError("La transacción emparejada no pertenece a la misma cuenta bancaria.")
            if self.matched_transaction.amount != self.amount:
                raise ValidationError("El importe de la transacción emparejada difiere del importe de la línea.")

    def __str__(self) -> str:
        return f"Línea {self.line_number} [{self.post_date}] {self.get_movement_type_display()}: {self.amount} {self.currency} - {self.description[:30]}"


class BankReconciliationMatch(TenantScopedModel):
    """
    Registro de Conciliación y Emparejamiento entre una línea de extracto bancario y una transacción ERP.
    Garantiza trazabilidad de auditoría, usuario responsable y previene emparejamientos dobles.
    """
    class MatchType(models.TextChoices):
        EXACT = 'EXACT', 'Coincidencia Exacta'
        MANUAL = 'MANUAL', 'Coincidencia Manual / Revisada'

    statement_line = models.OneToOneField(
        BankStatementLine,
        on_delete=models.CASCADE,
        related_name='reconciliation_match',
        verbose_name="Línea de Extracto",
    )
    financial_transaction = models.OneToOneField(
        FinancialTransaction,
        on_delete=models.PROTECT,
        related_name='reconciliation_match',
        verbose_name="Transacción Financiera ERP",
    )
    match_type = models.CharField(
        max_length=20,
        choices=MatchType.choices,
        default=MatchType.EXACT,
        verbose_name="Tipo de Coincidencia",
    )
    reconciled_at = models.DateTimeField(
        default=timezone.now,
        verbose_name="Fecha y Hora de Conciliación",
    )
    reconciled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='confirmed_reconciliations',
        verbose_name="Conciliado por",
    )
    notes = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Notas de Conciliación",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Conciliación Bancaria (Emparejamiento)"
        verbose_name_plural = "Conciliaciones Bancarias (Emparejamientos)"
        ordering = ['-reconciled_at']

    def clean(self):
        super().clean()
        if self.statement_line_id and self.financial_transaction_id:
            if self.statement_line.organization_id != self.financial_transaction.organization_id:
                raise ValidationError("Aislamiento multiempresa violado: La línea y la transacción pertenecen a organizaciones distintas.")
            if self.statement_line.bank_account_id != self.financial_transaction.bank_account_id:
                raise ValidationError("La línea del extracto y la transacción ERP deben pertenecer a la misma cuenta bancaria.")
            if self.statement_line.amount != self.financial_transaction.amount:
                raise ValidationError(f"Discrepancia de importe: {self.statement_line.amount} != {self.financial_transaction.amount}.")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Match [{self.get_match_type_display()}]: Line {self.statement_line_id} <-> TRX {self.financial_transaction.number}"
