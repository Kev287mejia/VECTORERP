from django.urls import path
from apps.finance import views

app_name = 'finance'

urlpatterns = [
    # Consola Principal de Tesorería, Cajas Chicas y Bancos
    path('', views.FinanceDashboardView.as_view(), name='dashboard'),

    # Cuentas Bancarias
    path('bank-accounts/create/', views.BankAccountCreateView.as_view(), name='bank_account_create'),

    # Cajas Chicas y Fondos Fijos
    path('funds/create/', views.PettyCashFundCreateView.as_view(), name='fund_create'),
    path('funds/<uuid:pk>/', views.PettyCashFundDetailView.as_view(), name='fund_detail'),

    # Operaciones de Gastos y Desembolsos
    path('expenses/create/', views.PettyCashExpenseCreateView.as_view(), name='expense_create'),
    path('replenishments/submit/', views.PettyCashReplenishSubmitView.as_view(), name='replenish_submit'),
    path('replenishments/disburse/', views.PettyCashDisburseView.as_view(), name='replenish_disburse'),

    # Estados Financieros en Tiempo Real (P&L, Balance General, Balanza de Comprobación)
    path('statements/', views.FinancialStatementsDashboardView.as_view(), name='statements'),
    path('statements/print/', views.FinancialStatementsPrintView.as_view(), name='statements_print'),

    # Consola Tributaria DGI Nicaragua y Exportación VET
    path('dgi-vet/', views.DgiTaxConsoleView.as_view(), name='dgi_vet'),
    path('dgi-vet/export-csv/', views.DgiVetCsvExportView.as_view(), name='dgi_vet_export_csv'),

    # Conciliación Bancaria y Trazabilidad de Extractos (Fase 1C.5.1)
    path('reconciliation/', views.BankReconciliationDashboardView.as_view(), name='reconciliation_dashboard'),
    path('reconciliation/import/', views.BankStatementImportView.as_view(), name='statement_import'),
    path('reconciliation/statements/<uuid:pk>/', views.BankStatementDetailView.as_view(), name='statement_detail'),
    path('reconciliation/matches/confirm/', views.BankReconciliationConfirmMatchView.as_view(), name='match_confirm'),
    path('reconciliation/matches/undo/', views.BankReconciliationUndoMatchView.as_view(), name='match_undo'),
    path('reconciliation/statements/<uuid:pk>/auto-match/', views.BankReconciliationAutoMatchView.as_view(), name='auto_match'),
]
