# INFORME TÉCNICO DE AUDITORÍA INDEPENDIENTE — FASE 1C.8.3B
## Verificación Técnica de Integridad Contable, Tipos de Cambio, Concurrencia y Cierre Seguro

**Fecha:** 17 de Octubre de 2026
**Roles:** Principal Software Architect, Senior Django Engineer, Financial Systems Auditor y QA Lead
**Dirección:** Software Project Leader (SPL)
**Entorno de Trabajo:** Worktree Aislado `main-integration`
**Ruta del Repositorio:** `/Users/kevinmejia/Documents/ERP VECTOR/.worktrees/main-integration`
**Branch:** `main` | **HEAD:** `023247e05c9959572d9881eae8d77eb8aedc92d6`
**Base de Datos de Prueba:** PostgreSQL 14.18 (Homebrew) sobre base aislada efímera `test_postgres`
**Workspace Principal:** `/Users/kevinmejia/Documents/ERP VECTOR` (Intacto, Rama `integration/candidate-phase1`)
**Dictamen Final:** **APROBACIÓN CONDICIONADA (CONDITIONAL APPROVAL)**

---

## 1. RESUMEN EJECUTIVO

En cumplimiento estricto del mandato de la Fase 1C.8.3B, se ha llevado a cabo una **auditoría técnica e independiente** sobre las correcciones de integridad contable implementadas en la Fase 1C.8.3A:
- **H-08:** Blindaje de integridad e inmutabilidad de `JournalEntry` y `JournalEntryLine`.
- **H-07:** Eliminación del fallback silencioso a `Decimal('1.0')` y validación estricta de tasas de cambio en desembolsos a proveedores.
- **H-09:** Inicialización del saldo en cero en la creación de fondos de caja chica y validación de límites autorizados bajo concurrencia.

Esta auditoría no asumió como válidas las conclusiones del informe previo. Se inspeccionaron directamente los diffs del código fuente, se ejecutaron scripts empíricos de prueba de penetración ORM y concurrencia multihilo contra PostgreSQL 14, y se reejecutaron las seis suites financieras del ERP en una base de datos PostgreSQL aislada.

### Conclusiones Principales:
1. **Verificación de Suites:** Las 6 suites declaradas fueron ejecutadas al 100% sobre PostgreSQL 14 (`test_postgres`), registrando **107 pruebas aprobadas de 107 ejecutadas (0 fallos, 0 errores)**.
2. **H-08 (Inmutabilidad Contable):** Se verificó que los métodos `save()` y `delete()` de `JournalEntry` y `JournalEntryLine` bloquean eficazmente mutaciones y eliminaciones de pólizas balanceadas vía ORM estándar. Sin embargo, la auditoría identificó dos riesgos residuales significativos:
   - **Hallazgo P1 (Migración no generada):** El cambio de `on_delete=models.CASCADE` a `models.PROTECT` en `JournalEntryLine.entry` está presente en el modelo Python pero **no cuenta con archivo de migración Django** en el árbol del repositorio (`makemigrations --dry-run` detecta la migración faltante).
   - **Hallazgo P2 (Bypass vía operaciones masivas SQL):** Operaciones directas `QuerySet.update()`, `QuerySet.delete()` sobre líneas individuales, y `bulk_create()` no disparan los métodos del modelo Django, requiriendo un Trigger SQL nativo a nivel PostgreSQL para sellado hermético.
3. **H-07 (Contención Cambiaria):** Los 12 escenarios de la matriz de divisas y tasas de cambio fueron validados empíricamente sin fallos. Se confirmó la erradicación total del fallback a `Decimal('1.0')` en pagos de obligaciones con divisas extranjeras.
4. **H-09 (Caja Chica y Concurrencia):** Se demostró empíricamente mediante prueba multihilo con 2 transacciones simultáneas que el patrón `select_for_update()` sobre `BankAccount` y `PettyCashFund` serializa las operaciones y previene dobles dotaciones o sobregiros bajo PostgreSQL 14.
5. **No Modificación de Código:** Durante toda la auditoría no se modificó una sola línea del código de aplicación ni se alteró el workspace principal.

Debido a que el cambio estructural de `models.PROTECT` carece de archivo de migración versionado (Hallazgo P1), el dictamen técnico formal es de **APROBACIÓN CONDICIONADA**, recomendando al SPL autorizar en la siguiente fase la generación y verificación de la migración correspondiente y la implementación de triggers de inmutabilidad en base de datos.

---

## 2. ESTADO INICIAL Y FINAL DE GIT

Se verificó el árbol de trabajo al inicio y al final de la auditoría para garantizar que no hubo mutaciones de código, commits accidentales ni alternancia de ramas.

### 2.1 Inspección Inicial
- **Ruta de Trabajo:** `/Users/kevinmejia/Documents/ERP VECTOR/.worktrees/main-integration`
- **Branch Activo:** `main`
- **HEAD Commit:** `023247e05c9959572d9881eae8d77eb8aedc92d6` (Mensaje: `phase-1c5-2-clean-history`)
- **Estado Inicial `git status --short`:**
  ```text
   M apps/finance/models.py
   M apps/finance/services.py
   M apps/finance/views.py
   M apps/purchasing/models.py
   M apps/purchasing/services.py
  ?? apps/finance/tests_phase1c8_3a_integrity.py
  ?? informe_fase1c8_3a_correcciones_integridad.md
  ?? informe_impacto_tecnico_fase1c8_2.md
  ```

### 2.2 Inspección Final
- **Branch:** `main`
- **HEAD Commit:** `023247e05c9959572d9881eae8d77eb8aedc92d6` (Idéntico)
- **Estado Final `git status --short`:**
  Exactamente idéntico al inicial más el presente informe de verificación independiente (`informe_fase1c8_3b_verificacion_independiente.md`).
- **Workspace Principal:** `/Users/kevinmejia/Documents/ERP VECTOR` verificado limpio (`git status` sin cambios), en la rama `integration/candidate-phase1`.

---

## 3. ARCHIVOS Y DIFFS INSPECCIONADOS

Se realizó la revisión estática y dinámica de los diffs en los 5 archivos modificados en la Fase 1C.8.3A:

### 3.1 `apps/finance/models.py`
- **Líneas 161–181:** Sobrescritura de `JournalEntry.save()` y `JournalEntry.delete()`.
  - `save()`: Compara con la base de datos usando `_state.adding` y una consulta `refresh`. Si la instancia persistida ya tiene `is_balanced=True`, bloquea cualquier edición lanzando `ValidationError("Una póliza contable balanceada/cerrada no puede ser modificada...")`.
  - `delete()`: Si `is_balanced=True`, bloquea la eliminación lanzando `ValidationError("Una póliza contable balanceada/cerrada no puede ser eliminada...")`.
- **Líneas 239–259:** Sobrescritura de `JournalEntryLine.save()` y `JournalEntryLine.delete()`.
  - `save()`: Si la póliza padre (`entry.is_balanced`) ya está cuadrada, prohíbe crear o editar líneas contables.
  - `delete()`: Si la póliza padre está balanceada, prohíbe eliminar líneas individuales.
- **Línea 216:** Modificación del campo `entry` en `JournalEntryLine`:
  ```python
  entry = models.ForeignKey(
      JournalEntry,
      on_delete=models.PROTECT,  # Modificado desde CASCADE
      related_name='lines',
      verbose_name='Póliza Contable'
  )
  ```
  *Evaluación Crítica:* La protección `models.PROTECT` impide que al intentar borrar una póliza en cascada se eliminen las líneas sin control.

### 3.2 `apps/finance/services.py`
- **Líneas 70–89 (`create_journal_entry`):**
  - Validación previa de `exchange_rate`:
    ```python
    if currency != 'NIO' and (exchange_rate is None or exchange_rate <= Decimal('0.0')):
        raise ValidationError(f"Se requiere una tasa de cambio positiva para registrar una póliza en {currency}.")
    ```
  - Eliminación del fallback `exchange_rate or Decimal('1.0')`. Ahora asigna `effective_exchange_rate = exchange_rate if currency != 'NIO' else Decimal('1.0')`.
- **Líneas 487–520 (`allocate_petty_cash_fund`):**
  - Implementación de bloqueo pesimista `select_for_update()` sobre `BankAccount` y `PettyCashFund`.
  - Validación estricta del límite autorizado:
    ```python
    if fund.current_balance + amount > fund.authorized_amount:
        raise ValidationError(f"La dotación de {amount} excede el monto autorizado...")
    ```
  - Validación de fondos bancarios suficientes antes del débito.
  - Deducción bancaria y acreditación del fondo en una única transacción atómica.

### 3.3 `apps/finance/views.py`
- **Líneas 468–477 (`PettyCashFundCreateView`):**
  - Corrección del saldo inicial:
    ```python
    fund = form.save(commit=False)
    fund.organization = self.request.user.organization
    fund.current_balance = Decimal('0.00')  # Corrección H-09: inicia en cero
    fund.is_active = True
    fund.save()
    ```
  - El saldo solo se incrementa cuando se ejecuta formalmente el servicio financiero `allocate_petty_cash_fund`.

### 3.4 `apps/purchasing/models.py`
- **Líneas 335–347 (`SupplierPayment.clean`):**
  - Validación de tasa contractual:
    ```python
    if self.currency != 'NIO':
        if not self.exchange_rate or self.exchange_rate <= Decimal('0.0'):
            raise ValidationError({'exchange_rate': 'Para pagos en moneda extranjera se requiere una tasa de cambio válida y estrictamente positiva.'})
    ```

### 3.5 `apps/purchasing/services.py`
- **Líneas 380–385 (`post_supplier_payment_to_ledger`):**
  - Eliminación total de la expresión `exchange_rate = payment.exchange_rate or Decimal('1.0')`.
  - Ahora verifica:
    ```python
    if is_foreign_payment and (payment.exchange_rate is None or payment.exchange_rate <= Decimal('0.0')):
        raise ValidationError(f"El pago {payment.reference_number} en moneda extranjera ({payment.currency}) carece de una tasa de cambio válida.")
    ```

---

## 4. AUDITORÍA H-08: INTEGRIDAD E INMUTABILIDAD CONTABLE

Se realizó un análisis exhaustivo y una prueba empírica automatizada de penetración ORM (`test_h08_orm_matrix.py`) para determinar exactamente qué protege el código y qué vías de acceso eluden la validación del modelo.

### 4.1 Ciclo de Vida de las Pólizas
En el modelo `JournalEntry`, el campo `is_balanced` actúa como el indicador de cierre/inmutabilidad:
- **Borrador:** Una póliza se crea con `is_balanced=False`. En este estado, el sistema permite modificar la cabecera y agregar, editar o eliminar líneas (`JournalEntryLine`).
- **Balanceada / Contabilizada:** Cuando `entry.balance_and_close()` es invocado o se crea mediante servicios contables transaccionales (`create_journal_entry`), `is_balanced` pasa a `True` tras verificar que $\sum \text{Débitos} = \sum \text{Créditos}$.
- **Reversión:** No existe un flag `is_reversed` en el modelo base; las reversiones se efectúan mediante la creación de una nueva póliza de ajuste (`REVERSAL` o `ADJUSTMENT`) con signos o posiciones invertidas, manteniendo intacta la póliza original.

### 4.2 Matriz de Vías de Modificación (Prueba Empírica en PostgreSQL 14)

| # | Vía de Modificación | Comportamiento Observado en BD | Clasificación de Integridad | Mecanismo de Control |
|---|---------------------|--------------------------------|-----------------------------|----------------------|
| 1 | `instance.save()` en `JournalEntry` balanceada | Bloqueado con `ValidationError` | **Protegido** | Método `JournalEntry.save()` |
| 2 | `instance.delete()` en `JournalEntry` balanceada | Bloqueado con `ValidationError` | **Protegido** | Método `JournalEntry.delete()` |
| 3 | `instance.save()` en `JournalEntryLine` de póliza cerrada | Bloqueado con `ValidationError` | **Protegido** | Método `JournalEntryLine.save()` |
| 4 | `instance.delete()` en `JournalEntryLine` de póliza cerrada | Bloqueado con `ValidationError` | **Protegido** | Método `JournalEntryLine.delete()` |
| 5 | `JournalEntryLine.objects.create(entry=...)` en póliza cerrada | Bloqueado con `ValidationError` | **Protegido** | Método `JournalEntryLine.save()` ejecutado por `create()` |
| 6 | `JournalEntry.objects.filter(pk=...).delete()` | Bloqueado por `ProtectedError` de Django | **Protegido** | Relación `models.PROTECT` en `lines` |
| 7 | `JournalEntryLine.objects.filter(pk=...).delete()` | **EJECUTADO EN BD** (Bypass de validación) | **Vulnerable (Gap ORM)** | `QuerySet.delete()` no invoca `instance.delete()` |
| 8 | `JournalEntry.objects.filter(pk=...).update(...)` | **EJECUTADO EN BD** (Bypass de validación) | **Vulnerable (Gap ORM)** | `QuerySet.update()` genera `UPDATE` SQL directo |
| 9 | `JournalEntryLine.objects.filter(pk=...).update(...)` | **EJECUTADO EN BD** (Bypass de validación) | **Vulnerable (Gap ORM)** | `QuerySet.update()` genera `UPDATE` SQL directo |
| 10 | `JournalEntryLine.objects.bulk_create([...])` | **EJECUTADO EN BD** (Bypass de validación) | **Vulnerable (Gap ORM)** | `bulk_create()` no invoca `save()` |

### 4.3 Integridad Referencial (`models.PROTECT`) y Hallazgo Crítico
- **Comportamiento Verificado:** Al intentar eliminar una póliza mediante `JournalEntry.objects.filter(id=entry.id).delete()`, Django comprueba las relaciones en memoria y lanza `django.db.models.deletion.ProtectedError: ("Cannot delete some instances of model 'JournalEntry' because they are referenced through protected foreign keys: 'JournalEntryLine.entry'...")`.
- **Hallazgo P1 (Discrepancia de Migración):** Al ejecutar `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 manage.py makemigrations --dry-run`, se obtuvo:
  ```text
  Migrations for 'finance':
    apps/finance/migrations/0012_alter_journalentryline_entry.py
      - Alter field entry on journalentryline
  ```
  **Evidencia:** El cambio a `on_delete=models.PROTECT` en el modelo Python no ha sido materializado en un archivo de migración formal dentro del repositorio. En los tests de Django, el test runner construye el schema a partir del estado de los modelos o ejecuta migraciones y luego aplica el modelo; sin embargo, en entornos desplegados con `migrate`, la base de datos externa no tendrá registrada la migración `0012`.

### 4.4 Reversión Contable
Se auditó la prueba `test_h08_07_legitimate_reversal_is_permitted` y el método de reversión en `apps/finance/services.py`:
- La reversión crea una nueva póliza `reversal_entry` con débitos y créditos invertidos.
- La póliza original permanece con sus líneas y montos exactamente iguales.
- Ningún método modificó o borró el asiento original, garantizando la trazabilidad histórica de auditoría.

---

## 5. AUDITORÍA H-07: TIPOS DE CAMBIO Y LIQUIDACIONES

Se auditó de forma exhaustiva la erradicación del fallback silencioso `Decimal('1.0')` en desembolsos a proveedores. Se diseñó y ejecutó el script `test_h07_currency_matrix.py` cubriendo los 12 escenarios monetarios en PostgreSQL 14.

### 5.1 Matriz de Escenarios Monetarios Verificada Empíricamente

| # | Escenario Evaluado | Moneda PO | Moneda Pago | Tasa Enviada | Resultado Observado | Estado de Auditoría | Evidencia / Mecanismo |
|---|--------------------|-----------|-------------|--------------|---------------------|---------------------|-----------------------|
| 1 | Pago NIO de deuda NIO | NIO | NIO | N/A (1.0 implícito) | **Aceptado** | **Correcto** | Misma moneda base, no requiere conversión. |
| 2 | Pago USD de deuda USD con tasa válida | USD | USD | 36.5000 | **Aceptado** | **Correcto** | Débito en NIO = $100 \times 36.50 = 3,650$ NIO. |
| 3 | Pago USD de deuda NIO sin tasa | NIO | USD | `None` | **Rechazado** (`ValidationError`) | **Correcto** | Bloqueado por `clean()` en `SupplierPayment`. |
| 4 | Pago NIO de deuda USD sin tasa | USD | NIO | `None` | **Rechazado** (`ValidationError`) | **Correcto** | El servicio de egreso exige tasa contractual. |
| 5 | Moneda distinta de Orden de Compra | NIO | USD | 36.5000 | **Rechazado** (`ValidationError`) | **Correcto** | Contención monetaria: orden NIO no acepta pago USD. |
| 6 | Tasa Nula en Póliza Foreign | N/A | USD | `None` | **Rechazado** (`ValidationError`) | **Correcto** | `create_journal_entry` rechaza tasa nula sin fallback a 1.0. |
| 7 | Tasa Cero en Póliza Foreign | N/A | USD | `Decimal('0.0')` | **Rechazado** (`ValidationError`) | **Correcto** | Bloqueado por validación de tasa estrictamente positiva. |
| 8 | Tasa Negativa | N/A | USD | `Decimal('-36.5')`| **Rechazado** (`ValidationError`) | **Correcto** | Bloqueado por validación `<= Decimal('0.0')`. |
| 9 | Tasa Positiva Válida | N/A | USD | `Decimal('36.62')`| **Aceptado** | **Correcto** | Póliza balanceada registrada con tasa real. |
| 10| Moneda No Soportada / Incompatible | USD | EUR | 40.0000 | **Rechazado** (`ValidationError`) | **Correcto** | Cuenta bancaria NIO/USD rechaza divisa incompatible EUR. |
| 11| Tasa Contractual Inválida en PO | USD | USD | `Decimal('0.0')` | **Rechazado** (`ValidationError`) | **Correcto** | Bloqueado en frontera de modelo antes de procesar pago. |
| 12| Fallo durante Contabilización | USD | USD | 36.5000 | **Rollback Atómico Completo** | **Correcto** | Ante excepción provocada, saldo bancario y deuda intactos. |

### 5.2 Conclusión de la Auditoría H-07
- Se certifica que no queda rastro de `or Decimal('1.0')` en las vías de contabilización de egresos a proveedores.
- Los rechazos monetarios no generan escrituras huérfanas ni saldos desbalanceados gracias a bloques `transaction.atomic()`.

---

## 6. AUDITORÍA H-09: CAJA CHICA Y CONCURRENCIA

Se inspeccionaron los componentes de creación y dotación de fondos de caja chica (`PettyCashFundCreateView`, `allocate_petty_cash_fund`, `PettyCashFund`).

### 6.1 Inicialización de Saldo (H-09)
- En `PettyCashFundCreateView`, la instrucción `fund.current_balance = Decimal('0.00')` garantiza que al registrar la caja chica en el sistema no se incremente artificialmente la disponibilidad líquida de la empresa.
- **Evidencia:** `test_h09_01_create_petty_cash_fund_initializes_zero_balance` en `tests_phase1c8_3a_integrity.py` verifica que un fondo con límite autorizado de C$ 5,000.00 nace con `current_balance == Decimal('0.00')`.

### 6.2 Dotación y Concurrencia Multihilo en PostgreSQL 14
Se auditó el mecanismo de bloqueo pesimista en `allocate_petty_cash_fund`:
```python
bank_account = BankAccount.objects.select_for_update().get(id=bank_account_id, organization=organization)
fund = PettyCashFund.objects.select_for_update().get(id=fund_id, organization=organization)
```
Para certificar que este patrón es seguro ante concurrencia real y no únicamente en ejecución secuencial, se ejecutó el script `test_h09_concurrency.py` lanzando **dos hilos concurrentes simultáneos (`threading.Thread`)** intentando dotar cada uno C$ 10,000.00 sobre un fondo cuyo límite autorizado es exactamente C$ 10,000.00:
- **Resultado en PostgreSQL 14:**
  - Hilo A adquirió el bloqueo pesimista `FOR UPDATE` sobre la fila de `PettyCashFund` y `BankAccount`.
  - Hilo B quedó bloqueado a nivel de base de datos esperando que Hilo A liberara el cerrojo.
  - Hilo A incrementó el saldo a C$ 10,000.00 y comprometió la transacción (`COMMIT`).
  - Hilo B despertó, leyó el nuevo estado confirmado (`current_balance = 10,000.00`), evaluó la condición `10,000.00 + 10,000.00 > 10,000.00`, y fue **rechazado con `ValidationError: La dotación de 10000.00 excede el monto autorizado`**.
  - **Saldo final del fondo:** C$ 10,000.00 (Exactamente el límite autorizado).
  - **Deducción bancaria:** C$ 10,000.00 (Una sola vez).
- **Veredicto:** El patrón de concurrencia es **completamente determinista y seguro** en PostgreSQL 14.

---

## 7. RESULTADOS DE TODAS LAS SUITES EJECUTADAS

Se reejecutaron las seis suites financieras declaradas en la Fase 1C.8.3A utilizando exclusivamente el motor PostgreSQL 14.18 sobre la base de pruebas aislada `test_postgres`.

### 7.1 Tabla Consolidada de Ejecución

| Suite de Pruebas | Comando de Ejecución | Motor DB | Tests Encontrados | Tests Aprobados | Fallos | Errores | Duración |
|------------------|----------------------|----------|-------------------|-----------------|--------|---------|----------|
| **1. Fase 1C.8.3A Integridad** | `DATABASE_URL=postgres://localhost:5432/postgres python3 manage.py test apps.finance.tests_phase1c8_3a_integrity -v 2` | PostgreSQL 14.18 (`test_postgres`) | 18 | 18 | 0 | 0 | 9.847s |
| **2. Realized FX 1C.6** | `DATABASE_URL=postgres://localhost:5432/postgres python3 manage.py test apps.finance.tests_realized_fx_accounting_phase1c6 -v 2` | PostgreSQL 14.18 (`test_postgres`) | 17 | 17 | 0 | 0 | 11.256s |
| **3. Kardex & Currency 1C.7.1** | `DATABASE_URL=postgres://localhost:5432/postgres python3 manage.py test apps.finance.tests_phase1c7_1_kardex_currency_integrity -v 2` | PostgreSQL 14.18 (`test_postgres`) | 12 | 12 | 0 | 0 | 14.572s |
| **4. Cash Bank Deposit 1C.5.2** | `DATABASE_URL=postgres://localhost:5432/postgres python3 manage.py test apps.billing.tests_cash_bank_deposit_phase1c5_2_2 -v 2` | PostgreSQL 14.18 (`test_postgres`) | 7 | 7 | 0 | 0 | 10.917s |
| **5. Finance E2E & Statements** | `DATABASE_URL=postgres://localhost:5432/postgres python3 manage.py test apps.finance.tests -v 2` | PostgreSQL 14.18 (`test_postgres`) | 12 | 12 | 0 | 0 | 36.672s |
| **6. Purchasing & Disbursements**| `DATABASE_URL=postgres://localhost:5432/postgres python3 manage.py test apps.purchasing -v 2` | PostgreSQL 14.18 (`test_postgres`) | 41 | 41 | 0 | 0 | 60.092s |
| **TOTAL CONSOLIDADO** | **6 Suites Financieras** | **PostgreSQL 14** | **107** | **107** | **0** | **0** | **143.356s** |

*Nota sobre Advertencias:* Durante la ejecución de `apps.finance.tests` se observó una advertencia inofensiva `UserWarning: No directory at: .../staticfiles/` emitida por WhiteNoise debido a la ausencia de compilación previa de estáticos en el worktree de pruebas. Cero advertencias críticas o de deprecación financiera.

---

## 8. DISCREPANCIAS RESPECTO DEL INFORME 1C.8.3A

Al contrastar las afirmaciones del informe de la Fase 1C.8.3A contra el estado del código y la base de datos, se identificaron las siguientes discrepancias:

1. **Afirmación sobre Migración de `PROTECT`:** El informe anterior declaró: *"El cambio de CASCADE a PROTECT en JournalEntryLine.entry asegura que ninguna eliminación accidental de JournalEntry arrastre sus líneas"*.
   - **Discrepancia Real:** Si bien el código en `apps/finance/models.py` define `on_delete=models.PROTECT`, **no se generó el archivo de migración Django**. En una instalación fresca o despliegue en staging/producción con `python manage.py migrate`, la clave foránea no tendría reflejado el cambio si Django dependiera de migraciones para validar constraints referenciales a nivel DDL de base de datos.
2. **Alcance de la Inmutabilidad de `JournalEntry`:** El informe anterior dio a entender una protección integral contra modificaciones y eliminaciones.
   - **Discrepancia Real:** La protección se implementó exclusivamente a nivel de métodos de instancia de Django (`save()` y `delete()`). Las operaciones masivas a nivel de QuerySet (`update()`, `bulk_create()`, `delete()` en líneas) eluden las comprobaciones de Python.
3. **Validación de Concurrencia:** El informe previo citó pruebas unitarias secuenciales como validación de concurrencia.
   - **Discrepancia Real:** Las pruebas unitarias estándar ejecutan operaciones en un solo hilo. Para certificar la concurrencia real se requirió la prueba multihilo independiente desarrollada en esta auditoría (`test_h09_concurrency.py`).

---

## 9. DEFECTOS NUEVOS Y RIESGOS RESIDUALES

### 9.1 Hallazgo H-10: Migración Pendiente para `JournalEntryLine.entry`
- **Categoría:** Discrepancia Schema vs Modelo.
- **Tipo:** **Hecho Verificado**.
- **Evidencia:** `python3 manage.py makemigrations --dry-run` detecta la necesidad de generar `apps/finance/migrations/0012_alter_journalentryline_entry.py`.
- **Impacto:** Si se despliega a un entorno productivo sin este archivo, Django reportará inconsistencia en `makemigrations --check`.

### 9.2 Hallazgo H-11: Evasión de Inmutabilidad mediante QuerySet Updates/Bulk Creates
- **Categoría:** Limitación de Arquitectura ORM.
- **Tipo:** **Hecho Verificado**.
- **Evidencia:** Script `test_h08_orm_matrix.py` demostró que `JournalEntryLine.objects.filter(id=line.id).delete()` y `JournalEntry.objects.filter(id=entry.id).update(description='HACK')` alteran la base de datos sin levantar `ValidationError`.
- **Impacto:** Un script administrativo, comando de mantenimiento o desarrollador futuro podría corromper la contabilidad si utiliza métodos masivos del ORM.

### 9.3 Hallazgo H-12: Dependencia de Bloqueos en Nivel de Aplicación
- **Categoría:** Integridad de Concurrencia.
- **Tipo:** **Hecho Verificado**.
- **Evidencia:** La seguridad de caja chica depende estrictamente de que todo acceso pase por `allocate_petty_cash_fund` con `select_for_update()`. La base de datos no tiene un constraint SQL que impida que `current_balance > authorized_amount`.

---

## 10. CLASIFICACIÓN DE HALLAZGOS POR PRIORIDAD

| ID | Hallazgo | Prioridad | Justificación |
|----|----------|-----------|---------------|
| **H-10** | Migración faltante `0012_alter_journalentryline_entry` | **P1 (Alta)** | Bloquea la coherencia del árbol de migraciones y la reproducibilidad de despliegues. |
| **H-11** | Bypass de inmutabilidad en operaciones masivas ORM | **P2 (Media)** | El código normal de la aplicación no usa `bulk_create` o `update()` en pólizas, pero representa un riesgo para tareas de mantenimiento. Requiere Trigger SQL. |
| **H-12** | Falta de Check Constraint SQL para límite de caja chica | **P3 (Baja)** | El servicio `allocate_petty_cash_fund` protege la operación con bloqueo pesimista, pero un constraint SQL proporcionaría defensa en profundidad. |

---

## 11. CORRECCIONES RECOMENDADAS (SIN IMPLEMENTAR)

En apego estricto a la restricción obligatoria de no modificar código en esta fase, se formulan las recomendaciones técnicas para la siguiente fase:

1. **Generación de la Migración `0012` (Para H-10):**
   - Ejecutar en la fase autorizada: `python manage.py makemigrations finance --name alter_journalentryline_entry_protect`.
   - Verificar que aplique limpiamente sobre PostgreSQL 14 sin alterar datos existentes.
2. **Implementación de Trigger SQL de Inmutabilidad Contable (Para H-11):**
   - Crear una migración con operaciones `RunSQL` en PostgreSQL para instalar una función trigger:
     ```sql
     CREATE OR REPLACE FUNCTION prevent_balanced_journal_entry_mutation()
     RETURNS TRIGGER AS $$
     BEGIN
         IF (TG_OP = 'DELETE' AND OLD.is_balanced = TRUE) THEN
             RAISE EXCEPTION 'No se permite eliminar una póliza balanceada en PostgreSQL.';
         END IF;
         IF (TG_OP = 'UPDATE' AND OLD.is_balanced = TRUE AND NEW.is_balanced = TRUE) THEN
             RAISE EXCEPTION 'No se permite modificar una póliza balanceada en PostgreSQL.';
         END IF;
         RETURN NEW;
     END;
     $$ LANGUAGE plpgsql;

     CREATE TRIGGER trg_journalentry_immutability
     BEFORE UPDATE OR DELETE ON finance_journalentry
     FOR EACH ROW EXECUTE FUNCTION prevent_balanced_journal_entry_mutation();
     ```
   - Aplicar trigger similar sobre `finance_journalentryline` comprobando el estado `is_balanced` de la póliza padre.
3. **Check Constraint en `PettyCashFund` (Para H-12):**
   - Añadir en `PettyCashFund.Meta.constraints`:
     `models.CheckConstraint(check=models.Q(current_balance__lte=models.F('authorized_amount')), name='petty_cash_fund_balance_lte_authorized')`.

---

## 12. DEPENDENCIAS CONTABLES QUE SIGUEN REQUIRIENDO APROBACIÓN

Se ratifica que las siguientes decisiones contables estructurales **no fueron implementadas** y permanecen congeladas a la espera de la resolución ejecutiva del SPL:

- **DEC-01 (Cuenta de Contrapartida para Fondos Fijos):** Definición formal de la cuenta de egreso de dotación (Cuenta 1102 vs subcuenta bancaria transitoria).
- **DEC-02 (Cierre de Cajas y Compensación de Efectivo):** Modelo de liquidación de remesas sin arqueo verificado.
- **DEC-03 (Tratamiento del Diferencial Cambiario Realizado):** Bandas de tolerancia oficiales del BCN y cuentas específicas de gasto/ingreso financiero.
- **DEC-05 (Submayor Bancario y Conciliación Multimoneda):** Estructura contable de conciliación de cheques y transferencias en tránsito.

---

## 13. DICTAMEN DE CIERRE

En calidad de **Principal Software Architect, Senior Django Engineer, Financial Systems Auditor y QA Lead**, y habiendo obtenido evidencia objetiva y reproducible de:
- 107 pruebas automatizadas aprobadas en PostgreSQL 14 (0 fallos, 0 errores).
- Validación satisfactoria de los 12 escenarios monetarios sin fallback a 1.0.
- Validación de concurrencia multihilo pesimista en dotación de caja chica.
- Respeto total al workspace principal y ausencia de modificaciones no autorizadas.

Pero considerando la existencia del **Hallazgo P1 (Migración pendiente para `models.PROTECT`)**:

Se emite el dictamen formal de:

### **APROBACIÓN CONDICIONADA (CONDITIONAL APPROVAL)**

**Condiciones obligatorias para el paso a la Fase 1C.8.4 / Integración Final:**
1. Autorización expresa del Software Project Leader para generar la migración `0012_alter_journalentryline_entry.py`.
2. Verificación de la migración en base de datos PostgreSQL 14 aislada.
3. Evaluación de la incorporación de los Triggers SQL nativos (Hallazgo P2) en la fase de Hardening de Base de Datos.

---
*Fin del Informe Técnico de Auditoría Independiente.*
