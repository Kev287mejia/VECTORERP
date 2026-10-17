# Generated manually for Phase 1C.6 Realized Foreign Exchange Accounting

from django.db import migrations


def add_realized_fx_accounts_forward(apps, schema_editor):
    Organization = apps.get_model('tenancy', 'Organization')
    AccountingAccount = apps.get_model('finance', 'AccountingAccount')

    fx_accounts = [
        ('4201-01', 'Ganancia por Diferencial Cambiario', 'INCOME'),
        ('6201-01', 'Pérdida por Diferencial Cambiario', 'EXPENSE'),
    ]

    for org in Organization.objects.all():
        for code, name, acct_type in fx_accounts:
            AccountingAccount.objects.get_or_create(
                organization=org,
                code=code,
                defaults={
                    'name': name,
                    'account_type': acct_type,
                    'is_active': True,
                }
            )


def add_realized_fx_accounts_backward(apps, schema_editor):
    AccountingAccount = apps.get_model('finance', 'AccountingAccount')
    AccountingAccount.objects.filter(code__in=['4201-01', '6201-01']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0010_alter_financialtransaction_transaction_type'),
    ]

    operations = [
        migrations.RunPython(
            add_realized_fx_accounts_forward,
            add_realized_fx_accounts_backward,
        ),
    ]
