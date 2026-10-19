import hashlib
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.tenancy.models import TenantScopedModel


class Crew(TenantScopedModel):
    """
    Cuadrilla Operativa / Brigada de Campo.
    Agrupa técnicos, líder técnico y vehículo asignado para despliegues de ingeniería.
    """
    code = models.CharField(
        max_length=20,
        db_index=True,
        verbose_name="Código de Cuadrilla",
        help_text="Identificador único (ej: CUAD-01)",
    )
    name = models.CharField(
        max_length=120,
        verbose_name="Nombre de la Brigada",
        help_text="ej: Cuadrilla Electromecánica Alfa",
    )
    specialty = models.CharField(
        max_length=120,
        blank=True,
        verbose_name="Especialidad Técnica",
        help_text="ej: Media Tensión, Subestaciones, Obras Civiles, Fibra Óptica",
    )
    leader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='led_crews',
        verbose_name="Líder de Cuadrilla (Capataz / Ingeniero)",
    )
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='crews',
        blank=True,
        verbose_name="Técnicos Integrantes",
    )
    vehicle_plate = models.CharField(
        max_length=30,
        blank=True,
        verbose_name="Placa del Vehículo",
        help_text="ej: M 345-890",
    )
    vehicle_description = models.CharField(
        max_length=120,
        blank=True,
        verbose_name="Descripción del Vehículo",
        help_text="ej: Camioneta Hilux 4x4 #04 con Canasta",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="Cuadrilla Activa",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Cuadrilla Operativa"
        verbose_name_plural = "Cuadrillas Operativas"
        ordering = ['code']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'code'],
                name='unique_crew_code_per_tenant',
            )
        ]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"

    def save(self, *args, **kwargs):
        if not self.code and self.organization_id:
            count = Crew.objects.filter(organization_id=self.organization_id).count() + 1
            candidate = f"CUAD-{count:02d}"
            while Crew.objects.filter(organization_id=self.organization_id, code=candidate).exists():
                count += 1
                candidate = f"CUAD-{count:02d}"
            self.code = candidate
        super().save(*args, **kwargs)


class WorkOrder(TenantScopedModel):
    """
    Orden de Trabajo Principal (OT).
    Documento técnico central que gobierna la ejecución de ingeniería, materiales (BOM sin precios),
    control de fases, seguridad SST, cuadrillas asignadas y evidencias de campo.
    """
    class Priority(models.TextChoices):
        LOW = 'LOW', 'Baja'
        NORMAL = 'NORMAL', 'Normal'
        HIGH = 'HIGH', 'Alta'
        CRITICAL = 'CRITICAL', 'Crítica / Emergencia'

    class Phase(models.TextChoices):
        DRAFT = 'DRAFT', '01 · Levantamiento / Apertura'
        ENGINEERING = 'ENGINEERING', '02 · Ingeniería y Materiales'
        SCHEDULED = 'SCHEDULED', '03 · Programada con Cuadrilla'
        IN_PROGRESS = 'IN_PROGRESS', '04 · En Ejecución de Campo'
        QUALITY_CHECK = 'QUALITY_CHECK', '05 · Control de Calidad / Revisión'
        COMPLETED = 'COMPLETED', '06 · Entregada y Cerrada'
        CANCELLED = 'CANCELLED', 'Cancelada'

    number = models.CharField(
        max_length=25,
        db_index=True,
        verbose_name="Folio de Orden de Trabajo",
        help_text="ej: OT-2026-0001",
    )
    account = models.ForeignKey(
        'crm.Account',
        on_delete=models.PROTECT,
        related_name='work_orders',
        verbose_name="Cliente Receptor",
    )
    source_quote = models.ForeignKey(
        'crm.Quote',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='work_orders',
        verbose_name="Cotización Origen (Aprobada)",
    )
    title = models.CharField(
        max_length=200,
        verbose_name="Título del Trabajo / Obra",
        help_text="ej: Mantenimiento Mayor y Reemplazo de Seccionadores Subestación Tipitapa",
    )
    technical_scope = models.TextField(
        blank=True,
        verbose_name="Alcance Técnico e Instrucciones de Ingeniería",
        help_text="Especificaciones técnicas, procedimientos de maniobra y normas aplicables",
    )
    site_address = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Ubicación / Planta de Ejecución",
        help_text="Dirección exacta, planta, coordenadas o frente de obra",
    )
    priority = models.CharField(
        max_length=20,
        choices=Priority.choices,
        default=Priority.NORMAL,
        db_index=True,
        verbose_name="Nivel de Prioridad",
    )
    phase = models.CharField(
        max_length=20,
        choices=Phase.choices,
        default=Phase.DRAFT,
        db_index=True,
        verbose_name="Fase de Ingeniería / Operación",
    )

    # Gobernanza de Seguridad Industrial (SST)
    is_high_risk = models.BooleanField(
        default=False,
        verbose_name="Labor de Alto Riesgo",
        help_text="Indica si implica trabajos en altura (>1.8m), media/alta tensión, espacios confinados o bloqueo LOTO",
    )
    sst_cleared = models.BooleanField(
        default=False,
        verbose_name="Liberado por SST para Campo",
        help_text="Autorización del Oficial SST (Nivel 70+) para habilitar inicio en campo",
    )

    # Asignaciones Técnicas y Operativas
    assigned_engineer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='supervised_work_orders',
        verbose_name="Ingeniero Residente / Supervisor",
    )
    assigned_crew = models.ForeignKey(
        Crew,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='work_orders',
        verbose_name="Cuadrilla Operativa Asignada",
    )

    # Cronograma Operativo
    scheduled_start = models.DateField(
        null=True,
        blank=True,
        verbose_name="Fecha de Inicio Programada",
    )
    scheduled_end = models.DateField(
        null=True,
        blank=True,
        verbose_name="Fecha de Finalización Estimada",
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Fecha y Hora de Cierre Definitivo",
    )
    # Conformidad y Firma Digital en Campo (PWA)
    client_signer_name = models.CharField(
        max_length=150,
        blank=True,
        verbose_name="Nombre de Quien Recibe (Cliente)",
    )
    client_signer_title = models.CharField(
        max_length=120,
        blank=True,
        verbose_name="Cargo del Receptor",
    )
    client_signer_id = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Cédula / ID del Receptor",
    )
    client_signature_data = models.TextField(
        blank=True,
        help_text="Imagen PNG en Base64 (data:image/png;base64,...) trazada en el lienzo táctil",
    )
    client_signed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Fecha y Hora de Firma",
    )
    signature_hash = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        help_text="Sello criptográfico SHA-256 de integridad de firma",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_work_orders',
        verbose_name="Creado Por",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Orden de Trabajo (OT)"
        verbose_name_plural = "Órdenes de Trabajo (OTs)"
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'number'],
                name='unique_work_order_number_per_tenant',
            ),
            models.UniqueConstraint(
                fields=['source_quote'],
                condition=models.Q(source_quote__isnull=False),
                name='unique_work_order_per_source_quote',
            ),
        ]

    def __str__(self) -> str:
        return f"{self.number} · {self.title} ({self.get_phase_display()})"

    @property
    def code(self) -> str:
        return self.number

    @property
    def sst_status(self) -> str:
        if not self.is_high_risk:
            return 'CLEARED'
        return 'CLEARED' if self.sst_cleared else 'BLOCKED'

    def get_sst_status_display(self) -> str:
        if not self.is_high_risk:
            return 'Bajo Riesgo / Liberado'
        return 'Liberado por SST' if self.sst_cleared else 'Bloqueado por SST (Alto Riesgo)'

    def can_start_field_execution(self) -> tuple[bool, str]:
        """
        Regla de Seguridad SST:
        Si la labor está clasificada como de alto riesgo y no ha sido liberada
        por el Oficial SST (sst_cleared == False), se prohíbe pasar a campo (IN_PROGRESS).
        """
        if self.is_high_risk and not self.sst_cleared:
            return False, "La labor está clasificada como de Alto Riesgo y requiere la Liberación formal de Seguridad (SST Nivel 70+) antes de iniciar en campo."
        return True, "Liberado para labores de campo."

    def record_client_signature(self, signer_name: str, signer_title: str, signer_id: str, signature_base64: str, actor=None) -> str:
        """
        Estampa la firma digital táctil de conformidad del cliente en la Orden de Trabajo.
        Garantiza la inmutabilidad sellando con estampa de tiempo y hash criptográfico SHA-256.
        Avanza la OT automáticamente a QUALITY_CHECK y genera un registro en la bitácora inmutable.
        """
        if self.client_signature_data or self.signature_hash:
            raise ValidationError("El acta de esta Orden de Trabajo ya cuenta con una firma digital inmutable.")

        sig_data = (signature_base64 or '').strip()
        if not sig_data.startswith('data:image/png;base64,') or len(sig_data) < 50:
            raise ValidationError("La firma digital debe ser una imagen PNG en formato Base64 válida.")

        clean_name = (signer_name or '').strip()
        clean_title = (signer_title or '').strip()
        clean_id = (signer_id or '').strip()

        if not clean_name:
            raise ValidationError("El nombre del receptor es obligatorio para estampar la firma de conformidad.")

        # Generación del sello SHA-256 de integridad: number|name|id|signature
        raw_payload = f"{self.number}|{clean_name}|{clean_id}|{sig_data}".encode('utf-8')
        computed_hash = hashlib.sha256(raw_payload).hexdigest()

        self.client_signer_name = clean_name
        self.client_signer_title = clean_title
        self.client_signer_id = clean_id
        self.client_signature_data = sig_data
        self.client_signed_at = timezone.now()
        self.signature_hash = computed_hash

        if self.phase == self.Phase.IN_PROGRESS:
            self.phase = self.Phase.QUALITY_CHECK

        self.save(update_fields=[
            'client_signer_name',
            'client_signer_title',
            'client_signer_id',
            'client_signature_data',
            'client_signed_at',
            'signature_hash',
            'phase',
            'updated_at',
        ])

        WorkOrderTimelineLog.objects.create(
            organization=self.organization,
            work_order=self,
            event_type='CLIENT_SIGNED',
            summary=f"Firma digital de conformidad estampada por {clean_name} ({clean_title or 'Cliente'})",
            details=f"Cédula/ID: {clean_id or 'N/R'} · Sello SHA-256: {computed_hash}",
            actor=actor,
        )
        return computed_hash

    def clean(self):
        super().clean()
        if self.phase == self.Phase.IN_PROGRESS:
            can_start, reason = self.can_start_field_execution()
            if not can_start:
                raise ValidationError({'phase': reason})

        if self.pk and not self._state.adding:
            original = WorkOrder.objects.filter(pk=self.pk).values(
                'signature_hash', 'client_signature_data', 'client_signer_name', 'client_signer_id'
            ).first()
            if original and original['signature_hash']:
                if (
                    self.signature_hash != original['signature_hash']
                    or self.client_signature_data != original['client_signature_data']
                    or self.client_signer_name != original['client_signer_name']
                    or self.client_signer_id != original['client_signer_id']
                ):
                    raise ValidationError("El acta de esta Orden de Trabajo ya cuenta con una firma digital inmutable y no puede modificarse.")

    def save(self, *args, **kwargs):
        if self.account_id and not self.organization_id:
            self.organization_id = self.account.organization_id

        if not self.number and self.organization_id:
            year = timezone.now().year
            count = WorkOrder.objects.filter(
                organization_id=self.organization_id,
                created_at__year=year
            ).count() + 1
            candidate = f"OT-{year}-{count:04d}"
            while WorkOrder.objects.filter(organization_id=self.organization_id, number=candidate).exists():
                count += 1
                candidate = f"OT-{year}-{count:04d}"
            self.number = candidate

        super().save(*args, **kwargs)


class WorkOrderItem(TenantScopedModel):
    """
    Lista de Materiales / Partidas Técnicas de la OT (Bill of Materials - BOM).
    Estricta confidencialidad financiera: NUNCA expone precios comerciales, costos ni montos monetarios.
    Opera exclusivamente con cantidades técnicas planificadas y ejecutadas.
    """
    class ItemType(models.TextChoices):
        SERVICE = 'SERVICE', 'Servicio / Maniobra Técnica'
        MATERIAL = 'MATERIAL', 'Material / Suministro'
        EQUIPMENT = 'EQUIPMENT', 'Maquinaria / Equipo'

    work_order = models.ForeignKey(
        WorkOrder,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name="Orden de Trabajo",
    )
    item_type = models.CharField(
        max_length=20,
        choices=ItemType.choices,
        default=ItemType.SERVICE,
        verbose_name="Tipo de Partida",
    )
    code = models.CharField(
        max_length=30,
        blank=True,
        verbose_name="Código Técnico",
        help_text="ej: SRV-ING-01, MAT-CAB-01",
    )
    description = models.CharField(
        max_length=255,
        verbose_name="Descripción de la Labor o Suministro",
    )
    unit = models.CharField(
        max_length=20,
        default='UND',
        verbose_name="Unidad de Medida",
        help_text="ej: UND, MTS, HRS, GLB, KG",
    )
    planned_quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('1.00'),
        verbose_name="Cantidad Planificada",
    )
    executed_quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Cantidad Ejecutada en Campo",
    )
    is_completed = models.BooleanField(
        default=False,
        verbose_name="Completada / Instalada",
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='executed_work_items',
        verbose_name="Registrado / Completado Por",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Partida Técnica de OT"
        verbose_name_plural = "Partidas Técnicas de OT (BOM)"
        ordering = ['created_at']

    def __str__(self) -> str:
        return f"{self.work_order.number} · {self.description} ({self.executed_quantity}/{self.planned_quantity} {self.unit})"

    def save(self, *args, **kwargs):
        if self.work_order_id and not self.organization_id:
            self.organization_id = self.work_order.organization_id
        super().save(*args, **kwargs)


class WorkOrderEvidence(TenantScopedModel):
    """
    Registro y Repositorio de Evidencias Técnicas, Fotográficas y de Entrega de Campo.
    """
    class EvidenceType(models.TextChoices):
        PHOTO_BEFORE = 'PHOTO_BEFORE', 'Fotografía Inicial / Antes'
        PHOTO_PROGRESS = 'PHOTO_PROGRESS', 'Evidencia de Ejecución'
        PHOTO_AFTER = 'PHOTO_AFTER', 'Fotografía Final / Entrega'
        TECHNICAL_REPORT = 'TECHNICAL_REPORT', 'Informe / Acta Técnica'

    work_order = models.ForeignKey(
        WorkOrder,
        on_delete=models.CASCADE,
        related_name='evidences',
        verbose_name="Orden de Trabajo",
    )
    evidence_type = models.CharField(
        max_length=30,
        choices=EvidenceType.choices,
        default=EvidenceType.PHOTO_PROGRESS,
        verbose_name="Tipo de Evidencia",
    )
    title = models.CharField(
        max_length=150,
        verbose_name="Título o Asunto de la Evidencia",
        help_text="ej: Conexión de bornes en transformador T-01",
    )
    notes = models.TextField(
        blank=True,
        verbose_name="Observaciones Técnicas de Campo",
    )
    file_reference = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Referencia o Ruta del Archivo",
        help_text="Nombre de archivo o identificador de almacenamiento",
    )
    compressed_image_data = models.TextField(
        blank=True,
        help_text="Imagen de evidencia comprimida en cliente (JPEG/WebP Base64 <= 250KB)",
    )
    original_size_kb = models.PositiveIntegerField(
        default=0,
        verbose_name="Peso Original (KB)",
    )
    compressed_size_kb = models.PositiveIntegerField(
        default=0,
        verbose_name="Peso Optimizado (KB)",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='uploaded_evidences',
        verbose_name="Cargado Por",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Evidencia Técnica de OT"
        verbose_name_plural = "Evidencias Técnicas de OT"
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"{self.work_order.number} · {self.get_evidence_type_display()} · {self.title}"

    def save(self, *args, **kwargs):
        if self.work_order_id and not self.organization_id:
            self.organization_id = self.work_order.organization_id
        super().save(*args, **kwargs)


class WorkOrderTimelineLog(TenantScopedModel):
    """
    Bitácora Cronológica Inmutable de Auditoría Operativa.
    Garantiza trazabilidad legal y técnica estricta: registra cada cambio de fase,
    liberación SST, asignación de cuadrilla, ejecución de partidas y carga de evidencias.
    INMUTABILIDAD ESTRICTA: Se prohíbe la modificación o eliminación de registros existentes.
    """
    work_order = models.ForeignKey(
        WorkOrder,
        on_delete=models.CASCADE,
        related_name='timeline_logs',
        verbose_name="Orden de Trabajo",
    )
    event_type = models.CharField(
        max_length=40,
        verbose_name="Tipo de Evento",
        help_text="ej: CREATED, FROM_QUOTE, PHASE_CHANGE, CREW_ASSIGNED, SST_CLEARANCE, ITEM_EXECUTED, EVIDENCE_ADDED",
    )
    summary = models.CharField(
        max_length=255,
        verbose_name="Resumen del Evento",
    )
    details = models.TextField(
        blank=True,
        verbose_name="Detalles Técnicos / Notas de Auditoría",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='work_order_actions',
        verbose_name="Colaborador Responsable",
    )

    class Meta(TenantScopedModel.Meta):
        verbose_name = "Registro de Bitácora OT"
        verbose_name_plural = "Bitácora Inmutable de OT"
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"[{self.created_at.strftime('%d/%m/%Y %H:%M')}] {self.work_order.number} · {self.event_type} · {self.summary}"

    def clean(self):
        super().clean()
        if not self._state.adding:
            raise ValidationError("La bitácora operativa es inmutable. No se permite editar registros existentes.")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("La bitácora operativa es inmutable. No se permite editar registros existentes.")
        if self.work_order_id and not self.organization_id:
            self.organization_id = self.work_order.organization_id
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("La bitácora operativa es inmutable. No se permite eliminar registros existentes.")
