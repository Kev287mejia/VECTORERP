import csv
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import hashlib
import io
import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.finance.models import (
    BankAccount,
    BankStatement,
    BankStatementLine,
    BankReconciliationMatch,
    FinancialTransaction,
)

MAX_STATEMENT_FILE_SIZE = 5 * 1024 * 1024  # 5 MB límite de seguridad

INFLOW_TYPES = {
    FinancialTransaction.TransactionType.CLIENT_COLLECTION,
    FinancialTransaction.TransactionType.CARD_SETTLEMENT,
    FinancialTransaction.TransactionType.CHECK_CLEARING,
}

OUTFLOW_TYPES = {
    FinancialTransaction.TransactionType.VENDOR_DISBURSEMENT,
    FinancialTransaction.TransactionType.PETTY_CASH_REPLENISH,
    FinancialTransaction.TransactionType.BANK_TRANSFER,
}


def compute_file_sha256(content: bytes | str) -> str:
    """Calcula el hash SHA-256 del contenido crudo de un archivo de extracto bancario."""
    if isinstance(content, str):
        raw_bytes = content.encode('utf-8')
    else:
        raw_bytes = content
    return hashlib.sha256(raw_bytes).hexdigest()


def compute_line_hash(
    org_id: int | str,
    bank_account_id: int | str,
    post_date: date,
    movement_type: str,
    amount: Decimal,
    reference: str,
    description: str,
    occurrence: int = 1,
) -> str:
    """
    Calcula un hash determinista para identificar de forma unívoca una línea de extracto.
    Incorpora la ocurrencia del movimiento para soportar legítimamente operaciones repetidas
    con idéntica fecha, monto y concepto dentro de un extracto bancario.
    """
    clean_ref = (reference or '').strip().upper()
    clean_desc = (description or '').strip().upper()
    payload = f"{org_id}:{bank_account_id}:{post_date.isoformat()}:{movement_type}:{amount:.2f}:{clean_ref}:{clean_desc}:{occurrence}"
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def normalize_reference(ref: str | None) -> str:
    """
    Normaliza una referencia bancaria para comparación determinista:
    - Remueve espacios, guiones y caracteres no alfanuméricos.
    - Convierte a mayúsculas.
    """
    if not ref:
        return ''
    cleaned = str(ref).strip().upper()
    return re.sub(r'[^A-Z0-9]', '', cleaned)


# Referencias genéricas que no aportan especificidad suficiente para una coincidencia exacta automática
GENERIC_REFERENCES = {
    '', '0', '00', '000', '0000', 'NA', 'SINREF', 'PAGO', 'TRANSF',
    'TRANSFERENCIA', 'DEPOSITO', 'CHEQUE', 'NOTA', 'ND', 'NC', 'TRX',
    'REF', 'BANCO', 'CARGO', 'ABONO', 'MOVIMIENTO',
}


def _parse_date(raw_date_str: str) -> date:
    """Parsea una cadena de fecha soportando múltiples formatos comunes en banca."""
    clean_str = (raw_date_str or '').strip()
    if not clean_str:
        raise ValueError("Fecha vacía o no especificada.")

    date_formats = [
        '%Y-%m-%d',
        '%d/%m/%Y',
        '%m/%d/%Y',
        '%d-%m-%Y',
        '%Y/%m/%d',
        '%d.%m.%Y',
    ]
    for fmt in date_formats:
        try:
            return datetime.strptime(clean_str, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Formato de fecha inválido o no reconocido: '{clean_str}'. Formatos soportados: YYYY-MM-DD, DD/MM/YYYY.")


def _parse_amount(raw_amount_str: str) -> Decimal:
    """Limpia y convierte cadenas monetarias a Decimal(0.01) con redondeo bancario ROUND_HALF_UP."""
    clean_str = (raw_amount_str or '').strip()
    if not clean_str:
        raise ValueError("Importe numérico vacío.")

    # Quitar símbolos de moneda comunes ($, C$, NIO, USD) y espacios
    clean_str = re.sub(r'[C\$USD\s]', '', clean_str, flags=re.IGNORECASE)

    # Manejo de separadores: formato 1,234.56 vs 1.234,56
    if ',' in clean_str and '.' in clean_str:
        if clean_str.rfind('.') > clean_str.rfind(','):
            clean_str = clean_str.replace(',', '')
        else:
            clean_str = clean_str.replace('.', '').replace(',', '.')
    elif ',' in clean_str:
        # Si tiene coma y no punto, asumimos coma decimal si tiene 2 decimales, o miles si no
        parts = clean_str.split(',')
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            clean_str = clean_str.replace(',', '.')
        else:
            clean_str = clean_str.replace(',', '')

    try:
        val = Decimal(clean_str).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return val
    except (InvalidOperation, TypeError):
        raise ValueError(f"Importe monetario numérico inválido: '{raw_amount_str}'.")


def parse_and_validate_bank_statement_csv(
    file_content: bytes | str,
    bank_account: BankAccount,
) -> dict:
    """
    Parser robusto y validado para extractos bancarios en formato CSV.
    Detecta columnas por alias en español e inglés:
    - Fecha: date, fecha, fecha_operacion, f_operacion
    - Fecha Valor: value_date, fecha_valor, f_valor
    - Referencia: reference, referencia, ref, doc, no_documento, numero_referencia
    - Descripción: description, descripcion, detalle, concepto, memo
    - Importe y Tipo:
      Opción A: monto/amount + tipo/movement_type (DEBIT/CREDIT, CARGO/ABONO, D/C)
      Opción B: debito/debit y credito/credit (columnas separadas)
      Opción C: monto con signo (+ credito, - debito)
    """
    if isinstance(file_content, bytes):
        if len(file_content) > MAX_STATEMENT_FILE_SIZE:
            raise ValidationError(f"El archivo excede el tamaño máximo permitido de {MAX_STATEMENT_FILE_SIZE // (1024 * 1024)} MB.")
        raw_bytes = file_content
        # Detección de encoding: utf-8-sig (BOM), utf-8, latin-1
        text = None
        for enc in ('utf-8-sig', 'utf-8', 'latin-1'):
            try:
                text = raw_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValidationError("No fue posible decodificar el archivo. Asegúrese de que sea un archivo de texto CSV válido.")
    else:
        text = file_content
        raw_bytes = text.encode('utf-8')
        if len(raw_bytes) > MAX_STATEMENT_FILE_SIZE:
            raise ValidationError("El archivo de texto excede el tamaño máximo permitido.")

    text = text.strip()
    if not text:
        raise ValidationError("El archivo de extracto bancario está vacío.")

    file_hash = hashlib.sha256(raw_bytes).hexdigest()

    # Detectar dialecto y delimitador de forma robusta (soporta comas, punto y coma, tabulador y barra vertical)
    sample = text[:4096]
    delimiter = ','
    try:
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(sample, delimiters=',;\t|')
        delimiter = dialect.delimiter
    except Exception:
        first_line = next((l for l in sample.splitlines() if l.strip()), '')
        counts = {d: first_line.count(d) for d in [';', '\t', '|', ',']}
        best_del = max(counts, key=counts.get)
        delimiter = best_del if counts[best_del] > 0 else ','

    f_stream = io.StringIO(text)
    reader = csv.reader(f_stream, delimiter=delimiter)

    rows = list(reader)
    if not rows or len(rows) < 2:
        raise ValidationError("El archivo CSV no contiene registros suficientes (debe incluir encabezado y al menos una fila de datos).")

    # Mapeo de encabezados
    header_raw = [h.strip().lower() for h in rows[0]]
    header_map = {}
    for idx, col in enumerate(header_raw):
        # Quitar acentos para normalización
        col_norm = re.sub(r'[áàä]', 'a', col)
        col_norm = re.sub(r'[éèë]', 'e', col_norm)
        col_norm = re.sub(r'[íìï]', 'i', col_norm)
        col_norm = re.sub(r'[óòö]', 'o', col_norm)
        col_norm = re.sub(r'[úùü]', 'u', col_norm)
        col_norm = re.sub(r'[^a-z0-9_]', '', col_norm)
        header_map[col_norm] = idx

    def find_col(aliases):
        for a in aliases:
            if a in header_map:
                return header_map[a]
        return None

    col_date = find_col(['fecha', 'date', 'fechaoperacion', 'foperacion'])
    col_vdate = find_col(['fechavalor', 'valuedate', 'fvalor'])
    col_ref = find_col(['referencia', 'reference', 'ref', 'doc', 'nodocumento', 'numeroreferencia', 'comprobante'])
    col_desc = find_col(['descripcion', 'description', 'detalle', 'concepto', 'memo', 'beneficiario'])
    col_amt = find_col(['monto', 'amount', 'importe', 'valor'])
    col_type = find_col(['tipo', 'movementtype', 'tipomovimiento', 'dc', 'debitocredito'])
    col_debit = find_col(['debito', 'debit', 'cargo', 'cargos', 'salida', 'egreso'])
    col_credit = find_col(['credito', 'credit', 'abono', 'abonos', 'entrada', 'ingreso'])
    col_curr = find_col(['moneda', 'currency'])

    if col_date is None:
        raise ValidationError("Columna obligatoria de fecha ('fecha' o 'date') no encontrada en el encabezado del archivo CSV.")
    if col_desc is None:
        raise ValidationError("Columna obligatoria de descripción ('descripcion' o 'description') no encontrada en el encabezado del archivo CSV.")

    # Validar que tengamos una forma válida de determinar monto y sentido
    has_amt_and_type = (col_amt is not None and col_type is not None)
    has_separate_debit_credit = (col_debit is not None and col_credit is not None)
    has_signed_amt = (col_amt is not None)

    if not (has_amt_and_type or has_separate_debit_credit or has_signed_amt):
        raise ValidationError(
            "Columnas insuficientes para determinar los importes. "
            "Se requiere columna 'monto' con 'tipo', o columnas separadas de 'debito' y 'credito'."
        )

    parsed_lines = []
    intrafile_sig_counts = {}
    total_debits = Decimal('0.00')
    total_credits = Decimal('0.00')
    min_date = None
    max_date = None

    for row_idx, row in enumerate(rows[1:], start=2):
        # Ignorar filas completamente vacías
        if not row or not any(field.strip() for field in row):
            continue

        # Validar longitud de fila
        required_indices = [idx for idx in (col_date, col_desc, col_amt, col_type, col_debit, col_credit) if idx is not None]
        max_req_idx = max(required_indices)
        if len(row) <= max_req_idx:
            raise ValidationError(f"Línea {row_idx}: Fila incompleta con menos columnas de las requeridas.")

        # 1. Fecha de operación
        try:
            line_date = _parse_date(row[col_date])
        except ValueError as e:
            raise ValidationError(f"Línea {row_idx}: {str(e)}")

        if min_date is None or line_date < min_date:
            min_date = line_date
        if max_date is None or line_date > max_date:
            max_date = line_date

        # 2. Fecha valor (opcional)
        line_vdate = None
        if col_vdate is not None and col_vdate < len(row):
            vdate_raw = row[col_vdate].strip()
            if vdate_raw:
                try:
                    line_vdate = _parse_date(vdate_raw)
                except ValueError:
                    line_vdate = None

        # 3. Referencia
        line_ref = ''
        if col_ref is not None and col_ref < len(row):
            line_ref = row[col_ref].strip()

        # 4. Descripción
        line_desc = row[col_desc].strip()
        if not line_desc:
            raise ValidationError(f"Línea {row_idx}: La descripción o concepto no puede estar vacía.")

        # 5. Moneda (si viene especificada)
        if col_curr is not None and col_curr < len(row):
            row_curr = row[col_curr].strip().upper()
            if row_curr and row_curr != bank_account.currency:
                raise ValidationError(
                    f"Línea {row_idx}: Moneda incompatible '{row_curr}'. "
                    f"La cuenta bancaria está configurada en '{bank_account.currency}'."
                )

        # 6. Importe y Tipo de Movimiento
        movement_type = None
        amount = None

        if has_separate_debit_credit:
            debit_raw = row[col_debit].strip() if col_debit < len(row) else ''
            credit_raw = row[col_credit].strip() if col_credit < len(row) else ''

            has_deb = bool(debit_raw and debit_raw not in ('0', '0.00', '0,00', '-'))
            has_cred = bool(credit_raw and credit_raw not in ('0', '0.00', '0,00', '-'))

            if has_deb and has_cred:
                raise ValidationError(f"Línea {row_idx}: Una fila no puede tener importe simultáneo en débito y crédito.")
            elif has_deb:
                movement_type = BankStatementLine.MovementType.DEBIT
                try:
                    amount = abs(_parse_amount(debit_raw))
                except ValueError as e:
                    raise ValidationError(f"Línea {row_idx}: {str(e)}")
            elif has_cred:
                movement_type = BankStatementLine.MovementType.CREDIT
                try:
                    amount = abs(_parse_amount(credit_raw))
                except ValueError as e:
                    raise ValidationError(f"Línea {row_idx}: {str(e)}")
            else:
                raise ValidationError(f"Línea {row_idx}: No se especificó importe en débito ni en crédito.")

        elif has_amt_and_type:
            amt_raw = row[col_amt].strip()
            type_raw = row[col_type].strip().upper()
            if not amt_raw:
                raise ValidationError(f"Línea {row_idx}: Importe vacío.")
            try:
                parsed_amt = _parse_amount(amt_raw)
            except ValueError as e:
                raise ValidationError(f"Línea {row_idx}: {str(e)}")

            if type_raw in ('DEBIT', 'DEBITO', 'CARGO', 'D', 'EGRESO', 'SALIDA'):
                movement_type = BankStatementLine.MovementType.DEBIT
                amount = abs(parsed_amt)
            elif type_raw in ('CREDIT', 'CREDITO', 'ABONO', 'C', 'INGRESO', 'ENTRADA'):
                movement_type = BankStatementLine.MovementType.CREDIT
                amount = abs(parsed_amt)
            else:
                raise ValidationError(f"Línea {row_idx}: Tipo de movimiento desconocido '{type_raw}'. Use DEBIT o CREDIT.")

        elif has_signed_amt:
            amt_raw = row[col_amt].strip()
            if not amt_raw:
                raise ValidationError(f"Línea {row_idx}: Importe vacío.")
            try:
                parsed_amt = _parse_amount(amt_raw)
            except ValueError as e:
                raise ValidationError(f"Línea {row_idx}: {str(e)}")
            if parsed_amt < Decimal('0.00'):
                movement_type = BankStatementLine.MovementType.DEBIT
                amount = abs(parsed_amt)
            elif parsed_amt > Decimal('0.00'):
                movement_type = BankStatementLine.MovementType.CREDIT
                amount = parsed_amt
            else:
                raise ValidationError(f"Línea {row_idx}: El importe no puede ser exactamente 0.00.")

        if amount is None or amount <= Decimal('0.00'):
            raise ValidationError(f"Línea {row_idx}: El importe debe ser un valor positivo mayor a 0.00.")

        # Rastrear ocurrencias de movimientos idénticos en el archivo para permitir operaciones legítimamente repetidas
        line_sig = (
            bank_account.organization_id,
            bank_account.id,
            line_date,
            movement_type,
            amount,
            (line_ref or '').strip().upper(),
            (line_desc or '').strip().upper(),
        )
        occurrence_in_file = intrafile_sig_counts.get(line_sig, 0) + 1
        intrafile_sig_counts[line_sig] = occurrence_in_file

        # Calcular hash de línea determinista incorporando la ocurrencia
        l_hash = compute_line_hash(
            org_id=bank_account.organization_id,
            bank_account_id=bank_account.id,
            post_date=line_date,
            movement_type=movement_type,
            amount=amount,
            reference=line_ref,
            description=line_desc,
            occurrence=occurrence_in_file,
        )

        if movement_type == BankStatementLine.MovementType.DEBIT:
            total_debits += amount
        else:
            total_credits += amount

        parsed_lines.append({
            'line_number': len(parsed_lines) + 1,
            'post_date': line_date,
            'value_date': line_vdate,
            'movement_type': movement_type,
            'amount': amount,
            'reference': line_ref,
            'description': line_desc,
            'line_hash': l_hash,
        })

    if not parsed_lines:
        raise ValidationError("El archivo CSV no contiene líneas de movimiento válidas.")

    return {
        'lines': parsed_lines,
        'file_hash': file_hash,
        'total_lines': len(parsed_lines),
        'total_debits': total_debits,
        'total_credits': total_credits,
        'period_start': min_date,
        'period_end': max_date,
    }


def import_bank_statement(
    organization,
    bank_account: BankAccount,
    file_content: bytes | str,
    file_name: str,
    user=None,
    initial_balance: Decimal = Decimal('0.00'),
    final_balance: Decimal = Decimal('0.00'),
    notes: str = '',
) -> BankStatement:
    """
    Importa un lote de extracto bancario en el ERP:
    - Valida pertenencia multi-tenant y estado activo de la cuenta bancaria.
    - Valida integridad, formato y columnas del archivo CSV.
    - Rechaza re-importación del mismo archivo (file_hash idéntico para la misma cuenta).
    - Rechaza movimientos con line_hash ya existente en extractos previos de la cuenta.
    - Preserva estrictamente el invariante: CERO modificación a BankAccount.current_balance.
    """
    if bank_account.organization_id != organization.id:
        raise ValidationError("La cuenta bancaria seleccionada no pertenece a la organización.")
    if not bank_account.is_active:
        raise ValidationError("La cuenta bancaria seleccionada se encuentra inactiva.")

    clean_file_name = (file_name or 'extracto_bancario.csv').strip()
    parsed = parse_and_validate_bank_statement_csv(file_content, bank_account)

    # 1. Detección de duplicado por archivo completo
    if BankStatement.objects.filter(
        organization=organization,
        bank_account=bank_account,
        file_hash=parsed['file_hash'],
    ).exists():
        raise ValidationError(
            f"El extracto bancario '{clean_file_name}' ya fue importado previamente para esta cuenta bancaria."
        )

    # 2. Detección de líneas duplicadas ya existentes en extractos anteriores
    incoming_hashes = [item['line_hash'] for item in parsed['lines']]
    existing_dup_line = BankStatementLine.objects.filter(
        organization=organization,
        bank_account=bank_account,
        line_hash__in=incoming_hashes,
    ).first()

    if existing_dup_line:
        raise ValidationError(
            f"El extracto contiene movimientos que ya fueron importados previamente en el lote {existing_dup_line.statement.statement_number}: "
            f"Fecha {existing_dup_line.post_date}, {existing_dup_line.description} por {existing_dup_line.amount} {existing_dup_line.currency}."
        )

    # 3. Creación atómica del lote y sus líneas
    with transaction.atomic():
        statement = BankStatement.objects.create(
            organization=organization,
            bank_account=bank_account,
            file_name=clean_file_name,
            file_hash=parsed['file_hash'],
            period_start=parsed['period_start'],
            period_end=parsed['period_end'],
            initial_balance=initial_balance,
            final_balance=final_balance,
            total_lines=parsed['total_lines'],
            total_debits=parsed['total_debits'],
            total_credits=parsed['total_credits'],
            status=BankStatement.Status.IMPORTED,
            imported_by=user,
            notes=notes or '',
        )

        lines_to_create = [
            BankStatementLine(
                organization=organization,
                statement=statement,
                bank_account=bank_account,
                line_number=item['line_number'],
                post_date=item['post_date'],
                value_date=item['value_date'],
                movement_type=item['movement_type'],
                amount=item['amount'],
                currency=bank_account.currency,
                reference=item['reference'],
                description=item['description'],
                line_hash=item['line_hash'],
                status=BankStatementLine.LineStatus.UNRECONCILED,
            )
            for item in parsed['lines']
        ]
        BankStatementLine.objects.bulk_create(lines_to_create)

    return statement


def run_reconciliation_matching(
    organization,
    bank_account: BankAccount,
    statement: BankStatement | None = None,
    user=None,
) -> dict:
    """
    Motor de Comparación y Matching de Conciliación Bancaria:
    Compara las líneas del extracto contra el libro mayor inmutable de tesorería (FinancialTransaction).
    
    Categorías transparentes resultantes:
    1. EXACT_MATCHES: Coincidencia unívoca (mismo monto, sentido, referencia coincidente y fecha dentro de 5 días).
    2. POTENTIAL_MATCHES: Coincidencia sugerida (mismo monto y sentido, pero referencia disconforme o múltiples candidatos).
    3. DISCREPANCIES: Referencia idéntica pero con diferencia de importe.
    4. UNMATCHED_STATEMENT: Líneas del extracto sin correspondencia en ERP (posibles comisiones, intereses, cargos directos).
    5. UNMATCHED_ERP: Transacciones ERP asentadas aún no visibles en el extracto (partidas en tránsito).
    6. ALREADY_RECONCILED: Movimientos ya conciliados previamente.
    """
    if bank_account.organization_id != organization.id:
        raise ValidationError("La cuenta bancaria no pertenece a la organización.")

    # 1. Obtener líneas de extracto no conciliadas
    lines_qs = BankStatementLine.objects.filter(
        organization=organization,
        bank_account=bank_account,
        status=BankStatementLine.LineStatus.UNRECONCILED,
    )
    if statement:
        lines_qs = lines_qs.filter(statement=statement)

    unreconciled_lines = list(lines_qs.order_by('post_date', 'line_number'))

    # 2. Obtener transacciones ERP no conciliadas para la cuenta
    unreconciled_txs = list(
        FinancialTransaction.objects.filter(
            organization=organization,
            bank_account=bank_account,
            reconciliation_match__isnull=True,
        ).order_by('created_at')
    )

    exact_matches = []
    potential_matches = []
    discrepancies = []
    unmatched_statement = []
    matched_tx_ids = set()

    for line in unreconciled_lines:
        expected_types = INFLOW_TYPES if line.movement_type == BankStatementLine.MovementType.CREDIT else OUTFLOW_TYPES
        
        # Filtrar candidatos en ERP que coincidan en cuenta, moneda, sentido, importe exacto y ventana de 15 días
        candidates = [
            tx for tx in unreconciled_txs
            if tx.id not in matched_tx_ids
            and tx.transaction_type in expected_types
            and tx.amount == line.amount
            and abs((line.post_date - tx.created_at.date()).days) <= 15
        ]

        line_ref_norm = normalize_reference(line.reference)
        is_line_ref_specific = bool(
            line_ref_norm
            and len(line_ref_norm) >= 3
            and line_ref_norm not in GENERIC_REFERENCES
        )

        exact_candidate = None
        exact_reject_reason = None

        if candidates:
            # Buscar candidatos cuya referencia normalizada sea EXACTAMENTE idéntica a la del extracto
            exact_ref_candidates = [
                cand for cand in candidates
                if is_line_ref_specific
                and normalize_reference(cand.reference_code) == line_ref_norm
                and abs((line.post_date - cand.created_at.date()).days) <= 5
            ]

            if len(exact_ref_candidates) == 1:
                # Coincidencia unívoca y estricta:
                # - Misma cuenta bancaria y organización
                # - Misma moneda
                # - Importe idéntico con precisión de centavos
                # - Sentido contable compatible
                # - Ventana de fecha <= 5 días
                # - Referencia normalizada EXACTA, no genérica y longitud >= 3
                # - Un único candidato ERP con esa referencia
                exact_candidate = exact_ref_candidates[0]
            elif len(exact_ref_candidates) > 1:
                # Ambigüedad: Múltiples transacciones ERP con la misma referencia exacta
                exact_reject_reason = (
                    f"Múltiples transacciones ERP ({len(exact_ref_candidates)}) comparten la referencia '{line.reference}'. "
                    f"Requiere desambiguación humana."
                )
            else:
                # No hay coincidencia exacta de referencia.
                # Explicar con transparencia por qué no es exacta y pasa a revisión manual
                if not is_line_ref_specific:
                    if not line_ref_norm:
                        exact_reject_reason = "El movimiento del extracto carece de referencia bancaria unívoca."
                    elif len(line_ref_norm) < 3:
                        exact_reject_reason = f"La referencia del extracto '{line.reference}' es demasiado corta para desambiguación automática."
                    else:
                        exact_reject_reason = f"La referencia del extracto '{line.reference}' es genérica o ambigua."
                elif len(candidates) == 1:
                    cand_ref = candidates[0].reference_code or 'N/A'
                    cand_ref_norm = normalize_reference(candidates[0].reference_code)
                    if cand_ref_norm and (line_ref_norm in cand_ref_norm or cand_ref_norm in line_ref_norm):
                        exact_reject_reason = f"Coincidencia parcial de referencia ('{line.reference}' vs ERP '{cand_ref}'). Requiere validación humana."
                    else:
                        exact_reject_reason = f"Las referencias difieren (Extracto '{line.reference}' vs ERP '{cand_ref}'). Requiere revisión manual."
                else:
                    exact_reject_reason = f"Existen {len(candidates)} transacciones ERP del mismo importe sin coincidencia unívoca de referencia."

        if exact_candidate:
            exact_matches.append({
                'statement_line': line,
                'financial_transaction': exact_candidate,
                'reasons': [
                    f"Importe exacto: {line.amount} {line.currency}",
                    f"Sentido coincidente: {line.get_movement_type_display()}",
                    f"Fecha con diferencia de {abs((line.post_date - exact_candidate.created_at.date()).days)} día(s)",
                    f"Referencia exacta normalizada: '{line.reference}' <-> '{exact_candidate.reference_code}'",
                ],
            })
            matched_tx_ids.add(exact_candidate.id)
        elif candidates:
            potential_matches.append({
                'statement_line': line,
                'candidate_transactions': candidates,
                'reasons': [
                    f"Importe coincidente: {line.amount} {line.currency}",
                    f"Candidatos disponibles en ventana de fechas: {len(candidates)}",
                    exact_reject_reason or "Requiere confirmación manual por ausencia de coincidencia unívoca de referencia.",
                ],
            })
        else:
            # Comprobar si hay discrepancia de importe con misma referencia exacta
            ref_same_diff_amt = None
            if is_line_ref_specific:
                ref_same_diff_amt = next(
                    (tx for tx in unreconciled_txs
                     if tx.id not in matched_tx_ids
                     and normalize_reference(tx.reference_code) == line_ref_norm),
                    None
                )

            if ref_same_diff_amt:
                discrepancies.append({
                    'statement_line': line,
                    'financial_transaction': ref_same_diff_amt,
                    'reasons': [
                        f"Referencia idéntica '{line.reference}', pero los montos difieren:",
                        f"Extracto={line.amount} {line.currency} vs ERP={ref_same_diff_amt.amount} {line.currency}",
                    ],
                })
            else:
                unmatched_statement.append({
                    'statement_line': line,
                    'reasons': ["No se encontró ninguna transacción ERP correspondiente en la ventana de fechas."],
                })

    # Partidas en tránsito ERP (no emparejadas)
    unmatched_erp = [
        tx for tx in unreconciled_txs
        if tx.id not in matched_tx_ids
    ]

    return {
        'exact_matches': exact_matches,
        'potential_matches': potential_matches,
        'discrepancies': discrepancies,
        'unmatched_statement_lines': unmatched_statement,
        'unmatched_erp_transactions': unmatched_erp,
        'summary': {
            'exact_matches_count': len(exact_matches),
            'potential_matches_count': len(potential_matches),
            'discrepancies_count': len(discrepancies),
            'unmatched_statement_count': len(unmatched_statement),
            'unmatched_erp_count': len(unmatched_erp),
        }
    }


def confirm_reconciliation_match(
    organization,
    statement_line: BankStatementLine,
    financial_transaction: FinancialTransaction,
    user=None,
    notes: str = '',
    match_type: str = BankReconciliationMatch.MatchType.EXACT,
) -> BankReconciliationMatch:
    """
    Confirma y asienta un emparejamiento de conciliación entre una línea de extracto y una transacción ERP:
    - Valida aislamiento multi-tenant estricto.
    - Valida que correspondan a la misma cuenta bancaria.
    - Valida que ninguno de los dos esté ya conciliado (previene doble conciliación).
    - Valida compatibilidad estricta de importe y sentido.
    - Preserva invariantes: CERO cambios en BankAccount.current_balance, CERO nuevas transacciones financieras.
    """
    # 1. Validaciones multi-tenant
    if statement_line.organization_id != organization.id:
        raise ValidationError("La línea del extracto no pertenece a la organización.")
    if financial_transaction.organization_id != organization.id:
        raise ValidationError("La transacción ERP no pertenece a la organización.")
    if statement_line.bank_account_id != financial_transaction.bank_account_id:
        raise ValidationError("La línea del extracto y la transacción ERP pertenecen a cuentas bancarias diferentes.")

    # 2. Validación de estado no conciliado previo
    if statement_line.status == BankStatementLine.LineStatus.RECONCILED or statement_line.matched_transaction_id is not None:
        raise ValidationError("Esta línea del extracto bancario ya se encuentra conciliada.")
    if financial_transaction.is_reconciled or (hasattr(financial_transaction, 'reconciled_statement_line') and financial_transaction.reconciled_statement_line is not None):
        raise ValidationError(f"La transacción {financial_transaction.number} ya fue conciliada previamente.")

    # 3. Validación de importe
    if statement_line.amount != financial_transaction.amount:
        raise ValidationError(
            f"Discrepancia material de importe: Extracto ({statement_line.amount}) != ERP ({financial_transaction.amount})."
        )

    # 4. Validación de sentido del flujo monetario
    expected_types = INFLOW_TYPES if statement_line.movement_type == BankStatementLine.MovementType.CREDIT else OUTFLOW_TYPES
    if financial_transaction.transaction_type not in expected_types:
        raise ValidationError(
            f"Incompatibilidad de sentido contable: {statement_line.get_movement_type_display()} no puede emparejarse con {financial_transaction.get_transaction_type_display()}."
        )

    with transaction.atomic():
        # Crear registro de auditoría de conciliación
        match = BankReconciliationMatch.objects.create(
            organization=organization,
            statement_line=statement_line,
            financial_transaction=financial_transaction,
            match_type=match_type,
            reconciled_by=user,
            notes=notes or '',
        )

        # Actualizar estado de la línea de extracto
        statement_line.status = BankStatementLine.LineStatus.RECONCILED
        statement_line.matched_transaction = financial_transaction
        statement_line.save(update_fields=['status', 'matched_transaction', 'updated_at'])

        # Actualizar estado del lote de extracto
        statement = statement_line.statement
        all_reconciled = not statement.lines.filter(
            status=BankStatementLine.LineStatus.UNRECONCILED
        ).exists()

        if all_reconciled:
            statement.status = BankStatement.Status.RECONCILED
        else:
            statement.status = BankStatement.Status.IN_PROGRESS
        statement.save(update_fields=['status', 'updated_at'])

    return match


def undo_reconciliation_match(
    organization,
    match_id: int | str,
    user=None,
    reason: str = '',
) -> None:
    """
    Deshace y revierte una conciliación confirmada previamente:
    - Valida permisos y aislamiento multi-tenant.
    - Restablece el estado de la línea de extracto a UNRECONCILED.
    - Libera la transacción ERP para futuros emparejamientos.
    - CERO alteración en BankAccount.current_balance.
    """
    try:
        match = BankReconciliationMatch.objects.select_related(
            'statement_line__statement', 'financial_transaction'
        ).get(id=match_id, organization=organization)
    except BankReconciliationMatch.DoesNotExist:
        raise ValidationError("El registro de conciliación no existe o no pertenece a la organización.")

    with transaction.atomic():
        statement_line = match.statement_line
        statement = statement_line.statement

        # Liberar línea
        statement_line.status = BankStatementLine.LineStatus.UNRECONCILED
        statement_line.matched_transaction = None
        statement_line.save(update_fields=['status', 'matched_transaction', 'updated_at'])

        # Eliminar match
        match.delete()

        # Actualizar estado del lote
        has_other_reconciled = statement.lines.filter(
            status=BankStatementLine.LineStatus.RECONCILED
        ).exists()
        if has_other_reconciled:
            statement.status = BankStatement.Status.IN_PROGRESS
        else:
            statement.status = BankStatement.Status.IMPORTED
        statement.save(update_fields=['status', 'updated_at'])


def auto_confirm_exact_matches(
    organization,
    bank_account: BankAccount,
    statement: BankStatement | None = None,
    user=None,
) -> list[BankReconciliationMatch]:
    """
    Concilia en lote y de forma atómica todas las coincidencias EXACTAS no ambiguas.
    Las coincidencias potenciales quedan intactas para revisión y decisión humana.
    """
    report = run_reconciliation_matching(
        organization=organization,
        bank_account=bank_account,
        statement=statement,
        user=user,
    )

    confirmed_matches = []
    with transaction.atomic():
        for item in report['exact_matches']:
            line = item['statement_line']
            tx = item['financial_transaction']
            match = confirm_reconciliation_match(
                organization=organization,
                statement_line=line,
                financial_transaction=tx,
                user=user,
                notes="Conciliación automática por coincidencia exacta de importe, referencia y fecha.",
                match_type=BankReconciliationMatch.MatchType.EXACT,
            )
            confirmed_matches.append(match)

    return confirmed_matches
