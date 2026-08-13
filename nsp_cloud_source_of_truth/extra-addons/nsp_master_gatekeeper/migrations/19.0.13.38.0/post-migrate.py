# -*- coding: utf-8 -*-


def _column_exists(cr, table, column):
    cr.execute("""
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = %s
           AND column_name = %s
    """, (table, column))
    return bool(cr.fetchone())


def migrate(cr, version):
    table = "nsp_parking_log"
    cr.execute("SELECT to_regclass(%s)", (table,))
    if not cr.fetchone()[0]:
        return

    # Preserve stable business relations from older snapshot columns before
    # dropping technical/code snapshots. Each step is guarded so the migration is
    # safe after partially-cleaned deployments too.
    if _column_exists(cr, table, "parking_area_code"):
        cr.execute("""
            UPDATE nsp_parking_log l
               SET parking_area_id = a.id
              FROM nsp_parking_area a
             WHERE l.parking_area_id IS NULL
               AND l.parking_area_code IS NOT NULL
               AND upper(btrim(l.parking_area_code)) = upper(btrim(a.code))
        """)
    if _column_exists(cr, table, "lane_code"):
        cr.execute("""
            UPDATE nsp_parking_log l
               SET lane_id = lane.id
              FROM nsp_parking_lane lane
             WHERE l.lane_id IS NULL
               AND l.lane_code IS NOT NULL
               AND upper(btrim(l.lane_code)) = upper(btrim(lane.code))
        """)
    cr.execute("""
        UPDATE nsp_parking_log l
           SET layout_lane_id = ll.id
          FROM nsp_parking_layout_lane ll
         WHERE l.layout_lane_id IS NULL
           AND l.parking_area_id = ll.parking_area_id
           AND l.lane_id = ll.lane_id
    """)
    if _column_exists(cr, table, "vehicle_code"):
        cr.execute("""
            UPDATE nsp_parking_log l
               SET vehicle_id = v.id
              FROM nsp_vehicle v
             WHERE l.vehicle_id IS NULL
               AND l.vehicle_code IS NOT NULL
               AND upper(btrim(l.vehicle_code)) = upper(btrim(v.vehicle_code))
        """)
    if _column_exists(cr, table, "user_code"):
        cr.execute("""
            UPDATE nsp_parking_log l
               SET user_id = u.id
              FROM nsp_user u
             WHERE l.user_id IS NULL
               AND l.user_code IS NOT NULL
               AND upper(btrim(l.user_code)) = upper(btrim(u.user_code))
        """)
    if _column_exists(cr, table, "borrow_code"):
        cr.execute("""
            UPDATE nsp_parking_log l
               SET borrow_id = b.id
              FROM nsp_vehicle_borrow b
             WHERE l.borrow_id IS NULL
               AND l.borrow_code IS NOT NULL
               AND btrim(l.borrow_code) = btrim(b.borrow_code)
        """)

    # Cloud Parking Log intentionally stores no Reader/Controller/timing snapshots.
    cr.execute("""
        ALTER TABLE nsp_parking_log
            DROP COLUMN IF EXISTS controller_id,
            DROP COLUMN IF EXISTS controller_code,
            DROP COLUMN IF EXISTS parking_area_code,
            DROP COLUMN IF EXISTS lane_code,
            DROP COLUMN IF EXISTS sequence_path,
            DROP COLUMN IF EXISTS observed_duration_seconds,
            DROP COLUMN IF EXISTS allowed_duration_seconds,
            DROP COLUMN IF EXISTS reader_id,
            DROP COLUMN IF EXISTS serial_number,
            DROP COLUMN IF EXISTS port_no,
            DROP COLUMN IF EXISTS error_message,
            DROP COLUMN IF EXISTS vehicle_code,
            DROP COLUMN IF EXISTS license_plate,
            DROP COLUMN IF EXISTS user_code,
            DROP COLUMN IF EXISTS observed_user_codes,
            DROP COLUMN IF EXISTS observed_user_tids,
            DROP COLUMN IF EXISTS borrow_code,
            DROP COLUMN IF EXISTS create_uid,
            DROP COLUMN IF EXISTS create_date,
            DROP COLUMN IF EXISTS write_uid,
            DROP COLUMN IF EXISTS write_date
    """)

    # Remove old standalone indexes after model.init() has created only the current
    # purpose-built indexes. Constraint-owned indexes are intentionally excluded.
    cr.execute("""
        DO $$
        DECLARE idx record;
        BEGIN
            FOR idx IN
                SELECT indexname
                  FROM pg_indexes
                 WHERE schemaname = current_schema()
                   AND tablename = 'nsp_parking_log'
                   AND indexname LIKE 'nsp_parking_transaction_%'
                   AND indexname NOT IN (
                       SELECT conname
                         FROM pg_constraint
                        WHERE conrelid = 'nsp_parking_log'::regclass
                   )
            LOOP
                EXECUTE format('DROP INDEX IF EXISTS %I', idx.indexname);
            END LOOP;
        END $$
    """)
