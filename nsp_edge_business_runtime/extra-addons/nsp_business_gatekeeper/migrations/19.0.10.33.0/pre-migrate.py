# -*- coding: utf-8 -*-
import logging


_logger = logging.getLogger(__name__)


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return bool(cr.fetchone()[0])


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = %s
           AND column_name = %s
         LIMIT 1
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def _rename_column(cr, table, old, new):
    if _column_exists(cr, table, old) and not _column_exists(cr, table, new):
        cr.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"')



def _rename_xmlid(cr, old_name, new_name, model=None):
    """Rename one module XML ID in place without creating duplicate metadata."""
    params = [old_name]
    model_clause = ""
    if model:
        model_clause = " AND model = %s"
        params.append(model)
    cr.execute(
        f"""
        UPDATE ir_model_data old
           SET name = %s
         WHERE module = 'nsp_business_gatekeeper'
           AND name = %s
           {model_clause}
           AND NOT EXISTS (
               SELECT 1
                 FROM ir_model_data target
                WHERE target.module = 'nsp_business_gatekeeper'
                  AND target.name = %s
           )
        """,
        [new_name, *params, new_name],
    )


def _rename_constraint(cr, table, old_name, new_name):
    cr.execute(
        """
        SELECT 1 FROM pg_constraint
         WHERE conrelid = %s::regclass AND conname = %s
        """,
        (table, old_name),
    )
    if not cr.fetchone():
        return
    cr.execute(
        """
        SELECT 1 FROM pg_constraint
         WHERE conrelid = %s::regclass AND conname = %s
        """,
        (table, new_name),
    )
    if not cr.fetchone():
        cr.execute(
            'ALTER TABLE "%s" RENAME CONSTRAINT "%s" TO "%s"'
            % (
                table.replace('"', '""'),
                old_name.replace('"', '""'),
                new_name.replace('"', '""'),
            )
        )


def _rename_field_metadata(cr, model, old_name, new_name):
    """Keep the existing ir.model.fields identity when a Python field is renamed."""
    cr.execute(
        """
        UPDATE ir_model_fields old
           SET name = %s
         WHERE model = %s
           AND name = %s
           AND NOT EXISTS (
               SELECT 1
                 FROM ir_model_fields target
                WHERE target.model = %s
                  AND target.name = %s
           )
        """,
        (new_name, model, old_name, model, new_name),
    )

def migrate(cr, version):
    """Rename the internal Parking business history model without losing data.

    The external Cloud push route may still be called `parking_transaction`; that
    is a transport compatibility name and is intentionally not migrated here.
    """
    old_table = "nsp_parking_transaction"
    new_table = "nsp_parking_log"

    old_exists = _table_exists(cr, old_table)
    new_exists = _table_exists(cr, new_table)
    if old_exists and new_exists:
        raise RuntimeError(
            "Both nsp_parking_transaction and nsp_parking_log exist. "
            "Refusing an ambiguous Parking Log migration."
        )
    if old_exists:
        cr.execute(f'ALTER TABLE "{old_table}" RENAME TO "{new_table}"')
        cr.execute(
            """
            DO $$
            BEGIN
                IF to_regclass('public.nsp_parking_transaction_id_seq') IS NOT NULL
                   AND to_regclass('public.nsp_parking_log_id_seq') IS NULL THEN
                    ALTER SEQUENCE nsp_parking_transaction_id_seq RENAME TO nsp_parking_log_id_seq;
                END IF;
            END $$
            """
        )

    if _table_exists(cr, new_table):
        _rename_column(cr, new_table, "transaction_uid", "log_uid")
        _rename_column(cr, new_table, "status", "decision")
        _rename_column(cr, new_table, "error_code", "reason_code")
        cr.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'nsp_parking_transaction_pkey')
                   AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'nsp_parking_log_pkey') THEN
                    ALTER TABLE nsp_parking_log
                    RENAME CONSTRAINT nsp_parking_transaction_pkey TO nsp_parking_log_pkey;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'nsp_parking_transaction_transaction_uid_unique')
                   AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'nsp_parking_log_log_uid_unique') THEN
                    ALTER TABLE nsp_parking_log
                    RENAME CONSTRAINT nsp_parking_transaction_transaction_uid_unique
                    TO nsp_parking_log_log_uid_unique;
                END IF;
            END $$
            """
        )
        for field_name in (
            "parking_area_id", "layout_lane_id", "lane_id",
            "vehicle_id", "user_id", "borrow_id",
        ):
            _rename_constraint(
                cr,
                new_table,
                f"nsp_parking_transaction_{field_name}_fkey",
                f"nsp_parking_log_{field_name}_fkey",
            )

    if _table_exists(cr, "nsp_parking_detection_event"):
        _rename_column(
            cr, "nsp_parking_detection_event", "transaction_id", "parking_log_id"
        )
        _rename_constraint(
            cr,
            "nsp_parking_detection_event",
            "nsp_parking_detection_event_transaction_id_fkey",
            "nsp_parking_detection_event_parking_log_id_fkey",
        )

    # Preserve the existing ir.model row so ACLs and metadata keep one identity.
    cr.execute(
        """
        UPDATE ir_model
           SET model = 'nsp.parking.log'
         WHERE model = 'nsp.parking.transaction'
           AND NOT EXISTS (
               SELECT 1 FROM ir_model WHERE model = 'nsp.parking.log'
           )
        """
    )
    # Preserve Python-model/field metadata so the ORM sees a rename, not a second
    # model with parallel fields. This is especially important for the detection
    # Many2one column, which is renamed physically above.
    cr.execute(
        """
        UPDATE ir_model_fields
           SET model = 'nsp.parking.log'
         WHERE model = 'nsp.parking.transaction'
        """
    )
    cr.execute(
        """
        UPDATE ir_model_fields
           SET relation = 'nsp.parking.log'
         WHERE relation = 'nsp.parking.transaction'
        """
    )
    for old_name, new_name in (
        ('transaction_uid', 'log_uid'),
        ('status', 'decision'),
        ('error_code', 'reason_code'),
        ('detection_event_ids', 'source_detection_ids'),
    ):
        _rename_field_metadata(cr, 'nsp.parking.log', old_name, new_name)
    _rename_field_metadata(
        cr, 'nsp.parking.detection.event', 'transaction_id', 'parking_log_id'
    )

    # Clean internal XML IDs as part of the same semantic rename. Keeping legacy
    # identifiers in source would make future maintenance ambiguous.
    xmlid_renames = (
        ('model_nsp_parking_transaction', 'model_nsp_parking_log', 'ir.model'),
        ('view_nsp_parking_transaction_search', 'view_nsp_parking_log_search', 'ir.ui.view'),
        ('view_nsp_parking_transaction_list', 'view_nsp_parking_log_list', 'ir.ui.view'),
        ('view_nsp_parking_transaction_form', 'view_nsp_parking_log_form', 'ir.ui.view'),
        ('action_nsp_parking_transaction', 'action_nsp_parking_log', 'ir.actions.act_window'),
        ('menu_nsp_transactions', 'menu_nsp_parking_logs', 'ir.ui.menu'),
        ('access_nsp_parking_transaction_it', 'access_nsp_parking_log_it', 'ir.model.access'),
        ('access_nsp_parking_transaction_operator', 'access_nsp_parking_log_operator', 'ir.model.access'),
    )
    for old_name, new_name, xml_model in xmlid_renames:
        _rename_xmlid(cr, old_name, new_name, xml_model)

    # Auto-generated field XML IDs are not required by the runtime, but renaming
    # the retained ones keeps developer metadata coherent after an upgrade.
    retained_field_renames = {
        'transaction_uid': 'log_uid',
        'status': 'decision',
        'error_code': 'reason_code',
        'detection_event_ids': 'source_detection_ids',
    }
    retained_fields = (
        'event_time', 'event_type', 'parking_area_id', 'layout_lane_id', 'lane_id',
        'layout_revision', 'vehicle_id', 'vehicle_tid', 'user_id', 'user_tid',
        'borrow_id', 'parking_area_display', 'lane_display', 'vehicle_display',
        *retained_field_renames.keys(),
    )
    for old_field in retained_fields:
        new_field = retained_field_renames.get(old_field, old_field)
        _rename_xmlid(
            cr,
            f'field_nsp_parking_transaction__{old_field}',
            f'field_nsp_parking_log__{new_field}',
            'ir.model.fields',
        )
    _rename_xmlid(
        cr,
        'field_nsp_parking_detection_event__transaction_id',
        'field_nsp_parking_detection_event__parking_log_id',
        'ir.model.fields',
    )

    if _table_exists(cr, "nsp_sync_record") and _column_exists(cr, "nsp_sync_record", "record_model"):
        cr.execute(
            """
            UPDATE nsp_sync_record
               SET record_model = 'nsp.parking.log'
             WHERE record_model = 'nsp.parking.transaction'
            """
        )

    _logger.info("NSP Parking Transaction -> Parking Log pre-migration completed")
