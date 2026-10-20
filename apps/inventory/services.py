from decimal import Decimal
import hashlib
import json
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.operations.models import WorkOrder, WorkOrderItem, WorkOrderTimelineLog
from apps.inventory.models import (
    Warehouse,
    InventoryItem,
    StockBalance,
    StockMovement,
    StockDispatchIdempotencyRecord,
)


@transaction.atomic
def record_stock_entry(
    organization,
    warehouse,
    item,
    quantity,
    unit_cost,
    reference_note,
    user,
    purchase_order=None,
    purchase_order_item=None,
) -> StockMovement:
    """
    Registra entrada de material/insumo a bodega por compra o ajuste inicial.
    Bloquea el saldo con select_for_update() y actualiza el costo promedio ponderado.
    Vincula el movimiento de Kardex a la Orden de Compra y Partida cuando aplica.
    """
    qty = Decimal(str(quantity))
    cost = Decimal(str(unit_cost)) if unit_cost is not None else Decimal('0.00')

    if qty <= Decimal('0.00'):
        raise ValidationError("La cantidad a ingresar debe ser estrictamente mayor a 0.")

    # 1. Bloquear o inicializar saldo en bodega
    balance, _ = StockBalance.objects.select_for_update().get_or_create(
        organization=organization,
        warehouse=warehouse,
        item=item,
        defaults={'quantity': Decimal('0.00')}
    )

    # 2. Recálculo de costo promedio ponderado si el costo ingresado es > 0
    if cost > Decimal('0.00'):
        current_total_stock = item.total_stock()
        current_valuation = current_total_stock * item.unit_cost
        new_entry_valuation = qty * cost
        new_total_stock = current_total_stock + qty

        if new_total_stock > Decimal('0.00'):
            new_weighted_cost = (current_valuation + new_entry_valuation) / new_total_stock
            item.unit_cost = new_weighted_cost.quantize(Decimal('0.01'))
            item.save(update_fields=['unit_cost', 'updated_at'])

    # 3. Incrementar saldo físico
    balance.quantity += qty
    balance.save(update_fields=['quantity', 'updated_at'])

    # 4. Crear registro inmutable de Kardex
    movement_type = StockMovement.MovementType.IN_PURCHASE if cost > Decimal('0.00') else StockMovement.MovementType.IN_ADJUSTMENT
    movement = StockMovement.objects.create(
        organization=organization,
        movement_type=movement_type,
        warehouse=warehouse,
        item=item,
        purchase_order=purchase_order,
        purchase_order_item=purchase_order_item,
        quantity=qty,
        unit_cost_snapshot=cost if cost > Decimal('0.00') else item.unit_cost,
        balance_after=balance.quantity,
        reference_note=reference_note or "Entrada a almacén",
        performed_by=user,
    )
    return movement


@transaction.atomic
def transfer_stock_between_warehouses(organization, source_warehouse, target_warehouse, item, quantity, reference_note, user) -> tuple[StockMovement, StockMovement]:
    """
    Traslada existencias entre dos bodegas físicas o hacia una unidad móvil de cuadrilla.
    Verifica disponibilidad con select_for_update() impidiendo saldos negativos.
    """
    qty = Decimal(str(quantity))

    if qty <= Decimal('0.00'):
        raise ValidationError("La cantidad a trasladar debe ser estrictamente mayor a 0.")

    if source_warehouse.id == target_warehouse.id:
        raise ValidationError("La bodega de origen y la bodega de destino no pueden ser la misma.")

    # 1. Bloquear y verificar saldo en bodega origen
    source_balance = StockBalance.objects.select_for_update().filter(
        organization=organization,
        warehouse=source_warehouse,
        item=item,
    ).first()

    current_qty = source_balance.quantity if source_balance else Decimal('0.00')
    if current_qty < qty:
        raise ValidationError(
            f"Existencia insuficiente en la bodega de origen ({source_warehouse.code}). "
            f"Disponible: {current_qty} {item.unit}, Solicitado: {qty} {item.unit}."
        )

    # 2. Descontar de bodega origen
    source_balance.quantity -= qty
    source_balance.save(update_fields=['quantity', 'updated_at'])

    # 3. Bloquear o inicializar saldo en bodega destino e incrementar
    target_balance, _ = StockBalance.objects.select_for_update().get_or_create(
        organization=organization,
        warehouse=target_warehouse,
        item=item,
        defaults={'quantity': Decimal('0.00')}
    )
    target_balance.quantity += qty
    target_balance.save(update_fields=['quantity', 'updated_at'])

    # 4. Generar par de movimientos en el Kardex inmutable
    mov_out = StockMovement.objects.create(
        organization=organization,
        movement_type=StockMovement.MovementType.TRANSFER_OUT,
        warehouse=source_warehouse,
        target_warehouse=target_warehouse,
        item=item,
        quantity=qty,
        unit_cost_snapshot=item.unit_cost,
        balance_after=source_balance.quantity,
        reference_note=f"Traslado hacia {target_warehouse.name}. {reference_note or ''}".strip(),
        performed_by=user,
    )

    mov_in = StockMovement.objects.create(
        organization=organization,
        movement_type=StockMovement.MovementType.TRANSFER_IN,
        warehouse=target_warehouse,
        target_warehouse=source_warehouse,
        item=item,
        quantity=qty,
        unit_cost_snapshot=item.unit_cost,
        balance_after=target_balance.quantity,
        reference_note=f"Recepción desde {source_warehouse.name}. {reference_note or ''}".strip(),
        performed_by=user,
    )

    return mov_out, mov_in


def dispatch_stock_to_work_order(
    organization,
    warehouse,
    item,
    work_order,
    quantity,
    user,
    work_order_item=None,
    idempotency_key=None,
) -> StockMovement:
    """
    Despacha materiales desde una bodega física o furgoneta móvil directamente a una OT activa.
    Descuenta de Kardex y actualiza la partida técnica del BOM de forma atómica y consistente.

    Garantías de integridad:
    - DEF-05: Idempotencia transaccional estricta (StockDispatchIdempotencyRecord) contra dobles clics,
              reintentos de red o reconexiones. Replay seguro idéntico y rechazo de payload alterado.
    - DEF-15: Aislamiento multiempresa estricto para bodega, material, OT y partida BOM.
    - DEF-16: Vinculación inequívoca con partida técnica del BOM (rechazo de discrepancias y ambigüedades).
    - DEF-17: Rechazo de despachos a OTs en fase COMPLETED o CANCELLED.
    - DEF-18: Adquisición ordenada de bloqueos (StockBalance -> WorkOrderItem) con select_for_update.
    """
    qty = Decimal(str(quantity))

    # 0. Validación de cantidad estrictamente positiva
    if qty <= Decimal('0.00'):
        raise ValidationError("La cantidad a despachar debe ser estrictamente mayor a 0.")

    clean_idempotency_key = (idempotency_key or '').strip() or None
    req_hash = None

    if clean_idempotency_key:
        payload_data = {
            'organization_id': str(organization.id),
            'warehouse_id': str(warehouse.id),
            'item_id': str(item.id),
            'work_order_id': str(work_order.id),
            'quantity': f"{qty:.2f}",
            'work_order_item_id': str(work_order_item.id if work_order_item else ''),
        }
        req_hash = hashlib.sha256(json.dumps(payload_data, sort_keys=True).encode('utf-8')).hexdigest()

        # 1. Chequeo preliminar de idempotencia (Fast path fuera de transacción)
        existing_rec = StockDispatchIdempotencyRecord.objects.filter(
            organization=organization,
            idempotency_key=clean_idempotency_key,
        ).select_related('stock_movement').first()

        if existing_rec:
            if existing_rec.request_hash == req_hash:
                return existing_rec.stock_movement
            else:
                raise ValidationError("La clave de idempotencia especificada ya fue utilizada con parámetros de solicitud diferentes.")

    # 2. Ejecución transaccional atómica protegida contra colisiones de concurrencia
    try:
        with transaction.atomic():
            return _execute_dispatch_to_work_order_inner(
                organization=organization,
                warehouse=warehouse,
                item=item,
                work_order=work_order,
                qty=qty,
                user=user,
                work_order_item=work_order_item,
                clean_idempotency_key=clean_idempotency_key,
                req_hash=req_hash,
            )
    except IntegrityError:
        # Colisión concurrente en la clave única de idempotencia
        if clean_idempotency_key:
            existing_rec = StockDispatchIdempotencyRecord.objects.filter(
                organization=organization,
                idempotency_key=clean_idempotency_key,
            ).select_related('stock_movement').first()
            if existing_rec:
                if existing_rec.request_hash == req_hash:
                    return existing_rec.stock_movement
                else:
                    raise ValidationError("La clave de idempotencia especificada ya fue utilizada con parámetros de solicitud diferentes.")
        raise


def _execute_dispatch_to_work_order_inner(
    organization,
    warehouse,
    item,
    work_order,
    qty,
    user,
    work_order_item,
    clean_idempotency_key,
    req_hash,
) -> StockMovement:
    # 1. DEF-15: Aislamiento multiempresa estricto previo a cualquier mutación de datos
    if warehouse.organization_id != organization.id:
        raise ValidationError(
            f"Aislamiento multiempresa violado: La bodega '{warehouse.name}' ({warehouse.code}) "
            f"no pertenece a la organización '{organization.name}'."
        )
    if item.organization_id != organization.id:
        raise ValidationError(
            f"Aislamiento multiempresa violado: El material '{item.name}' ({item.sku}) "
            f"no pertenece a la organización '{organization.name}'."
        )
    if work_order.organization_id != organization.id:
        raise ValidationError(
            f"Aislamiento multiempresa violado: La orden de trabajo '{work_order.number}' "
            f"no pertenece a la organización '{organization.name}'."
        )
    if work_order_item is not None and work_order_item.organization_id != organization.id:
        raise ValidationError(
            f"Aislamiento multiempresa violado: La partida del BOM '{work_order_item.description}' "
            f"no pertenece a la organización '{organization.name}'."
        )

    # 2. DEF-17: Validación de fase activa de la OT
    if work_order.phase in [WorkOrder.Phase.COMPLETED, WorkOrder.Phase.CANCELLED]:
        raise ValidationError(
            f"No es posible despachar materiales a la Orden de Trabajo {work_order.number} "
            f"porque se encuentra en fase {work_order.get_phase_display()}."
        )

    # 3. DEF-16: Resolución estricta e inequívoca de la partida técnica del BOM
    target_wo_item_id = None
    if work_order_item is not None:
        if work_order_item.work_order_id != work_order.id:
            raise ValidationError(
                f"La partida técnica '{work_order_item.description}' no pertenece a la "
                f"Orden de Trabajo {work_order.number}."
            )
        if work_order_item.code != item.sku:
            raise ValidationError(
                f"Discrepancia de código: La partida técnica '{work_order_item.description}' "
                f"({work_order_item.code or 'sin código'}) no corresponde al SKU del material a despachar ({item.sku})."
            )
        target_wo_item_id = work_order_item.id
    else:
        if not item.sku:
            raise ValidationError(
                f"El material '{item.name}' no tiene un SKU definido en el catálogo maestro. "
                f"No es posible vincularlo de forma inequívoca al BOM de la OT."
            )
        matching_ids = list(work_order.items.filter(code=item.sku).values_list('id', flat=True))
        if len(matching_ids) == 0:
            raise ValidationError(
                f"No se encontró ninguna partida técnica en la OT {work_order.number} que coincida con el SKU '{item.sku}'. "
                f"Para despachar este material, ingeniería debe incorporar previamente la partida correspondiente en el BOM de la OT."
            )
        elif len(matching_ids) > 1:
            raise ValidationError(
                f"Existen múltiples partidas técnicas ({len(matching_ids)}) en la OT {work_order.number} con el código '{item.sku}'. "
                f"Debe especificarse la partida técnica exacta para evitar imputaciones ambiguas."
            )
        target_wo_item_id = matching_ids[0]

    # 4. DEF-18: Adquisición ordenada de bloqueos (StockBalance -> WorkOrderItem)
    # 4a. Bloquear y verificar saldo en bodega
    balance = StockBalance.objects.select_for_update().filter(
        organization=organization,
        warehouse=warehouse,
        item=item,
    ).first()

    # Re-chequeo de idempotencia post-bloqueo para solicitudes serializadas por select_for_update
    if clean_idempotency_key:
        existing_rec = StockDispatchIdempotencyRecord.objects.filter(
            organization=organization,
            idempotency_key=clean_idempotency_key,
        ).select_related('stock_movement').first()
        if existing_rec:
            if existing_rec.request_hash == req_hash:
                return existing_rec.stock_movement
            else:
                raise ValidationError("La clave de idempotencia especificada ya fue utilizada con parámetros de solicitud diferentes.")

    current_qty = balance.quantity if balance else Decimal('0.00')
    if current_qty < qty:
        raise ValidationError(
            f"Existencia insuficiente en {warehouse.name} ({warehouse.code}) para despachar a la OT. "
            f"Disponible: {current_qty} {item.unit}, Solicitado: {qty} {item.unit}."
        )

    # 4b. Bloquear y revalidar la partida técnica del BOM
    target_wo_item = WorkOrderItem.objects.select_for_update().filter(
        id=target_wo_item_id,
        work_order=work_order,
        organization=organization,
    ).first()

    if not target_wo_item:
        raise ValidationError(
            f"La partida técnica del BOM seleccionada ya no está disponible en la OT {work_order.number}."
        )

    # 5. Descontar existencia física
    balance.quantity -= qty
    balance.save(update_fields=['quantity', 'updated_at'])

    # 6. Actualizar partida técnica del BOM
    target_wo_item.executed_quantity += qty
    if target_wo_item.executed_quantity >= target_wo_item.planned_quantity:
        target_wo_item.is_completed = True
    target_wo_item.completed_by = user
    target_wo_item.save(update_fields=['executed_quantity', 'is_completed', 'completed_by', 'updated_at'])

    # 7. Registrar en el libro mayor de Kardex inmutable
    movement = StockMovement.objects.create(
        organization=organization,
        movement_type=StockMovement.MovementType.OUT_WORKORDER,
        warehouse=warehouse,
        item=item,
        work_order=work_order,
        work_order_item=target_wo_item,
        quantity=qty,
        unit_cost_snapshot=item.unit_cost,
        balance_after=balance.quantity,
        reference_note=f"Despacho a {work_order.number} · {work_order.title}",
        performed_by=user,
    )

    # 8. Registrar evento obligatorio en la bitácora inmutable de la OT
    WorkOrderTimelineLog.objects.create(
        organization=organization,
        work_order=work_order,
        event_type='MATERIAL_DISPATCHED',
        summary=f"Material despachado desde {warehouse.name}: {item.sku} ({qty} {item.unit})",
        details=(
            f"Folio de Kardex {movement.number}. "
            f"Material: {item.name}. "
            f"Partida BOM: {target_wo_item.description} ({target_wo_item.executed_quantity}/{target_wo_item.planned_quantity} {target_wo_item.unit}). "
            f"Despachado por {user.full_name or user.email}."
        ),
        actor=user,
    )

    # 9. DEF-05: Asentar Registro de Idempotencia de Solicitud en la misma transacción atómica
    if clean_idempotency_key and req_hash:
        StockDispatchIdempotencyRecord.objects.create(
            organization=organization,
            idempotency_key=clean_idempotency_key,
            work_order=work_order,
            stock_movement=movement,
            request_hash=req_hash,
            status=StockDispatchIdempotencyRecord.ProcessingStatus.SUCCESS,
            response_payload={
                'movement_id': str(movement.pk),
                'movement_number': movement.number,
                'quantity': str(movement.quantity),
                'warehouse_id': str(warehouse.id),
                'warehouse_code': warehouse.code,
                'item_id': str(item.id),
                'item_sku': item.sku,
                'work_order_id': str(work_order.id),
                'work_order_number': work_order.number,
                'work_order_item_id': str(target_wo_item.id),
            },
        )

    return movement

