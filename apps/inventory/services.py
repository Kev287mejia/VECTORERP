from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.operations.models import WorkOrderTimelineLog
from apps.inventory.models import Warehouse, InventoryItem, StockBalance, StockMovement


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


@transaction.atomic
def dispatch_stock_to_work_order(organization, warehouse, item, work_order, quantity, user, work_order_item=None) -> StockMovement:
    """
    Despacha materiales desde una bodega física o furgoneta móvil directamente a una OT activa.
    Descuenta de Kardex y actualiza la partida técnica del BOM si coincide el SKU/código.
    """
    qty = Decimal(str(quantity))

    if qty <= Decimal('0.00'):
        raise ValidationError("La cantidad a despachar debe ser estrictamente mayor a 0.")

    # 1. Bloquear y verificar saldo en bodega
    balance = StockBalance.objects.select_for_update().filter(
        organization=organization,
        warehouse=warehouse,
        item=item,
    ).first()

    current_qty = balance.quantity if balance else Decimal('0.00')
    if current_qty < qty:
        raise ValidationError(
            f"Existencia insuficiente en {warehouse.name} ({warehouse.code}) para despachar a la OT. "
            f"Disponible: {current_qty} {item.unit}, Solicitado: {qty} {item.unit}."
        )

    # 2. Descontar existencia física
    balance.quantity -= qty
    balance.save(update_fields=['quantity', 'updated_at'])

    # 3. Vincular con partida técnica de la OT (BOM)
    target_wo_item = work_order_item
    if not target_wo_item:
        target_wo_item = work_order.items.filter(code=item.sku).first()

    if target_wo_item:
        target_wo_item.executed_quantity += qty
        if target_wo_item.executed_quantity >= target_wo_item.planned_quantity:
            target_wo_item.is_completed = True
        target_wo_item.completed_by = user
        target_wo_item.save(update_fields=['executed_quantity', 'is_completed', 'completed_by', 'updated_at'])

    # 4. Registrar en el libro mayor de Kardex inmutable
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

    # 5. Registrar evento obligatorio en la bitácora inmutable de la OT
    WorkOrderTimelineLog.objects.create(
        organization=organization,
        work_order=work_order,
        event_type='MATERIAL_DISPATCHED',
        summary=f"Material despachado desde {warehouse.name}: {item.sku} ({qty} {item.unit})",
        details=(
            f"Folio de Kardex {movement.number}. "
            f"Material: {item.name}. "
            f"Despachado por {user.full_name or user.email}."
        ),
        actor=user,
    )

    return movement
