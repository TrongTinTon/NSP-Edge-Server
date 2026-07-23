# -*- coding: utf-8 -*-
"""One-time upgrade bridge from the retired borrow-request workflow.

Fresh installs do not use the old model. This migration exists only so an
existing NSP database can upgrade without leaving actions/sequences/data tied
to ``nsp.vehicle.borrow.request``.
"""


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", ("public.%s" % table,))
    return bool(cr.fetchone()[0])


def migrate(cr, version):
    old_table = "nsp_vehicle_borrow_request"
    new_table = "nsp_vehicle_borrow"
    old_exists = _table_exists(cr, old_table)
    new_exists = _table_exists(cr, new_table)

    # Normal upgrade path: preserve IDs and all common business columns by
    # renaming the physical table before the new model initializes it.
    if old_exists and not new_exists:
        cr.execute('ALTER TABLE "%s" RENAME TO "%s"' % (old_table, new_table))
        old_exists = False
        new_exists = True

    # Recovery path for a database where a prior failed upgrade already
    # created the new table. Preserve business records without copying the old
    # approval-only workflow fields.
    if old_exists and new_exists:
        cr.execute(
            """
            INSERT INTO nsp_vehicle_borrow
                (borrow_code, vehicle_id, borrower_id, valid_from, valid_to,
                 state, returned_at, create_uid, create_date, write_uid, write_date)
            SELECT old.borrow_code,
                   old.vehicle_id,
                   old.borrower_id,
                   old.valid_from,
                   old.valid_to,
                   CASE old.state
                       WHEN 'approved' THEN 'active'
                       WHEN 'returned' THEN 'returned'
                       ELSE 'cancelled'
                   END,
                   old.returned_at,
                   old.create_uid,
                   old.create_date,
                   old.write_uid,
                   old.write_date
              FROM nsp_vehicle_borrow_request old
             WHERE old.borrow_code IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM nsp_vehicle_borrow new
                    WHERE new.borrow_code = old.borrow_code
               )
            """
        )

    # Translate the old approval states when the table was renamed in place.
    if new_exists:
        cr.execute(
            """
            UPDATE nsp_vehicle_borrow
               SET state = CASE state
                   WHEN 'approved' THEN 'active'
                   WHEN 'returned' THEN 'returned'
                   WHEN 'cancelled' THEN 'cancelled'
                   ELSE 'cancelled'
               END
             WHERE state IN ('draft','waiting','approved','returned','rejected','cancelled')
            """
        )

    # The XML ID is intentionally kept stable across the upgrade, while the
    # sequence code follows the new technical model used by create().
    cr.execute(
        "UPDATE ir_sequence SET code = %s, name = %s WHERE code = %s",
        ("nsp.vehicle.borrow", "NSP Vehicle Borrow", "nsp.vehicle.borrow.request"),
    )

    # Repair stale window actions immediately, before users can hit a removed
    # res_model after the service restarts.
    cr.execute(
        "UPDATE ir_act_window SET res_model = %s WHERE res_model = %s",
        ("nsp.vehicle.borrow", "nsp.vehicle.borrow.request"),
    )
