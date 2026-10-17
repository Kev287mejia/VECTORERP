from decimal import Decimal
from django.db.models import Q
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.core.models import CountryConfig
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel
from apps.finance.models import BankAccount
from apps.billing.models import (
    PosRegisterShift,
    CashMovement,
    CashBankDeposit,
    CashRemittance,
)

User = get_user_model()


class OrmRetrocompatibilityAuditTestCase(TestCase):
    """
    Auditoría formal de retrocompatibilidad del ORM (Fase 1C.5.2.2.1).
    Valida:
    - Acceso mediante alias de clase CashRemittance
    - Consultas filter() y exclude() con campos legados
    - Relaciones anidadas (remittances__..., remittance__...)
    - select_related() y prefetch_related()
    - values(), values_list() y order_by()
    - Consultas con objetos Q()
    - Acceso a remittance_id, remittance_number y campos del turno
    - Creación mediante interfaces de compatibilidad soportadas
    """

    def setUp(self):
        self.country_ni, _ = CountryConfig.objects.get_or_create(
            code='NI',
            defaults={
                'name': 'Nicaragua',
                'flag_emoji': '🇳🇮',
                'primary_currency_code': 'NIO',
                'primary_currency_symbol': 'C$',
                'secondary_currency_code': 'USD',
                'secondary_currency_symbol': '$',
                'default_tax_name': 'IVA',
                'default_tax_rate': Decimal('15.00'),
                'is_active': True,
            }
        )

        self.org = Organization.objects.create(
            name="Org Audit ORM SA",
            legal_name="Org Audit ORM SA",
            tax_id="J0310000000999",
            slug="org-audit-orm-sa",
            country=self.country_ni,
            currency="NIO",
            is_active=True,
        )

        self.user = User.objects.create_user(
            email="cajero.orm@test.com",
            password="Password123!",
            full_name="Cajero ORM Test"
        )

        self.bank_acc = BankAccount.objects.create(
            organization=self.org,
            bank_name="Banco ORM Test",
            account_number="ORM-TEST-001",
            account_type=BankAccount.AccountType.CHECKING,
            currency="NIO",
            current_balance=Decimal('50000.00'),
            is_active=True,
        )

        self.shift = PosRegisterShift.objects.create(
            organization=self.org,
            cashier=self.user,
            shift_number="TURNO-ORM-01",
            register_name="CAJA-ORM",
            opening_cash_nio=Decimal('10000.00'),
            total_remittances_nio=Decimal('3500.00'),
        )

        self.rem = CashRemittance.objects.create(
            organization=self.org,
            shift=self.shift,
            destination_bank_account=self.bank_acc,
            amount=Decimal('3500.00'),
            remittance_number="REM-ORM-2026-0001",
            status=CashRemittance.Status.DEPOSITED,
            prepared_by=self.user,
            deposit_slip_number="MINUTA-ORM-123",
        )

        self.mov = CashMovement.objects.create(
            organization=self.org,
            shift=self.shift,
            movement_type=CashMovement.MovementType.REMITTANCE_DISPATCH,
            amount=Decimal('3500.00'),
            concept="Despacho de prueba ORM",
            performed_by=self.user,
            remittance=self.rem,
        )

    def test_class_identity_and_property_access(self):
        """Verifica que el alias de clase y las properties funcionen bidireccionalmente."""
        self.assertIs(CashRemittance, CashBankDeposit)
        self.assertEqual(self.rem.remittance_number, "REM-ORM-2026-0001")
        self.assertEqual(self.rem.deposit_number, "REM-ORM-2026-0001")
        self.assertEqual(self.shift.total_remittances_nio, Decimal('3500.00'))
        self.assertEqual(self.shift.total_bank_deposits_nio, Decimal('3500.00'))
        self.assertEqual(self.mov.remittance.id, self.rem.id)
        self.assertEqual(self.mov.bank_deposit.id, self.rem.id)
        self.assertEqual(self.shift.remittances.count(), 1)
        self.assertEqual(self.shift.bank_deposits.count(), 1)

    def test_filter_and_exclude_with_legacy_lookups(self):
        """Verifica consultas filter() y exclude() con términos legados."""
        q1 = CashRemittance.objects.filter(remittance_number="REM-ORM-2026-0001")
        self.assertEqual(q1.count(), 1)

        q2 = CashBankDeposit.objects.filter(remittance_number="REM-ORM-2026-0001")
        self.assertEqual(q2.count(), 1)

        q3 = CashRemittance.objects.exclude(remittance_number="REM-ORM-2026-0001")
        self.assertFalse(q3.filter(id=self.rem.id).exists())

        q_shift = PosRegisterShift.objects.filter(total_remittances_nio=Decimal('3500.00'))
        self.assertTrue(q_shift.filter(id=self.shift.id).exists())

        q_shift_gt = PosRegisterShift.objects.filter(total_remittances_nio__gt=Decimal('1000.00'))
        self.assertTrue(q_shift_gt.filter(id=self.shift.id).exists())

        q_mov = CashMovement.objects.filter(remittance=self.rem)
        self.assertTrue(q_mov.filter(id=self.mov.id).exists())

        q_mov_id = CashMovement.objects.filter(remittance_id=self.rem.id)
        self.assertTrue(q_mov_id.filter(id=self.mov.id).exists())

    def test_nested_relations_lookups(self):
        """Verifica búsquedas anidadas a través de remittances__... y remittance__..."""
        q_nest1 = PosRegisterShift.objects.filter(remittances__deposit_slip_number="MINUTA-ORM-123")
        self.assertTrue(q_nest1.filter(id=self.shift.id).exists())

        q_nest2 = CashMovement.objects.filter(remittance__deposit_slip_number="MINUTA-ORM-123")
        self.assertTrue(q_nest2.filter(id=self.mov.id).exists())

    def test_q_objects_with_legacy_terms(self):
        """Verifica que los objetos Q remapeen correctamente sus expresiones."""
        q_obj1 = CashRemittance.objects.filter(Q(remittance_number="REM-ORM-2026-0001"))
        self.assertEqual(q_obj1.count(), 1)

        q_obj2 = CashMovement.objects.filter(Q(remittance=self.rem) | Q(remittance_id=self.rem.id))
        self.assertEqual(q_obj2.count(), 1)

        q_obj3 = PosRegisterShift.objects.filter(
            Q(total_remittances_nio__gte=Decimal('3000.00')) & Q(remittances__amount=Decimal('3500.00'))
        )
        self.assertTrue(q_obj3.filter(id=self.shift.id).exists())

    def test_order_by_with_legacy_fields(self):
        """Verifica ordenamiento mediante campos legados."""
        ordered = list(
            CashRemittance.objects.filter(organization=self.org)
            .order_by("-remittance_number")
            .values_list("deposit_number", flat=True)
        )
        self.assertIn("REM-ORM-2026-0001", ordered)

        ordered_shift = list(
            PosRegisterShift.objects.filter(organization=self.org)
            .order_by("-total_remittances_nio")
            .values_list("shift_number", flat=True)
        )
        self.assertIn("TURNO-ORM-01", ordered_shift)

    def test_values_and_values_list_with_legacy_fields(self):
        """Verifica values() y values_list() al solicitar campos legados."""
        v1 = list(CashRemittance.objects.filter(id=self.rem.id).values("remittance_number"))
        self.assertEqual(v1[0]["deposit_number"], "REM-ORM-2026-0001")

        vl1 = list(CashRemittance.objects.filter(id=self.rem.id).values_list("remittance_number", flat=True))
        self.assertEqual(vl1[0], "REM-ORM-2026-0001")

    def test_select_and_prefetch_related_legacy(self):
        """Verifica select_related y prefetch_related utilizando nombres legados."""
        mov_sr = CashMovement.objects.filter(id=self.mov.id).select_related("remittance").first()
        self.assertEqual(mov_sr.bank_deposit.deposit_number, "REM-ORM-2026-0001")

        shift_pr = PosRegisterShift.objects.filter(id=self.shift.id).prefetch_related("remittances").first()
        self.assertEqual(shift_pr.bank_deposits.count(), 1)
