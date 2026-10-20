from decimal import Decimal
from django import forms

from apps.operations.models import Crew, WorkOrder, WorkOrderItem
from apps.inventory.models import Warehouse, InventoryItem

INPUT_BASE_CLS = (
    "w-full bg-white border border-slate-300 rounded px-3 py-1.5 text-xs text-slate-800 "
    "placeholder-slate-400 focus:outline-none focus:ring-1 focus:ring-slate-900 focus:border-slate-900 "
    "transition duration-150 disabled:bg-slate-50 disabled:text-slate-400"
)
INPUT_MONO_CLS = f"{INPUT_BASE_CLS} font-mono tabular-nums"
SELECT_BASE_CLS = (
    "w-full bg-white border border-slate-300 rounded px-2.5 py-1.5 text-xs text-slate-800 "
    "focus:outline-none focus:ring-1 focus:ring-slate-900 focus:border-slate-900 "
    "transition duration-150"
)
CHECKBOX_CLS = "rounded border-slate-300 text-slate-900 focus:ring-slate-900 h-4 w-4 text-xs"


class WarehouseForm(forms.ModelForm):
    """Formulario de alta y configuración de Bodega o Unidad Móvil."""
    class Meta:
        model = Warehouse
        fields = [
            'code',
            'name',
            'warehouse_type',
            'linked_crew',
            'address',
            'is_active',
        ]
        widgets = {
            'code': forms.TextInput(attrs={'class': INPUT_MONO_CLS, 'placeholder': 'ej: BOD-01 (Autogenerado si vacío)'}),
            'name': forms.TextInput(attrs={'class': INPUT_BASE_CLS, 'placeholder': 'ej: Bodega Central Managua'}),
            'warehouse_type': forms.Select(attrs={'class': SELECT_BASE_CLS}),
            'linked_crew': forms.Select(attrs={'class': SELECT_BASE_CLS}),
            'address': forms.TextInput(attrs={'class': INPUT_BASE_CLS, 'placeholder': 'ej: Km 7 Carretera Norte, Módulo B-4'}),
            'is_active': forms.CheckboxInput(attrs={'class': CHECKBOX_CLS}),
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['code'].required = False
        self.fields['is_active'].initial = True
        if organization:
            self.fields['linked_crew'].queryset = Crew.objects.for_org(organization).filter(is_active=True)
        self.fields['linked_crew'].empty_label = "— Sin Cuadrilla Asignada —"

    def clean_is_active(self):
        if self.data and 'is_active' in self.data:
            return bool(self.cleaned_data.get('is_active'))
        return True


class InventoryItemForm(forms.ModelForm):
    """Formulario de alta de material o insumo en Catálogo Maestro."""
    class Meta:
        model = InventoryItem
        fields = [
            'sku',
            'name',
            'category',
            'unit',
            'min_stock_level',
            'unit_cost',
            'is_active',
        ]
        widgets = {
            'sku': forms.TextInput(attrs={'class': INPUT_MONO_CLS, 'placeholder': 'ej: MAT-CAB-01'}),
            'name': forms.TextInput(attrs={'class': INPUT_BASE_CLS, 'placeholder': 'ej: Cable de Potencia XLP 15kV'}),
            'category': forms.Select(attrs={'class': SELECT_BASE_CLS}),
            'unit': forms.TextInput(attrs={'class': INPUT_BASE_CLS, 'placeholder': 'UND, MTS, KG, GLB'}),
            'min_stock_level': forms.NumberInput(attrs={'class': INPUT_MONO_CLS, 'step': '0.01'}),
            'unit_cost': forms.NumberInput(attrs={'class': INPUT_MONO_CLS, 'step': '0.01'}),
            'is_active': forms.CheckboxInput(attrs={'class': CHECKBOX_CLS}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['sku'].required = False
        self.fields['is_active'].initial = True

    def clean_is_active(self):
        if self.data and 'is_active' in self.data:
            return bool(self.cleaned_data.get('is_active'))
        return True


class StockEntryForm(forms.Form):
    """Formulario de Recepción / Entrada de Material a Bodega."""
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(),
        widget=forms.Select(attrs={'class': SELECT_BASE_CLS}),
        label="Bodega Receptora"
    )
    item = forms.ModelChoiceField(
        queryset=InventoryItem.objects.none(),
        widget=forms.Select(attrs={'class': SELECT_BASE_CLS}),
        label="Material / Ítem"
    )
    quantity = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={'class': INPUT_MONO_CLS, 'step': '0.01', 'min': '0.01', 'placeholder': '0.00'}),
        label="Cantidad a Ingresar"
    )
    unit_cost = forms.DecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        widget=forms.NumberInput(attrs={'class': INPUT_MONO_CLS, 'step': '0.01', 'placeholder': '0.00'}),
        label="Costo Unitario de Compra"
    )
    reference_note = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={'class': INPUT_BASE_CLS, 'placeholder': 'Factura Proveedor #1234, Recepción Contenedor...'}),
        label="Referencia / Factura"
    )

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization:
            self.fields['warehouse'].queryset = Warehouse.objects.for_org(organization).filter(is_active=True)
            self.fields['item'].queryset = InventoryItem.objects.for_org(organization).filter(is_active=True)
        self.fields['warehouse'].empty_label = "— Seleccione Bodega —"
        self.fields['item'].empty_label = "— Seleccione Ítem de Inventario —"


class StockTransferForm(forms.Form):
    """Formulario de Traslado entre Bodegas / Carga a Furgoneta de Cuadrilla."""
    source_warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(),
        widget=forms.Select(attrs={'class': SELECT_BASE_CLS}),
        label="Bodega Origen"
    )
    target_warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(),
        widget=forms.Select(attrs={'class': SELECT_BASE_CLS}),
        label="Bodega Destino / Unidad Móvil"
    )
    item = forms.ModelChoiceField(
        queryset=InventoryItem.objects.none(),
        widget=forms.Select(attrs={'class': SELECT_BASE_CLS}),
        label="Material / Ítem"
    )
    quantity = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={'class': INPUT_MONO_CLS, 'step': '0.01', 'min': '0.01'}),
        label="Cantidad a Trasladar"
    )
    reference_note = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={'class': INPUT_BASE_CLS, 'placeholder': 'Carga semanal para cuadrilla, orden de trabajo...'}),
        label="Motivo del Traslado"
    )

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization:
            warehouses_qs = Warehouse.objects.for_org(organization).filter(is_active=True)
            self.fields['source_warehouse'].queryset = warehouses_qs
            self.fields['target_warehouse'].queryset = warehouses_qs
            self.fields['item'].queryset = InventoryItem.objects.for_org(organization).filter(is_active=True)
        self.fields['source_warehouse'].empty_label = "— Seleccione Bodega Origen —"
        self.fields['target_warehouse'].empty_label = "— Seleccione Bodega Destino —"
        self.fields['item'].empty_label = "— Seleccione Ítem a Trasladar —"


class StockDispatchForm(forms.Form):
    """Formulario de Despacho directo a Orden de Trabajo."""
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(),
        widget=forms.Select(attrs={'class': SELECT_BASE_CLS}),
        label="Bodega / Furgoneta Móvil de Salida"
    )
    work_order = forms.ModelChoiceField(
        queryset=WorkOrder.objects.none(),
        widget=forms.Select(attrs={'class': SELECT_BASE_CLS}),
        label="Orden de Trabajo Destino"
    )
    item = forms.ModelChoiceField(
        queryset=InventoryItem.objects.none(),
        widget=forms.Select(attrs={'class': SELECT_BASE_CLS}),
        label="Material / Ítem a Despachar"
    )
    quantity = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={'class': INPUT_MONO_CLS, 'step': '0.01', 'min': '0.01'}),
        label="Cantidad Despachada"
    )
    work_order_item = forms.ModelChoiceField(
        queryset=WorkOrderItem.objects.none(),
        required=False,
        widget=forms.Select(attrs={'class': SELECT_BASE_CLS}),
        label="Partida Técnica Específica (Opcional)"
    )

    def __init__(self, *args, organization=None, initial_work_order=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization:
            self.fields['warehouse'].queryset = Warehouse.objects.for_org(organization).filter(is_active=True)
            self.fields['work_order'].queryset = WorkOrder.objects.for_org(organization).exclude(
                phase__in=[WorkOrder.Phase.COMPLETED, WorkOrder.Phase.CANCELLED]
            )
            self.fields['item'].queryset = InventoryItem.objects.for_org(organization).filter(is_active=True)
            self.fields['work_order_item'].queryset = WorkOrderItem.objects.for_org(organization)

        self.fields['warehouse'].empty_label = "— Seleccione Bodega de Salida —"
        self.fields['work_order'].empty_label = "— Seleccione Orden de Trabajo —"
        self.fields['item'].empty_label = "— Seleccione Material —"
        self.fields['work_order_item'].empty_label = "— Auto-vincular por SKU/Código —"

        if initial_work_order:
            self.fields['work_order'].initial = initial_work_order
