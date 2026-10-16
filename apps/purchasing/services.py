from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.inventory.services import record_stock_entry
from apps.finance.services import record_purchase_order_journal_entry
from apps.purchasing.models import PurchaseOrder, SupplierInvoice


@transaction.atomic
def receive_purchase_order_stock(
    organization,
    purchase_order,
    user,
    supplier_invoice_number: str = '',
    supplier_invoice_date=None,
):
    """
    Orquesta atómicamente la recepción física de mercancías en almacén:
    1. Bloqueo pesimista con select_for_update() sobre PurchaseOrder para impedir doble recepción concurrente.
    2. Validación de pertenencia multi-tenant (organización, proveedor, bodega y catálogo).
    3. Validación del estado de la orden (rechaza órdenes ya recibidas, canceladas o liquidadas).
    4. Separación y creación del documento fiscal/comercial del proveedor (SupplierInvoice).
    5. Actualización de existencias y costo promedio ponderado en Kardex (StockMovement inmutable)
       con vinculación explícita a la orden de compra y partida recibida.
    6. Asiento contable automático, cuadrado e bimonetario en Libro Diario (PURCHASES)
       con Débito a Inventarios (Activo 1106-01 / 5101-01) y Crédito a Proveedores (2101-01).
    7. Manejo estricto de atomicidad: cualquier fallo intermedio revierte toda la operación.
    """
    if purchase_order.organization_id != organization.id:
        raise ValidationError("La orden de compra no pertenece a la organización activa.")

    # 1. Bloqueo pesimista de la Orden de Compra
    po = (
        PurchaseOrder.objects.select_for_update()
        .select_related('supplier', 'target_warehouse')
        .get(pk=purchase_order.pk, organization=organization)
    )

    if po.stock_received or po.status in (
        PurchaseOrder.Status.RECEIVED,
        PurchaseOrder.Status.PAID,
        PurchaseOrder.Status.CANCELLED,
    ):
        raise ValidationError(
            f"La orden de compra {po.number} ya fue recepcionada previamente en bodega o se encuentra en estado {po.get_status_display()}."
        )

    # 2. Validación de bodega
    warehouse = po.target_warehouse
    if not warehouse or warehouse.organization_id != organization.id or not warehouse.is_active:
        raise ValidationError("La bodega de destino no pertenece a la organización o se encuentra inactiva.")

    # 3. Validación de partidas
    items = list(po.items.select_related('inventory_item').all())
    if not items:
        raise ValidationError("La orden de compra no contiene partidas para ingresar a bodega.")

    for line in items:
        if line.organization_id != organization.id or line.inventory_item.organization_id != organization.id:
            raise ValidationError(f"La partida '{line.description}' o su ítem no pertenecen a la organización autorizada.")
        if line.quantity <= Decimal('0.00'):
            raise ValidationError(f"La cantidad de la partida '{line.description}' debe ser mayor a 0.")

    # 4. Separación de Factura del Proveedor (SupplierInvoice)
    clean_inv_num = (supplier_invoice_number or po.supplier_invoice_number or '').strip()
    supplier_invoice = None

    if clean_inv_num:
        existing_inv = SupplierInvoice.objects.filter(
            organization=organization,
            supplier=po.supplier,
            invoice_number=clean_inv_num,
        ).first()

        if existing_inv:
            if existing_inv.purchase_order_id and existing_inv.purchase_order_id != po.id:
                raise ValidationError(
                    f"La factura del proveedor '{clean_inv_num}' ya fue registrada previamente para la orden {existing_inv.purchase_order.number}."
                )
            supplier_invoice = existing_inv
        else:
            supplier_invoice = SupplierInvoice.objects.create(
                organization=organization,
                supplier=po.supplier,
                purchase_order=po,
                invoice_number=clean_inv_num,
                issue_date=supplier_invoice_date or (po.issue_date or timezone.now().date()),
                due_date=po.due_date,
                currency_code=po.currency_code,
                exchange_rate=po.exchange_rate,
                subtotal=po.subtotal,
                tax_total=po.tax_total,
                gross_total=po.gross_total,
                ir_withheld_amount=po.ir_withheld_amount,
                imi_withheld_amount=po.imi_withheld_amount,
                net_payable=po.net_payable,
                amount_paid=po.amount_paid,
                balance_due=po.balance_due,
                status=SupplierInvoice.Status.PENDING,
                created_by=user,
                notes=f"Generada automáticamente por recepción de OC {po.number}",
            )

        po.supplier_invoice_number = clean_inv_num

    # 5. Ingreso a Bodega y Kardex por partida
    movements = []
    for line in items:
        ref_note = f"Recepción {po.number} · Factura Proveedor {po.supplier_invoice_number or 'S/F'}"
        mov = record_stock_entry(
            organization=organization,
            warehouse=warehouse,
            item=line.inventory_item,
            quantity=line.quantity,
            unit_cost=line.unit_cost,
            reference_note=ref_note,
            user=user,
            purchase_order=po,
            purchase_order_item=line,
        )
        movements.append(mov)
        line.quantity_received = line.quantity
        line.save(update_fields=['quantity_received'])

    # 6. Actualización de estado en PurchaseOrder
    po.stock_received = True
    po.status = PurchaseOrder.Status.RECEIVED
    po.received_by = user
    po.received_at = timezone.now()
    po.recalculate_totals()
    po.save()

    # 7. Asiento contable de compra en Libro Diario
    journal_entry = record_purchase_order_journal_entry(
        organization=organization,
        purchase_order=po,
        user=user,
        supplier_invoice=supplier_invoice,
    )

    return po, movements, supplier_invoice, journal_entry
