from decimal import Decimal
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView, DetailView

from apps.tenancy.models import RoleLevel
from apps.tenancy.permissions import ModuleAccessRequiredMixin
from apps.finance.models import (
    BankAccount,
    BankStatement,
    BankStatementLine,
    BankReconciliationMatch,
    FinancialTransaction,
)
from apps.finance.forms import BankStatementUploadForm
from apps.finance.services_reconciliation import (
    import_bank_statement,
    run_reconciliation_matching,
    confirm_reconciliation_match,
    undo_reconciliation_match,
    auto_confirm_exact_matches,
)


class BankReconciliationDashboardView(ModuleAccessRequiredMixin, TemplateView):
    """
    Consola Principal de Conciliación Bancaria y Trazabilidad de Extractos.
    Requiere Nivel OPERATIONS_COORD (80) o superior para resguardar la confidencialidad bancaria.
    """
    template_name = 'finance/reconciliation_dashboard.html'
    required_module = 'finance'
    min_role_level = RoleLevel.OPERATIONS_COORD

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.request.organization

        bank_accounts = list(BankAccount.objects.for_org(org).filter(is_active=True))
        
        # Resumen por cuenta bancaria
        account_summaries = []
        for b in bank_accounts:
            latest_stmt = BankStatement.objects.for_org(org).filter(bank_account=b).order_by('-period_end', '-created_at').first()
            unrec_lines_count = BankStatementLine.objects.for_org(org).filter(
                bank_account=b, status=BankStatementLine.LineStatus.UNRECONCILED
            ).count()
            unrec_tx_count = FinancialTransaction.objects.for_org(org).filter(
                bank_account=b, reconciliation_match__isnull=True
            ).count()

            account_summaries.append({
                'bank_account': b,
                'latest_statement': latest_stmt,
                'unreconciled_lines_count': unrec_lines_count,
                'unreconciled_tx_count': unrec_tx_count,
            })

        statements = list(
            BankStatement.objects.for_org(org)
            .select_related('bank_account', 'imported_by')
            .order_by('-created_at')[:20]
        )

        recent_matches = list(
            BankReconciliationMatch.objects.for_org(org)
            .select_related('statement_line__bank_account', 'financial_transaction', 'reconciled_by')
            .order_by('-reconciled_at')[:30]
        )

        context.update({
            'account_summaries': account_summaries,
            'statements': statements,
            'recent_matches': recent_matches,
            'upload_form': BankStatementUploadForm(organization=org),
        })
        return context


class BankStatementImportView(ModuleAccessRequiredMixin, View):
    """
    Carga y procesamiento controlado de extractos bancarios en CSV.
    """
    required_module = 'finance'
    min_role_level = RoleLevel.OPERATIONS_COORD

    def post(self, request, *args, **kwargs):
        org = request.organization
        form = BankStatementUploadForm(request.POST, request.FILES, organization=org)

        if not form.is_valid():
            for field, errors in form.errors.items():
                for err in errors:
                    messages.error(request, f"{err}")
            return redirect('finance:reconciliation_dashboard')

        bank_account = form.cleaned_data['bank_account']
        file_obj = form.cleaned_data['file']
        init_bal = form.cleaned_data.get('initial_balance') or Decimal('0.00')
        final_bal = form.cleaned_data.get('final_balance') or Decimal('0.00')
        notes = form.cleaned_data.get('notes') or ''

        try:
            file_bytes = file_obj.read()
            statement = import_bank_statement(
                organization=org,
                bank_account=bank_account,
                file_content=file_bytes,
                file_name=file_obj.name,
                user=request.user,
                initial_balance=init_bal,
                final_balance=final_bal,
                notes=notes,
            )
            messages.success(
                request,
                f"Extracto bancario '{statement.statement_number}' importado exitosamente: "
                f"{statement.total_lines} movimientos procesados ({statement.total_credits} C$ abonos, {statement.total_debits} C$ cargos)."
            )
            return redirect('finance:statement_detail', pk=statement.id)
        except ValidationError as e:
            messages.error(request, f"Error de validación en extracto bancario: {e.message if hasattr(e, 'message') else str(e)}")
            return redirect('finance:reconciliation_dashboard')
        except Exception as e:
            messages.error(request, f"Ocurrió un error inesperado al procesar el archivo: {str(e)}")
            return redirect('finance:reconciliation_dashboard')


class BankStatementDetailView(ModuleAccessRequiredMixin, DetailView):
    """
    Vista de Auditoría y Conciliación Interactiva para un Extracto Bancario:
    Muestra líneas del extracto, coincidencias exactas, potenciales y transacciones en tránsito.
    """
    template_name = 'finance/statement_detail.html'
    required_module = 'finance'
    min_role_level = RoleLevel.OPERATIONS_COORD
    context_object_name = 'statement'

    def get_queryset(self):
        return BankStatement.objects.for_org(self.request.organization).select_related('bank_account', 'imported_by')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        statement = self.object
        org = self.request.organization

        matching_report = run_reconciliation_matching(
            organization=org,
            bank_account=statement.bank_account,
            statement=statement,
            user=self.request.user,
        )

        all_lines = list(statement.lines.select_related('matched_transaction', 'reconciliation_match').order_by('line_number'))

        context.update({
            'matching_report': matching_report,
            'all_lines': all_lines,
        })
        return context


class BankReconciliationConfirmMatchView(ModuleAccessRequiredMixin, View):
    """
    Confirma un emparejamiento entre una línea de extracto y una transacción ERP.
    """
    required_module = 'finance'
    min_role_level = RoleLevel.OPERATIONS_COORD

    def post(self, request, *args, **kwargs):
        org = request.organization
        line_id = request.POST.get('statement_line_id')
        tx_id = request.POST.get('transaction_id')
        notes = request.POST.get('notes', '').strip()
        match_type = request.POST.get('match_type', BankReconciliationMatch.MatchType.EXACT)

        if not line_id or not tx_id:
            messages.error(request, "Se requiere identificar la línea de extracto y la transacción ERP a conciliar.")
            return redirect('finance:reconciliation_dashboard')

        statement_line = get_object_or_404(BankStatementLine, id=line_id, organization=org)
        tx = get_object_or_404(FinancialTransaction, id=tx_id, organization=org)

        try:
            match = confirm_reconciliation_match(
                organization=org,
                statement_line=statement_line,
                financial_transaction=tx,
                user=request.user,
                notes=notes,
                match_type=match_type,
            )
            messages.success(
                request,
                f"Conciliación confirmada exitosamente: Línea #{statement_line.line_number} emparejada con {tx.number} ({tx.amount} {statement_line.currency})."
            )
        except ValidationError as e:
            messages.error(request, f"Error al conciliar: {e.message if hasattr(e, 'message') else str(e)}")
        except Exception as e:
            messages.error(request, f"Error inesperado al asentar conciliación: {str(e)}")

        return redirect('finance:statement_detail', pk=statement_line.statement_id)


class BankReconciliationUndoMatchView(ModuleAccessRequiredMixin, View):
    """
    Revierte una conciliación asentada de forma trazable y segura.
    """
    required_module = 'finance'
    min_role_level = RoleLevel.OPERATIONS_COORD

    def post(self, request, *args, **kwargs):
        org = request.organization
        match_id = request.POST.get('match_id')
        statement_id = request.POST.get('statement_id')

        if not match_id:
            messages.error(request, "Identificador de conciliación no especificado.")
            return redirect('finance:reconciliation_dashboard')

        try:
            undo_reconciliation_match(
                organization=org,
                match_id=match_id,
                user=request.user,
                reason=request.POST.get('reason', 'Reversión manual solicitada por operador'),
            )
            messages.success(request, "La conciliación fue revertida exitosamente. Los movimientos vuelven a quedar pendientes.")
        except ValidationError as e:
            messages.error(request, f"Error al revertir: {e.message if hasattr(e, 'message') else str(e)}")
        except Exception as e:
            messages.error(request, f"Error inesperado al revertir: {str(e)}")

        if statement_id:
            return redirect('finance:statement_detail', pk=statement_id)
        return redirect('finance:reconciliation_dashboard')


class BankReconciliationAutoMatchView(ModuleAccessRequiredMixin, View):
    """
    Ejecuta el auto-emparejamiento seguro de todas las coincidencias EXACTAS de un extracto.
    """
    required_module = 'finance'
    min_role_level = RoleLevel.OPERATIONS_COORD

    def post(self, request, pk, *args, **kwargs):
        org = request.organization
        statement = get_object_or_404(BankStatement, id=pk, organization=org)

        try:
            confirmed = auto_confirm_exact_matches(
                organization=org,
                bank_account=statement.bank_account,
                statement=statement,
                user=request.user,
            )
            messages.success(
                request,
                f"Conciliación automática completada: {len(confirmed)} coincidencia(s) exacta(s) confirmada(s) sin ambigüedad."
            )
        except Exception as e:
            messages.error(request, f"Error durante la conciliación automática: {str(e)}")

        return redirect('finance:statement_detail', pk=statement.id)
