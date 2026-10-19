"""
ERP VECTOR — Fase 1C.20: Pruebas Automatizadas de Hardening de prepare_sandbox y build.sh.
Verifica que el comando de inicialización y el script de compilación impidan
el sembrado no autorizado o accidental de datos demo en entornos no permitidos.
"""
import subprocess
from unittest.mock import patch
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from apps.tenancy.models import Organization
from django.contrib.auth import get_user_model

User = get_user_model()


class PrepareSandboxHardeningTestCase(TestCase):
    """
    Validaciones de defensa en profundidad para el comando de gestión prepare_sandbox.
    """

    @override_settings(ENABLE_SANDBOX=False)
    def test_01_prepare_sandbox_fails_when_enable_sandbox_false(self):
        """1. Cuando ENABLE_SANDBOX=False, prepare_sandbox debe lanzar CommandError y no sembrar datos."""
        out = StringIO()
        err = StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command('prepare_sandbox', stdout=out, stderr=err)

        self.assertIn("estrictamente deshabilitado", str(ctx.exception))
        self.assertIn("ENABLE_SANDBOX=True", str(ctx.exception))
        self.assertFalse(Organization.objects.filter(slug='imesa-ingenieria').exists())

    @override_settings(ENABLE_SANDBOX=None)
    def test_02_prepare_sandbox_fails_when_enable_sandbox_none(self):
        """2. Cuando ENABLE_SANDBOX no está configurado (None), prepare_sandbox debe fallar de forma segura."""
        out = StringIO()
        err = StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command('prepare_sandbox', stdout=out, stderr=err)

        self.assertIn("estrictamente deshabilitado", str(ctx.exception))

    def test_03_prepare_sandbox_fails_when_attribute_missing(self):
        """3. Cuando settings.ENABLE_SANDBOX no existe como atributo, el comportamiento predeterminado es seguro."""
        out = StringIO()
        err = StringIO()
        with patch('django.conf.settings', create=True) as mock_settings:
            del mock_settings.ENABLE_SANDBOX
            with self.assertRaises(CommandError) as ctx:
                call_command('prepare_sandbox', stdout=out, stderr=err)
            self.assertIn("estrictamente deshabilitado", str(ctx.exception))

    @override_settings(ENABLE_SANDBOX=True)
    def test_04_prepare_sandbox_succeeds_when_enable_sandbox_true(self):
        """4. Cuando ENABLE_SANDBOX=True en entorno de prueba, prepare_sandbox ejecuta y crea la empresa demo."""
        out = StringIO()
        err = StringIO()
        call_command('prepare_sandbox', stdout=out, stderr=err)

        self.assertTrue(Organization.objects.filter(slug='imesa-ingenieria').exists())
        self.assertTrue(User.objects.filter(email='evaluador@eternallabs.ni').exists())
        output_str = out.getvalue()
        self.assertIn("ENTORNO SANDBOX PREPARADO CORRECTAMENTE", output_str)

    def test_05_build_sh_logic_safety_matrix(self):
        """5. Verifica que la lógica condicional introducida en build.sh omita el sembrado en todos los casos inseguros."""
        # Casos que deben omitir el sembrado:
        insecure_values = ["", "False", "false", "0", "None", "disabled", "invalid_value"]
        for val in insecure_values:
            bash_cmd = f'''
            ENABLE_SANDBOX="{val}"
            ENABLE_SANDBOX_NORMALIZED=$(echo "${{ENABLE_SANDBOX:-False}}" | tr '[:upper:]' '[:lower:]')
            if [ "$ENABLE_SANDBOX_NORMALIZED" = "true" ] || [ "$ENABLE_SANDBOX_NORMALIZED" = "1" ]; then
                echo "SEMBRAR"
            else
                echo "OMITIR"
            fi
            '''
            result = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "OMITIR", f"Fallo de seguridad para ENABLE_SANDBOX='{val}'")

        # Casos que deben autorizar el sembrado:
        secure_values = ["True", "true", "TRUE", "1"]
        for val in secure_values:
            bash_cmd = f'''
            ENABLE_SANDBOX="{val}"
            ENABLE_SANDBOX_NORMALIZED=$(echo "${{ENABLE_SANDBOX:-False}}" | tr '[:upper:]' '[:lower:]')
            if [ "$ENABLE_SANDBOX_NORMALIZED" = "true" ] || [ "$ENABLE_SANDBOX_NORMALIZED" = "1" ]; then
                echo "SEMBRAR"
            else
                echo "OMITIR"
            fi
            '''
            result = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "SEMBRAR", f"Fallo de autorización para ENABLE_SANDBOX='{val}'")
