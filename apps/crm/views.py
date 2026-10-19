from decimal import Decimal
from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum, Count
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View
from django.views.generic import ListView, DetailView, CreateView, TemplateView

from apps.tenancy.models import RoleLevel, OrganizationMembership
from apps.tenancy.permissions import ModuleAccessRequiredMixin, render_403_rbac
from apps.crm.models import Account, AccountContact, Opportunity, Quote, QuoteItem
from apps.crm.forms import (
    AccountForm,
    AccountContactForm,
    OpportunityForm,
    QuoteCreateForm,
    QuoteItemForm,
)

CRM_WRITE_ROLES = (RoleLevel.SALES_ADVISOR, RoleLevel.COMMERCIAL_MGR, RoleLevel.OWNER_ADMIN)


class CrmWriteRequiredMixin(ModuleAccessRequiredMixin):
    """
    Guardián de operaciones de creación y mutación en CRM.
    Autoriza exclusivamente a:
    - SALES_ADVISOR (15): Creación de cuentas, oportunidades, cotizaciones borrador y partidas propias.
    - COMMERCIAL_MGR (85): Creación y administración comercial general.
    - OWNER_ADMIN (100): Control total.
    Bloquea con HTTP 403 Forbidden a OPERATIONS_COORD (80), roles técnicos (<=70) y observadores.
    """
    required_module = 'crm'
    min_role_level = RoleLevel.SALES_ADVISOR

    def dispatch(self, request, *args, **kwargs):
        # 1. Validar tenancy y autenticación antes de evaluar membresía o role_level
        tenant_error = self.check_tenant_requirements(request)
        if tenant_error:
            return tenant_error

        # 2. Validar que la organización tenga CRM habilitado y la membresía tenga acceso
        membership = getattr(request, 'membership', None)
        role_level = getattr(request, 'role_level', 0)
        if not membership or not membership.has_module_access('crm', effective_role_level=role_level):
            return render_403_rbac(
                request,
                required_module='crm',
                min_role_level=RoleLevel.SALES_ADVISOR,
                reason="El módulo de Gestión Comercial no está habilitado o autorizado para su membresía."
            )

        # 3. Validar rol de escritura ANTES de delegar la ejecución al handler (evita mutaciones fantasma)
        if role_level not in CRM_WRITE_ROLES:
            return render_403_rbac(
                request,
                required_module='crm',
                min_role_level=RoleLevel.COMMERCIAL_MGR,
                reason="Las operaciones de registro o modificación en CRM requieren rol de Asesor Comercial o Gerencia."
            )

        return super().dispatch(request, *args, **kwargs)


# ==============================================================================
# 1. DIRECTORIO DE CLIENTES (ACCOUNTS)
# ==============================================================================

class AccountListView(ModuleAccessRequiredMixin, ListView):
    """
    Directorio Unificado de Cuentas / Clientes B2B con aislamiento multi-tenant.
    Lectura permitida para Asesor Comercial (15) y roles de coordinación/gerencia (>=80).
    """
    model = Account
    template_name = 'crm/account_list.html'
    context_object_name = 'accounts'
    paginate_by = 25
    required_module = 'crm'
    min_role_level = RoleLevel.SALES_ADVISOR

    def get_queryset(self):
        org = self.request.organization
        qs = Account.objects.for_org(org).select_related('assigned_to')

        query = self.request.GET.get('q', '').strip()
        if query:
            qs = qs.filter(
                Q(code__icontains=query) |
                Q(commercial_name__icontains=query) |
                Q(legal_name__icontains=query) |
                Q(tax_id__icontains=query) |
                Q(email__icontains=query)
            )

        stage = self.request.GET.get('stage')
        if stage and stage in Account.LifecycleStage.values:
            qs = qs.filter(lifecycle_stage=stage)

        account_type = self.request.GET.get('type')
        if account_type and account_type in Account.AccountType.values:
            qs = qs.filter(account_type=account_type)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.request.organization
        all_accounts = Account.objects.for_org(org)
        context.update({
            'total_count': all_accounts.count(),
            'prospect_count': all_accounts.filter(lifecycle_stage=Account.LifecycleStage.PROSPECT).count(),
            'active_count': all_accounts.filter(lifecycle_stage=Account.LifecycleStage.ACTIVE).count(),
            'inactive_count': all_accounts.filter(lifecycle_stage=Account.LifecycleStage.INACTIVE).count(),
            'search_query': self.request.GET.get('q', ''),
            'current_stage': self.request.GET.get('stage', ''),
            'current_type': self.request.GET.get('type', ''),
            'stage_choices': Account.LifecycleStage.choices,
            'type_choices': Account.AccountType.choices,
        })
        return context


class AccountCreateView(CrmWriteRequiredMixin, CreateView):
    """
    Alta de nuevo cliente comercial.
    Permitido para Asesor Comercial (15), Gerente Comercial (85) y Owner (100).
    """
    model = Account
    form_class = AccountForm
    template_name = 'crm/account_form.html'

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['organization'] = self.request.organization
        return kwargs

    def form_valid(self, form):
        account = form.save(commit=False)
        account.organization = self.request.organization
        # Validar que assigned_to pertenezca a la organización activa si fue asignado
        if account.assigned_to and not OrganizationMembership.objects.filter(
            user=account.assigned_to,
            organization=self.request.organization,
            is_active=True
        ).exists():
            messages.error(self.request, "El usuario asignado no pertenece a esta organización o está inactivo.")
            return self.form_invalid(form)
        account.save()
        messages.success(self.request, f"Cliente {account.code} ({account.commercial_name}) creado exitosamente.")
        return redirect('crm:account_detail', pk=account.pk)


class AccountDetailView(ModuleAccessRequiredMixin, DetailView):
    """
    Ficha Técnica Integral del Cliente: Contactos, Oportunidades y Cotizaciones.
    Lectura permitida para Asesor Comercial (15) y roles de coordinación/gerencia (>=80).
    """
    model = Account
    template_name = 'crm/account_detail.html'
    context_object_name = 'account'
    required_module = 'crm'
    min_role_level = RoleLevel.SALES_ADVISOR

    def get_queryset(self):
        return Account.objects.for_org(self.request.organization).select_related('assigned_to')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        account = self.object
        context['contacts'] = account.contacts.all()
        context['opportunities'] = account.opportunities.all()
        context['quotes'] = account.quotes.all()
        context['contact_form'] = AccountContactForm()
        return context


class AccountContactCreateView(CrmWriteRequiredMixin, View):
    """
    Creación inline de contacto técnico/comercial para una cuenta.
    Permitido para Asesor Comercial (15), Gerente Comercial (85) y Owner (100).
    """
    def post(self, request, pk):
        account = get_object_or_404(Account.objects.for_org(request.organization), pk=pk)
        form = AccountContactForm(request.POST)
        if form.is_valid():
            contact = form.save(commit=False)
            contact.account = account
            contact.organization = request.organization
            contact.save()
            messages.success(request, f"Contacto '{contact.full_name}' agregado a la cuenta.")
        else:
            messages.error(request, "Error al guardar el contacto. Verifique los campos obligatorios.")
        return redirect('crm:account_detail', pk=account.pk)


# ==============================================================================
# 2. PIPELINE KANBAN (OPPORTUNITY)
# ==============================================================================

class OpportunityKanbanView(ModuleAccessRequiredMixin, TemplateView):
    """
    Tablero Kanban de Pipeline de Ventas e Ingeniería B2B en 6 etapas.
    Lectura permitida para Asesor Comercial (15) y roles de coordinación/gerencia (>=80).
    """
    template_name = 'crm/opportunity_kanban.html'
    required_module = 'crm'
    min_role_level = RoleLevel.SALES_ADVISOR

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.request.organization
        country = getattr(org, 'country', None)
        currency_symbol = country.primary_currency_symbol if country else 'C$'

        opportunities = Opportunity.objects.for_org(org).select_related('account', 'assigned_to')

        # Agrupar por etapas con totales monetarios
        columns = []
        for stage_code, stage_label in Opportunity.Stage.choices:
            stage_opps = [o for o in opportunities if o.stage == stage_code]
            total_val = sum(o.estimated_value for o in stage_opps)
            columns.append({
                'code': stage_code,
                'label': stage_label,
                'opportunities': stage_opps,
                'count': len(stage_opps),
                'total_value': total_val,
            })

        context['columns'] = columns
        context['currency_symbol'] = currency_symbol
        context['total_opportunities'] = len(opportunities)
        context['total_pipeline_value'] = sum(o.estimated_value for o in opportunities)
        context['new_opp_form'] = OpportunityForm(organization=org)
        return context


class OpportunityCreateView(CrmWriteRequiredMixin, CreateView):
    """
    Alta de nueva oportunidad comercial.
    Permitido para Asesor Comercial (15), Gerente Comercial (85) y Owner (100).
    """
    model = Opportunity
    form_class = OpportunityForm
    template_name = 'crm/opportunity_form.html'

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['organization'] = self.request.organization
        return kwargs

    def form_valid(self, form):
        opp = form.save(commit=False)
        opp.organization = self.request.organization
        # Validar cuenta de la organización
        if opp.account.organization_id != self.request.organization.id:
            messages.error(self.request, "La cuenta especificada no pertenece a su organización.")
            return self.form_invalid(form)
        # Validar asignado
        if opp.assigned_to and not OrganizationMembership.objects.filter(
            user=opp.assigned_to,
            organization=self.request.organization,
            is_active=True
        ).exists():
            messages.error(self.request, "El responsable asignado no pertenece a esta organización o está inactivo.")
            return self.form_invalid(form)
        opp.save()
        messages.success(self.request, f"Oportunidad {opp.code} registrada con éxito.")
        return redirect('crm:opportunity_kanban')


class OpportunityStageUpdateView(CrmWriteRequiredMixin, View):
    """
    Actualización rápida de etapa Kanban vía POST / HTMX.
    Permitido para Asesor Comercial (15), Gerente Comercial (85) y Owner (100).
    """
    def post(self, request, pk):
        opp = get_object_or_404(Opportunity.objects.for_org(request.organization), pk=pk)
        new_stage = request.POST.get('stage')

        if new_stage in Opportunity.Stage.values:
            opp.stage = new_stage
            opp.save(update_fields=['stage', 'updated_at'])
            messages.success(request, f"Oportunidad {opp.code} movida a '{opp.get_stage_display()}'.")
        else:
            messages.error(request, "Etapa inválida.")

        return redirect('crm:opportunity_kanban')


# ==============================================================================
# 3. MOTOR DE COTIZACIONES DINÁMICAS (QUOTES)
# ==============================================================================

class QuoteListView(ModuleAccessRequiredMixin, ListView):
    """
    Listado y consola de Cotizaciones Formales.
    Lectura permitida para Asesor Comercial (15) y roles de coordinación/gerencia (>=80).
    """
    model = Quote
    template_name = 'crm/quote_list.html'
    context_object_name = 'quotes'
    paginate_by = 25
    required_module = 'crm'
    min_role_level = RoleLevel.SALES_ADVISOR

    def get_queryset(self):
        org = self.request.organization
        qs = Quote.objects.for_org(org).select_related('account', 'created_by')

        status = self.request.GET.get('status')
        if status and status in Quote.Status.values:
            qs = qs.filter(status=status)

        query = self.request.GET.get('q', '').strip()
        if query:
            qs = qs.filter(
                Q(number__icontains=query) |
                Q(title__icontains=query) |
                Q(account__commercial_name__icontains=query) |
                Q(account__tax_id__icontains=query)
            )

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.request.organization
        all_quotes = Quote.objects.for_org(org)

        context.update({
            'total_count': all_quotes.count(),
            'draft_count': all_quotes.filter(status=Quote.Status.DRAFT).count(),
            'sent_count': all_quotes.filter(status=Quote.Status.SENT).count(),
            'approved_count': all_quotes.filter(status=Quote.Status.APPROVED).count(),
            'current_status': self.request.GET.get('status', ''),
            'search_query': self.request.GET.get('q', ''),
            'status_choices': Quote.Status.choices,
        })
        return context


class QuoteCreateView(CrmWriteRequiredMixin, CreateView):
    """
    Creación de nueva Cotización Formal.
    Permitido para Asesor Comercial (15), Gerente Comercial (85) y Owner (100).
    """
    model = Quote
    form_class = QuoteCreateForm
    template_name = 'crm/quote_form.html'

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['organization'] = self.request.organization
        return kwargs

    def form_valid(self, form):
        org = self.request.organization
        country = getattr(org, 'country', None)

        quote = form.save(commit=False)
        quote.organization = org
        quote.created_by = self.request.user
        quote.status = Quote.Status.DRAFT

        # Validar pertenencia de cuenta y oportunidad al tenant
        if quote.account.organization_id != org.id:
            messages.error(self.request, "El cliente especificado no pertenece a su organización.")
            return self.form_invalid(form)

        if quote.opportunity and quote.opportunity.organization_id != org.id:
            messages.error(self.request, "La oportunidad especificada no pertenece a su organización.")
            return self.form_invalid(form)

        # Asignar símbolo monetario adecuado
        if country:
            if quote.currency_code == country.secondary_currency_code:
                quote.currency_symbol = country.secondary_currency_symbol
            else:
                quote.currency_symbol = country.primary_currency_symbol

        quote.save()
        messages.success(self.request, f"Cotización {quote.number} creada. Agregue las partidas técnicas requeridas.")
        return redirect('crm:quote_detail', pk=quote.pk)


class QuoteDetailView(ModuleAccessRequiredMixin, DetailView):
    """
    Consola técnica de armado de cotización.
    Desglose por partidas, cálculo de impuestos DGI y control de estados.
    Lectura permitida para Asesor Comercial (15) y roles de coordinación/gerencia (>=80).
    """
    model = Quote
    template_name = 'crm/quote_detail.html'
    context_object_name = 'quote'
    required_module = 'crm'
    min_role_level = RoleLevel.SALES_ADVISOR

    def get_queryset(self):
        return Quote.objects.for_org(self.request.organization).select_related(
            'account', 'opportunity', 'created_by', 'approved_by', 'organization__country'
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        quote = self.object
        items = list(quote.items.all().order_by('created_at'))
        role_level = getattr(self.request, 'role_level', 0)

        services = [i for i in items if i.item_type == QuoteItem.ItemType.SERVICE]
        materials = [i for i in items if i.item_type == QuoteItem.ItemType.MATERIAL]
        equipments = [i for i in items if i.item_type == QuoteItem.ItemType.EQUIPMENT]

        # Condición de aprobación SoD para frontend
        can_approve = (
            role_level >= RoleLevel.COMMERCIAL_MGR and
            (quote.created_by_id != self.request.user.id or role_level >= RoleLevel.OWNER_ADMIN)
        )

        context.update({
            'items': items,
            'services': services,
            'materials': materials,
            'equipments': equipments,
            'item_form': QuoteItemForm(),
            'can_edit': quote.status in [Quote.Status.DRAFT, Quote.Status.SENT],
            'can_approve': can_approve,
        })
        return context


class QuoteAddItemView(CrmWriteRequiredMixin, View):
    """
    Agregado de partida a la cotización con recálculo dinámico.
    Permitido para Asesor Comercial (15), Gerente Comercial (85) y Owner (100).
    Reglas de seguridad:
    - Las cotizaciones en estado APPROVED son inmutables.
    - Un Asesor Comercial solo puede agregar partidas a cotizaciones elaboradas por él mismo.
    """
    def post(self, request, pk):
        quote = get_object_or_404(Quote.objects.for_org(request.organization), pk=pk)

        # Inmutabilidad de cotización aprobada o cerrada
        if quote.status not in [Quote.Status.DRAFT, Quote.Status.SENT]:
            messages.error(request, "No se pueden modificar partidas en una cotización aprobada o cerrada.")
            return redirect('crm:quote_detail', pk=quote.pk)

        # Regla de autor para SALES_ADVISOR
        if request.role_level == RoleLevel.SALES_ADVISOR and quote.created_by_id != request.user.id:
            return render_403_rbac(
                request,
                required_module='crm',
                reason="Un Asesor Comercial solo puede agregar partidas a cotizaciones elaboradas por él mismo."
            )

        form = QuoteItemForm(request.POST)
        if form.is_valid():
            item = form.save(commit=False)
            item.quote = quote
            item.organization = request.organization
            item.save()  # save() recalcula automáticamente quote.recalculate_totals()
            messages.success(request, f"Partida '{item.description}' agregada con éxito.")
        else:
            messages.error(request, "Error al ingresar la partida. Verifique cantidades y precios.")

        return redirect('crm:quote_detail', pk=quote.pk)


class QuoteRemoveItemView(CrmWriteRequiredMixin, View):
    """
    Eliminación de partida de la cotización con recálculo dinámico.
    Permitido para Asesor Comercial (15), Gerente Comercial (85) y Owner (100).
    Reglas de seguridad:
    - Las cotizaciones en estado APPROVED son inmutables.
    - Un Asesor Comercial solo puede eliminar partidas de cotizaciones elaboradas por él mismo.
    """
    def post(self, request, quote_pk, item_pk):
        quote = get_object_or_404(Quote.objects.for_org(request.organization), pk=quote_pk)

        # Inmutabilidad de cotización aprobada o cerrada
        if quote.status not in [Quote.Status.DRAFT, Quote.Status.SENT]:
            messages.error(request, "No se pueden modificar partidas en una cotización aprobada o cerrada.")
            return redirect('crm:quote_detail', pk=quote.pk)

        # Regla de autor para SALES_ADVISOR
        if request.role_level == RoleLevel.SALES_ADVISOR and quote.created_by_id != request.user.id:
            return render_403_rbac(
                request,
                required_module='crm',
                reason="Un Asesor Comercial solo puede eliminar partidas de cotizaciones elaboradas por él mismo."
            )

        item = get_object_or_404(quote.items.all(), pk=item_pk)
        item.delete()  # delete() recalcula automáticamente quote.recalculate_totals()
        messages.success(request, "Partida eliminada y totales recalculados.")

        return redirect('crm:quote_detail', pk=quote.pk)


class QuoteStatusTransitionView(CrmWriteRequiredMixin, View):
    """
    Transición de estado de la cotización (Aprobar, Enviar, Rechazar, Vencer).
    Aplica Segregación de Funciones (SoD) y Máquina de Estados Estricta:
    - SALES_ADVISOR: Solo puede transicionar a SENT, REJECTED, EXPIRED.
    - APPROVED: Exige RoleLevel >= COMMERCIAL_MGR (85).
    - SoD: COMMERCIAL_MGR (85) NO puede autoaprobar cotizaciones creadas por él mismo.
           Excepción concedida exclusivamente a OWNER_ADMIN (100).
    - Inmutabilidad: Una cotización APPROVED no puede cambiar de estado vía POST.
    - Condiciones de negocio: La aprobación exige partidas existentes y estado DRAFT o SENT.
    """
    @transaction.atomic
    def post(self, request, pk):
        quote = get_object_or_404(Quote.objects.for_org(request.organization), pk=pk)
        new_status = request.POST.get('status')

        if new_status not in Quote.Status.values:
            messages.error(request, "Estado de transición inválido.")
            return redirect('crm:quote_detail', pk=quote.pk)

        # Inmutabilidad de cotización ya aprobada
        if quote.status == Quote.Status.APPROVED:
            messages.error(request, "Una cotización formalmente aprobada es inmutable y no puede cambiar de estado.")
            return redirect('crm:quote_detail', pk=quote.pk)

        # Regla de autoría para SALES_ADVISOR (REV-03):
        # Un Asesor Comercial solo puede gestionar el ciclo de vida de cotizaciones elaboradas por él mismo.
        if request.role_level == RoleLevel.SALES_ADVISOR and quote.created_by_id != request.user.id:
            return render_403_rbac(
                request,
                required_module='crm',
                reason="Un Asesor Comercial solo puede modificar el estado de cotizaciones elaboradas por él mismo."
            )

        if new_status == Quote.Status.APPROVED:
            # 1. Validación de rol mínimo (COMMERCIAL_MGR 85)
            if request.role_level < RoleLevel.COMMERCIAL_MGR:
                return render_403_rbac(
                    request,
                    required_module='crm',
                    min_role_level=RoleLevel.COMMERCIAL_MGR,
                    reason="La aprobación formal de cotizaciones requiere nivel Gerente Comercial o Administrador General."
                )

            # 2. Segregación de Funciones (SoD): No autoaprobación salvo OWNER_ADMIN
            if quote.created_by_id == request.user.id and request.role_level < RoleLevel.OWNER_ADMIN:
                return render_403_rbac(
                    request,
                    required_module='crm',
                    min_role_level=RoleLevel.OWNER_ADMIN,
                    reason="Segregación de Funciones (SoD): Un Gerente Comercial no puede autoaprobar cotizaciones elaboradas por él mismo. Requiere la autorización de otro Gerente Comercial o del Administrador General."
                )

            # 3. Validación de estado origen (solo DRAFT o SENT)
            if quote.status not in [Quote.Status.DRAFT, Quote.Status.SENT]:
                messages.error(request, f"No se puede aprobar una cotización en estado '{quote.get_status_display()}'.")
                return redirect('crm:quote_detail', pk=quote.pk)

            # 4. Validación de existencia de partidas técnicas/comerciales
            if not quote.items.exists():
                messages.error(request, "No se puede aprobar una cotización que no posee partidas registradas.")
                return redirect('crm:quote_detail', pk=quote.pk)

            quote.status = Quote.Status.APPROVED
            quote.approved_by = request.user
            quote.approved_at = timezone.now()
            quote.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])

            # Activar cliente si estaba en prospecto
            account = quote.account
            if account.lifecycle_stage != Account.LifecycleStage.ACTIVE:
                account.lifecycle_stage = Account.LifecycleStage.ACTIVE
                account.save(update_fields=['lifecycle_stage', 'updated_at'])

            # Ganar oportunidad si está asociada
            if quote.opportunity:
                opp = quote.opportunity
                if opp.stage != Opportunity.Stage.WON:
                    opp.stage = Opportunity.Stage.WON
                    opp.save(update_fields=['stage', 'updated_at'])

            messages.success(
                request,
                f"Cotización {quote.number} APROBADA exitosamente. "
                f"Cliente {account.commercial_name} ahora es ACTIVO. "
                "Lista para apertura de Orden de Trabajo (OT)."
            )

        elif new_status == Quote.Status.SENT:
            if quote.status != Quote.Status.DRAFT:
                messages.error(request, "Solo cotizaciones en borrador pueden ser marcadas como enviadas.")
                return redirect('crm:quote_detail', pk=quote.pk)

            quote.status = Quote.Status.SENT
            quote.save(update_fields=['status', 'updated_at'])
            messages.info(request, f"Cotización {quote.number} marcada como ENVIADA al cliente.")

        elif new_status == Quote.Status.REJECTED:
            quote.status = Quote.Status.REJECTED
            quote.save(update_fields=['status', 'updated_at'])
            if quote.opportunity:
                quote.opportunity.stage = Opportunity.Stage.LOST
                quote.opportunity.save(update_fields=['stage', 'updated_at'])
            messages.warning(request, f"Cotización {quote.number} marcada como RECHAZADA.")

        elif new_status == Quote.Status.EXPIRED:
            quote.status = Quote.Status.EXPIRED
            quote.save(update_fields=['status', 'updated_at'])
            messages.warning(request, f"Cotización {quote.number} marcada como VENCIDA.")

        return redirect('crm:quote_detail', pk=quote.pk)


class QuotePrintView(ModuleAccessRequiredMixin, DetailView):
    """
    Vista de Impresión / Hoja Técnica Oficial DGI Nicaragua.
    Formato limpio sin sidebar ni navegación, listo para window.print() a PDF.
    Lectura permitida para Asesor Comercial (15) y roles de coordinación/gerencia (>=80).
    """
    model = Quote
    template_name = 'crm/quote_print.html'
    context_object_name = 'quote'
    required_module = 'crm'
    min_role_level = RoleLevel.SALES_ADVISOR

    def get_queryset(self):
        return Quote.objects.for_org(self.request.organization).select_related(
            'account', 'opportunity', 'created_by', 'approved_by', 'organization__country'
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        quote = self.object
        items = list(quote.items.all().order_by('created_at'))
        context.update({
            'items': items,
            'services': [i for i in items if i.item_type == QuoteItem.ItemType.SERVICE],
            'materials': [i for i in items if i.item_type == QuoteItem.ItemType.MATERIAL],
            'equipments': [i for i in items if i.item_type == QuoteItem.ItemType.EQUIPMENT],
        })
        return context
