import datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from apps.tenancy.models import TenantScopedModel


class Invoice(TenantScopedModel):
    """
    Factura Fiscal Oficial DGI / Cuenta por Cobrar (CxC).
    Implementa la normativa tributaria de Nicaragua (Ley 822) con soporte
    bimonetario (NIO/USD), IVA 15% y retenciones en la fuente (IR e IMI).
    """
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Borrador'
        ISSUED = 'ISSUED', 'Emitida / Pendiente de Cobro'
        PARTIAL = 'PARTIAL', 'Cobro Parcial'
        PAID = 'PAID', 'Cancelada / Pagada'
        VOID = 'VOID', 'Anulada'

    class PaymentCondition(models.TextChoices):
        CASH = 'CASH', 'Contado'
        CREDIT_15 = 'CREDIT_15', 'Crédito 15 Días'
        CREDIT_30 = 'CREDIT_30', 'Crédito 30 Días'

    number = models.CharField(
        max_length=25,
        db_index=True,
        verbose_name="Folio de Factura",
        help_text="Número oficial consecutivo (ej: FAC-2026-0001)"
    )
    account = models.ForeignKey(
        'crm.Account',
        on_delete=models.PROTECT,
        related_name='invoices',
        verbose_name="Cliente / Cuenta"
    )
    source_quote = models.ForeignKey(
        'crm.Quote',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoices',
        verbose_name="Cotización de Origen"
    )
    source_work_order = models.ForeignKey(
        'operations.WorkOrder',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoices',
        verbose_name="Orden de Trabajo de Origen"
    )
    pos_shift = models.ForeignKey(
        'billing.PosRegisterShift',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='shift_invoices',
        verbose_name="Turno POS",
        help_text="Turno operativo de caja en el que se emitió esta factura POS",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
        verbose_name="Estado de Factura"
    )
    payment_condition = models.CharField(
        max_length=20,
        choices=PaymentCondition.choices,
        default=PaymentCondition.CASH,
        verbose_name="Condición de Pago"
    )
    issue_date = models.DateField(
        default=timezone.now,
        verbose_name="Fecha de Emisión"
    )
    due_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Fecha de Vencimiento"
    )
    currency_code = models.CharField(
        max_length=3,
        default='NIO',
        verbose_name="Moneda de Facturación"
    )
    currency_symbol = models.CharField(
        max_length=10,
        default='C$',
        verbose_name="Símbolo Monetario"
    )
    exchange_rate = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal('36.6243'),
        verbose_name="Tipo de Cambio Oficial (BCN)",
        help_text="Tasa oficial BCN NIO/USD"
    )
    apply_tax = models.BooleanField(
        default=True,
        verbose_name="Aplicar Impuesto al Valor Agregado"
    )
    tax_name = models.CharField(
        max_length=20,
        default='IVA',
        verbose_name="Nombre del Impuesto"
    )
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('15.00'),
        verbose_name="Tasa de IVA (%)"
    )

    # Retenciones Fiscales de Ley Nicaragua (Ley 822 DGI & Alcaldía)
    ir_retention_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Retención IR DGI (%)",
        help_text="0%, 1% (Bienes), 2% (Servicios), 10% (Profesionales)"
    )
    imi_retention_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Retención Alcaldía IMI (%)",
        help_text="0% o 1% Retención Municipal Alcaldía (IMI)"
    )
    retention_certificate_number = models.CharField(
        max_length=60,
        blank=True,
        verbose_name="N° Constancia de Retención del Cliente",
        help_text="Identificador legal de la constancia emitida por el agente retenedor"
    )

    # Totales Financieros y Saldos de Cartera
    subtotal = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Subtotal Partidas Bruto"
    )
    discount_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Descuentos Comerciales"
    )
    tax_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total IVA Trasladado"
    )
    gross_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total Facturado (Bruto)"
    )
    ir_retention_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Monto Retención IR"
    )
    imi_retention_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Monto Retención IMI"
    )
    net_receivable = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Neto a Cobrar en Caja"
    )
    amount_paid = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total Recaudado (Abonos)"
    )
    balance_due = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Saldo Pendiente de Cobro"
    )

    notes = models.TextField(
        blank=True,
        verbose_name="Términos y Observaciones de Facturación"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_invoices',
        verbose_name="Facturador Responsable"
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Factura Fiscal"
        verbose_name_plural = "Facturas Fiscales (CxC)"
        ordering = ['-issue_date', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'number'],
                name='unique_invoice_number_per_tenant'
            )
        ]

    def __str__(self) -> str:
        return f"{self.number} · {self.account.commercial_name} ({self.currency_symbol} {self.net_receivable:,.2f})"

    @property
    def taxable_base(self) -> Decimal:
        """Base imponible gravable (Subtotal menos Descuentos)."""
        base = self.subtotal - self.discount_total
        return max(Decimal('0.00'), base)

    @property
    def total_retentions(self) -> Decimal:
        """Suma de retenciones en la fuente aplicadas por el cliente."""
        return self.ir_retention_amount + self.imi_retention_amount

    def recalculate_totals(self):
        """
        Calcula con precisión Decimal estricta bajo Ley 822 de Concertación Tributaria:
        1. Subtotal bruto y descuentos por partida.
        2. Base Imponible Gravable = Subtotal - Descuentos.
        3. IVA 15% sobre Base Imponible (si aplica y la cuenta no está exenta).
        4. Total Facturado (Gross Total) = Base Imponible + IVA.
        5. Retención IR e IMI calculadas EXCLUSIVAMENTE sobre la Base Imponible Gravable.
        6. Total Retenciones = IR + IMI.
        7. Neto a Cobrar = Total Facturado - Total Retenciones.
        8. Saldo Pendiente = Neto a Cobrar - Monto Recaudado.
        9. Transición de estado a PAID o PARTIAL.
        """
        items = list(self.items.all())
        subtotal = Decimal('0.00')
        discount_total = Decimal('0.00')

        for item in items:
            gross = (item.quantity * item.unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            disc = (gross * (item.discount_percent / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            subtotal += gross
            discount_total += disc

        taxable_base = subtotal - discount_total
        if taxable_base < Decimal('0.00'):
            taxable_base = Decimal('0.00')

        is_exempt = getattr(self.account, 'is_tax_exempt', False) if self.account_id else False

        if self.apply_tax and not is_exempt:
            tax_total = (taxable_base * (self.tax_rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            tax_total = Decimal('0.00')

        gross_total = taxable_base + tax_total

        ir_retention_amount = (taxable_base * (self.ir_retention_rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        imi_retention_amount = (taxable_base * (self.imi_retention_rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        total_retentions = ir_retention_amount + imi_retention_amount

        net_receivable = gross_total - total_retentions
        balance_due = net_receivable - self.amount_paid
        # Tolerancia técnica de centavos por redondeo de tipo de cambio oficial BCN
        if Decimal('0.00') < balance_due <= Decimal('0.05') and self.amount_paid > Decimal('0.00'):
            balance_due = Decimal('0.00')
        elif balance_due < Decimal('0.00'):
            balance_due = Decimal('0.00')

        self.subtotal = subtotal
        self.discount_total = discount_total
        self.tax_total = tax_total
        self.gross_total = gross_total
        self.ir_retention_amount = ir_retention_amount
        self.imi_retention_amount = imi_retention_amount
        self.net_receivable = net_receivable
        self.balance_due = balance_due

        # Transiciones de estado automático
        if self.status != self.Status.VOID and self.status != self.Status.DRAFT:
            if self.balance_due == Decimal('0.00') and self.amount_paid > Decimal('0.00'):
                self.status = self.Status.PAID
            elif Decimal('0.00') < self.amount_paid < self.net_receivable:
                self.status = self.Status.PARTIAL
            elif self.amount_paid == Decimal('0.00'):
                self.status = self.Status.ISSUED

    def save(self, *args, **kwargs):
        # Resolver fecha de vencimiento según la condición de pago
        if not self.due_date and self.issue_date:
            if self.payment_condition == self.PaymentCondition.CREDIT_15:
                self.due_date = self.issue_date + datetime.timedelta(days=15)
            elif self.payment_condition == self.PaymentCondition.CREDIT_30:
                self.due_date = self.issue_date + datetime.timedelta(days=30)
            else:
                self.due_date = self.issue_date

        # Asignar organización si no está seteada pero se tiene account
        if self.account_id and not self.organization_id:
            self.organization_id = self.account.organization_id

        # Generador de folio consecutivo fiscal
        if not self.number and self.organization_id:
            year = self.issue_date.year if self.issue_date else timezone.now().year
            prefix = f"FAC-{year}-"
            count = Invoice.objects.filter(
                organization_id=self.organization_id,
                number__startswith=prefix
            ).count() + 1
            candidate = f"{prefix}{count:04d}"
            while Invoice.objects.filter(organization_id=self.organization_id, number=candidate).exists():
                count += 1
                candidate = f"{prefix}{count:04d}"
            self.number = candidate

        super().save(*args, **kwargs)


class InvoiceItem(TenantScopedModel):
    """
    Partida o renglón técnico de la Factura Fiscal.
    """
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name="Factura"
    )
    item_type = models.CharField(
        max_length=20,
        default='SERVICE',
        verbose_name="Tipo de Partida"
    )
    code = models.CharField(
        max_length=30,
        blank=True,
        verbose_name="Código de Producto / Servicio"
    )
    description = models.CharField(
        max_length=255,
        verbose_name="Descripción de la Partida"
    )
    unit = models.CharField(
        max_length=20,
        default='UND',
        verbose_name="Unidad de Medida"
    )
    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('1.00'),
        verbose_name="Cantidad"
    )
    unit_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Precio Unitario"
    )
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Descuento (%)"
    )
    line_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total Partida"
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Partida de Factura"
        verbose_name_plural = "Partidas de Factura"
        ordering = ['created_at']

    def __str__(self) -> str:
        return f"{self.invoice.number} · {self.description} ({self.quantity} {self.unit})"

    def save(self, *args, **kwargs):
        if self.invoice_id and not self.organization_id:
            self.organization_id = self.invoice.organization_id

        gross = (self.quantity * self.unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        disc = (gross * (self.discount_percent / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        self.line_total = gross - disc

        super().save(*args, **kwargs)


class PaymentReceipt(TenantScopedModel):
    """
    Recibo Oficial de Caja (ROC) Bimonetario.
    Documento inmutable de trazabilidad fiscal y control de tesorería.
    Permite pagos en Córdobas (NIO) o Dólares (USD) con conversión automática.
    """
    class PaymentMethod(models.TextChoices):
        TRANSFER_BAC = 'TRANSFER_BAC', 'Transferencia BAC Credomatic'
        TRANSFER_LAFISE = 'TRANSFER_LAFISE', 'Transferencia Bancentro LAFISE'
        TRANSFER_BANPRO = 'TRANSFER_BANPRO', 'Transferencia BANPRO'
        TRANSFER_FICOHSA = 'TRANSFER_FICOHSA', 'Transferencia FICOHSA / BDF'
        CHECK = 'CHECK', 'Cheque Certificado'
        CASH = 'CASH', 'Efectivo en Caja'
        CARD = 'CARD', 'Tarjeta de Crédito / Débito'

    number = models.CharField(
        max_length=25,
        db_index=True,
        verbose_name="N° Recibo Oficial de Caja (ROC)",
        help_text="Consecutivo oficial (ej: ROC-2026-0001)"
    )
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.PROTECT,
        related_name='payments',
        verbose_name="Factura Liquidada"
    )
    payment_date = models.DateField(
        default=timezone.now,
        verbose_name="Fecha de Recaudación"
    )
    payment_method = models.CharField(
        max_length=30,
        choices=PaymentMethod.choices,
        default=PaymentMethod.TRANSFER_BAC,
        verbose_name="Método de Pago"
    )
    bank_account = models.ForeignKey(
        'finance.BankAccount',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='payment_receipts',
        verbose_name="Cuenta Bancaria Destino",
        help_text="Cuenta bancaria donde se acreditaron los fondos recaudados",
    )
    reference_number = models.CharField(
        max_length=80,
        blank=True,
        verbose_name="Referencia Bancaria / N° Cheque",
        help_text="ej: TRF-BAC-992014"
    )
    currency_paid = models.CharField(
        max_length=3,
        default='NIO',
        verbose_name="Moneda de Pago",
        help_text="Moneda en la que pagó el cliente (NIO o USD)"
    )
    amount_in_payment_currency = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        verbose_name="Monto Recaudado en Moneda de Pago",
        help_text="Monto entregado en la moneda de pago"
    )
    exchange_rate_applied = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal('36.6243'),
        verbose_name="Tipo de Cambio Aplicado"
    )
    amount_applied_to_invoice = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        verbose_name="Monto Acreditado a la Factura",
        help_text="Monto acreditado en la moneda de la factura"
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='received_receipts',
        verbose_name="Cajero / Receptor"
    )
    notes = models.TextField(
        blank=True,
        verbose_name="Concepto / Observaciones del Recibo"
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Recibo Oficial de Caja"
        verbose_name_plural = "Recibos Oficiales de Caja (ROC)"
        ordering = ['-payment_date', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'number'],
                name='unique_payment_receipt_number_per_tenant'
            ),
            models.UniqueConstraint(
                fields=['organization', 'bank_account', 'reference_number'],
                condition=models.Q(bank_account__isnull=False) & ~models.Q(reference_number=''),
                name='unique_bank_reference_per_account',
            ),
        ]

    def __str__(self) -> str:
        return f"{self.number} · {self.currency_paid} {self.amount_in_payment_currency:,.2f} -> {self.invoice.number}"

    def clean(self):
        super().clean()
        if self.reference_number:
            self.reference_number = self.reference_number.strip()
        # Regla de Inmutabilidad Estricta
        if not self._state.adding and self.pk:
            raise ValidationError("Un Recibo Oficial de Caja (ROC) emitido es inmutable conforme a la normativa fiscal y no puede ser modificado.")

    def save(self, *args, **kwargs):
        if not self._state.adding and self.pk:
            raise ValidationError("Un Recibo Oficial de Caja (ROC) emitido es inmutable conforme a la normativa fiscal y no puede ser modificado.")

        if self.invoice_id and not self.organization_id:
            self.organization_id = self.invoice.organization_id

        # Cálculo automático de conversión bimonetaria
        if not self.amount_applied_to_invoice or self.amount_applied_to_invoice <= Decimal('0.00'):
            if self.currency_paid == self.invoice.currency_code:
                self.amount_applied_to_invoice = self.amount_in_payment_currency
            elif self.currency_paid == 'USD' and self.invoice.currency_code == 'NIO':
                self.amount_applied_to_invoice = (
                    self.amount_in_payment_currency * self.exchange_rate_applied
                ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            elif self.currency_paid == 'NIO' and self.invoice.currency_code == 'USD':
                self.amount_applied_to_invoice = (
                    self.amount_in_payment_currency / self.exchange_rate_applied
                ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # Generador de folio consecutivo ROC
        if not self.number and self.organization_id:
            year = self.payment_date.year if self.payment_date else timezone.now().year
            prefix = f"ROC-{year}-"
            count = PaymentReceipt.objects.filter(
                organization_id=self.organization_id,
                number__startswith=prefix
            ).count() + 1
            candidate = f"{prefix}{count:04d}"
            while PaymentReceipt.objects.filter(organization_id=self.organization_id, number=candidate).exists():
                count += 1
                candidate = f"{prefix}{count:04d}"
            self.number = candidate

        super().save(*args, **kwargs)

        # Sincronizar saldo y estado en la factura correspondiente
        total_paid = (
            self.invoice.payments.aggregate(t=models.Sum('amount_applied_to_invoice'))['t']
            or Decimal('0.00')
        )
        self.invoice.amount_paid = total_paid
        self.invoice.recalculate_totals()
        Invoice.objects.filter(pk=self.invoice.pk).update(
            amount_paid=self.invoice.amount_paid,
            balance_due=self.invoice.balance_due,
            status=self.invoice.status,
            updated_at=timezone.now(),
        )

    def delete(self, *args, **kwargs):
        raise ValidationError("Un Recibo Oficial de Caja (ROC) emitido no puede ser eliminado por razones de auditoría y trazabilidad fiscal.")


class PaymentIdempotencyRecord(TenantScopedModel):
    """
    Registro de Idempotencia Transaccional para Cobros y Pagos.
    Garantiza que peticiones repetidas (doble clic, reintentos HTTP, problemas de red)
    no dupliquen cobros, recibos, transacciones financieras ni pólizas contables.
    Distingue reintentos idénticos de cuotas legítimas consecutivas por el mismo monto.
    """
    idempotency_key = models.CharField(
        max_length=120,
        db_index=True,
        verbose_name="Clave de Idempotencia",
        help_text="Token o UUID único asignado a la petición",
    )
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name='idempotency_records',
        verbose_name="Factura de Destino",
    )
    payment_receipt = models.ForeignKey(
        PaymentReceipt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='idempotency_records',
        verbose_name="Recibo Emitido",
    )
    request_hash = models.CharField(
        max_length=64,
        verbose_name="Firma Criptográfica SHA-256 de la Petición",
    )
    status = models.CharField(
        max_length=20,
        default='SUCCESS',
        verbose_name="Estado de Operación",
    )
    response_payload = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Payload de Respuesta Guardado",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Registro de Idempotencia de Pago"
        verbose_name_plural = "Registros de Idempotencia de Pagos"
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'idempotency_key'],
                name='unique_payment_idempotency_key_per_tenant',
            )
        ]

    def __str__(self) -> str:
        return f"{self.idempotency_key} · Fac {self.invoice.number} ({self.status})"


class CashRegisterSession(TenantScopedModel):
    """
    Sesión y Arqueo de Caja Z Bimonetario (C$ / $).
    Control estricto de apertura, recaudaciones por método de pago (Efectivo / Transferencias),
    conteo físico declarado en gaveta y cálculo de sobrantes/faltantes con sello criptográfico SHA-256.
    """
    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Turno Abierto'
        CLOSED = 'CLOSED', 'Arqueo Cerrado y Sellado (Corte Z)'

    number = models.CharField(
        max_length=25,
        db_index=True,
        verbose_name="Folio de Arqueo",
        help_text="Consecutivo oficial ej: ARQ-2026-0001",
    )
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='cash_sessions',
        verbose_name="Cajero Responsable",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
        verbose_name="Estado de Sesión",
    )
    opened_at = models.DateTimeField(
        default=timezone.now,
        verbose_name="Fecha y Hora de Apertura",
    )
    closed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Fecha y Hora de Cierre",
    )
    exchange_rate = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal('36.6243'),
        verbose_name="Tipo de Cambio BCN",
    )

    # Fondos Iniciales de Apertura
    opening_cash_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Fondo Inicial Córdobas (C$)",
    )
    opening_cash_usd = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Fondo Inicial Dólares ($)",
    )

    # Totales Calculados por el Sistema desde PaymentReceipt
    system_cash_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Efectivo Sistema Córdobas (C$)",
    )
    system_cash_usd = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Efectivo Sistema Dólares ($)",
    )
    system_transfer_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Transferencias Sistema Córdobas (C$)",
    )
    system_transfer_usd = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Transferencias Sistema Dólares ($)",
    )

    # Efectivo Físico Declarado en Gaveta al Cerrar
    declared_cash_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Efectivo Declarado Córdobas (C$)",
    )
    declared_cash_usd = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Efectivo Declarado Dólares ($)",
    )

    # Totales Esperados
    expected_cash_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total Esperado Córdobas (C$)",
    )
    expected_cash_usd = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total Esperado Dólares ($)",
    )

    # Diferencias de Arqueo (Sobrante + / Faltante -)
    difference_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Diferencia Córdobas (C$)",
    )
    difference_usd = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Diferencia Dólares ($)",
    )

    notes = models.TextField(
        blank=True,
        verbose_name="Observaciones del Turno / Arqueo",
    )
    audit_hash = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        verbose_name="Sello Criptográfico SHA-256 (Corte Z)",
        help_text="Firma inmutable que certifica el arqueo de caja",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Sesión de Caja (Arqueo Z)"
        verbose_name_plural = "Sesiones de Caja (Arqueos Z)"
        ordering = ['-opened_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'number'],
                name='unique_cash_session_number_per_tenant',
            )
        ]

    def __str__(self) -> str:
        return f"{self.number} · {self.cashier.get_full_name() or self.cashier.email} [{self.get_status_display()}]"

    def recalculate_system_totals(self):
        """
        Consulta todos los PaymentReceipt creados en la organización desde opened_at
        hasta closed_at (o timezone.now() si sigue abierta) y computa los montos
        recaudados por moneda y método de pago.
        """
        end_time = self.closed_at or timezone.now()
        receipts = PaymentReceipt.objects.filter(
            organization_id=self.organization_id,
            created_at__gte=self.opened_at,
            created_at__lte=end_time,
        )

        cash_nio = (
            receipts.filter(
                payment_method=PaymentReceipt.PaymentMethod.CASH,
                currency_paid='NIO',
            ).aggregate(t=models.Sum('amount_in_payment_currency'))['t']
            or Decimal('0.00')
        )
        cash_usd = (
            receipts.filter(
                payment_method=PaymentReceipt.PaymentMethod.CASH,
                currency_paid='USD',
            ).aggregate(t=models.Sum('amount_in_payment_currency'))['t']
            or Decimal('0.00')
        )

        transfer_nio = (
            receipts.exclude(payment_method=PaymentReceipt.PaymentMethod.CASH)
            .filter(currency_paid='NIO')
            .aggregate(t=models.Sum('amount_in_payment_currency'))['t']
            or Decimal('0.00')
        )
        transfer_usd = (
            receipts.exclude(payment_method=PaymentReceipt.PaymentMethod.CASH)
            .filter(currency_paid='USD')
            .aggregate(t=models.Sum('amount_in_payment_currency'))['t']
            or Decimal('0.00')
        )

        self.system_cash_nio = cash_nio
        self.system_cash_usd = cash_usd
        self.system_transfer_nio = transfer_nio
        self.system_transfer_usd = transfer_usd

        self.expected_cash_nio = self.opening_cash_nio + self.system_cash_nio
        self.expected_cash_usd = self.opening_cash_usd + self.system_cash_usd

    def close_and_seal_session(self, declared_nio: Decimal, declared_usd: Decimal, notes: str = ''):
        """
        Cierra y sella el turno de caja de forma inmutable:
        1. Valida que no esté previamente cerrada.
        2. Recalcula los totales del sistema.
        3. Fija el efectivo físico declarado y calcula faltantes/sobrantes.
        4. Genera el sello criptográfico SHA-256 del Corte Z.
        5. Guarda y bloquea la sesión.
        """
        if self.status == self.Status.CLOSED:
            raise ValidationError("Este arqueo de caja ya fue cerrado y sellado de forma inmutable.")

        self.declared_cash_nio = Decimal(str(declared_nio))
        self.declared_cash_usd = Decimal(str(declared_usd))
        if notes:
            self.notes = notes

        self.closed_at = timezone.now()
        self.recalculate_system_totals()

        self.difference_nio = self.declared_cash_nio - self.expected_cash_nio
        self.difference_usd = self.declared_cash_usd - self.expected_cash_usd
        self.status = self.Status.CLOSED

        # Generación del Sello SHA-256
        payload = (
            f"{self.organization_id}|{self.number}|{self.opened_at.isoformat()}|{self.closed_at.isoformat()}|"
            f"{self.opening_cash_nio:.2f}|{self.opening_cash_usd:.2f}|"
            f"{self.system_cash_nio:.2f}|{self.system_cash_usd:.2f}|"
            f"{self.declared_cash_nio:.2f}|{self.declared_cash_usd:.2f}|"
            f"{self.difference_nio:.2f}|{self.difference_usd:.2f}|"
            f"{self.exchange_rate:.4f}"
        )
        self.audit_hash = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        self.save()

    def clean(self):
        super().clean()
        if self.pk and not self._state.adding:
            original = CashRegisterSession.objects.filter(pk=self.pk).first()
            if original and original.status == self.Status.CLOSED and original.audit_hash:
                if (
                    self.declared_cash_nio != original.declared_cash_nio
                    or self.declared_cash_usd != original.declared_cash_usd
                    or self.expected_cash_nio != original.expected_cash_nio
                    or self.expected_cash_usd != original.expected_cash_usd
                    or self.difference_nio != original.difference_nio
                    or self.difference_usd != original.difference_usd
                    or self.audit_hash != original.audit_hash
                ):
                    raise ValidationError("Un Arqueo de Caja (Corte Z) cerrado y sellado es estrictamente inmutable.")

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            original = CashRegisterSession.objects.filter(pk=self.pk).first()
            if original and original.status == self.Status.CLOSED and original.audit_hash:
                if (
                    self.declared_cash_nio != original.declared_cash_nio
                    or self.declared_cash_usd != original.declared_cash_usd
                    or self.expected_cash_nio != original.expected_cash_nio
                    or self.expected_cash_usd != original.expected_cash_usd
                    or self.difference_nio != original.difference_nio
                    or self.difference_usd != original.difference_usd
                    or self.audit_hash != original.audit_hash
                ):
                    raise ValidationError("Un Arqueo de Caja (Corte Z) cerrado y sellado es estrictamente inmutable.")

        if not self.number and self.organization_id:
            year = self.opened_at.year if self.opened_at else timezone.now().year
            prefix = f"ARQ-{year}-"
            count = (
                CashRegisterSession.objects.filter(
                    organization_id=self.organization_id,
                    number__startswith=prefix,
                ).count()
                + 1
            )
            candidate = f"{prefix}{count:04d}"
            while CashRegisterSession.objects.filter(
                organization_id=self.organization_id, number=candidate
            ).exists():
                count += 1
                candidate = f"{prefix}{count:04d}"
            self.number = candidate

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.Status.CLOSED and self.audit_hash:
            raise ValidationError("No se puede eliminar un Arqueo de Caja (Corte Z) sellado con hash de auditoría.")
        super().delete(*args, **kwargs)


def _remap_q_object(q, remap_fn):
    if not isinstance(q, models.Q):
        return q
    new_children = []
    for child in q.children:
        if isinstance(child, models.Q):
            new_children.append(_remap_q_object(child, remap_fn))
        elif isinstance(child, tuple) and len(child) == 2:
            k, v = child
            new_children.append((remap_fn(k), v))
        else:
            new_children.append(child)
    new_q = models.Q()
    new_q.connector = q.connector
    new_q.negated = q.negated
    new_q.children = new_children
    return new_q


class PosRegisterShiftQuerySet(models.QuerySet):
    @staticmethod
    def _remap_key(k: str) -> str:
        if k == 'total_remittances_nio':
            return 'total_bank_deposits_nio'
        if k.startswith('total_remittances_nio__'):
            return 'total_bank_deposits_nio__' + k[len('total_remittances_nio__'):]
        if k == 'remittances':
            return 'bank_deposits'
        if k.startswith('remittances__'):
            return 'bank_deposits__' + k[len('remittances__'):]
        return k

    def _remap_args(self, args):
        return tuple(_remap_q_object(a, self._remap_key) for a in args)

    def _remap_kwargs(self, kwargs):
        return {self._remap_key(k): v for k, v in kwargs.items()}

    def filter(self, *args, **kwargs):
        return super().filter(*self._remap_args(args), **self._remap_kwargs(kwargs))

    def exclude(self, *args, **kwargs):
        return super().exclude(*self._remap_args(args), **self._remap_kwargs(kwargs))

    def order_by(self, *field_names):
        remapped = []
        for f in field_names:
            prefix = '-' if f.startswith('-') else ''
            base = f[1:] if prefix else f
            remapped.append(prefix + self._remap_key(base))
        return super().order_by(*remapped)

    def values(self, *fields, **expressions):
        remapped_fields = [self._remap_key(f) for f in fields]
        remapped_exprs = {self._remap_key(k): v for k, v in expressions.items()}
        return super().values(*remapped_fields, **remapped_exprs)

    def values_list(self, *fields, **kwargs):
        remapped_fields = [self._remap_key(f) for f in fields]
        return super().values_list(*remapped_fields, **kwargs)

    def prefetch_related(self, *lookups):
        remapped = [self._remap_key(l) if isinstance(l, str) else l for l in lookups]
        return super().prefetch_related(*remapped)

    def create(self, **kwargs):
        return super().create(**self._remap_kwargs(kwargs))


class PosRegisterShift(TenantScopedModel):
    """
    Control de Turno de Caja POS, Corte X Parcial y Arqueo Ciego Corte Z.
    Gestiona la apertura con fondo fijo en gaveta, acumulación de ventas en vivo,
    arqueo ciego de efectivo declarado y sellado inmutable SHA-256.
    """
    objects = PosRegisterShiftQuerySet.as_manager()

    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Turno Abierto'
        CLOSED_Z = 'CLOSED_Z', 'Cerrado (Corte Z Sellado)'

    shift_number = models.CharField(
        max_length=25,
        db_index=True,
        verbose_name="Número de Turno",
        help_text="Folio secuencial por organización ej: TURNO-2026-0001",
    )
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='pos_shifts',
        verbose_name="Cajero Responsable",
    )
    supervisor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='supervised_pos_shifts',
        verbose_name="Supervisor que Autoriza Cierre",
    )
    register_name = models.CharField(
        max_length=100,
        default='CAJA-PRINCIPAL-01',
        verbose_name="Identificador de Caja / Terminal",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
        verbose_name="Estado del Turno",
    )
    opened_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Fecha y Hora de Apertura",
    )
    closed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Fecha y Hora de Cierre (Corte Z)",
    )

    # Importes Monetarios (C$ NIO)
    opening_cash_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Fondo Fijo Inicial Córdobas (C$)",
    )
    system_cash_sales_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Ventas en Efectivo del Turno (C$)",
    )
    system_card_transfer_sales_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Ventas Tarjeta / Transferencia / Crédito (C$)",
    )
    total_cash_inflow_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Entradas Adicionales de Efectivo (C$)",
    )
    total_cash_outflow_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Salidas de Efectivo / Gastos Menores (C$)",
    )
    total_bank_deposits_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Depósitos Bancarios Despachados (C$)",
    )
    total_refunds_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Devoluciones en Efectivo (C$)",
    )
    total_supplier_payments_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Pagos a Proveedores en Efectivo (C$)",
    )
    expected_cash_in_drawer_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Efectivo Esperado en Gaveta (C$)",
    )
    declared_cash_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Efectivo Real Declarado en Arqueo (C$)",
    )
    cash_difference_nio = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Diferencia de Caja (C$)",
    )
    denomination_breakdown = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Desglose de Efectivo por Denominación",
    )
    difference_justification = models.TextField(
        blank=True,
        verbose_name="Justificación de Diferencia de Arqueo",
    )
    transactions_count = models.IntegerField(
        default=0,
        verbose_name="Cantidad de Transacciones / Facturas",
    )
    closing_notes = models.TextField(
        blank=True,
        verbose_name="Notas de Cierre y Arqueo",
    )
    audit_hash = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        verbose_name="Sello Criptográfico SHA-256 (Corte Z)",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Turno de Caja POS"
        verbose_name_plural = "Turnos de Caja POS"
        ordering = ['-opened_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'shift_number'],
                name='unique_pos_shift_number_per_tenant',
            )
        ]

    def __str__(self) -> str:
        return f"{self.shift_number} · {self.register_name} [{self.get_status_display()}]"

    @property
    def total_remittances_nio(self) -> Decimal:
        """Alias de compatibilidad retroactiva hacia total_bank_deposits_nio."""
        return self.total_bank_deposits_nio

    @total_remittances_nio.setter
    def total_remittances_nio(self, value: Decimal):
        self.total_bank_deposits_nio = value

    @property
    def remittances(self):
        """Alias de compatibilidad retroactiva hacia bank_deposits."""
        return self.bank_deposits

    def clean(self):
        super().clean()
        if self.pk:
            original = PosRegisterShift.objects.filter(pk=self.pk).values('status', 'audit_hash').first()
            if original and original['status'] == self.Status.CLOSED_Z:
                raise ValidationError("Un turno con Corte Z sellado es inmutable.")

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            original = PosRegisterShift.objects.filter(pk=self.pk).values('status', 'audit_hash').first()
            if original and original['status'] == self.Status.CLOSED_Z:
                raise ValidationError("Un turno con Corte Z sellado es inmutable.")

        if not self.shift_number and self.organization_id:
            year = timezone.now().year
            prefix = f"TURNO-{year}-"
            count = (
                PosRegisterShift.objects.filter(
                    organization_id=self.organization_id,
                    shift_number__startswith=prefix,
                ).count()
                + 1
            )
            candidate = f"{prefix}{count:04d}"
            while PosRegisterShift.objects.filter(
                organization_id=self.organization_id, shift_number=candidate
            ).exists():
                count += 1
                candidate = f"{prefix}{count:04d}"
            self.shift_number = candidate

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.Status.CLOSED_Z or (
            self.pk and PosRegisterShift.objects.filter(pk=self.pk, status=self.Status.CLOSED_Z).exists()
        ):
            raise ValidationError("Un turno con Corte Z sellado es inmutable.")
        super().delete(*args, **kwargs)


class CashMovementQuerySet(models.QuerySet):
    @staticmethod
    def _remap_key(k: str) -> str:
        if k == 'remittance':
            return 'bank_deposit'
        if k == 'remittance_id':
            return 'bank_deposit_id'
        if k.startswith('remittance__'):
            return 'bank_deposit__' + k[len('remittance__'):]
        return k

    def _remap_args(self, args):
        return tuple(_remap_q_object(a, self._remap_key) for a in args)

    def _remap_kwargs(self, kwargs):
        return {self._remap_key(k): v for k, v in kwargs.items()}

    def filter(self, *args, **kwargs):
        return super().filter(*self._remap_args(args), **self._remap_kwargs(kwargs))

    def exclude(self, *args, **kwargs):
        return super().exclude(*self._remap_args(args), **self._remap_kwargs(kwargs))

    def order_by(self, *field_names):
        remapped = []
        for f in field_names:
            prefix = '-' if f.startswith('-') else ''
            base = f[1:] if prefix else f
            remapped.append(prefix + self._remap_key(base))
        return super().order_by(*remapped)

    def values(self, *fields, **expressions):
        remapped_fields = [self._remap_key(f) for f in fields]
        remapped_exprs = {self._remap_key(k): v for k, v in expressions.items()}
        return super().values(*remapped_fields, **remapped_exprs)

    def values_list(self, *fields, **kwargs):
        remapped_fields = [self._remap_key(f) for f in fields]
        return super().values_list(*remapped_fields, **kwargs)

    def select_related(self, *fields):
        remapped = [self._remap_key(f) if isinstance(f, str) else f for f in fields]
        return super().select_related(*remapped)

    def create(self, **kwargs):
        return super().create(**self._remap_kwargs(kwargs))


class CashMovement(TenantScopedModel):
    """
    Subledger / Libro Auxiliar de Movimientos de Efectivo en Gaveta POS.
    Registra cada entrada y salida física con trazabilidad de origen comercial y operativo.
    """
    objects = CashMovementQuerySet.as_manager()

    class MovementType(models.TextChoices):
        CASH_IN = 'CASH_IN', 'Entrada Adicional de Efectivo'
        CASH_OUT = 'CASH_OUT', 'Salida de Efectivo / Gasto Menor'
        POS_SALE = 'POS_SALE', 'Venta POS en Efectivo'
        CUSTOMER_COLLECTION = 'CUSTOMER_COLLECTION', 'Cobro de Factura en Efectivo'
        CUSTOMER_REFUND = 'CUSTOMER_REFUND', 'Devolución en Efectivo a Cliente'
        SUPPLIER_PAYMENT = 'SUPPLIER_PAYMENT', 'Pago a Proveedor en Efectivo'
        CASH_DEPOSIT_DISPATCH = 'CASH_DEPOSIT_DISPATCH', 'Despacho de Efectivo para Depósito Bancario'
        REMITTANCE_DISPATCH = 'REMITTANCE_DISPATCH', 'Despacho de Remesa para Depósito Bancario (Legacy)'
        ADJUSTMENT = 'ADJUSTMENT', 'Ajuste Autorizado de Caja'

    shift = models.ForeignKey(
        PosRegisterShift,
        on_delete=models.PROTECT,
        related_name='cash_movements',
        verbose_name="Turno de Caja POS",
    )
    movement_type = models.CharField(
        max_length=30,
        choices=MovementType.choices,
        db_index=True,
        verbose_name="Tipo de Movimiento",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        verbose_name="Monto de Efectivo (C$)",
    )
    concept = models.CharField(
        max_length=255,
        verbose_name="Concepto / Detalle del Movimiento",
    )
    reference = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Documento / Referencia Externa",
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='performed_cash_movements',
        verbose_name="Responsable del Movimiento",
    )
    authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='authorized_cash_movements',
        verbose_name="Autorizado por (Supervisor)",
    )
    invoice = models.ForeignKey(
        'billing.Invoice',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_movements',
        verbose_name="Factura Relacionada",
    )
    payment_receipt = models.ForeignKey(
        'billing.PaymentReceipt',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_movements',
        verbose_name="Recibo de Caja Relacionado",
    )
    supplier_payment = models.ForeignKey(
        'purchasing.SupplierPayment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_movements',
        verbose_name="Pago a Proveedor Relacionado",
    )
    bank_deposit = models.ForeignKey(
        'billing.CashBankDeposit',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_movements',
        verbose_name="Depósito Bancario Relacionado",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Fecha y Hora de Registro",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Movimiento de Efectivo en Caja"
        verbose_name_plural = "Movimientos de Efectivo en Caja"
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"{self.get_movement_type_display()} · C$ {self.amount} ({self.concept})"

    @property
    def remittance(self):
        """Alias de compatibilidad hacia bank_deposit."""
        return self.bank_deposit

    @remittance.setter
    def remittance(self, value):
        self.bank_deposit = value

    def clean(self):
        super().clean()
        if self.shift_id:
            if self.shift.organization_id != self.organization_id:
                raise ValidationError("El turno de caja no pertenece a la misma organización.")
            if self.shift.status == PosRegisterShift.Status.CLOSED_Z:
                raise ValidationError("No se pueden registrar movimientos de efectivo en un turno cerrado con Corte Z.")
        if self.amount is not None and self.amount <= Decimal('0.00'):
            raise ValidationError("El monto del movimiento debe ser estrictamente positivo.")

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            raise ValidationError("Los movimientos de efectivo de caja son inmutables.")
        self.clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Los movimientos de efectivo de caja son inmutables. Prohibido eliminar movimientos históricos.")


class CashBankDepositQuerySet(models.QuerySet):
    @staticmethod
    def _remap_key(k: str) -> str:
        if k == 'remittance_number':
            return 'deposit_number'
        if k.startswith('remittance_number__'):
            return 'deposit_number__' + k[len('remittance_number__'):]
        return k

    def _remap_args(self, args):
        return tuple(_remap_q_object(a, self._remap_key) for a in args)

    def _remap_kwargs(self, kwargs):
        return {self._remap_key(k): v for k, v in kwargs.items()}

    def filter(self, *args, **kwargs):
        return super().filter(*self._remap_args(args), **self._remap_kwargs(kwargs))

    def exclude(self, *args, **kwargs):
        return super().exclude(*self._remap_args(args), **self._remap_kwargs(kwargs))

    def order_by(self, *field_names):
        remapped = []
        for f in field_names:
            prefix = '-' if f.startswith('-') else ''
            base = f[1:] if prefix else f
            remapped.append(prefix + self._remap_key(base))
        return super().order_by(*remapped)

    def values(self, *fields, **expressions):
        remapped_fields = [self._remap_key(f) for f in fields]
        remapped_exprs = {self._remap_key(k): v for k, v in expressions.items()}
        return super().values(*remapped_fields, **remapped_exprs)

    def values_list(self, *fields, **kwargs):
        remapped_fields = [self._remap_key(f) for f in fields]
        return super().values_list(*remapped_fields, **kwargs)

    def create(self, **kwargs):
        return super().create(**self._remap_kwargs(kwargs))


class CashBankDeposit(TenantScopedModel):
    """
    Ciclo Operativo de Depósitos Bancarios de Efectivo desde Caja POS:
    PREPARED (en gaveta) -> IN_TRANSIT (en camino al banco) -> BANK_DEPOSIT_RECORDED (boleta asentada) -> CONFIRMED (acreditado en banco) o REJECTED.
    """
    objects = CashBankDepositQuerySet.as_manager()

    class Status(models.TextChoices):
        PREPARED = 'PREPARED', 'Preparado en Gaveta'
        IN_TRANSIT = 'IN_TRANSIT', 'Entregado a Custodio / En Tránsito'
        BANK_DEPOSIT_RECORDED = 'BANK_DEPOSIT_RECORDED', 'Boleta Bancaria Registrada (Pendiente Acreditación)'
        CONFIRMED = 'CONFIRMED', 'Acreditado y Confirmado por Banco'
        REJECTED = 'REJECTED', 'Depósito Rechazado'

    # Alias de compatibilidad hacia atrás con máquina de estados anterior
    Status.DEPOSITED = Status.BANK_DEPOSIT_RECORDED

    deposit_number = models.CharField(
        max_length=30,
        db_index=True,
        verbose_name="Número de Depósito Bancario",
        help_text="Folio secuencial ej: DEP-2026-0001",
    )
    shift = models.ForeignKey(
        PosRegisterShift,
        on_delete=models.PROTECT,
        related_name='bank_deposits',
        verbose_name="Turno Origen de Fondos",
    )
    destination_bank_account = models.ForeignKey(
        'finance.BankAccount',
        on_delete=models.PROTECT,
        related_name='cash_bank_deposits',
        verbose_name="Cuenta Bancaria Destino",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        verbose_name="Monto a Depositar (C$ NIO)",
    )
    status = models.CharField(
        max_length=25,
        choices=Status.choices,
        default=Status.PREPARED,
        db_index=True,
        verbose_name="Estado del Depósito Bancario",
    )
    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='prepared_bank_deposits',
        verbose_name="Preparado por (Cajero)",
    )
    prepared_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Fecha de Preparación",
    )
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='dispatched_bank_deposits',
        verbose_name="Despachado / Entregado por",
    )
    dispatched_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Fecha y Hora de Despacho",
    )
    carrier_name = models.CharField(
        max_length=150,
        blank=True,
        verbose_name="Nombre de Custodio / Mensajero / Valija",
    )
    carrier_id = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Identificación / Cédula del Custodio",
    )
    deposit_slip_number = models.CharField(
        max_length=80,
        blank=True,
        db_index=True,
        verbose_name="Número de Boleta de Depósito Bancario",
    )
    deposit_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Fecha del Depósito en Banco",
    )
    deposited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='recorded_bank_deposits',
        verbose_name="Registrado en Banco por",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='confirmed_bank_deposits',
        verbose_name="Confirmado / Acreditado por",
    )
    confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Fecha y Hora de Confirmación Bancaria",
    )
    rejection_reason = models.TextField(
        blank=True,
        verbose_name="Motivo de Rechazo o Discrepancia",
    )
    notes = models.TextField(
        blank=True,
        verbose_name="Observaciones",
    )
    financial_transaction = models.ForeignKey(
        'finance.FinancialTransaction',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_bank_deposits',
        verbose_name="Transacción Financiera Bancaria Asentada",
    )
    transit_journal_entry = models.ForeignKey(
        'finance.JournalEntry',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transit_bank_deposits',
        verbose_name="Póliza Contable de Efectivo en Tránsito",
    )
    confirmation_journal_entry = models.ForeignKey(
        'finance.JournalEntry',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='confirmation_bank_deposits',
        verbose_name="Póliza Contable de Acreditación Bancaria",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Depósito Bancario de Efectivo"
        verbose_name_plural = "Depósitos Bancarios de Efectivo"
        ordering = ['-prepared_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'deposit_number'],
                name='unique_pos_deposit_number_per_tenant',
            ),
            models.UniqueConstraint(
                fields=['organization', 'destination_bank_account', 'deposit_slip_number'],
                condition=models.Q(deposit_slip_number__gt=''),
                name='unique_bank_deposit_slip_per_account',
            )
        ]

    def __str__(self) -> str:
        return f"{self.deposit_number} · C$ {self.amount} -> {self.destination_bank_account.bank_name} [{self.get_status_display()}]"

    @property
    def remittance_number(self) -> str:
        """Alias de compatibilidad hacia deposit_number."""
        return self.deposit_number

    @remittance_number.setter
    def remittance_number(self, value: str):
        self.deposit_number = value

    def clean(self):
        super().clean()
        if self.shift_id and self.shift.organization_id != self.organization_id:
            raise ValidationError("El turno de origen no pertenece a la misma organización.")
        if self.destination_bank_account_id and self.destination_bank_account.organization_id != self.organization_id:
            raise ValidationError("La cuenta bancaria destino no pertenece a la misma organización.")
        if self.amount is not None and self.amount <= Decimal('0.00'):
            raise ValidationError("El monto a depositar debe ser estrictamente positivo.")

    def save(self, *args, **kwargs):
        if not self.deposit_number and self.organization_id:
            year = timezone.now().year
            prefix = f"DEP-{year}-"
            count = (
                CashBankDeposit.objects.filter(
                    organization_id=self.organization_id,
                    deposit_number__startswith=prefix,
                ).count()
                + 1
            )
            candidate = f"{prefix}{count:04d}"
            while CashBankDeposit.objects.filter(
                organization_id=self.organization_id, deposit_number=candidate
            ).exists():
                count += 1
                candidate = f"{prefix}{count:04d}"
            self.deposit_number = candidate

        self.clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != self.Status.PREPARED:
            raise ValidationError("No se puede eliminar un depósito que ya fue despachado, registrado o confirmado.")
        super().delete(*args, **kwargs)


# Alias a nivel de módulo para compatibilidad total con importaciones preexistentes
CashRemittance = CashBankDeposit


