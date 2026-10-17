# Generated manually for Phase 1C.5.2.2 Bank Deposit Domain Refactoring

from decimal import Decimal
from django.conf import settings
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion


def migrate_deposit_data_forward(apps, schema_editor):
    CashBankDeposit = apps.get_model('billing', 'CashBankDeposit')
    CashBankDeposit.objects.filter(status='DEPOSITED').update(status='BANK_DEPOSIT_RECORDED')

    CashMovement = apps.get_model('billing', 'CashMovement')
    CashMovement.objects.filter(movement_type='REMITTANCE_DISPATCH').update(movement_type='CASH_DEPOSIT_DISPATCH')


def migrate_deposit_data_backward(apps, schema_editor):
    CashBankDeposit = apps.get_model('billing', 'CashBankDeposit')
    CashBankDeposit.objects.filter(status='BANK_DEPOSIT_RECORDED').update(status='DEPOSITED')

    CashMovement = apps.get_model('billing', 'CashMovement')
    CashMovement.objects.filter(movement_type='CASH_DEPOSIT_DISPATCH').update(movement_type='REMITTANCE_DISPATCH')


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('finance', '0010_alter_financialtransaction_transaction_type'),
        ('billing', '0007_posregistershift_denomination_breakdown_and_more'),
    ]

    operations = [
        # 1. Remove old unique constraint before field/model rename
        migrations.RemoveConstraint(
            model_name='cashremittance',
            name='unique_pos_remittance_number_per_tenant',
        ),

        # 2. Rename field remittance_number -> deposit_number on CashRemittance
        migrations.RenameField(
            model_name='cashremittance',
            old_name='remittance_number',
            new_name='deposit_number',
        ),

        # 3. Rename Model CashRemittance -> CashBankDeposit
        migrations.RenameModel(
            old_name='CashRemittance',
            new_name='CashBankDeposit',
        ),
        migrations.AlterModelOptions(
            name='cashbankdeposit',
            options={
                'ordering': ['-prepared_at'],
                'verbose_name': 'Depósito Bancario de Efectivo',
                'verbose_name_plural': 'Depósitos Bancarios de Efectivo',
            },
        ),
        migrations.RenameIndex(
            model_name='cashbankdeposit',
            new_name='billing_cas_organiz_96ba02_idx',
            old_name='billing_cas_organiz_18e39f_idx',
        ),

        # 4. Rename PosRegisterShift.total_remittances_nio -> total_bank_deposits_nio
        migrations.RenameField(
            model_name='posregistershift',
            old_name='total_remittances_nio',
            new_name='total_bank_deposits_nio',
        ),
        migrations.AlterField(
            model_name='posregistershift',
            name='total_bank_deposits_nio',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('0.00'),
                max_digits=14,
                verbose_name='Depósitos Bancarios Despachados (C$)',
            ),
        ),

        # 5. Rename CashMovement.remittance -> bank_deposit
        migrations.RenameField(
            model_name='cashmovement',
            old_name='remittance',
            new_name='bank_deposit',
        ),
        migrations.AlterField(
            model_name='cashmovement',
            name='bank_deposit',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='cash_movements',
                to='billing.cashbankdeposit',
                verbose_name='Depósito Bancario Relacionado',
            ),
        ),
        migrations.AlterField(
            model_name='cashmovement',
            name='movement_type',
            field=models.CharField(
                choices=[
                    ('CASH_IN', 'Entrada Adicional de Efectivo'),
                    ('CASH_OUT', 'Salida de Efectivo / Gasto Menor'),
                    ('POS_SALE', 'Venta POS en Efectivo'),
                    ('CUSTOMER_COLLECTION', 'Cobro de Factura en Efectivo'),
                    ('CUSTOMER_REFUND', 'Devolución en Efectivo a Cliente'),
                    ('SUPPLIER_PAYMENT', 'Pago a Proveedor en Efectivo'),
                    ('CASH_DEPOSIT_DISPATCH', 'Despacho de Efectivo para Depósito Bancario'),
                    ('REMITTANCE_DISPATCH', 'Despacho de Remesa para Depósito Bancario (Legacy)'),
                    ('ADJUSTMENT', 'Ajuste Autorizado de Caja'),
                ],
                db_index=True,
                max_length=30,
                verbose_name='Tipo de Movimiento',
            ),
        ),

        # 6. Alter fields on CashBankDeposit
        migrations.AlterField(
            model_name='cashbankdeposit',
            name='deposit_number',
            field=models.CharField(
                db_index=True,
                help_text='Folio secuencial ej: DEP-2026-0001',
                max_length=30,
                verbose_name='Número de Depósito Bancario',
            ),
        ),
        migrations.AlterField(
            model_name='cashbankdeposit',
            name='shift',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='bank_deposits',
                to='billing.posregistershift',
                verbose_name='Turno Origen de Fondos',
            ),
        ),
        migrations.AlterField(
            model_name='cashbankdeposit',
            name='destination_bank_account',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='cash_bank_deposits',
                to='finance.bankaccount',
                verbose_name='Cuenta Bancaria Destino',
            ),
        ),
        migrations.AlterField(
            model_name='cashbankdeposit',
            name='amount',
            field=models.DecimalField(
                decimal_places=2,
                max_digits=14,
                validators=[django.core.validators.MinValueValidator(Decimal('0.01'))],
                verbose_name='Monto a Depositar (C$ NIO)',
            ),
        ),
        migrations.AlterField(
            model_name='cashbankdeposit',
            name='status',
            field=models.CharField(
                choices=[
                    ('PREPARED', 'Preparado en Gaveta'),
                    ('IN_TRANSIT', 'Entregado a Custodio / En Tránsito'),
                    ('BANK_DEPOSIT_RECORDED', 'Boleta Bancaria Registrada (Pendiente Acreditación)'),
                    ('CONFIRMED', 'Acreditado y Confirmado por Banco'),
                    ('REJECTED', 'Depósito Rechazado'),
                ],
                db_index=True,
                default='PREPARED',
                max_length=25,
                verbose_name='Estado del Depósito Bancario',
            ),
        ),
        migrations.AlterField(
            model_name='cashbankdeposit',
            name='prepared_by',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='prepared_bank_deposits',
                to=settings.AUTH_USER_MODEL,
                verbose_name='Preparado por (Cajero)',
            ),
        ),
        migrations.AlterField(
            model_name='cashbankdeposit',
            name='dispatched_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='dispatched_bank_deposits',
                to=settings.AUTH_USER_MODEL,
                verbose_name='Despachado / Entregado por',
            ),
        ),
        migrations.AlterField(
            model_name='cashbankdeposit',
            name='deposit_slip_number',
            field=models.CharField(
                blank=True,
                db_index=True,
                max_length=80,
                verbose_name='Número de Boleta de Depósito Bancario',
            ),
        ),
        migrations.AlterField(
            model_name='cashbankdeposit',
            name='deposited_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='recorded_bank_deposits',
                to=settings.AUTH_USER_MODEL,
                verbose_name='Registrado en Banco por',
            ),
        ),
        migrations.AlterField(
            model_name='cashbankdeposit',
            name='confirmed_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='confirmed_bank_deposits',
                to=settings.AUTH_USER_MODEL,
                verbose_name='Confirmado / Acreditado por',
            ),
        ),
        migrations.AlterField(
            model_name='cashbankdeposit',
            name='financial_transaction',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='cash_bank_deposits',
                to='finance.financialtransaction',
                verbose_name='Transacción Financiera Bancaria Asentada',
            ),
        ),
        migrations.AlterField(
            model_name='cashbankdeposit',
            name='transit_journal_entry',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='transit_bank_deposits',
                to='finance.journalentry',
                verbose_name='Póliza Contable de Efectivo en Tránsito',
            ),
        ),
        migrations.AlterField(
            model_name='cashbankdeposit',
            name='confirmation_journal_entry',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='confirmation_bank_deposits',
                to='finance.journalentry',
                verbose_name='Póliza Contable de Acreditación Bancaria',
            ),
        ),

        # 7. Add new constraints on CashBankDeposit
        migrations.AddConstraint(
            model_name='cashbankdeposit',
            constraint=models.UniqueConstraint(
                fields=('organization', 'deposit_number'),
                name='unique_pos_deposit_number_per_tenant',
            ),
        ),
        migrations.AddConstraint(
            model_name='cashbankdeposit',
            constraint=models.UniqueConstraint(
                condition=models.Q(deposit_slip_number__gt=''),
                fields=('organization', 'destination_bank_account', 'deposit_slip_number'),
                name='unique_bank_deposit_slip_per_account',
            ),
        ),

        # 8. Data migration
        migrations.RunPython(
            migrate_deposit_data_forward,
            migrate_deposit_data_backward,
        ),
    ]
