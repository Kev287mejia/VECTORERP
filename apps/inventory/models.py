from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.tenancy.models import TenantScopedModel


class Warehouse(TenantScopedModel):
    """
    Bodega Física Central, Bodega de Proyecto o Unidad Móvil / Furgoneta de Cuadrilla.
    """
    class WarehouseType(models.TextChoices):
        CENTRAL = 'CENTRAL', 'Bodega Central / Principal'
        PROJECT = 'PROJECT', 'Bodega de Proyecto / Sitio'
        MOBILE_CREW = 'MOBILE_CREW', 'Vehículo / Furgoneta de Cuadrilla'

    code = models.CharField(
        max_length=20,
        db_index=True,
        blank=True,
        verbose_name="Código de Bodega",
        help_text="ej: BOD-01, MOV-04",
    )
    name = models.CharField(
        max_length=120,
        verbose_name="Nombre de Bodega / Unidad Móvil",
        help_text="ej: Bodega Central Managua, Unidad Móvil Hilux #04",
    )
    warehouse_type = models.CharField(
        max_length=30,
        choices=WarehouseType.choices,
        default=WarehouseType.CENTRAL,
        verbose_name="Tipo de Bodega",
    )
    linked_crew = models.ForeignKey(
        'operations.Crew',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='warehouses',
        verbose_name="Cuadrilla Vinculada",
    )
    address = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="Ubicación / Dirección Física",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="Bodega Activa",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Bodega / Almacén"
        verbose_name_plural = "Bodegas y Unidades Móviles"
        ordering = ['code']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'code'],
                name='unique_warehouse_code_per_tenant'
            )
        ]

    def __str__(self) -> str:
        return f"{self.code} · {self.name} ({self.get_warehouse_type_display()})"

    def save(self, *args, **kwargs):
        if not self.code and self.organization_id:
            count = Warehouse.objects.filter(organization_id=self.organization_id).count() + 1
            prefix = "MOV" if self.warehouse_type == self.WarehouseType.MOBILE_CREW else "BOD"
            candidate = f"{prefix}-{count:02d}"
            while Warehouse.objects.filter(organization_id=self.organization_id, code=candidate).exists():
                count += 1
                candidate = f"{prefix}-{count:02d}"
            self.code = candidate
        super().save(*args, **kwargs)


class InventoryItem(TenantScopedModel):
    """
    Catálogo Maestro de Materiales, Repuestos, Insumos y Herramientas.
    Control de stock mínimo y costeo unitario promedio ponderado.
    """
    class Category(models.TextChoices):
        ELECTRICAL = 'ELECTRICAL', 'Material Eléctrico y Potencia'
        MECHANICAL = 'MECHANICAL', 'Repuestos Mecánicos y Ferretería'
        SAFETY_EPP = 'SAFETY_EPP', 'Insumos de Seguridad y EPP'
        TOOL = 'TOOL', 'Herramientas y Equipos de Medición'
        CONSUMABLE = 'CONSUMABLE', 'Consumibles Generales'

    sku = models.CharField(
        max_length=30,
        db_index=True,
        blank=True,
        verbose_name="Código SKU",
        help_text="ej: MAT-CAB-01, HER-MUL-02",
    )
    name = models.CharField(
        max_length=200,
        verbose_name="Descripción / Nombre del Ítem",
        help_text="ej: Cable de Potencia XLP 15kV Calibre 2 AWG",
    )
    category = models.CharField(
        max_length=30,
        choices=Category.choices,
        default=Category.ELECTRICAL,
        verbose_name="Categoría de Inventario",
    )
    unit = models.CharField(
        max_length=20,
        default='UND',
        verbose_name="Unidad de Medida",
        help_text="UND, MTS, KG, GLB, JGO",
    )
    min_stock_level = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('5.00'),
        verbose_name="Stock Mínimo (Alerta Reorden)",
    )
    unit_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Costo Unitario Promedio (Moneda Primaria)",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="Ítem Activo",
    )

    # Extensiones Polimórficas de Industria (Paso 12.1)
    # 1. Ferreterías y Mayoristas (Escalas de Precios y Unidades de Conversión)
    price_retail = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Precio Público / Detalle",
        help_text="Precio de venta estándar en mostrador al cliente final",
    )
    price_contractor = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Precio Maestro de Obra / Contratista",
        help_text="Precio preferencial para maestros de obra y subcontratistas",
    )
    price_wholesale = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Precio Mayorista por Volumen",
        help_text="Precio por compras mayores o iguales al umbral mínimo mayorista",
    )
    wholesale_min_qty = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('12.00'),
        verbose_name="Cantidad Mínima Mayorista",
        help_text="Cantidad mínima a partir de la cual aplica el precio mayorista",
    )
    purchase_unit = models.CharField(
        max_length=20,
        default='UND',
        verbose_name="Unidad de Compra / Embalaje",
        help_text="ej: CAJA, ROLLO, QUINTAL, TAMBOR",
    )
    conversion_factor = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('1.00'),
        verbose_name="Factor de Conversión a Unidad Base",
        help_text="ej: 1 ROLLO = 100 METROS -> factor 100.00",
    )

    # 2. Farmacias y Droguerías (Normativa Sanitaria MINSA & Control FEFO)
    requires_batch_control = models.BooleanField(
        default=False,
        verbose_name="Requiere Control de Lotes & Vencimiento",
        help_text="Activa el trazado sanitario FEFO obligatorio",
    )
    sanitary_registration = models.CharField(
        max_length=80,
        blank=True,
        verbose_name="Registro Sanitario MINSA",
        help_text="ej: RS-NI-2026-04819",
    )
    active_ingredient = models.CharField(
        max_length=150,
        blank=True,
        verbose_name="Principio Activo / Ficha Farmacológica",
        help_text="ej: Amoxicilina 500mg + Ácido Clavulánico",
    )
    laboratory_brand = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Laboratorio / Fabricante",
        help_text="ej: Laboratorios Ramos / Bayer / MK",
    )
    requires_prescription = models.BooleanField(
        default=False,
        verbose_name="Requiere Receta Médica / Medicamento Controlado",
        help_text="Alerta sanitaria obligatoria para despacho en mostrador",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Ítem de Inventario"
        verbose_name_plural = "Catálogo Maestro de Inventario"
        ordering = ['sku']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'sku'],
                name='unique_inventory_sku_per_tenant'
            )
        ]

    def __str__(self) -> str:
        return f"{self.sku} · {self.name} [{self.unit}]"

    def total_stock(self) -> Decimal:
        """Suma las existencias físicas en todas las bodegas activas del tenant."""
        result = self.balances.filter(
            warehouse__is_active=True
        ).aggregate(total=models.Sum('quantity'))['total']
        return result or Decimal('0.00')

    def is_low_stock(self) -> bool:
        """Determina si las existencias globales alcanzaron el umbral mínimo de seguridad."""
        return self.total_stock() <= self.min_stock_level

    def save(self, *args, **kwargs):
        if not self.sku and self.organization_id:
            count = InventoryItem.objects.filter(organization_id=self.organization_id).count() + 1
            candidate = f"SKU-{count:04d}"
            while InventoryItem.objects.filter(organization_id=self.organization_id, sku=candidate).exists():
                count += 1
                candidate = f"SKU-{count:04d}"
            self.sku = candidate
        super().save(*args, **kwargs)


class StockBalance(TenantScopedModel):
    """
    Existencia Física Real por Bodega e Ítem de Inventario.
    Actualizado atómicamente con select_for_update().
    """
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='balances',
        verbose_name="Bodega / Unidad Móvil",
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.CASCADE,
        related_name='balances',
        verbose_name="Ítem de Inventario",
    )
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Existencia Física",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Saldo de Stock"
        verbose_name_plural = "Saldos de Stock por Bodega"
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'warehouse', 'item'],
                name='unique_warehouse_item_balance'
            )
        ]

    def __str__(self) -> str:
        return f"{self.warehouse.code} | {self.item.sku}: {self.quantity} {self.item.unit}"

    def save(self, *args, **kwargs):
        if self.warehouse_id and not self.organization_id:
            self.organization_id = self.warehouse.organization_id
        super().save(*args, **kwargs)


class InventoryBatch(TenantScopedModel):
    """
    Control Sanitario y de Perecederos FEFO (First Expired, First Out).
    Gestiona trazabilidad de lotes, fechas de elaboración, vencimiento y existencias.
    """
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.CASCADE,
        related_name='batches',
        verbose_name="Ítem de Inventario",
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='batches',
        verbose_name="Bodega de Custodia",
    )
    batch_number = models.CharField(
        max_length=60,
        db_index=True,
        verbose_name="Número de Lote de Fabricación",
        help_text="ej: LOT-2026-09A",
    )
    manufacturing_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Fecha de Fabricación / Envasado",
    )
    expiration_date = models.DateField(
        db_index=True,
        verbose_name="Fecha de Expiración / Caducidad",
    )
    quantity_available = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Cantidad Disponible en Lote",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Lote de Inventario (FEFO)"
        verbose_name_plural = "Lotes de Inventario (FEFO)"
        ordering = ['expiration_date', 'batch_number']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'warehouse', 'item', 'batch_number'],
                name='unique_warehouse_item_batch'
            )
        ]

    def __str__(self) -> str:
        return f"{self.item.sku} | Lote: {self.batch_number} (Vence: {self.expiration_date}) - {self.quantity_available} {self.item.unit}"

    @property
    def expiry_status(self) -> str:
        """
        Calcula el estado sanitario del lote en base al vencimiento:
        - EXPIRED: Caducado (bloqueado para venta).
        - EXPIRING_SOON: Vence en <= 90 días (prioridad de salida FEFO).
        - OPTIMAL: Vence en más de 90 días.
        """
        today = timezone.now().date()
        if self.expiration_date <= today:
            return 'EXPIRED'
        delta = (self.expiration_date - today).days
        if delta <= 90:
            return 'EXPIRING_SOON'
        return 'OPTIMAL'

    @property
    def days_until_expiry(self) -> int:
        return (self.expiration_date - timezone.now().date()).days

    def save(self, *args, **kwargs):
        if self.warehouse_id and not self.organization_id:
            self.organization_id = self.warehouse.organization_id
        elif self.item_id and not self.organization_id:
            self.organization_id = self.item.organization_id
        super().save(*args, **kwargs)


class StockMovement(TenantScopedModel):
    """
    Libro Mayor de Kardex Transaccional Inmutable.
    Garantiza trazabilidad de compras, traslados entre bodegas y despachos directos a OTs.
    INMUTABILIDAD: Prohibido modificar o eliminar registros existentes.
    """
    class MovementType(models.TextChoices):
        IN_PURCHASE = 'IN_PURCHASE', 'Entrada por Compra / Recepción'
        IN_ADJUSTMENT = 'IN_ADJUSTMENT', 'Entrada por Ajuste Inicial'
        TRANSFER_OUT = 'TRANSFER_OUT', 'Traslado entre Bodegas (Salida)'
        TRANSFER_IN = 'TRANSFER_IN', 'Traslado entre Bodegas (Entrada)'
        OUT_WORKORDER = 'OUT_WORKORDER', 'Despacho a Orden de Trabajo (OT)'
        OUT_SALE_POS = 'OUT_SALE_POS', 'Venta en Mostrador / POS'

    number = models.CharField(
        max_length=25,
        db_index=True,
        verbose_name="Folio de Kardex",
        help_text="ej: KDX-2026-0001",
    )
    movement_type = models.CharField(
        max_length=30,
        choices=MovementType.choices,
        verbose_name="Tipo de Movimiento",
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name='movements',
        verbose_name="Bodega de Origen / Movimiento",
    )
    target_warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='incoming_transfers',
        verbose_name="Bodega Destino (En Traslados)",
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name='movements',
        verbose_name="Ítem de Inventario",
    )
    batch = models.ForeignKey(
        InventoryBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='movements',
        verbose_name="Lote Sanitario (FEFO)",
    )
    invoice = models.ForeignKey(
        'billing.Invoice',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_movements',
        verbose_name="Factura Fiscal / Venta Mostrador",
    )
    work_order = models.ForeignKey(
        'operations.WorkOrder',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_movements',
        verbose_name="Orden de Trabajo Vinculada",
    )
    work_order_item = models.ForeignKey(
        'operations.WorkOrderItem',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_movements',
        verbose_name="Partida de la OT",
    )
    purchase_order = models.ForeignKey(
        'purchasing.PurchaseOrder',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_movements',
        verbose_name="Orden de Compra Vinculada",
    )
    purchase_order_item = models.ForeignKey(
        'purchasing.PurchaseOrderItem',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_movements',
        verbose_name="Partida de la OC",
    )
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Cantidad Operada",
    )
    unit_cost_snapshot = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Costo Unitario Histórico",
    )
    balance_after = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Saldo Resultante en Bodega",
        help_text="Saldo resultante en la bodega tras la operación",
    )
    reference_note = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Nota de Referencia / Factura / Conduce",
        help_text="ej: Factura Proveedor #8812 / Despacho a Cuadrilla Alfa",
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_movements',
        verbose_name="Operador Responsable",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Movimiento de Kardex"
        verbose_name_plural = "Libro Mayor de Kardex"
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'number'],
                name='unique_stock_movement_number_per_tenant'
            )
        ]

    def __str__(self) -> str:
        return f"{self.number} · {self.get_movement_type_display()} · {self.item.sku} ({self.quantity} {self.item.unit})"

    def clean(self):
        super().clean()
        if not self._state.adding:
            raise ValidationError("El registro de Kardex es inmutable. No se permite editar movimientos existentes.")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("El registro de Kardex es inmutable. No se permite editar movimientos existentes.")

        if self.warehouse_id and not self.organization_id:
            self.organization_id = self.warehouse.organization_id

        if not self.number and self.organization_id:
            year = timezone.now().year
            count = StockMovement.objects.filter(
                organization_id=self.organization_id,
                number__startswith=f"KDX-{year}-"
            ).count() + 1
            candidate = f"KDX-{year}-{count:04d}"
            while StockMovement.objects.filter(organization_id=self.organization_id, number=candidate).exists():
                count += 1
                candidate = f"KDX-{year}-{count:04d}"
            self.number = candidate

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("El registro de Kardex es inmutable. No se permite eliminar movimientos existentes.")
