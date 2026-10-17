from decimal import Decimal
from django import forms
from django.contrib.auth import get_user_model

from apps.operations.models import Crew, WorkOrder
from apps.finance.models import (
    BankAccount,
    PettyCashFund,
    PettyCashExpense,
    PettyCashReplenishment,
)

User = get_user_model()

INPUT_BASE_CLS = (
    "w-full h-9 px-3 text-[13px] bg-white border border-zinc-200 rounded-[4px] text-zinc-900 "
    "placeholder-zinc-400 focus:outline-none focus:border-zinc-900 focus:ring-1 focus:ring-zinc-900 "
    "transition-colors font-sans disabled:bg-zinc-50 disabled:text-zinc-400"
)
INPUT_MONO_CLS = f"{INPUT_BASE_CLS} font-mono tabular-nums"
SELECT_BASE_CLS = (
    "w-full h-9 px-3 text-[13px] bg-white border border-zinc-200 rounded-[4px] text-zinc-900 "
    "focus:outline-none focus:border-zinc-900 focus:ring-1 focus:ring-zinc-900 "
    "transition-colors font-sans"
)
CHECKBOX_CLS = "rounded-[3px] border-zinc-300 text-zinc-900 focus:ring-zinc-900 h-4 w-4"


class BankAccountForm(forms.ModelForm):
    """Formulario de configuración de Cuenta Bancaria."""
    class Meta:
        model = BankAccount
        fields = [
            'bank_name',
            'account_number',
            'currency',
            'account_type',
            'description',
            'is_active',
        ]
        widgets = {
            'bank_name': forms.Select(attrs={'class': SELECT_BASE_CLS}),
            'account_number': forms.TextInput(attrs={'class': INPUT_MONO_CLS, 'placeholder': 'ej: 100-234567-89'}),
            'currency': forms.TextInput(attrs={'class': INPUT_MONO_CLS, 'placeholder': 'NIO o USD'}),
            'account_type': forms.Select(attrs={'class': SELECT_BASE_CLS}),
            'description': forms.TextInput(attrs={'class': INPUT_BASE_CLS, 'placeholder': 'ej: Cuenta Operativa Planilla BAC'}),
            'is_active': forms.CheckboxInput(attrs={'class': CHECKBOX_CLS}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['is_active'].initial = True

    def clean_is_active(self):
        if self.data and 'is_active' in self.data:
            return bool(self.cleaned_data.get('is_active'))
        return True


class PettyCashFundForm(forms.ModelForm):
    """Formulario de alta y asignación de Fondo Fijo / Caja Chica."""
    class Meta:
        model = PettyCashFund
        fields = [
            'code',
            'name',
            'custodian',
            'linked_crew',
            'allocated_amount',
            'is_active',
        ]
        widgets = {
            'code': forms.TextInput(attrs={'class': INPUT_MONO_CLS, 'placeholder': 'ej: CAJ-01 (Autogenerado si vacío)'}),
            'name': forms.TextInput(attrs={'class': INPUT_BASE_CLS, 'placeholder': 'ej: Fondo Operativo Cuadrilla Alfa'}),
            'custodian': forms.Select(attrs={'class': SELECT_BASE_CLS}),
            'linked_crew': forms.Select(attrs={'class': SELECT_BASE_CLS}),
            'allocated_amount': forms.NumberInput(attrs={'class': INPUT_MONO_CLS, 'step': '0.01'}),
            'is_active': forms.CheckboxInput(attrs={'class': CHECKBOX_CLS}),
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['code'].required = False
        self.fields['is_active'].initial = True
        if organization:
            self.fields['custodian'].queryset = User.objects.filter(
                memberships__organization=organization,
                memberships__is_active=True
            ).distinct()
            self.fields['linked_crew'].queryset = Crew.objects.for_org(organization).filter(is_active=True)
        self.fields['linked_crew'].empty_label = "— Sin Cuadrilla Vinculada —"

    def clean_is_active(self):
        if self.data and 'is_active' in self.data:
            return bool(self.cleaned_data.get('is_active'))
        return True


class PettyCashExpenseForm(forms.ModelForm):
    """Formulario de registro de gasto en frente de obra."""
    class Meta:
        model = PettyCashExpense
        fields = [
            'fund',
            'work_order',
            'expense_category',
            'vendor_name',
            'vendor_tax_id',
            'invoice_number',
            'amount',
            'description',
            'spent_at',
        ]
        widgets = {
            'fund': forms.Select(attrs={'class': SELECT_BASE_CLS}),
            'work_order': forms.Select(attrs={'class': SELECT_BASE_CLS}),
            'expense_category': forms.Select(attrs={'class': SELECT_BASE_CLS}),
            'vendor_name': forms.TextInput(attrs={'class': INPUT_BASE_CLS, 'placeholder': 'ej: Gasolinera Puma Tipitapa'}),
            'vendor_tax_id': forms.TextInput(attrs={'class': INPUT_MONO_CLS, 'placeholder': 'ej: J0310000000000'}),
            'invoice_number': forms.TextInput(attrs={'class': INPUT_MONO_CLS, 'placeholder': 'ej: FACT-88120'}),
            'amount': forms.NumberInput(attrs={'class': INPUT_MONO_CLS, 'step': '0.01', 'placeholder': '0.00'}),
            'description': forms.TextInput(attrs={'class': INPUT_BASE_CLS, 'placeholder': 'ej: 30 Glns Diesel para Cuadrilla Alfa'}),
            'spent_at': forms.DateInput(attrs={'class': INPUT_MONO_CLS, 'type': 'date'}),
        }

    def __init__(self, *args, organization=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization:
            funds_qs = PettyCashFund.objects.for_org(organization).filter(is_active=True)
            # Si es Ingeniero custodio (rol < 80), filtrar solo a sus cajas asignadas
            role_level = getattr(user, 'role_level', 0) if user else 0
            if user and role_level < 80:
                funds_qs = funds_qs.filter(custodian=user)
            self.fields['fund'].queryset = funds_qs
            self.fields['work_order'].queryset = WorkOrder.objects.for_org(organization).filter(
                phase__in=[WorkOrder.Phase.IN_PROGRESS, WorkOrder.Phase.SCHEDULED]
            )
        self.fields['work_order'].empty_label = "— Sin OT Imputable (Gasto General) —"


class PettyCashReplenishmentSubmitForm(forms.Form):
    """Formulario para enviar arqueo y solicitud de reposición de fondo."""
    fund = forms.ModelChoiceField(
        queryset=PettyCashFund.objects.none(),
        widget=forms.Select(attrs={'class': SELECT_BASE_CLS}),
        label="Caja Chica a Rendir"
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'class': f"{INPUT_BASE_CLS} h-20",
            'placeholder': 'Observaciones del arqueo: cantidad de comprobantes, frentes de obra...'
        }),
        label="Notas de Rendición"
    )

    def __init__(self, *args, organization=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization:
            funds_qs = PettyCashFund.objects.for_org(organization).filter(is_active=True)
            role_level = getattr(user, 'role_level', 0) if user else 0
            if user and role_level < 80:
                funds_qs = funds_qs.filter(custodian=user)
            self.fields['fund'].queryset = funds_qs


class PettyCashDisburseForm(forms.Form):
    """Formulario para autorizar y desembolsar reposición desde banco."""
    bank_account = forms.ModelChoiceField(
        queryset=BankAccount.objects.none(),
        widget=forms.Select(attrs={'class': SELECT_BASE_CLS}),
        label="Cuenta Bancaria Origen"
    )
    replenishment_id = forms.CharField(widget=forms.HiddenInput())

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization:
            self.fields['bank_account'].queryset = BankAccount.objects.for_org(organization).filter(is_active=True)


class BankStatementUploadForm(forms.Form):
    """Formulario para carga y validación de extractos bancarios en CSV."""
    bank_account = forms.ModelChoiceField(
        queryset=BankAccount.objects.none(),
        widget=forms.Select(attrs={'class': SELECT_BASE_CLS}),
        label="Cuenta Bancaria Institucional"
    )
    file = forms.FileField(
        widget=forms.FileInput(attrs={
            'class': 'block w-full text-xs text-zinc-500 file:mr-4 file:py-2 file:px-4 file:rounded file:border-0 file:text-xs file:font-medium file:bg-zinc-900 file:text-white hover:file:bg-zinc-800 cursor-pointer',
            'accept': '.csv,text/csv,text/plain'
        }),
        label="Archivo de Extracto Bancario (.csv)",
        help_text="Formatos admitidos: CSV estándar (BAC, Banpro, Lafise, BDF, Ficohsa). Tamaño máx: 5MB."
    )
    initial_balance = forms.DecimalField(
        required=False,
        initial=Decimal('0.00'),
        widget=forms.TextInput(attrs={'class': INPUT_MONO_CLS, 'placeholder': '0.00'}),
        label="Saldo Inicial según Extracto (Opcional)"
    )
    final_balance = forms.DecimalField(
        required=False,
        initial=Decimal('0.00'),
        widget=forms.TextInput(attrs={'class': INPUT_MONO_CLS, 'placeholder': '0.00'}),
        label="Saldo Final según Extracto (Opcional)"
    )
    notes = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': INPUT_BASE_CLS, 'placeholder': 'ej: Extracto Mensual Cuentas Corrientes'}),
        label="Notas / Memo del Lote"
    )

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization:
            self.fields['bank_account'].queryset = BankAccount.objects.for_org(organization).filter(is_active=True)
