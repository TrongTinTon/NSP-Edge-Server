# -*- coding: utf-8 -*-


def _table_exists(cr, table_name):
    cr.execute("SELECT to_regclass(%s)", ("public.%s" % table_name,))
    return bool(cr.fetchone()[0])


def _column_exists(cr, table_name, column_name):
    cr.execute("""
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = %s
           AND column_name = %s
    """, (table_name, column_name))
    return bool(cr.fetchone())


def _add_column(cr, table_name, column_name, definition):
    if not _column_exists(cr, table_name, column_name):
        cr.execute('ALTER TABLE "%s" ADD COLUMN "%s" %s' % (table_name, column_name, definition))


def _drop_not_null(cr, table_name, columns):
    for column in columns:
        if _column_exists(cr, table_name, column):
            cr.execute('ALTER TABLE "%s" ALTER COLUMN "%s" DROP NOT NULL' % (table_name, column))


def _delete_legacy_transition_metadata(cr):
    names = [
        'view_nsp_gate_measurement_transition_search',
        'view_nsp_gate_measurement_transition_list',
        'view_nsp_gate_measurement_transition_form',
        'action_nsp_gate_measurement_transition',
        'access_nsp_gate_measurement_transition_it',
        'access_nsp_gate_measurement_transition_operator',
    ]
    cr.execute("""
        SELECT name, model, res_id
          FROM ir_model_data
         WHERE module = 'nsp_gatekeeper'
           AND name = ANY(%s)
    """, (names,))
    rows = cr.fetchall()
    for name, model, res_id in rows:
        if model == 'ir.ui.view':
            cr.execute("DELETE FROM ir_ui_view WHERE id = %s", (res_id,))
        elif model == 'ir.actions.act_window':
            cr.execute("DELETE FROM ir_act_window WHERE id = %s", (res_id,))
        elif model == 'ir.model.access':
            cr.execute("DELETE FROM ir_model_access WHERE id = %s", (res_id,))
    cr.execute("""
        DELETE FROM ir_model_data
         WHERE module = 'nsp_gatekeeper'
           AND name = ANY(%s)
    """, (names,))



def _delete_legacy_measurement_api_actions(cr):
    names = [
        'action_core_api_nsp_gatekeeper_gate_measurement_sessions',
        'action_core_api_nsp_gatekeeper_gate_measurement_report',
        'action_core_api_nsp_gatekeeper_gate_measurement_sync',
    ]
    cr.execute("""
        SELECT res_id
          FROM ir_model_data
         WHERE module = 'nsp_gatekeeper'
           AND name = ANY(%s)
           AND model = 'ir.actions.core_api'
    """, (names,))
    action_ids = [row[0] for row in cr.fetchall()]
    if action_ids:
        if _table_exists(cr, 'core_api_endpoint'):
            cr.execute("DELETE FROM core_api_endpoint WHERE action_id = ANY(%s)", (action_ids,))
        for table_name in ('ir_actions_core_api', 'ir_act_core_api'):
            if _table_exists(cr, table_name):
                cr.execute('DELETE FROM %s WHERE id = ANY(%%s)' % table_name, (action_ids,))
        if _table_exists(cr, 'ir_actions'):
            cr.execute("DELETE FROM ir_actions WHERE id = ANY(%s)", (action_ids,))
    cr.execute("""
        DELETE FROM ir_model_data
         WHERE module = 'nsp_gatekeeper'
           AND name = ANY(%s)
    """, (names,))

def migrate(cr, version):
    _delete_legacy_transition_metadata(cr)
    _delete_legacy_measurement_api_actions(cr)

    session_table = 'nsp_gate_measurement_session'
    event_table = 'nsp_gate_measurement_event'
    run_table = 'nsp_gate_measurement_run'
    pair_table = 'nsp_gate_measurement_pair_summary'

    if not _table_exists(cr, session_table):
        return

    _add_column(cr, session_table, 'measurement_session_uid', 'varchar')
    _add_column(cr, session_table, 'measurement_code', 'varchar')
    _add_column(cr, session_table, 'planned_direction', 'varchar')
    _add_column(cr, session_table, 'planned_start_at', 'timestamp without time zone')
    _add_column(cr, session_table, 'planned_end_at', 'timestamp without time zone')
    _add_column(cr, session_table, 'objective_note', 'text')
    _add_column(cr, session_table, 'measurement_status', 'varchar')
    _add_column(cr, session_table, 'config_revision', 'integer')
    _add_column(cr, session_table, 'config_hash', 'varchar')
    _add_column(cr, session_table, 'generated_at', 'timestamp without time zone')
    _add_column(cr, session_table, 'sync_state', 'varchar')
    _add_column(cr, session_table, 'apply_status', 'varchar')
    _add_column(cr, session_table, 'applied_revision', 'integer')
    _add_column(cr, session_table, 'applied_hash', 'varchar')
    _add_column(cr, session_table, 'applied_at', 'timestamp without time zone')
    _add_column(cr, session_table, 'apply_error_code', 'varchar')
    _add_column(cr, session_table, 'apply_error_message', 'text')
    _add_column(cr, session_table, 'completed_at', 'timestamp without time zone')
    _add_column(cr, session_table, 'cancelled_at', 'timestamp without time zone')
    _add_column(cr, session_table, 'event_count', 'integer')

    # Resolve a missing legacy Controller from the old session relation or Gate relation.
    cr.execute("""
        UPDATE nsp_gate_measurement_session s
           SET controller_id = COALESCE(
               s.controller_id,
               (SELECT r.controller_id
                  FROM nsp_gate_measurement_controller_rel r
                 WHERE r.session_id = s.id
                 ORDER BY r.controller_id
                 LIMIT 1),
               (SELECT r.controller_id
                  FROM nsp_gate_controller_rel r
                 WHERE r.gate_id = s.gate_id
                 ORDER BY r.controller_id
                 LIMIT 1)
           )
         WHERE s.controller_id IS NULL
    """)

    # Remove only structurally orphaned legacy rows that cannot satisfy the new scope.
    if _table_exists(cr, event_table):
        cr.execute("""
            DELETE FROM nsp_gate_measurement_event e
             USING nsp_gate_measurement_session s
             WHERE e.session_id = s.id
               AND (s.gate_id IS NULL OR s.controller_id IS NULL)
        """)
    cr.execute("DELETE FROM nsp_gate_measurement_session WHERE gate_id IS NULL OR controller_id IS NULL")

    cr.execute("""
        UPDATE nsp_gate_measurement_session
           SET measurement_session_uid = COALESCE(NULLIF(btrim(measurement_session_uid), ''),
                                                  NULLIF(btrim(measurement_uid), ''),
                                                  'MSR-LEGACY-' || id::text),
               measurement_code = COALESCE(NULLIF(btrim(measurement_code), ''),
                                           'MSR-LEGACY-' || id::text),
               planned_direction = CASE
                   WHEN direction IN ('entry', 'exit') THEN direction
                   ELSE 'undetermined'
               END,
               measurement_status = CASE
                   WHEN state = 'draft' THEN 'draft'
                   WHEN state = 'measuring' THEN 'measuring'
                   WHEN state = 'cancelled' THEN 'cancelled'
                   ELSE 'completed'
               END,
               config_revision = COALESCE(config_revision, 0),
               sync_state = COALESCE(NULLIF(sync_state, ''), 'synced'),
               apply_status = COALESCE(NULLIF(apply_status, ''), 'pending'),
               generated_at = COALESCE(generated_at, create_date),
               completed_at = CASE
                   WHEN state IN ('reported', 'analyzed', 'applied')
                   THEN COALESCE(completed_at, ended_at, write_date)
                   ELSE completed_at
               END,
               cancelled_at = CASE
                   WHEN state = 'cancelled'
                   THEN COALESCE(cancelled_at, ended_at, write_date)
                   ELSE cancelled_at
               END,
               event_count = COALESCE(event_count, 0)
    """)

    # Ensure the new stable identifiers are unique before ORM constraints are created.
    cr.execute("""
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (PARTITION BY measurement_session_uid ORDER BY id) AS rn
              FROM nsp_gate_measurement_session
        )
        UPDATE nsp_gate_measurement_session s
           SET measurement_session_uid = s.measurement_session_uid || '-' || s.id::text
          FROM ranked r
         WHERE s.id = r.id AND r.rn > 1
    """)
    cr.execute("""
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (PARTITION BY measurement_code ORDER BY id) AS rn
              FROM nsp_gate_measurement_session
        )
        UPDATE nsp_gate_measurement_session s
           SET measurement_code = s.measurement_code || '-' || s.id::text
          FROM ranked r
         WHERE s.id = r.id AND r.rn > 1
    """)

    _drop_not_null(cr, session_table, [
        'name', 'measurement_source', 'direction', 'state', 'measurement_uid',
    ])

    # Create the Run table before the ORM enforces event.run_id as required.
    if not _table_exists(cr, run_table):
        cr.execute("""
            CREATE TABLE nsp_gate_measurement_run (
                id serial PRIMARY KEY,
                measurement_run_uid varchar,
                session_id integer,
                actual_direction varchar,
                run_status varchar,
                started_at timestamp without time zone,
                stopped_at timestamp without time zone,
                measurement_count integer DEFAULT 0,
                create_uid integer,
                create_date timestamp without time zone,
                write_uid integer,
                write_date timestamp without time zone
            )
        """)

    cr.execute("""
        INSERT INTO nsp_gate_measurement_run (
            measurement_run_uid, session_id, actual_direction, run_status,
            started_at, stopped_at, measurement_count,
            create_uid, create_date, write_uid, write_date
        )
        SELECT 'RUN-LEGACY-' || s.id::text,
               s.id,
               CASE WHEN s.planned_direction IN ('entry', 'exit') THEN s.planned_direction ELSE 'undetermined' END,
               CASE WHEN s.measurement_status = 'measuring' THEN 'running' ELSE 'stopped' END,
               s.started_at,
               COALESCE(s.ended_at, s.completed_at, s.cancelled_at),
               0,
               s.create_uid, s.create_date, s.write_uid, s.write_date
          FROM nsp_gate_measurement_session s
         WHERE NOT EXISTS (
               SELECT 1 FROM nsp_gate_measurement_run r WHERE r.session_id = s.id
         )
    """)

    if _table_exists(cr, event_table):
        _add_column(cr, event_table, 'measurement_uid', 'varchar')
        _add_column(cr, event_table, 'run_id', 'integer')
        _add_column(cr, event_table, 'read_at', 'timestamp without time zone')
        _add_column(cr, event_table, 'rssi_dbm', 'double precision')
        _add_column(cr, event_table, 'payload_hash', 'varchar')
        _add_column(cr, event_table, 'sync_state', 'varchar')
        _add_column(cr, event_table, 'retry_count', 'integer')
        _add_column(cr, event_table, 'next_retry_at', 'timestamp without time zone')
        _add_column(cr, event_table, 'last_sync_at', 'timestamp without time zone')

        cr.execute("""
            UPDATE nsp_gate_measurement_event e
               SET serial_number = COALESCE(NULLIF(btrim(e.serial_number), ''), d.serial_number),
                   antenna_no = COALESCE(NULLIF(e.antenna_no, 0), e.antenna_id),
                   measurement_uid = COALESCE(NULLIF(btrim(e.measurement_uid), ''),
                                              NULLIF(btrim(e.sample_uid), ''),
                                              'MEAS-LEGACY-' || e.id::text),
                   run_id = COALESCE(e.run_id, (
                       SELECT r.id FROM nsp_gate_measurement_run r
                        WHERE r.session_id = e.session_id
                        ORDER BY r.id LIMIT 1
                   )),
                   read_at = COALESCE(e.read_at, e.read_time),
                   rssi_dbm = COALESCE(e.rssi_dbm, e.rssi),
                   payload_hash = COALESCE(NULLIF(btrim(e.payload_hash), ''), 'legacy:' || e.id::text),
                   sync_state = COALESCE(NULLIF(e.sync_state, ''), 'synced'),
                   retry_count = COALESCE(e.retry_count, 0),
                   last_sync_at = COALESCE(e.last_sync_at, e.write_date)
              FROM nsp_device d
             WHERE e.device_id = d.id
        """)
        cr.execute("""
            UPDATE nsp_gate_measurement_event e
               SET measurement_uid = COALESCE(NULLIF(btrim(e.measurement_uid), ''), 'MEAS-LEGACY-' || e.id::text),
                   run_id = COALESCE(e.run_id, (
                       SELECT r.id FROM nsp_gate_measurement_run r
                        WHERE r.session_id = e.session_id
                        ORDER BY r.id LIMIT 1
                   )),
                   read_at = COALESCE(e.read_at, e.read_time),
                   rssi_dbm = COALESCE(e.rssi_dbm, e.rssi),
                   payload_hash = COALESCE(NULLIF(btrim(e.payload_hash), ''), 'legacy:' || e.id::text),
                   sync_state = COALESCE(NULLIF(e.sync_state, ''), 'synced'),
                   retry_count = COALESCE(e.retry_count, 0),
                   last_sync_at = COALESCE(e.last_sync_at, e.write_date)
             WHERE e.measurement_uid IS NULL OR e.run_id IS NULL OR e.read_at IS NULL
        """)
        cr.execute("""
            DELETE FROM nsp_gate_measurement_event
             WHERE session_id IS NULL
                OR run_id IS NULL
                OR serial_number IS NULL OR btrim(serial_number) = ''
                OR antenna_no IS NULL OR antenna_no <= 0
                OR tid IS NULL OR btrim(tid) = ''
                OR read_at IS NULL
        """)
        cr.execute("""
            WITH ranked AS (
                SELECT id,
                       row_number() OVER (PARTITION BY measurement_uid ORDER BY id) AS rn
                  FROM nsp_gate_measurement_event
            )
            UPDATE nsp_gate_measurement_event e
               SET measurement_uid = e.measurement_uid || '-' || e.id::text
              FROM ranked r
             WHERE e.id = r.id AND r.rn > 1
        """)
        _drop_not_null(cr, event_table, ['read_time'])

        cr.execute("""
            UPDATE nsp_gate_measurement_run r
               SET measurement_count = x.event_count
              FROM (
                    SELECT run_id, count(*)::integer AS event_count
                      FROM nsp_gate_measurement_event
                     GROUP BY run_id
              ) x
             WHERE r.id = x.run_id
        """)
        cr.execute("""
            UPDATE nsp_gate_measurement_session s
               SET event_count = x.event_count
              FROM (
                    SELECT session_id, count(*)::integer AS event_count
                      FROM nsp_gate_measurement_event
                     GROUP BY session_id
              ) x
             WHERE s.id = x.session_id
        """)

    # The old pair summary used ambiguous integer antenna IDs. It cannot be
    # migrated safely to serial_number + antenna_no and will be rebuilt from events.
    if _table_exists(cr, pair_table):
        cr.execute("TRUNCATE TABLE nsp_gate_measurement_pair_summary")
        _drop_not_null(cr, pair_table, ['from_antenna_id', 'to_antenna_id'])
