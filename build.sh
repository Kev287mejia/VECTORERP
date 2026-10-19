#!/usr/bin/env bash
# ==============================================================================
# ETERNAL LABS - ENTERPRISE SOFTWARE DIVISION
# SCRIPT DE CONSTRUCCIÓN Y DESPLIEGUE CONTINUO (CI/CD)
# Proyecto: ERP SAS Multi-Tenant B2B (Centroamérica / Nicaragua Core)
# ==============================================================================

set -o errexit

echo "==> [ETERNAL LABS] Instalando dependencias de producción..."
pip install --upgrade pip
pip install -r requirements.txt

echo "==> [ETERNAL LABS] Recopilando archivos estáticos con WhiteNoise..."
python manage.py collectstatic --no-input

echo "==> [ETERNAL LABS] Aplicando migraciones de base de datos..."
python manage.py migrate

echo "==> [ETERNAL LABS] Inicializando catálogo regional..."
python manage.py seed_countries || true

# Hardening Fase 1C.20: El sembrado de sandbox debe ser explícito y seguro.
# No se ejecuta en despliegues estándar a menos que ENABLE_SANDBOX esté expresamente activo.
ENABLE_SANDBOX_NORMALIZED=$(echo "${ENABLE_SANDBOX:-False}" | tr '[:upper:]' '[:lower:]')
if [ "$ENABLE_SANDBOX_NORMALIZED" = "true" ] || [ "$ENABLE_SANDBOX_NORMALIZED" = "1" ]; then
    echo "==> [ETERNAL LABS] ENABLE_SANDBOX activo: inicializando escenario sandbox..."
    python manage.py prepare_sandbox
else
    echo "==> [ETERNAL LABS] ENABLE_SANDBOX inactivo o no configurado ($ENABLE_SANDBOX_NORMALIZED): omitiendo sembrado de datos de demostración."
fi

echo "==> [ETERNAL LABS] Proceso de construcción finalizado exitosamente."
