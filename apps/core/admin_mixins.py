"""
Mixins de Aislamiento y Seguridad Multi-Tenant para Django ModelAdmin.
ERP VECTOR — Fase 1C.11 (SEC-04).
"""
from django.contrib import admin


class TenantAdminMixin:
    """
    Mixin para ModelAdmin que garantiza aislamiento multi-tenant estricto
    en el panel administrativo de Django (/platform-superadmin/).

    Reglas de autorización e integridad:
    1. Superusuarios de infraestructura (is_superuser=True):
       Conservan la capacidad global de administración sobre todos los tenants.
    2. Personal del tenant (is_staff=True, is_superuser=False):
       - Sus listados (changelist) filtran estrictamente por request.organization.
       - El acceso a vistas de detalle o cambio (/change/) verifica que el objeto
         pertenezca a request.organization; si pertenece a otro tenant, se deniega (404/403).
       - La creación asigna automáticamente request.organization.
       - Los campos foráneos (FK) seleccionables se filtran exclusivamente a los registros
         pertenecientes a request.organization.
    3. Usuarios staff sin organización resuelta (is_staff=True, request.organization=None):
       Retorna un queryset vacío (none()) y deniega toda consulta o modificación.
    """

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs

        org = getattr(request, 'organization', None)
        if org and hasattr(self.model, 'organization'):
            return qs.filter(organization=org)
        return qs.none()

    def has_view_permission(self, request, obj=None):
        if not super().has_view_permission(request, obj):
            return False
        if request.user.is_superuser:
            return True
        if obj is not None:
            org = getattr(request, 'organization', None)
            if not org or getattr(obj, 'organization_id', None) != org.id:
                return False
        return True

    def has_change_permission(self, request, obj=None):
        if not super().has_change_permission(request, obj):
            return False
        if request.user.is_superuser:
            return True
        if obj is not None:
            org = getattr(request, 'organization', None)
            if not org or getattr(obj, 'organization_id', None) != org.id:
                return False
        return True

    def has_delete_permission(self, request, obj=None):
        if not super().has_delete_permission(request, obj):
            return False
        if request.user.is_superuser:
            return True
        if obj is not None:
            org = getattr(request, 'organization', None)
            if not org or getattr(obj, 'organization_id', None) != org.id:
                return False
        return True

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            org = getattr(request, 'organization', None)
            if org and hasattr(obj, 'organization') and not obj.organization_id:
                obj.organization = org
        super().save_model(request, obj, form, change)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if not request.user.is_superuser:
            org = getattr(request, 'organization', None)
            if org:
                field_model = db_field.remote_field.model
                if hasattr(field_model, 'organization'):
                    kwargs['queryset'] = field_model.objects.filter(organization=org)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
