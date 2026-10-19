import json
from decimal import Decimal
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction, IntegrityError
from django.db.models import Q, Count
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View
from django.views.generic import ListView, DetailView, CreateView, TemplateView

from apps.tenancy.models import RoleLevel
from apps.tenancy.permissions import ModuleAccessRequiredMixin, render_403_rbac
from apps.crm.models import Quote, QuoteItem
from apps.operations.models import (
    Crew,
    WorkOrder,
    WorkOrderItem,
    WorkOrderEvidence,
    WorkOrderTimelineLog,
)
from apps.operations.forms import (
    WorkOrderCreateForm,
    CrewForm,
    WorkOrderItemForm,
    WorkOrderEvidenceForm,
)
from apps.sst.forms import SafetyChecklistForm


# ==============================================================================
# 1. CONVERSIÓN EN 1 CLIC (QUOTE -> WORKORDER)
# ==============================================================================

class ConvertQuoteToWorkOrderView(ModuleAccessRequiredMixin, View):
    """
    Convierte una Cotización formal aprobada en una Orden de Trabajo operativa.
    Copia los ítems comerciales como partidas técnicas (BOM) SIN PRECIOS NI DESCUENTOS.
    Garantiza estricta idempotencia y unicidad 1:1 entre Quote y WorkOrder.
    Registra el evento de apertura en la bitácora inmutable.
    Exige min_role_level = RoleLevel.OPERATIONS_COORD (80).
    """
    required_module = 'operations'
    min_role_level = RoleLevel.OPERATIONS_COORD

    def get(self, request, quote_pk):
        """
        Manejo controlado de peticiones directas vía GET.
        Si la OT ya existe, redirige a su detalle informando al usuario.
        Si aún no existe, redirige a la cotización con mensaje informativo.
        """
        quote = get_object_or_404(
            Quote.objects.for_org(request.organization),
            pk=quote_pk
        )
        existing_wo = WorkOrder.objects.for_org(request.organization).filter(source_quote=quote).first()
        if existing_wo:
            messages.info(
                request,
                f"La cotización {quote.number} ya cuenta con la Orden de Trabajo {existing_wo.number}."
            )
            return redirect('operations:workorder_detail', pk=existing_wo.pk)

        messages.info(
            request,
            f"Para generar la Orden de Trabajo de la cotización {quote.number}, confirme la conversión desde el detalle comercial."
        )
        return redirect('crm:quote_detail', pk=quote.pk)

    @transaction.atomic
    def post(self, request, quote_pk):
        # 1. Bloqueo de fila para serializar solicitudes concurrentes en la cotización
        quote = get_object_or_404(
            Quote.objects.for_org(request.organization).select_for_update().select_related('account'),
            pk=quote_pk
        )

        # 2. Validación de estado APROBADA
        if quote.status != Quote.Status.APPROVED:
            messages.error(
                request,
                f"La cotización {quote.number} no está aprobada. "
                "Solo cotizaciones en estado APROBADA pueden convertirse en Orden de Trabajo."
            )
            return redirect('crm:quote_detail', pk=quote.pk)

        # 3. Verificación de idempotencia previa
        existing_wo = WorkOrder.objects.for_org(request.organization).filter(source_quote=quote).first()
        if existing_wo:
            messages.warning(
                request,
                f"La cotización {quote.number} ya posee la Orden de Trabajo {existing_wo.number} generada previamente."
            )
            return redirect('operations:workorder_detail', pk=existing_wo.pk)

        # 4. Creación atómica protegida contra colisiones concurrentes (IntegrityError)
        try:
            with transaction.atomic():
                work_order = WorkOrder.objects.create(
                    organization=request.organization,
                    account=quote.account,
                    source_quote=quote,
                    title=quote.title,
                    technical_scope=quote.notes or f"Alcance según Cotización {quote.number}.",
                    site_address=quote.account.address or quote.account.city,
                    priority=WorkOrder.Priority.NORMAL,
                    phase=WorkOrder.Phase.DRAFT,
                    created_by=request.user,
                )

                # 5. Copiar partidas comerciales a partidas técnicas (BOM SIN PRECIOS)
                quote_items = quote.items.all().order_by('created_at')
                items_to_create = []
                for item in quote_items:
                    items_to_create.append(
                        WorkOrderItem(
                            organization=request.organization,
                            work_order=work_order,
                            item_type=item.item_type,
                            code=item.code or '',
                            description=item.description,
                            unit=item.unit or 'UND',
                            planned_quantity=item.quantity,
                            executed_quantity=Decimal('0.00'),
                            is_completed=False,
                        )
                    )

                if items_to_create:
                    WorkOrderItem.objects.bulk_create(items_to_create)

                # 6. Registrar primer evento en la bitácora cronológica inmutable
                WorkOrderTimelineLog.objects.create(
                    organization=request.organization,
                    work_order=work_order,
                    event_type='FROM_QUOTE',
                    summary=f"Orden de Trabajo generada desde Cotización {quote.number}",
                    details=(
                        f"Apertura formal de OT a partir de propuesta comercial aprobada. "
                        f"Se importaron {len(items_to_create)} partidas técnicas al BOM de ingeniería."
                    ),
                    actor=request.user,
                )
        except IntegrityError:
            # En caso de colisión concurrente en la restricción de base de datos
            existing_wo = WorkOrder.objects.for_org(request.organization).filter(source_quote=quote).first()
            if existing_wo:
                messages.warning(
                    request,
                    f"La cotización {quote.number} ya posee la Orden de Trabajo {existing_wo.number} generada previamente."
                )
                return redirect('operations:workorder_detail', pk=existing_wo.pk)
            raise

        messages.success(
            request,
            f"Orden de Trabajo {work_order.number} generada exitosamente desde la Cotización {quote.number}. "
            f"Se importaron {len(items_to_create)} partidas técnicas al BOM."
        )
        return redirect('operations:workorder_detail', pk=work_order.pk)


# ==============================================================================
# 2. TABLERO MAESTRO DE ÓRDENES DE TRABAJO
# ==============================================================================

class WorkOrderListView(ModuleAccessRequiredMixin, ListView):
    """
    Listado y Tablero de Órdenes de Trabajo.
    - Para Técnicos (20): Vista 'Mis Tareas de Hoy', filtrada estrictamente a OTs
      donde el usuario es miembro de la cuadrilla asignada.
    - Para Ingenieros / Coordinadores (50+): Consola integral con filtros por fase,
      prioridad, cuadrilla y nivel de riesgo SST.
    """
    model = WorkOrder
    template_name = 'operations/workorder_list.html'
    context_object_name = 'work_orders'
    paginate_by = 25
    required_module = 'operations'
    min_role_level = RoleLevel.TECHNICIAN

    def get_queryset(self):
        org = self.request.organization
        role_level = getattr(self.request, 'role_level', 0)
        user = self.request.user

        qs = WorkOrder.objects.for_org(org).select_related(
            'account', 'assigned_engineer', 'assigned_crew', 'assigned_crew__leader'
        )

        # Filtrado estricto para técnicos de campo (Nivel 20)
        if role_level == RoleLevel.TECHNICIAN:
            qs = qs.filter(
                Q(assigned_crew__members=user) | Q(assigned_crew__leader=user)
            ).distinct()
            return qs

        # Filtros para Ingenieros, SST y Coordinadores (50+)
        query = self.request.GET.get('q', '').strip()
        if query:
            qs = qs.filter(
                Q(number__icontains=query) |
                Q(title__icontains=query) |
                Q(account__commercial_name__icontains=query) |
                Q(site_address__icontains=query)
            )

        phase = self.request.GET.get('phase')
        if phase and phase in WorkOrder.Phase.values:
            qs = qs.filter(phase=phase)

        priority = self.request.GET.get('priority')
        if priority and priority in WorkOrder.Priority.values:
            qs = qs.filter(priority=priority)

        crew_id = self.request.GET.get('crew')
        if crew_id:
            qs = qs.filter(assigned_crew_id=crew_id)

        high_risk = self.request.GET.get('high_risk')
        if high_risk == '1':
            qs = qs.filter(is_high_risk=True)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.request.organization
        role_level = getattr(self.request, 'role_level', 0)
        user = self.request.user

        is_field_tech = (role_level == RoleLevel.TECHNICIAN)

        if is_field_tech:
            user_crews = Crew.objects.for_org(org).filter(Q(members=user) | Q(leader=user)).distinct()
            context.update({
                'is_field_tech': True,
                'user_crews': user_crews,
                'total_tasks': self.get_queryset().count(),
                'in_progress_tasks': self.get_queryset().filter(phase=WorkOrder.Phase.IN_PROGRESS).count(),
            })
        else:
            all_ots = WorkOrder.objects.for_org(org)
            context.update({
                'is_field_tech': False,
                'total_count': all_ots.count(),
                'draft_count': all_ots.filter(phase=WorkOrder.Phase.DRAFT).count(),
                'engineering_count': all_ots.filter(phase=WorkOrder.Phase.ENGINEERING).count(),
                'scheduled_count': all_ots.filter(phase=WorkOrder.Phase.SCHEDULED).count(),
                'in_progress_count': all_ots.filter(phase=WorkOrder.Phase.IN_PROGRESS).count(),
                'quality_count': all_ots.filter(phase=WorkOrder.Phase.QUALITY_CHECK).count(),
                'completed_count': all_ots.filter(phase=WorkOrder.Phase.COMPLETED).count(),
                'high_risk_count': all_ots.filter(is_high_risk=True).count(),
                'crews': Crew.objects.for_org(org).filter(is_active=True),
                'phase_choices': WorkOrder.Phase.choices,
                'priority_choices': WorkOrder.Priority.choices,
                'current_phase': self.request.GET.get('phase', ''),
                'current_priority': self.request.GET.get('priority', ''),
                'current_crew': self.request.GET.get('crew', ''),
                'current_high_risk': self.request.GET.get('high_risk', ''),
                'search_query': self.request.GET.get('q', ''),
            })

        return context


class WorkOrderCreateView(ModuleAccessRequiredMixin, CreateView):
    """
    Apertura manual de Orden de Trabajo.
    Exige min_role_level = RoleLevel.OPERATIONS_COORD (80).
    """
    model = WorkOrder
    form_class = WorkOrderCreateForm
    template_name = 'operations/workorder_form.html'
    required_module = 'operations'
    min_role_level = RoleLevel.OPERATIONS_COORD

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['organization'] = self.request.organization
        return kwargs

    def form_valid(self, form):
        wo = form.save(commit=False)
        wo.organization = self.request.organization
        wo.created_by = self.request.user
        wo.save()

        # Registro en bitácora inmutable
        WorkOrderTimelineLog.objects.create(
            organization=self.request.organization,
            work_order=wo,
            event_type='CREATED',
            summary=f"Apertura de Orden de Trabajo {wo.number}",
            details=f"Creada manualmente por {self.request.user.full_name}. Prioridad: {wo.get_priority_display()}.",
            actor=self.request.user,
        )

        messages.success(self.request, f"Orden de Trabajo {wo.number} creada exitosamente.")
        return redirect('operations:workorder_detail', pk=wo.pk)


# ==============================================================================
# 3. CONSOLA TÉCNICA DE LA ORDEN DE TRABAJO
# ==============================================================================

class WorkOrderDetailView(ModuleAccessRequiredMixin, DetailView):
    """
    Consola Integral de Ingeniería y Operaciones de Campo.
    - Embudo de 6 Fases de Ejecución.
    - Ficha de Cliente y Ubicación Técnica.
    - Estado de Seguridad Industrial (SST).
    - Partidas Técnicas (BOM sin precios comerciales).
    - Evidencias Técnicas / Fotográficas.
    - Timeline / Bitácora Cronológica Inmutable.
    """
    model = WorkOrder
    template_name = 'operations/workorder_detail.html'
    context_object_name = 'work_order'
    required_module = 'operations'
    min_role_level = RoleLevel.TECHNICIAN

    def get_queryset(self):
        org = self.request.organization
        role_level = getattr(self.request, 'role_level', 0)
        qs = WorkOrder.objects.for_org(org).select_related(
            'account', 'source_quote', 'assigned_engineer', 'assigned_crew', 'created_by'
        )
        # Técnicos solo pueden consultar OTs donde son miembros de cuadrilla
        if role_level == RoleLevel.TECHNICIAN:
            qs = qs.filter(
                Q(assigned_crew__members=self.request.user) | Q(assigned_crew__leader=self.request.user)
            ).distinct()
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        wo = self.object
        role_level = getattr(self.request, 'role_level', 0)

        items = list(wo.items.all().select_related('completed_by'))
        evidences = list(wo.evidences.all().select_related('uploaded_by'))
        timeline_logs = list(wo.timeline_logs.all().select_related('actor'))

        can_start_field, sst_reason = wo.can_start_field_execution()

        safety_checklists = list(wo.safety_checklists.all().select_related('submitted_by', 'reviewed_by'))
        stock_movements = list(wo.stock_movements.all().select_related('warehouse', 'item', 'performed_by'))

        # Determinar fases a las que puede transicionar según su rol
        context.update({
            'items': items,
            'evidences': evidences,
            'timeline_logs': timeline_logs,
            'safety_checklists': safety_checklists,
            'stock_movements': stock_movements,
            'total_items': len(items),
            'completed_items': len([i for i in items if i.is_completed]),
            'can_start_field': can_start_field,
            'sst_reason': sst_reason,
            'item_form': WorkOrderItemForm(),
            'evidence_form': WorkOrderEvidenceForm(),
            'checklist_form': SafetyChecklistForm(organization=self.request.organization, initial_work_order=wo),
            'can_manage_phases': role_level >= RoleLevel.ENGINEER,
            'can_close_ot': role_level >= RoleLevel.OPERATIONS_COORD,
            'can_manage_sst': role_level >= RoleLevel.SST_OFFICER,
            'can_execute_items': role_level >= RoleLevel.TECHNICIAN and role_level != RoleLevel.SST_OFFICER,
            'phases': WorkOrder.Phase.choices,
        })
        return context


class WorkOrderPhaseTransitionView(ModuleAccessRequiredMixin, View):
    """
    Transición de Fase de Ingeniería / Operaciones.
    - Ingenieros (50+): pueden avanzar fases hasta QUALITY_CHECK.
    - Coordinador (80+): exclusivo para autorizar el Cierre Definitivo (COMPLETED).
    - Regla SST en Alto Riesgo: impide pasar a IN_PROGRESS si is_high_risk=True y sst_cleared=False.
    """
    required_module = 'operations'
    min_role_level = RoleLevel.ENGINEER

    @transaction.atomic
    def post(self, request, pk):
        wo = get_object_or_404(WorkOrder.objects.for_org(request.organization), pk=pk)
        target_phase = request.POST.get('phase')
        notes = request.POST.get('notes', '').strip()
        role_level = getattr(request, 'role_level', 0)

        if target_phase not in WorkOrder.Phase.values:
            messages.error(request, "Fase operativa no válida.")
            return redirect('operations:workorder_detail', pk=wo.pk)

        # Regla RBAC: Cierre definitivo (COMPLETED) exige Nivel 80+
        if target_phase == WorkOrder.Phase.COMPLETED and role_level < RoleLevel.OPERATIONS_COORD:
            return render_403_rbac(
                request,
                required_module='operations',
                min_role_level=RoleLevel.OPERATIONS_COORD,
                reason="El cierre definitivo (COMPLETED) de una Orden de Trabajo requiere nivel de Coordinador de Operaciones (80+)."
            )

        # Regla SST: Inicio en campo (IN_PROGRESS) en Alto Riesgo
        if target_phase == WorkOrder.Phase.IN_PROGRESS:
            can_start, reason = wo.can_start_field_execution()
            if not can_start:
                messages.error(request, f"BLOQUEO SST: {reason}")
                return redirect('operations:workorder_detail', pk=wo.pk)

        old_phase_display = wo.get_phase_display()
        wo.phase = target_phase

        if target_phase == WorkOrder.Phase.COMPLETED:
            wo.completed_at = timezone.now()

        wo.save(update_fields=['phase', 'completed_at', 'updated_at'])

        # Registro obligatorio en bitácora inmutable
        WorkOrderTimelineLog.objects.create(
            organization=request.organization,
            work_order=wo,
            event_type='PHASE_CHANGE',
            summary=f"Fase actualizada: {old_phase_display} &rarr; {wo.get_phase_display()}",
            details=notes or f"Transición autorizada por {request.user.full_name} ({RoleLevel(role_level).label if role_level in RoleLevel.values else ''}).",
            actor=request.user,
        )

        messages.success(request, f"Orden de Trabajo {wo.number} movida a '{wo.get_phase_display()}'.")
        return redirect('operations:workorder_detail', pk=wo.pk)


class WorkOrderSSTClearanceView(ModuleAccessRequiredMixin, View):
    """
    Liberación o Bloqueo Preventivo de Seguridad Industrial (SST).
    Exige min_role_level = RoleLevel.SST_OFFICER (70).
    """
    required_module = 'operations'
    min_role_level = RoleLevel.SST_OFFICER

    @transaction.atomic
    def post(self, request, pk):
        wo = get_object_or_404(WorkOrder.objects.for_org(request.organization), pk=pk)
        action = request.POST.get('action')
        notes = request.POST.get('notes', '').strip()

        if action == 'grant':
            wo.sst_cleared = True
            summary = "Liberación SST Otorgada para Campo"
            msg = f"Se ha otorgado la liberación SST para la OT {wo.number}. Cuadrilla habilitada para iniciar labores."
            messages.success(request, msg)
        elif action == 'revoke':
            wo.sst_cleared = False
            summary = "Liberación SST Revocada / Bloqueo Preventivo"
            msg = f"Se ha revocado la liberación SST para la OT {wo.number}. Labores en campo suspendidas preventivamente."
            messages.warning(request, msg)
        else:
            messages.error(request, "Acción SST no válida.")
            return redirect('operations:workorder_detail', pk=wo.pk)

        wo.save(update_fields=['sst_cleared', 'updated_at'])

        # Registro en bitácora inmutable
        WorkOrderTimelineLog.objects.create(
            organization=request.organization,
            work_order=wo,
            event_type='SST_CLEARANCE',
            summary=summary,
            details=notes or f"Dictamen emitido por el Oficial SST {request.user.full_name}.",
            actor=request.user,
        )

        return redirect('operations:workorder_detail', pk=wo.pk)


class WorkOrderItemExecuteView(ModuleAccessRequiredMixin, View):
    """
    Registro de ejecución de partida técnica en campo (Checklist de avance).
    Accesible para Técnicos de Campo (Nivel 20+).
    """
    required_module = 'operations'
    min_role_level = RoleLevel.TECHNICIAN

    @transaction.atomic
    def post(self, request, wo_pk, item_pk):
        role_level = getattr(request, 'role_level', 0)
        if role_level == RoleLevel.SST_OFFICER:
            return render_403_rbac(
                request,
                required_module='operations',
                min_role_level=RoleLevel.TECHNICIAN,
                reason="Segregación de funciones: El Oficial SST no está facultado para ejecutar partidas técnicas de ingeniería del BOM."
            )

        wo = get_object_or_404(WorkOrder.objects.for_org(request.organization), pk=wo_pk)
        item = get_object_or_404(wo.items.all(), pk=item_pk)

        executed_qty_raw = request.POST.get('executed_quantity', '0')
        try:
            executed_qty = Decimal(executed_qty_raw)
        except Exception:
            executed_qty = item.planned_quantity

        is_completed = request.POST.get('is_completed') == '1' or (executed_qty >= item.planned_quantity)

        item.executed_quantity = executed_qty
        item.is_completed = is_completed
        item.completed_by = request.user
        item.save(update_fields=['executed_quantity', 'is_completed', 'completed_by', 'updated_at'])

        # Registro en bitácora inmutable
        WorkOrderTimelineLog.objects.create(
            organization=request.organization,
            work_order=wo,
            event_type='ITEM_EXECUTED',
            summary=f"Partida ejecutada: {item.description} ({item.executed_quantity}/{item.planned_quantity} {item.unit})",
            details=f"Registro de campo por {request.user.full_name}. Estado completado: {is_completed}.",
            actor=request.user,
        )

        messages.success(request, f"Avance registrado en partida '{item.description}'.")
        return redirect('operations:workorder_detail', pk=wo.pk)


class WorkOrderEvidenceCreateView(ModuleAccessRequiredMixin, View):
    """
    Carga de Evidencia Técnica / Fotográfica desde el campo.
    Accesible para Técnicos de Campo (Nivel 20+).
    """
    required_module = 'operations'
    min_role_level = RoleLevel.TECHNICIAN

    @transaction.atomic
    def post(self, request, pk):
        wo = get_object_or_404(WorkOrder.objects.for_org(request.organization), pk=pk)
        form = WorkOrderEvidenceForm(request.POST)

        if form.is_valid():
            evidence = form.save(commit=False)
            evidence.organization = request.organization
            evidence.work_order = wo
            evidence.uploaded_by = request.user
            evidence.save()

            if evidence.compressed_size_kb > 0 and evidence.original_size_kb > 0:
                opt_note = f"Evidencia fotográfica optimizada: {evidence.original_size_kb:,} KB -> {evidence.compressed_size_kb:,} KB"
                details = f"{evidence.notes}\n{opt_note}".strip() if evidence.notes else opt_note
            else:
                details = evidence.notes or f"Referencia: {evidence.file_reference}"

            # Registro en bitácora inmutable
            WorkOrderTimelineLog.objects.create(
                organization=request.organization,
                work_order=wo,
                event_type='EVIDENCE_ADDED',
                summary=f"Nueva evidencia técnica: {evidence.get_evidence_type_display()} · {evidence.title}",
                details=details,
                actor=request.user,
            )

            messages.success(request, f"Evidencia '{evidence.title}' registrada correctamente.")
        else:
            messages.error(request, "Error al registrar evidencia. Verifique los campos obligatorios.")

        return redirect('operations:workorder_detail', pk=wo.pk)


class WorkOrderAddItemView(ModuleAccessRequiredMixin, View):
    """
    Agrega una partida técnica al BOM de la OT.
    Exige min_role_level = RoleLevel.ENGINEER (50).
    """
    required_module = 'operations'
    min_role_level = RoleLevel.ENGINEER

    def post(self, request, pk):
        wo = get_object_or_404(WorkOrder.objects.for_org(request.organization), pk=pk)
        form = WorkOrderItemForm(request.POST)

        if form.is_valid():
            item = form.save(commit=False)
            item.organization = request.organization
            item.work_order = wo
            item.save()

            # Registro en bitácora inmutable
            WorkOrderTimelineLog.objects.create(
                organization=request.organization,
                work_order=wo,
                event_type='BOM_ITEM_ADDED',
                summary=f"Partida técnica agregada: {item.description} ({item.planned_quantity} {item.unit})",
                details=f"Agregada por ingeniería ({request.user.full_name}).",
                actor=request.user,
            )

            messages.success(request, f"Partida técnica '{item.description}' agregada al BOM de la OT.")
        else:
            messages.error(request, "Error al ingresar partida. Verifique la cantidad y descripción.")

        return redirect('operations:workorder_detail', pk=wo.pk)


# ==============================================================================
# 4. PLANEACIÓN SEMANAL DE CUADRILLAS Y VEHÍCULOS
# ==============================================================================

class CrewPlanningMatrixView(ModuleAccessRequiredMixin, TemplateView):
    """
    Matriz de Planeación Semanal de Cuadrillas, Vehículos y Órdenes de Trabajo.
    Permite lectura desde Nivel 70 (SST_OFFICER) y gestión a Nivel 80 (OPERATIONS_COORD).
    """
    template_name = 'operations/crew_planning.html'
    required_module = 'operations'
    min_role_level = RoleLevel.SST_OFFICER

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.request.organization

        crews = Crew.objects.for_org(org).prefetch_related('members').select_related('leader')
        work_orders = WorkOrder.objects.for_org(org).filter(
            phase__in=[
                WorkOrder.Phase.SCHEDULED,
                WorkOrder.Phase.IN_PROGRESS,
                WorkOrder.Phase.QUALITY_CHECK,
            ]
        ).select_related('account', 'assigned_crew')

        # Agrupar OTs activas por cuadrilla
        crew_matrix = []
        for crew in crews:
            crew_ots = [wo for wo in work_orders if wo.assigned_crew_id == crew.id]
            crew_matrix.append({
                'crew': crew,
                'active_ots': crew_ots,
                'ots_count': len(crew_ots),
            })

        unassigned_ots = [wo for wo in work_orders if not wo.assigned_crew_id]

        context.update({
            'crew_matrix': crew_matrix,
            'unassigned_ots': unassigned_ots,
            'total_crews': crews.count(),
            'active_crews': crews.filter(is_active=True).count(),
            'total_scheduled_ots': len(work_orders),
            'crew_form': CrewForm(organization=org),
        })
        return context


class CrewCreateView(ModuleAccessRequiredMixin, CreateView):
    """
    Creación de nueva Cuadrilla Operativa / Brigada.
    Exige min_role_level = RoleLevel.OPERATIONS_COORD (80).
    """
    model = Crew
    form_class = CrewForm
    template_name = 'operations/crew_form.html'
    required_module = 'operations'
    min_role_level = RoleLevel.OPERATIONS_COORD

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['organization'] = self.request.organization
        return kwargs

    def form_valid(self, form):
        crew = form.save(commit=False)
        crew.organization = self.request.organization
        crew.save()
        form.save_m2m()
        messages.success(self.request, f"Cuadrilla {crew.code} ({crew.name}) registrada exitosamente.")
        return redirect('operations:crew_planning')


# ==============================================================================
# 5. FIRMA DIGITAL EN CAMPO, SINCRONIZACIÓN OFFLINE Y ACTA OFICIAL
# ==============================================================================

class WorkOrderSignCaptureView(ModuleAccessRequiredMixin, View):
    """
    Captura de Firma Digital Táctil de Conformidad del Cliente (<canvas>).
    Accesible desde Nivel 20 (TECHNICIAN) en adelante, con segregación estricta:
    - SST_OFFICER (70): Bloqueado con HTTP 403 por segregación de funciones.
    - TECHNICIAN (20): Debe pertenecer a la cuadrilla asignada a la OT (si no, HTTP 403).
    """
    required_module = 'operations'
    min_role_level = RoleLevel.TECHNICIAN

    @transaction.atomic
    def post(self, request, pk):
        wo = get_object_or_404(
            WorkOrder.objects.for_org(request.organization).select_related('assigned_crew'),
            pk=pk
        )

        role_level = request.membership.role_level

        # Segregación de funciones: Oficial SST no firma actas de recepción técnica
        if role_level == RoleLevel.SST_OFFICER:
            return render_403_rbac(
                request,
                "Segregación de Funciones: El Oficial de Seguridad (SST) no está facultado para estampar firmas de entrega técnica o recepción comercial de OTs."
            )

        # Si es técnico de campo, debe pertenecer a la cuadrilla asignada
        if role_level == RoleLevel.TECHNICIAN:
            is_member = False
            if wo.assigned_crew:
                if wo.assigned_crew.leader_id == request.user.id or wo.assigned_crew.members.filter(id=request.user.id).exists():
                    is_member = True
            if not is_member:
                return render_403_rbac(
                    request,
                    "Acceso Denegado: Como técnico de campo solo puede capturar firmas de entrega en órdenes asignadas a su cuadrilla activa."
                )

        signer_name = request.POST.get('signer_name', '').strip()
        signer_title = request.POST.get('signer_title', '').strip()
        signer_id = request.POST.get('signer_id', '').strip()
        signature_base64 = (request.POST.get('client_signature_data') or request.POST.get('signature_data') or '').strip()

        try:
            hash_seal = wo.record_client_signature(
                signer_name=signer_name,
                signer_title=signer_title,
                signer_id=signer_id,
                signature_base64=signature_base64,
                actor=request.user,
            )
            messages.success(
                request,
                f"Firma digital de conformidad estampada exitosamente. Sello SHA-256: {hash_seal[:16]}... (Inmutable)."
            )
        except ValidationError as e:
            messages.error(request, str(e.message if hasattr(e, 'message') else e))
        except Exception as e:
            messages.error(request, f"Error al sellar firma digital: {str(e)}")

        return redirect('operations:workorder_detail', pk=wo.pk)


class FieldOfflineSyncEndpointView(ModuleAccessRequiredMixin, View):
    """
    Endpoint JSON de Sincronización de Campo Offline (PWA Field Mode).
    Procesa colas de acciones capturadas localmente por el técnico sin conexión:
    - Avances en partidas técnicas (BOM).
    - Evidencias fotográficas comprimidas en Base64.
    - Firma digital táctil de recepción conforme.
    Exige autenticación y membresía activa en la organización.
    """
    required_module = 'operations'
    min_role_level = RoleLevel.TECHNICIAN

    def post(self, request, *args, **kwargs):
        role_level = request.membership.role_level
        if role_level == RoleLevel.SST_OFFICER:
            return JsonResponse({'error': 'Segregación de funciones: SST_OFFICER no procesa ejecuciones de campo.'}, status=403)

        try:
            data = json.loads(request.body.decode('utf-8'))
        except Exception:
            return JsonResponse({'error': 'Payload JSON inválido.'}, status=400)

        wo_id = data.get('work_order_id')
        if not wo_id:
            return JsonResponse({'error': 'work_order_id es obligatorio.'}, status=400)

        wo = get_object_or_404(
            WorkOrder.objects.for_org(request.organization).select_related('assigned_crew'),
            pk=wo_id
        )

        # Verificación de pertenencia a cuadrilla para técnicos
        if role_level == RoleLevel.TECHNICIAN:
            is_member = False
            if wo.assigned_crew:
                if wo.assigned_crew.leader_id == request.user.id or wo.assigned_crew.members.filter(id=request.user.id).exists():
                    is_member = True
            if not is_member:
                return JsonResponse({'error': 'No pertenece a la cuadrilla asignada a esta orden.'}, status=403)

        processed_count = 0
        with transaction.atomic():
            # 1. Partidas técnicas ejecutadas
            items_executed = data.get('items_executed', [])
            for item_data in items_executed:
                item_pk = item_data.get('item_id')
                qty = Decimal(str(item_data.get('executed_quantity') or item_data.get('quantity') or 0))
                notes = item_data.get('notes', '')
                if item_pk and qty > 0:
                    item = wo.items.filter(pk=item_pk).first()
                    if item:
                        item.executed_quantity = qty
                        if item_data.get('is_completed'):
                            item.is_completed = True
                        item.completed_by = request.user
                        item.save(update_fields=['executed_quantity', 'is_completed', 'completed_by', 'updated_at'])
                        WorkOrderTimelineLog.objects.create(
                            organization=request.organization,
                            work_order=wo,
                            event_type='ITEM_EXECUTED',
                            summary=f"Sincronización Offline: Avance en {item.description} ({qty} {item.unit})",
                            details=notes or f"Registrado offline por {request.user.full_name}",
                            actor=request.user,
                        )
                        processed_count += 1

            # 2. Evidencias fotográficas
            evidences = data.get('evidences', [])
            for ev_data in evidences:
                title = ev_data.get('title', 'Evidencia sincronizada offline')
                ev_type = ev_data.get('evidence_type', WorkOrderEvidence.EvidenceType.PHOTO_PROGRESS)
                comp_data = ev_data.get('compressed_image_data', '')
                orig_kb = int(ev_data.get('original_size_kb', 0))
                comp_kb = int(ev_data.get('compressed_size_kb', 0))
                notes = ev_data.get('notes', '')

                evidence = WorkOrderEvidence.objects.create(
                    organization=request.organization,
                    work_order=wo,
                    evidence_type=ev_type,
                    title=title,
                    notes=notes,
                    file_reference=ev_data.get('file_reference', 'CAPTURA-OFFLINE.JPG'),
                    compressed_image_data=comp_data,
                    original_size_kb=orig_kb,
                    compressed_size_kb=comp_kb,
                    uploaded_by=request.user,
                )
                if comp_kb > 0 and orig_kb > 0:
                    details = f"{notes}\nEvidencia optimizada: {orig_kb:,} KB -> {comp_kb:,} KB".strip()
                else:
                    details = notes

                WorkOrderTimelineLog.objects.create(
                    organization=request.organization,
                    work_order=wo,
                    event_type='EVIDENCE_ADDED',
                    summary=f"Sincronización Offline: Evidencia {evidence.title}",
                    details=details,
                    actor=request.user,
                )
                processed_count += 1

            # 3. Firma digital de recepción
            sig_data = data.get('signature')
            if sig_data and isinstance(sig_data, dict):
                signer_name = sig_data.get('signer_name', '')
                signer_title = sig_data.get('signer_title', '')
                signer_id = sig_data.get('signer_id', '')
                signature_base64 = sig_data.get('signature_data') or sig_data.get('client_signature_data') or ''

                if signer_name and signature_base64 and not wo.signature_hash:
                    wo.record_client_signature(
                        signer_name=signer_name,
                        signer_title=signer_title,
                        signer_id=signer_id,
                        signature_base64=signature_base64,
                        actor=request.user,
                    )
                    processed_count += 1

        return JsonResponse({
            'status': 'synced',
            'work_order_number': wo.number,
            'processed_count': processed_count,
        })


class WorkOrderCertificatePrintView(ModuleAccessRequiredMixin, View):
    """
    Acta Oficial de Entrega y Recepción Técnica de Obra / Mantenimiento.
    Formato formal de alta densidad para impresión o PDF que incluye
    la firma digital sellada por SHA-256 de conformidad del cliente.
    """
    required_module = 'operations'
    min_role_level = RoleLevel.TECHNICIAN
    template_name = 'operations/workorder_certificate_print.html'

    def get(self, request, pk):
        wo = get_object_or_404(
            WorkOrder.objects.for_org(request.organization)
            .select_related('account', 'assigned_crew', 'assigned_engineer', 'created_by')
            .prefetch_related('items', 'evidences'),
            pk=pk
        )
        return render(request, self.template_name, {
            'work_order': wo,
            'org': request.organization,
            'items': wo.items.all(),
            'evidences': wo.evidences.all(),
        })

