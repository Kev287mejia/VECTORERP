import datetime
from decimal import Decimal, ROUND_HALF_UP
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.tenancy.models import TenantScopedModel


class Supplier(TenantScopedModel):
    """
    Directorio Institucional de Proveedores de Materiales, Equipos y Subcontratistas.
    Aislamiento Multi-Tenant estricto y validación de RUC contra CountryConfig.
    """
    code = models.CharField(
        max_length=20,
        db_index=True,
        verbose_name="Código del Proveedor",
        help_text="Identificador único ej: PRV-0001",
    )
    commercial_name = models.CharField(
        max_length=150,
        verbose_name="Nombre Comercial",
        help_text="ej: SINSA Industrial / Materiales Eléctricos",
    )
    legal_name = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="Razón Social",
        help_text="Razón social registrada ante la DGI",
    )
    tax_id = models.CharField(
        max_length=50,
        db_index=True,
        verbose_name="RUC / Identificación Fiscal",
    )
    contact_name = models.CharField(
        max_length=120,
        blank=True,
        verbose_name="Contacto Principal / Ejecutivo",
    )
    email = models.EmailField(
        blank=True,
        verbose_name="Correo de Contacto / Compras",
    )
    phone = models.CharField(
        max_length=30,
        blank=True,
        verbose_name="Teléfono Directo",
    )
    address = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Dirección Comercial",
    )
    city = models.CharField(
        max_length=60,
        default='Managua',
        verbose_name="Ciudad / Departamento",
    )
    credit_days = models.PositiveIntegerField(
        default=30,
        verbose_name="Días de Crédito",
        help_text="Plazo de crédito comercial otorgado por el proveedor",
    )
    default_ir_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('1.00'),
        verbose_name="Retención IR Habitual (%)",
        help_text="Retención IR de ley: 1.00% Bienes, 2.00% Servicios",
    )
    default_imi_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('1.00'),
        verbose_name="Retención IMI Habitual (%)",
        help_text="Retención Municipal Alcaldía habitual: 1.00%",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="Proveedor Activo",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Proveedor"
        verbose_name_plural = "Proveedores"
        ordering = ['commercial_name']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'code'],
                name='unique_supplier_code_per_tenant',
            )
        ]

    def __str__(self) -> str:
        return f"{self.code} · {self.commercial_name} (RUC: {self.tax_id})"

    def clean(self):
        super().clean()
        if self.tax_id and self.organization_id:
            country = getattr(self.organization, 'country', None)
            if country and hasattr(country, 'validate_tax_id') and not country.validate_tax_id(self.tax_id):
                raise ValidationError({
                    'tax_id': f"El formato del RUC no es válido para {country.name}."
                })

    def save(self, *args, **kwargs):
        if not self.code and self.organization_id:
            count = Supplier.objects.filter(organization_id=self.organization_id).count() + 1
            candidate = f"PRV-{count:04d}"
            while Supplier.objects.filter(organization_id=self.organization_id, code=candidate).exists():
                count += 1
                candidate = f"PRV-{count:04d}"
            self.code = candidate
        super().save(*args, **kwargs)


class PurchaseOrder(TenantScopedModel):
    """
    Orden de Compra Técnica y Cuenta por Pagar a Proveedores (CxP).
    Gestiona la recepción física en almacén e integra las retenciones Ley 822 DGI.
    """
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Borrador / Cotizando'
        APPROVED = 'APPROVED', 'Emitida al Proveedor'
        RECEIVED = 'RECEIVED', 'Recibida en Bodega (CxP Activa)'
        PARTIAL_PAID = 'PARTIAL_PAID', 'Pago Parcial'
        PAID = 'PAID', 'Cancelada / Pagada'
        CANCELLED = 'CANCELLED', 'Anulada'

    number = models.CharField(
        max_length=25,
        db_index=True,
        verbose_name="Folio de Orden de Compra",
        help_text="ej: OC-2026-0001",
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name='purchase_orders',
        verbose_name="Proveedor",
    )
    target_warehouse = models.ForeignKey(
        'inventory.Warehouse',
        on_delete=models.PROTECT,
        related_name='purchase_orders',
        verbose_name="Bodega de Recepción",
    )
    supplier_invoice_number = models.CharField(
        max_length=60,
        blank=True,
        verbose_name="N° Factura del Proveedor",
        help_text="Número de factura comercial/fiscal emitida por el proveedor",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
        verbose_name="Estado de la Orden",
    )
    issue_date = models.DateField(
        default=timezone.now,
        verbose_name="Fecha de Emisión",
    )
    due_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Fecha de Vencimiento de Pago",
    )
    received_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Fecha y Hora de Recepción en Bodega",
    )
    currency_code = models.CharField(
        max_length=3,
        default='NIO',
        verbose_name="Moneda",
    )
    currency_symbol = models.CharField(
        max_length=10,
        default='C$',
        verbose_name="Símbolo Monetario",
    )
    exchange_rate = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal('36.6243'),
        verbose_name="Tipo de Cambio Oficial (BCN)",
    )
    apply_tax = models.BooleanField(
        default=True,
        verbose_name="Acreditar IVA (15%)",
    )
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('15.00'),
        verbose_name="Tasa de IVA Crédito Fiscal (%)",
    )

    # Retenciones en la Fuente Ley 822 (Agente Retenedor)
    ir_withholding_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('1.00'),
        verbose_name="Retención IR DGI (%)",
        help_text="1% Bienes, 2% Servicios, 10% Servicios Profesionales",
    )
    imi_withholding_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('1.00'),
        verbose_name="Retención IMI Alcaldía (%)",
        help_text="1% Impuesto de Matrícula / Ingresos Municipal",
    )
    withholding_certificate_number = models.CharField(
        max_length=30,
        blank=True,
        verbose_name="N° Constancia de Retención Emitida (RET-*)",
        help_text="Folio de la constancia entregada al proveedor",
    )

    # Totales Financieros y Cuentas por Pagar (CxP)
    subtotal = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Base Imponible / Subtotal",
    )
    tax_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="IVA Crédito Fiscal",
    )
    gross_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total Facturado por Proveedor",
    )
    ir_withheld_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Monto Retención IR DGI",
    )
    imi_withheld_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Monto Retención IMI Alcaldía",
    )
    net_payable = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Monto Neto a Pagar al Proveedor",
    )
    amount_paid = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Monto Liquidado / Pagado",
    )
    balance_due = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Saldo Pendiente de Pago (CxP)",
    )

    stock_received = models.BooleanField(
        default=False,
        verbose_name="Mercadería Recibida en Kardex",
        help_text="Garantiza idempotencia estricta para no duplicar entradas en bodega",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_purchase_orders',
        verbose_name="Comprador / Creador",
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='received_purchase_orders',
        verbose_name="Bodeguero Receptor",
    )
    notes = models.TextField(
        blank=True,
        verbose_name="Notas de Compra / Términos de Entrega",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Orden de Compra"
        verbose_name_plural = "Órdenes de Compra (CxP)"
        ordering = ['-issue_date', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'number'],
                name='unique_purchase_order_number_per_tenant',
            )
        ]

    def __str__(self) -> str:
        return f"{self.number} · {self.supplier.commercial_name} ({self.currency_symbol} {self.net_payable:,.2f})"

    @property
    def total_withheld(self) -> Decimal:
        return self.ir_withheld_amount + self.imi_withheld_amount

    def recalculate_totals(self):
        """
        Calcula con precisión Decimal estricta bajo Ley 822 de Concertación Tributaria:
        1. Subtotal = Sumatoria de (quantity * unit_cost).
        2. IVA Crédito Fiscal (15%) = subtotal * (tax_rate / 100).
        3. Total Facturado = subtotal + tax_total.
        4. Retenciones practicadas al Proveedor calculadas EXCLUSIVAMENTE sobre subtotal (Base Imponible sin IVA):
           - IR Bienes (1%) o Servicios (2%)
           - IMI Alcaldía (1%)
        5. Neto a Pagar = Total Facturado - Total Retenciones.
        6. Saldo Pendiente = Neto a Pagar - Monto Pagado.
        7. Asignación automática de Folio de Retención RET-YYYY-XXXX si hay retenciones practicadas.
        8. Sincronización de estado en CxP.
        """
        items = list(self.items.all())
        subtotal = Decimal('0.00')
        for it in items:
            subtotal += (it.quantity * it.unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if self.apply_tax:
            tax_total = (subtotal * (self.tax_rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            tax_total = Decimal('0.00')

        gross_total = subtotal + tax_total

        ir_withheld = (subtotal * (self.ir_withholding_rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        imi_withheld = (subtotal * (self.imi_withholding_rate / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        total_withheld = ir_withheld + imi_withheld

        net_payable = gross_total - total_withheld
        balance_due = net_payable - self.amount_paid

        # Tolerancia cambiaria de centavos para pagos en divisas
        if Decimal('0.00') < balance_due <= Decimal('0.05') and self.amount_paid > Decimal('0.00'):
            balance_due = Decimal('0.00')
        elif balance_due < Decimal('0.00'):
            balance_due = Decimal('0.00')

        self.subtotal = subtotal
        self.tax_total = tax_total
        self.gross_total = gross_total
        self.ir_withheld_amount = ir_withheld
        self.imi_withheld_amount = imi_withheld
        self.net_payable = net_payable
        self.balance_due = balance_due

        # Generador de Constancia Oficial de Retención DGI si corresponde
        if total_withheld > Decimal('0.00') and not self.withholding_certificate_number and self.organization_id:
            year = self.issue_date.year if self.issue_date else timezone.now().year
            prefix = f"RET-{year}-"
            count = PurchaseOrder.objects.filter(
                organization_id=self.organization_id,
                withholding_certificate_number__startswith=prefix
            ).count() + 1
            candidate = f"{prefix}{count:04d}"
            while PurchaseOrder.objects.filter(
                organization_id=self.organization_id,
                withholding_certificate_number=candidate
            ).exists():
                count += 1
                candidate = f"{prefix}{count:04d}"
            self.withholding_certificate_number = candidate

        # Sincronización de estados
        if self.status not in (self.Status.CANCELLED, self.Status.DRAFT):
            if self.balance_due == Decimal('0.00') and self.amount_paid > Decimal('0.00'):
                self.status = self.Status.PAID
            elif Decimal('0.00') < self.amount_paid < self.net_payable:
                self.status = self.Status.PARTIAL_PAID
            elif self.amount_paid == Decimal('0.00') and self.stock_received:
                self.status = self.Status.RECEIVED

    def save(self, *args, **kwargs):
        if not self.due_date and self.issue_date and self.supplier_id:
            credit_days = self.supplier.credit_days or 30
            self.due_date = self.issue_date + datetime.timedelta(days=credit_days)

        if not self.number and self.organization_id:
            year = self.issue_date.year if self.issue_date else timezone.now().year
            prefix = f"OC-{year}-"
            count = PurchaseOrder.objects.filter(
                organization_id=self.organization_id,
                number__startswith=prefix
            ).count() + 1
            candidate = f"{prefix}{count:04d}"
            while PurchaseOrder.objects.filter(organization_id=self.organization_id, number=candidate).exists():
                count += 1
                candidate = f"{prefix}{count:04d}"
            self.number = candidate

        super().save(*args, **kwargs)


class PurchaseOrderItem(TenantScopedModel):
    """
    Línea de Orden de Compra vinculada a un ítem del catálogo de inventario.
    """
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name="Orden de Compra",
    )
    inventory_item = models.ForeignKey(
        'inventory.InventoryItem',
        on_delete=models.PROTECT,
        related_name='purchase_items',
        verbose_name="Material / Insumo del Catálogo",
    )
    description = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Descripción de la Partida",
    )
    unit = models.CharField(
        max_length=20,
        default='UND',
        verbose_name="Unidad de Medida",
    )
    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('1.00'),
        verbose_name="Cantidad Solicitada",
    )
    quantity_received = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Cantidad Recibida en Bodega",
        help_text="Cantidad física ingresada en bodega",
    )
    unit_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Costo Unitario de Compra",
    )
    line_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total Partida",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Partida de Orden de Compra"
        verbose_name_plural = "Partidas de Orden de Compra"

    def __str__(self) -> str:
        return f"{self.inventory_item.code} - {self.description} ({self.quantity} {self.unit})"

    def save(self, *args, **kwargs):
        if not self.description and self.inventory_item_id:
            self.description = self.inventory_item.name
        if not self.unit and self.inventory_item_id:
            self.unit = self.inventory_item.unit
        if self.purchase_order_id and not self.organization_id:
            self.organization_id = self.purchase_order.organization_id

        self.line_total = (self.quantity * self.unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        super().save(*args, **kwargs)


class SupplierInvoice(TenantScopedModel):
    """
    Documento Comercial / Fiscal del Proveedor (Factura de Compra / Cuenta por Pagar).
    Representa formalmente la obligación exigible con el proveedor, separando la orden interna
    de la factura legal recibida.
    """
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pendiente de Pago'
        PARTIAL = 'PARTIAL', 'Pago Parcial'
        PAID = 'PAID', 'Pagada / Liquidada'
        VOID = 'VOID', 'Anulada'

    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name='invoices',
        verbose_name="Proveedor Emisor",
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='supplier_invoices',
        verbose_name="Orden de Compra Asociada",
    )
    invoice_number = models.CharField(
        max_length=60,
        db_index=True,
        verbose_name="N° Factura del Proveedor",
        help_text="Folio fiscal o comercial emitido por el proveedor",
    )
    issue_date = models.DateField(
        default=timezone.now,
        verbose_name="Fecha de Factura",
    )
    due_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Fecha de Vencimiento",
    )
    currency_code = models.CharField(
        max_length=3,
        default='NIO',
        verbose_name="Moneda de Facturación",
    )
    exchange_rate = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal('36.6243'),
        verbose_name="Tipo de Cambio (BCN)",
    )
    subtotal = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Subtotal Gravado / Exento",
    )
    tax_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="IVA Crédito Fiscal",
    )
    gross_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Total Bruto Facturado",
    )
    ir_withheld_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Retención IR DGI",
    )
    imi_withheld_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Retención IMI Alcaldía",
    )
    net_payable = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Monto Neto a Pagar",
    )
    amount_paid = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Monto Liquidado",
    )
    balance_due = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Saldo Pendiente (CxP)",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        verbose_name="Estado de Pago",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_supplier_invoices',
        verbose_name="Registrado Por",
    )
    notes = models.TextField(
        blank=True,
        verbose_name="Notas / Observaciones",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Factura de Proveedor"
        verbose_name_plural = "Facturas de Proveedores (CxP)"
        ordering = ['-issue_date', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'supplier', 'invoice_number'],
                name='unique_supplier_invoice_number_per_vendor',
            )
        ]

    def __str__(self) -> str:
        return f"{self.supplier.commercial_name} · Factura #{self.invoice_number} ({self.currency_code} {self.net_payable:,.2f})"

    def clean(self):
        super().clean()
        if self.supplier_id and self.organization_id and self.supplier.organization_id != self.organization_id:
            raise ValidationError("El proveedor no pertenece a la misma organización.")
        if self.purchase_order_id and self.organization_id and self.purchase_order.organization_id != self.organization_id:
            raise ValidationError("La orden de compra no pertenece a la misma organización.")


class SupplierPayment(TenantScopedModel):
    """
    Comprobante de Egreso / Pago a Proveedor (Inmutable).
    Garantiza trazabilidad fiscal y deducción bancaria del saldo deudor.
    """
    class PaymentMethod(models.TextChoices):
        TRANSFER_BAC = 'TRANSFER_BAC', 'Transferencia BAC Credomatic'
        TRANSFER_LAFISE = 'TRANSFER_LAFISE', 'Transferencia Bancentro LAFISE'
        TRANSFER_BANPRO = 'TRANSFER_BANPRO', 'Transferencia BANPRO Grupo Promerica'
        TRANSFER_FICOHSA = 'TRANSFER_FICOHSA', 'Transferencia Banco Ficohsa'
        CHECK = 'CHECK', 'Cheque Fiscal / Certificado'
        CASH = 'CASH', 'Efectivo en Caja General'

    number = models.CharField(
        max_length=25,
        db_index=True,
        verbose_name="Folio de Egreso",
        help_text="ej: EGR-2026-0001",
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.PROTECT,
        related_name='payments',
        verbose_name="Orden de Compra / Factura Proveedor",
    )
    payment_date = models.DateField(
        default=timezone.now,
        verbose_name="Fecha de Pago",
    )
    payment_method = models.CharField(
        max_length=25,
        choices=PaymentMethod.choices,
        default=PaymentMethod.TRANSFER_BAC,
        verbose_name="Medio de Liquidación Bancaria",
    )
    reference_number = models.CharField(
        max_length=80,
        blank=True,
        verbose_name="N° Transferencia / Cheque / Minuta",
    )
    currency_paid = models.CharField(
        max_length=3,
        default='NIO',
        verbose_name="Moneda Desembolsada",
    )
    amount_in_payment_currency = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        verbose_name="Monto Desembolsado",
    )
    exchange_rate_applied = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal('36.6243'),
        verbose_name="Tasa de Cambio Oficial Aplicada",
    )
    amount_applied_to_order = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        verbose_name="Monto Acreditado a la Orden",
    )
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Responsable Financiero",
    )
    notes = models.TextField(
        blank=True,
        verbose_name="Concepto / Observaciones del Egreso",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Comprobante de Egreso a Proveedor"
        verbose_name_plural = "Comprobantes de Egreso (EGR)"
        ordering = ['-payment_date', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'number'],
                name='unique_supplier_payment_number_per_tenant',
            )
        ]

    def __str__(self) -> str:
        return f"{self.number} · {self.currency_paid} {self.amount_in_payment_currency:,.2f} -> {self.purchase_order.number}"

    def clean(self):
        super().clean()
        if not self._state.adding and self.pk:
            raise ValidationError("Un Comprobante de Egreso (EGR) emitido es inmutable conforme a la normativa fiscal y no puede ser modificado.")

    def save(self, *args, **kwargs):
        if not self._state.adding and self.pk:
            raise ValidationError("Un Comprobante de Egreso (EGR) emitido es inmutable conforme a la normativa fiscal y no puede ser modificado.")

        if self.purchase_order_id and not self.organization_id:
            self.organization_id = self.purchase_order.organization_id

        # Conversión bimonetaria
        if not self.amount_applied_to_order or self.amount_applied_to_order <= Decimal('0.00'):
            if self.currency_paid == self.purchase_order.currency_code:
                self.amount_applied_to_order = self.amount_in_payment_currency
            elif self.currency_paid == 'USD' and self.purchase_order.currency_code == 'NIO':
                self.amount_applied_to_order = (
                    self.amount_in_payment_currency * self.exchange_rate_applied
                ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            elif self.currency_paid == 'NIO' and self.purchase_order.currency_code == 'USD':
                self.amount_applied_to_order = (
                    self.amount_in_payment_currency / self.exchange_rate_applied
                ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if not self.number and self.organization_id:
            year = self.payment_date.year if self.payment_date else timezone.now().year
            prefix = f"EGR-{year}-"
            count = SupplierPayment.objects.filter(
                organization_id=self.organization_id,
                number__startswith=prefix
            ).count() + 1
            candidate = f"{prefix}{count:04d}"
            while SupplierPayment.objects.filter(organization_id=self.organization_id, number=candidate).exists():
                count += 1
                candidate = f"{prefix}{count:04d}"
            self.number = candidate

        super().save(*args, **kwargs)

        # Sincronizar pago en la orden de compra
        total_paid = self.purchase_order.payments.aggregate(t=models.Sum('amount_applied_to_order'))['t'] or Decimal('0.00')
        self.purchase_order.amount_paid = total_paid
        self.purchase_order.recalculate_totals()
        PurchaseOrder.objects.filter(pk=self.purchase_order.pk).update(
            amount_paid=self.purchase_order.amount_paid,
            balance_due=self.purchase_order.balance_due,
            status=self.purchase_order.status,
            updated_at=timezone.now(),
        )

    def delete(self, *args, **kwargs):
        raise ValidationError("Un Comprobante de Egreso (EGR) emitido no puede ser eliminado por razones de auditoría fiscal y control bancario.")
