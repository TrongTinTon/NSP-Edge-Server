# -*- coding: utf-8 -*-
"""Remove persisted Server->Controller->Reader ownership from Edge master identities."""


def _table(cr, name):
    cr.execute("SELECT to_regclass(%s)", ("public.%s" % name,))
    return bool(cr.fetchone()[0])


def _column(cr, table, name):
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public' AND table_name=%s AND column_name=%s
        """,
        (table, name),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    # Preserve last physical Reader evidence before legacy runtime fields leave
    # Reader Master. Observation is contextual to Controller+SDK Serial and is the
    # correct owner of these facts.
    if (
        _table(cr, "nsp_device")
        and _table(cr, "nsp_reader_observation")
        and _column(cr, "nsp_device", "controller_id")
    ):
        cols = {
            name: _column(cr, "nsp_device", name)
            for name in (
                "status", "last_seen", "firmware_version", "runtime_power_dbm",
                "runtime_read_interval_ms", "runtime_ports_json",
            )
        }
        status_expr = "COALESCE(device.status, 'offline')" if cols["status"] else "'offline'"
        last_seen_expr = "device.last_seen" if cols["last_seen"] else "NULL"
        firmware_expr = "device.firmware_version" if cols["firmware_version"] else "NULL"
        power_expr = "device.runtime_power_dbm" if cols["runtime_power_dbm"] else "NULL"
        interval_expr = "device.runtime_read_interval_ms" if cols["runtime_read_interval_ms"] else "NULL"
        ports_expr = "COALESCE(device.runtime_ports_json, '[]')" if cols["runtime_ports_json"] else "'[]'"
        cr.execute(
            f"""
            INSERT INTO nsp_reader_observation
                (controller_id, serial_number, status, last_seen_at,
                 last_reported_at, firmware_version, power_dbm,
                 read_interval_ms, ports_json, create_date, write_date)
            SELECT device.controller_id,
                   UPPER(TRIM(device.serial_number)),
                   {status_expr},
                   {last_seen_expr},
                   COALESCE({last_seen_expr}, NOW()),
                   {firmware_expr},
                   {power_expr},
                   {interval_expr},
                   {ports_expr},
                   NOW(), NOW()
              FROM nsp_device device
             WHERE device.controller_id IS NOT NULL
               AND COALESCE(TRIM(device.serial_number), '') <> ''
            ON CONFLICT (controller_id, serial_number) DO UPDATE SET
                status = EXCLUDED.status,
                last_seen_at = COALESCE(EXCLUDED.last_seen_at, nsp_reader_observation.last_seen_at),
                last_reported_at = GREATEST(
                    EXCLUDED.last_reported_at,
                    nsp_reader_observation.last_reported_at
                ),
                firmware_version = COALESCE(EXCLUDED.firmware_version, nsp_reader_observation.firmware_version),
                power_dbm = COALESCE(EXCLUDED.power_dbm, nsp_reader_observation.power_dbm),
                read_interval_ms = COALESCE(EXCLUDED.read_interval_ms, nsp_reader_observation.read_interval_ms),
                ports_json = CASE
                    WHEN EXCLUDED.ports_json IS NOT NULL AND EXCLUDED.ports_json <> '[]'
                    THEN EXCLUDED.ports_json
                    ELSE nsp_reader_observation.ports_json
                END,
                write_date = NOW()
            """
        )

    # Master identities are deliberately independent. Contextual relationships are
    # already stored by nsp.parking.layout.lane and nsp.measurement.device.node.
    if _table(cr, "nsp_controller") and _column(cr, "nsp_controller", "edge_server_id"):
        cr.execute("ALTER TABLE nsp_controller DROP COLUMN edge_server_id CASCADE")

    if _table(cr, "nsp_device"):
        for column in (
            "controller_id",
            "status",
            "last_seen",
            "firmware_version",
            "runtime_power_dbm",
            "runtime_read_interval_ms",
            "runtime_ports_json",
            "power_dbm",
            "read_interval_ms",
            "tid_addr",
            "tid_len",
        ):
            if _column(cr, "nsp_device", column):
                cr.execute('ALTER TABLE nsp_device DROP COLUMN "%s" CASCADE' % column)
