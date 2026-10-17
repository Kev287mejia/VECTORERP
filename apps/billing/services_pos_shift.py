import hashlib
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction, models
from django.utils import timezone

from apps.billing.models import PosRegisterShift, Invoice, PaymentReceipt


def open_pos_shift(organization, cashier, opening_cash_nio, register_name: str = 'CAJA-PRINCIPAL-01') -> PosRegisterShift:
    """
    Abre un nuevo turno de caja POS para el cajero y organización especificados.
    Valida que no exista otro turno con estado OPEN para este cajero en la misma organización.
    """
    already_open = PosRegisterShift.objects.filter(
        organization=organization,
        cashier=cashier,
        status=PosRegisterShift.Status.OPEN,
    ).exists()

    if already_open:
        raise ValidationError("Ya existe un turno de caja abierto para este cajero en la organización.")

    opening_amount = Decimal(str(opening_cash_nio or '0.00')).quantize(Decimal('0.01'))
    if opening_amount < Decimal('0.00'):
        raise ValidationError("El fondo fijo inicial de apertura no puede ser negativo.")

    shift = PosRegisterShift.objects.create(
        organization=organization,
        cashier=cashier,
        register_name=register_name or 'CAJA-PRINCIPAL-01',
        opening_cash_nio=opening_amount,
        expected_cash_in_drawer_nio=opening_amount,
        status=PosRegisterShift.Status.OPEN,
    )
    return shift


def compute_shift_x_cut_metrics(shift: PosRegisterShift) -> dict:
    """
    Calcula en tiempo real las métricas de Corte X Parcial para un turno de caja POS,
    incluyendo ventas, entradas/salidas de efectivo, remesas y pagos a proveedor.
    """
    from apps.billing.services_cash import recalculate_shift_cash_metrics
    return recalculate_shift_cash_metrics(shift)


@transaction.atomic
def close_pos_shift_z_cut(
    shift: PosRegisterShift,
    declared_cash_nio: Decimal,
    user,
    notes: str = '',
    supervisor=None,
    denomination_breakdown: dict = None,
    difference_justification: str = '',
) -> PosRegisterShift:
    """
    Ejecuta el Arqueo y Cierre de Corte Z Definitivo para el turno de caja POS delegando
    en la lógica de control con arqueo por denominación y justificación obligatoria de diferencias.
    """
    from apps.billing.services_cash import close_pos_shift_z_cut as _close_z
    return _close_z(
        shift=shift,
        declared_cash_nio=declared_cash_nio,
        user=user,
        supervisor=supervisor,
        notes=notes,
        denomination_breakdown=denomination_breakdown,
        difference_justification=difference_justification,
    )
