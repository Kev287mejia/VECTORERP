# Generated manually for Phase 1C.5.2.2 Bank Deposit Refactoring

from django.db import migrations, models


def migrate_legacy_remittance_transactions(apps, schema_editor):
    FinancialTransaction = apps.get_model('finance', 'FinancialTransaction')
    FinancialTransaction.objects.filter(
        transaction_type='CASH_REMITTANCE_DEPOSIT'
    ).update(transaction_type='CASH_BANK_DEPOSIT')


def reverse_legacy_remittance_transactions(apps, schema_editor):
    FinancialTransaction = apps.get_model('finance', 'FinancialTransaction')
    FinancialTransaction.objects.filter(
        transaction_type='CASH_BANK_DEPOSIT'
    ).update(transaction_type='CASH_REMITTANCE_DEPOSIT')


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0009_alter_financialtransaction_transaction_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='financialtransaction',
            name='transaction_type',
            field=models.CharField(
                choices=[
                    ('PETTY_CASH_ALLOCATION', 'Apertura / Dotación Inicial de Fondo Fijo'),
                    ('PETTY_CASH_EXPENSE', 'Egreso de Caja Chica por Gasto de Campo'),
                    ('PETTY_CASH_REPLENISH', 'Desembolso Bancario de Reposición'),
                    ('BANK_TRANSFER', 'Transferencia entre Cuentas Bancarias'),
                    ('CLIENT_COLLECTION', 'Cobro de Factura a Cliente'),
                    ('VENDOR_DISBURSEMENT', 'Pago a Proveedor con Retenciones'),
                    ('CARD_SETTLEMENT', 'Liquidación de Lote de Tarjetas'),
                    ('CHECK_CLEARING', 'Compensación y Acreditación de Cheque'),
                    ('CHECK_REJECTION', 'Rechazo de Cheque'),
                    ('CASH_BANK_DEPOSIT', 'Depósito Bancario de Efectivo'),
                    ('CASH_REMITTANCE_DEPOSIT', 'Depósito de Remesa de Efectivo (Legacy)'),
                ],
                max_length=35,
                verbose_name='Tipo de Transacción',
            ),
        ),
        migrations.RunPython(
            migrate_legacy_remittance_transactions,
            reverse_legacy_remittance_transactions,
        ),
    ]
