from django.contrib.auth import login as auth_login, logout as auth_logout
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods
from apps.accounts_auth.forms import EmailAuthenticationForm


@require_http_methods(["GET", "POST"])
def login_view(request):
    """
    Vista de Inicio de Sesión Corporativo B2B.
    Autentica al usuario mediante email y contraseña.
    """
    if request.user.is_authenticated:
        if getattr(request, 'organization', None):
            return redirect('tenancy:dashboard')
        return redirect('tenancy:onboarding')

    redirect_to = request.POST.get('next', request.GET.get('next', ''))

    if request.method == "POST":
        form = EmailAuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            auth_login(request, user)

            # Validar URL de redirección segura
            if not url_has_allowed_host_and_scheme(
                url=redirect_to,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                redirect_to = 'tenancy:dashboard'

            return redirect(redirect_to)
    else:
        form = EmailAuthenticationForm(request)

    return render(request, 'accounts_auth/login.html', {
        'form': form,
        'next': redirect_to,
    })


from django.views import View
from apps.accounts_auth.forms import EmailAuthenticationForm, CorporateSignUpForm


class CorporateRegisterView(View):
    """
    Registro autónomo de nuevos evaluadores y directores de empresa.
    Autentica al usuario inmediatamente tras el registro y lo transfiere
    al Onboarding Wizard para inicializar su organización.
    """
    template_name = 'accounts_auth/register.html'

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            if getattr(request, 'organization', None):
                return redirect('tenancy:dashboard')
            return redirect('tenancy:onboarding')
        form = CorporateSignUpForm()
        return render(request, self.template_name, {'form': form})

    def post(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect('tenancy:onboarding')
        form = CorporateSignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            auth_login(request, user)
            return redirect('tenancy:onboarding')
        return render(request, self.template_name, {'form': form}, status=422)


@require_http_methods(["GET", "POST"])
def logout_view(request):
    """
    Cierra la sesión activa del usuario y limpia el identificador de tenant en sesión.
    """
    request.session.pop('active_org_id', None)
    auth_logout(request)
    return redirect('accounts_auth:login')


from django.conf import settings
from django.contrib import messages
from django.http import Http404
from apps.core.models import CountryConfig
from apps.core.services.sandbox_seeder import SandboxScenarioSeeder
from apps.crm.models import Account
from apps.tenancy.models import Organization, OrganizationMembership, RoleLevel
from django.contrib.auth import get_user_model

User = get_user_model()


class QuickSandboxLoginView(View):
    """
    Acceso directo en 1 clic para evaluadores de Producto, Marketing y UX.
    Autentica al usuario evaluador@eternallabs.ni, asegura la existencia de la
    organización demo IMESA INGENIERÍA S.A. y siembra el escenario completo si no existe.

    Protección de seguridad estricta (SEC-01):
    Inaccesible a menos que settings.DEBUG sea True Y settings.ENABLE_SANDBOX sea True.
    En cualquier otro entorno o configuración ausente/inválida, despacha Http404.
    """
    def dispatch(self, request, *args, **kwargs):
        is_debug = getattr(settings, 'DEBUG', False)
        is_sandbox_enabled = getattr(settings, 'ENABLE_SANDBOX', False)
        if not (is_debug and is_sandbox_enabled):
            raise Http404("El acceso directo a sandbox se encuentra deshabilitado en este entorno.")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        country_ni = CountryConfig.objects.filter(code='NI').first()
        if not country_ni:
            from django.core.management import call_command
            call_command('seed_countries')
            country_ni = CountryConfig.objects.filter(code='NI').first()

        evaluator_user, created_user = User.objects.get_or_create(
            email='evaluador@eternallabs.ni',
            defaults={
                'full_name': 'Ing. Evaluador de Producto & UX',
                'personal_id_number': '001-120590-0001Z',
                'phone': '+505 8888-0000',
            }
        )
        if created_user or not evaluator_user.check_password('Eternal2026*'):
            evaluator_user.set_password('Eternal2026*')
            evaluator_user.save()

        org, created_org = Organization.objects.get_or_create(
            slug='imesa-ingenieria',
            defaults={
                'name': 'IMESA INGENIERÍA S.A.',
                'legal_name': 'Instalaciones y Mantenimiento Electromecánico Sociedad Anónima',
                'tax_id': 'J0310000458912',
                'country': country_ni,
                'currency': Organization.Currency.NIO,
                'tax_regime': Organization.TaxRegime.GENERAL,
                'industry': Organization.Industry.ENERGY_TELCO,
                'accounting_template': Organization.AccountingTemplate.NIIF_PYMES,
                'enabled_modules': ['crm', 'operations', 'sst', 'inventory', 'finance'],
                'onboarding_completed': True,
                'is_active': True,
            }
        )

        OrganizationMembership.objects.update_or_create(
            user=evaluator_user,
            organization=org,
            defaults={
                'role_level': RoleLevel.OWNER_ADMIN,
                'job_title': 'Director General / Evaluador de Producto',
                'is_active': True,
                'is_default_org': True,
            }
        )

        # Si no tiene cuentas u órdenes de trabajo creadas, poblar el escenario
        if not Account.objects.filter(organization=org).exists():
            SandboxScenarioSeeder.populate_for_organization(org, evaluator_user)

        auth_login(request, evaluator_user)
        request.session['active_org_id'] = str(org.id)
        request.session.pop('simulated_role_level', None)

        messages.success(
            request,
            "Sesión iniciada en modo Sandbox Industrial (IMESA INGENIERÍA S.A.) con autoridad total Owner/Admin."
        )
        return redirect('tenancy:dashboard')
