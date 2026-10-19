# ARQUITECTURA DE TENANCY, RAZÓN SOCIAL Y AUTORIZACIÓN MULTIEMPRESA
**ERP VECTOR — Documento de Referencia de Arquitectura Técnica**  
**Fecha de Publicación:** 19 de Octubre de 2026  
**Referencia de Auditoría:** Fase 1C.10 (SEC-06) / Fase 1C.11

---

## 1. Propósito y Alcance

Este documento establece la especificación oficial y vigente del modelo multiempresa (*multi-tenant*) de ERP VECTOR, aclarando la unificación de los atributos de identidad fiscal dentro de la entidad organizativa y documentando la ausencia intencional del modelo conceptual `LegalEntity`.

---

## 2. Decisión Arquitectónica: Modelo Unificado `Organization`

### 2.1 Antecedentes y Discrepancia Documental (SEC-06)
En especificaciones iniciales y términos de referencia preliminares se hacía alusión a un modelo separado denominado `LegalEntity` para gestionar razones sociales, RUC y personerías jurídicas. 

Durante la auditoría de seguridad y arquitectura de la Fase 1C.10 se constató que:
1. No existe ni ha existido una tabla o modelo `LegalEntity` en el código fuente de Django ni en el historial de migraciones.
2. Todos los campos fiscales, legales, tributarios y de localización geográfica residen de manera directa e indivisible en el modelo [`apps.tenancy.models.Organization`](file:///Users/kevinmejia/Documents/ERP%20VECTOR/apps/tenancy/models.py).

### 2.2 Atributos Legales y Fiscales en `Organization`
El modelo `Organization` concentra las siguientes dimensiones institucionales:

- **Identidad Corporativa y Razón Social:**
  - `name`: Nombre comercial o de fantasía de la empresa (ej: *IMESA INGENIERÍA S.A.*).
  - `legal_name`: Razón social legalmente inscrita ante el Registro Mercantil (ej: *Instalaciones y Mantenimiento Electromecánico Sociedad Anónima*).
  - `tax_id`: Identificación fiscal única (RUC en Nicaragua, RTN en Honduras, NIT en Guatemala/El Salvador).
  - `tax_regime`: Régimen tributario aplicable (`GENERAL`, `SIMPLIFIED`, `SPECIAL`).

- **Giro y Operación Modular:**
  - `industry`: Sector económico primario (`SERVICES`, `ENERGY_TELCO`, `COMMERCE`, `CONSTRUCTION`, etc.).
  - `business_vertical`: Vertical operativa que adapta los catálogos y pantallas POS/servicios.
  - `enabled_modules`: Lista JSON con los módulos activados contractualmente para el tenant.

- **Configuración Financiera y Contable:**
  - `country`: Clave foránea protegida (`PROTECT`) hacia `core.CountryConfig`.
  - `currency`: Moneda operativa base (`NIO`, `USD`, `CRC`, etc.).
  - `accounting_template`: Catálogo de cuentas inicial (`NIIF_PYMES`, `SIMPLIFIED`, `BLANK`).

### 2.3 Justificación Técnica de la Consolidación
La unificación en `Organization` proporciona ventajas críticas de seguridad e integridad:
1. **Evita inconsistencias relacionales:** Elimina el riesgo de discrepancias donde una transacción apunte a un `Organization` cuyo `LegalEntity` difiera del emisor real.
2. **Optimización de consultas multi-tenant:** Al heredar todos los modelos operativos de `TenantScopedModel` (`organization = ForeignKey('tenancy.Organization')`), las comprobaciones de pertenencia en middleware y querysets (`.for_org(request.organization)`) son atómicas e indexadas por una sola clave foránea directa.
3. **Gobierno y Aislamiento:** Las membresías de colaboradores (`OrganizationMembership`) vinculan directamente al usuario con `Organization`, fijando el nivel de rol (`RoleLevel`) para toda la esfera operativa, fiscal y contable de dicha empresa.

---

## 3. Estado de Vigencia

Cualquier referencia en especificaciones históricas o diagramas de diseño conceptual hacia `LegalEntity` debe interpretarse formalmente como un alias o atributo residente en `Organization`. No deben crearse migraciones ni modelos adicionales para descomponer esta estructura salvo instrucción explícita del Software Project Leader (SPL).
