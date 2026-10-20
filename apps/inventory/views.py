from decimal import Decimal
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView

from apps.tenancy.models import RoleLevel
from apps.tenancy.permissions import ModuleAccessRequiredMixin, render_403_rbac
from apps.operations.models import WorkOrder
from apps.inventory.models import Warehouse, InventoryItem, StockBalance, StockMovement, InventoryBatch
from apps.inventory.forms import (
    WarehouseForm,
    InventoryItemForm,
    StockEntryForm,
    StockTransferForm,
    StockDispatchForm,
)
from apps.inventory.services import (
    record_stock_entry,
    transfer_stock_between_warehouses,
    dispatch_stock_to_work_order,
)


class InventoryDashboardView(ModuleAccessRequiredMixin, TemplateView):
    """
    Consola Integral de Inventarios, Bodegas Físicas, Furgonetas Móviles y Kardex.
    Accesible a partir de Nivel ENGINEER (50).
    Confidencialidad Financiera:
    - Ingenieros (50) y Oficiales SST (70): Ven cantidades físicas y ubicaciones, cero costos unitarios.
    - Coordinadores (80) y Owners (100): Acceso a valorización total y costos unitarios promedios.
    """
    template_name = 'inventory/dashboard.html'
    required_module = 'inventory'
    min_role_level = RoleLevel.ENGINEER

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.request.organization
        role_level = getattr(self.request, 'role_level', 0)
        can_view_financials = role_level >= RoleLevel.OPERATIONS_COORD

        warehouses = list(Warehouse.objects.for_org(org).select_related('linked_crew'))
        items = list(InventoryItem.objects.for_org(org).prefetch_related('balances', 'balances__warehouse'))
        movements = list(
            StockMovement.objects.for_org(org)
            .select_related('warehouse', 'target_warehouse', 'item', 'work_order', 'performed_by')
            [:60]
        )

        low_stock_count = sum(1 for item in items if item.is_low_stock())
        total_valuation = sum(item.total_stock() * item.unit_cost for item in items)

        # Mapeo de existencias por ítem para display rápido
        item_rows = []
        for it in items:
            total_qty = it.total_stock()
            balances_dict = {b.warehouse_id: b.quantity for b in it.balances.all()}
            item_rows.append({
                'item': it,
                'total_qty': total_qty,
                'is_low_stock': total_qty <= it.min_stock_level,
                'balances_dict': balances_dict,
            })

        context.update({
            'warehouses': warehouses,
            'items': items,
            'item_rows': item_rows,
            'movements': movements,
            'low_stock_count': low_stock_count,
            'total_valuation': total_valuation,
            'can_view_financials': can_view_financials,
            'can_manage_inventory': role_level >= RoleLevel.OPERATIONS_COORD,
            'warehouse_form': WarehouseForm(organization=org),
            'item_form': InventoryItemForm(),
            'entry_form': StockEntryForm(organization=org),
            'transfer_form': StockTransferForm(organization=org),
            'dispatch_form': StockDispatchForm(organization=org),
        })
        return context


class WarehouseCreateView(ModuleAccessRequiredMixin, View):
    """
    Alta de Bodega Física o Unidad Móvil de Cuadrilla.
    Exige min_role_level = RoleLevel.OPERATIONS_COORD (80).
    """
    required_module = 'inventory'
    min_role_level = RoleLevel.OPERATIONS_COORD

    @transaction.atomic
    def post(self, request):
        org = request.organization
        form = WarehouseForm(request.POST, organization=org)

        if form.is_valid():
            wh = form.save(commit=False)
            wh.organization = org
            wh.save()
            messages.success(request, f"Bodega {wh.code} ({wh.name}) registrada exitosamente.")
        else:
            messages.error(request, "Error al registrar bodega. Verifique los datos ingresados.")

        return redirect('inventory:dashboard')


class InventoryItemCreateView(ModuleAccessRequiredMixin, View):
    """
    Alta de Material, Insumo o Herramienta en el Catálogo Maestro.
    Exige min_role_level = RoleLevel.OPERATIONS_COORD (80).
    """
    required_module = 'inventory'
    min_role_level = RoleLevel.OPERATIONS_COORD

    @transaction.atomic
    def post(self, request):
        org = request.organization
        form = InventoryItemForm(request.POST)

        if form.is_valid():
            item = form.save(commit=False)
            item.organization = org
            item.save()
            messages.success(request, f"Ítem {item.sku} ({item.name}) registrado en el catálogo maestro.")
        else:
            messages.error(request, "Error al registrar ítem de inventario. Verifique el SKU y campos obligatorios.")

        return redirect('inventory:dashboard')


class StockEntryCreateView(ModuleAccessRequiredMixin, View):
    """
    Recepción / Entrada de Materiales a Bodega por Compra o Ajuste.
    Exige min_role_level = RoleLevel.OPERATIONS_COORD (80).
    """
    required_module = 'inventory'
    min_role_level = RoleLevel.OPERATIONS_COORD

    def post(self, request):
        org = request.organization
        form = StockEntryForm(request.POST, organization=org)

        if form.is_valid():
            warehouse = form.cleaned_data['warehouse']
            item = form.cleaned_data['item']
            quantity = form.cleaned_data['quantity']
            unit_cost = form.cleaned_data['unit_cost']
            reference_note = form.cleaned_data['reference_note']

            try:
                mov = record_stock_entry(
                    organization=org,
                    warehouse=warehouse,
                    item=item,
                    quantity=quantity,
                    unit_cost=unit_cost,
                    reference_note=reference_note,
                    user=request.user,
                )
                messages.success(
                    request,
                    f"Entrada registrada: Folio {mov.number}. +{quantity} {item.unit} ingresados a {warehouse.name}."
                )
            except ValidationError as e:
                messages.error(request, f"Error de validación: {e.message if hasattr(e, 'message') else str(e)}")
        else:
            messages.error(request, "Error en los datos de entrada a bodega. Verifique los campos.")

        return redirect('inventory:dashboard')


class StockTransferCreateView(ModuleAccessRequiredMixin, View):
    """
    Traslado entre Bodegas o Carga a Furgoneta Móvil de Cuadrilla.
    Exige min_role_level = RoleLevel.OPERATIONS_COORD (80).
    """
    required_module = 'inventory'
    min_role_level = RoleLevel.OPERATIONS_COORD

    def post(self, request):
        org = request.organization
        form = StockTransferForm(request.POST, organization=org)

        if form.is_valid():
            source_wh = form.cleaned_data['source_warehouse']
            target_wh = form.cleaned_data['target_warehouse']
            item = form.cleaned_data['item']
            quantity = form.cleaned_data['quantity']
            reference_note = form.cleaned_data['reference_note']

            try:
                mov_out, mov_in = transfer_stock_between_warehouses(
                    organization=org,
                    source_warehouse=source_wh,
                    target_warehouse=target_wh,
                    item=item,
                    quantity=quantity,
                    reference_note=reference_note,
                    user=request.user,
                )
                messages.success(
                    request,
                    f"Traslado completado ({mov_out.number} & {mov_in.number}): "
                    f"{quantity} {item.unit} trasladados de {source_wh.name} a {target_wh.name}."
                )
            except ValidationError as e:
                messages.error(request, f"Error en traslado: {e.message if hasattr(e, 'message') else str(e)}")
        else:
            messages.error(request, "Error en datos del traslado. Verifique que las bodegas y cantidad sean válidas.")

        return redirect('inventory:dashboard')


class StockDispatchToOTView(ModuleAccessRequiredMixin, View):
    """
    Despacho de Materiales desde Bodega o Unidad Móvil directamente a una Orden de Trabajo (OT).
    Accesible para Ingenieros (50) y Coordinadores (80+).
    Segregación de funciones: Bloquea con HTTP 403 al Oficial SST (70).
    """
    required_module = 'inventory'
    min_role_level = RoleLevel.ENGINEER

    def post(self, request):
        org = request.organization
        role_level = getattr(request, 'role_level', 0)

        # Regla de segregación de funciones: el Oficial SST supervisa seguridad pero no despacha materiales
        if role_level == RoleLevel.SST_OFFICER:
            return render_403_rbac(
                request,
                required_module='inventory',
                min_role_level=RoleLevel.ENGINEER,
                reason="Segregación de funciones: El Oficial SST no está facultado para realizar despachos o consumos de materiales hacia OTs."
            )

        form = StockDispatchForm(request.POST, organization=org)

        if form.is_valid():
            warehouse = form.cleaned_data['warehouse']
            work_order = form.cleaned_data['work_order']
            item = form.cleaned_data['item']
            quantity = form.cleaned_data['quantity']

            work_order_item = form.cleaned_data.get('work_order_item')
            idempotency_key = (
                form.cleaned_data.get('idempotency_key')
                or request.POST.get('idempotency_key')
                or request.headers.get('Idempotency-Key')
            )

            try:
                mov = dispatch_stock_to_work_order(
                    organization=org,
                    warehouse=warehouse,
                    item=item,
                    work_order=work_order,
                    quantity=quantity,
                    user=request.user,
                    work_order_item=work_order_item,
                    idempotency_key=idempotency_key,
                )
                messages.success(
                    request,
                    f"Despacho a OT completado ({mov.number}): "
                    f"{quantity} {item.unit} de {item.name} despachados a {work_order.number} desde {warehouse.name}."
                )
            except ValidationError as e:
                err_text = e.message if hasattr(e, 'message') else (', '.join(e.messages) if hasattr(e, 'messages') else str(e))
                messages.error(request, f"Error en despacho a OT: {err_text}")
        else:
            messages.error(request, "Error en datos del despacho. Verifique la bodega, OT y cantidad.")

        next_url = request.POST.get('next')
        if next_url:
            return redirect(next_url)
        return redirect('inventory:dashboard')


class BatchExpiryMonitorView(ModuleAccessRequiredMixin, View):
    """
    Tablero de Control Sanitario FEFO (First Expired, First Out) & Vencimientos Farmacéuticos.
    Monitorea lotes activos, lotes próximos a vencer (<90 días) y lotes caducados bloqueados (MINSA).
    Permite registrar nuevos lotes con número de serie y fecha de expiración.
    """
    required_module = 'inventory'
    min_role_level = RoleLevel.TECHNICIAN
    template_name = 'inventory/batches_fefo.html'

    def get(self, request):
        from django.utils import timezone
        org = request.organization
        batches = list(
            InventoryBatch.objects.for_org(org)
            .select_related('item', 'warehouse')
            .order_by('expiration_date', 'item__name')
        )

        today = timezone.now().date()
        optimal_batches = []
        expiring_soon_batches = []
        expired_batches = []

        for b in batches:
            status = b.expiry_status
            if status == 'EXPIRED':
                expired_batches.append(b)
            elif status == 'EXPIRING_SOON':
                expiring_soon_batches.append(b)
            else:
                optimal_batches.append(b)

        warehouses = list(Warehouse.objects.for_org(org).filter(is_active=True))
        items = list(InventoryItem.objects.for_org(org).filter(is_active=True))

        context = {
            'batches': batches,
            'optimal_count': len(optimal_batches),
            'expiring_soon_count': len(expiring_soon_batches),
            'expired_count': len(expired_batches),
            'total_batches_count': len(batches),
            'warehouses': warehouses,
            'items': items,
            'today': today,
        }
        return render(request, self.template_name, context)

    def post(self, request):
        from django.utils import timezone
        org = request.organization
        role_level = getattr(request, 'role_level', 0)
        if role_level < RoleLevel.ENGINEER:
            messages.error(request, "Permisos insuficientes para registrar lotes de inventario (Mínimo Nivel 50).")
            return redirect('inventory:batches_fefo')

        item_id = request.POST.get('item_id')
        warehouse_id = request.POST.get('warehouse_id')
        batch_number = (request.POST.get('batch_number') or '').strip().upper()
        manufacturing_date = request.POST.get('manufacturing_date') or None
        expiration_date = request.POST.get('expiration_date')
        quantity_str = request.POST.get('quantity') or '0.00'

        if not item_id or not warehouse_id or not batch_number or not expiration_date:
            messages.error(request, "Campos obligatorios incompletos: Producto, Bodega, N° de Lote y Fecha de Vencimiento son requeridos.")
            return redirect('inventory:batches_fefo')

        try:
            item = get_object_or_404(InventoryItem, id=item_id, organization=org)
            warehouse = get_object_or_404(Warehouse, id=warehouse_id, organization=org)
            qty = Decimal(quantity_str)
            if qty < Decimal('0.00'):
                raise ValidationError("La cantidad no puede ser negativa.")

            with transaction.atomic():
                batch, created = InventoryBatch.objects.get_or_create(
                    organization=org,
                    warehouse=warehouse,
                    item=item,
                    batch_number=batch_number,
                    defaults={
                        'manufacturing_date': manufacturing_date,
                        'expiration_date': expiration_date,
                        'quantity_available': qty,
                    }
                )
                if not created:
                    batch.quantity_available += qty
                    if manufacturing_date:
                        batch.manufacturing_date = manufacturing_date
                    batch.expiration_date = expiration_date
                    batch.save()

                # Actualizar también StockBalance de esa bodega e ítem
                if qty > Decimal('0.00'):
                    balance, _ = StockBalance.objects.select_for_update().get_or_create(
                        organization=org,
                        warehouse=warehouse,
                        item=item,
                        defaults={'quantity': Decimal('0.00')}
                    )
                    balance.quantity += qty
                    balance.save(update_fields=['quantity', 'updated_at'])

                    # Kardex inmutable
                    StockMovement.objects.create(
                        organization=org,
                        movement_type=StockMovement.MovementType.IN_ADJUSTMENT,
                        warehouse=warehouse,
                        item=item,
                        batch=batch,
                        quantity=qty,
                        unit_cost_snapshot=item.unit_cost,
                        balance_after=balance.quantity,
                        reference_note=f"Alta/Ingreso de Lote FEFO: {batch.batch_number}",
                        performed_by=request.user,
                    )

            messages.success(request, f"Lote {batch_number} para '{item.name}' registrado exitosamente ({qty} {item.unit}).")
        except Exception as e:
            messages.error(request, f"Error al registrar lote: {str(e)}")

        return redirect('inventory:batches_fefo')
