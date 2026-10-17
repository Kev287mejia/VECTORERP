# Generated manually for Phase 1C.8.3C: Database-level Immutability Triggers for Journal Entries

from django.db import migrations


def create_immutability_triggers(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return

    with schema_editor.connection.cursor() as cursor:
        # 1. Trigger Function for JournalEntry (finance_journalentry)
        cursor.execute("""
            CREATE OR REPLACE FUNCTION check_journal_entry_immutability()
            RETURNS TRIGGER AS $$
            BEGIN
                -- Prohibir eliminación de pólizas balanceadas
                IF (TG_OP = 'DELETE') THEN
                    IF (OLD.is_balanced = TRUE) THEN
                        RAISE EXCEPTION 'ERP VECTOR (Integridad Contable): No se permite eliminar la póliza contable balanceada/cerrada (ID: %, Folio: %).',
                            OLD.id, OLD.number
                            USING ERRCODE = 'check_violation';
                    END IF;
                    RETURN OLD;
                END IF;

                -- Prohibir actualización de pólizas que ya estaban balanceadas
                IF (TG_OP = 'UPDATE') THEN
                    IF (OLD.is_balanced = TRUE) THEN
                        RAISE EXCEPTION 'ERP VECTOR (Integridad Contable): No se permite modificar la póliza contable balanceada/cerrada (ID: %, Folio: %).',
                            OLD.id, OLD.number
                            USING ERRCODE = 'check_violation';
                    END IF;
                    -- Si OLD.is_balanced = FALSE, se permite actualizar (incluyendo el cierre pasando a is_balanced = TRUE)
                    RETURN NEW;
                END IF;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)

        cursor.execute("""
            DROP TRIGGER IF EXISTS trg_prevent_journal_entry_mutation ON finance_journalentry;
            CREATE TRIGGER trg_prevent_journal_entry_mutation
            BEFORE UPDATE OR DELETE ON finance_journalentry
            FOR EACH ROW
            EXECUTE FUNCTION check_journal_entry_immutability();
        """)

        # 2. Trigger Function for JournalEntryLine (finance_journalentryline)
        cursor.execute("""
            CREATE OR REPLACE FUNCTION check_journal_entry_line_immutability()
            RETURNS TRIGGER AS $$
            DECLARE
                v_parent_balanced BOOLEAN;
                v_new_parent_balanced BOOLEAN;
            BEGIN
                -- INSERT: Comprobar que la póliza destino no esté balanceada
                IF (TG_OP = 'INSERT') THEN
                    SELECT is_balanced INTO v_parent_balanced
                    FROM finance_journalentry
                    WHERE id = NEW.entry_id;

                    IF (v_parent_balanced IS TRUE) THEN
                        RAISE EXCEPTION 'ERP VECTOR (Integridad Contable): No se permite insertar líneas en la póliza contable balanceada/cerrada (Entry ID: %).',
                            NEW.entry_id
                            USING ERRCODE = 'check_violation';
                    END IF;
                    RETURN NEW;
                END IF;

                -- UPDATE: Comprobar que la póliza original y la destino no estén balanceadas
                IF (TG_OP = 'UPDATE') THEN
                    SELECT is_balanced INTO v_parent_balanced
                    FROM finance_journalentry
                    WHERE id = OLD.entry_id;

                    IF (v_parent_balanced IS TRUE) THEN
                        RAISE EXCEPTION 'ERP VECTOR (Integridad Contable): No se permite modificar una línea perteneciente a la póliza balanceada/cerrada (Line ID: %, Entry ID: %).',
                            OLD.id, OLD.entry_id
                            USING ERRCODE = 'check_violation';
                    END IF;

                    IF (NEW.entry_id IS DISTINCT FROM OLD.entry_id) THEN
                        SELECT is_balanced INTO v_new_parent_balanced
                        FROM finance_journalentry
                        WHERE id = NEW.entry_id;

                        IF (v_new_parent_balanced IS TRUE) THEN
                            RAISE EXCEPTION 'ERP VECTOR (Integridad Contable): No se permite transferir una línea a una póliza balanceada/cerrada (Line ID: %, Target Entry ID: %).',
                                OLD.id, NEW.entry_id
                                USING ERRCODE = 'check_violation';
                        END IF;
                    END IF;
                    RETURN NEW;
                END IF;

                -- DELETE: Comprobar que la póliza a la que pertenece la línea no esté balanceada
                IF (TG_OP = 'DELETE') THEN
                    SELECT is_balanced INTO v_parent_balanced
                    FROM finance_journalentry
                    WHERE id = OLD.entry_id;

                    IF (v_parent_balanced IS TRUE) THEN
                        RAISE EXCEPTION 'ERP VECTOR (Integridad Contable): No se permite eliminar una línea perteneciente a la póliza balanceada/cerrada (Line ID: %, Entry ID: %).',
                            OLD.id, OLD.entry_id
                            USING ERRCODE = 'check_violation';
                    END IF;
                    RETURN OLD;
                END IF;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)

        cursor.execute("""
            DROP TRIGGER IF EXISTS trg_prevent_journal_entry_line_mutation ON finance_journalentryline;
            CREATE TRIGGER trg_prevent_journal_entry_line_mutation
            BEFORE INSERT OR UPDATE OR DELETE ON finance_journalentryline
            FOR EACH ROW
            EXECUTE FUNCTION check_journal_entry_line_immutability();
        """)


def drop_immutability_triggers(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return

    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DROP TRIGGER IF EXISTS trg_prevent_journal_entry_line_mutation ON finance_journalentryline;")
        cursor.execute("DROP FUNCTION IF EXISTS check_journal_entry_line_immutability();")
        cursor.execute("DROP TRIGGER IF EXISTS trg_prevent_journal_entry_mutation ON finance_journalentry;")
        cursor.execute("DROP FUNCTION IF EXISTS check_journal_entry_immutability();")


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0012_alter_journalentryline_entry_and_petty_cash_constraints'),
    ]

    operations = [
        migrations.RunPython(
            create_immutability_triggers,
            drop_immutability_triggers,
        ),
    ]
