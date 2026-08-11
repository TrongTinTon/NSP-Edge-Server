# -*- coding: utf-8 -*-
"""Migrate Edge Lane Calibration from Reader Assembly rows to flat Device Tree nodes."""


def _table_exists(cr, table_name):
    cr.execute("SELECT to_regclass(%s)", (table_name,))
    return bool(cr.fetchone()[0])


def _column_exists(cr, table_name, column_name):
    cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
        """,
        (table_name, column_name),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    if _table_exists(cr, "nsp_device") and _column_exists(cr, "nsp_device", "connection_type"):
        cr.execute("ALTER TABLE nsp_device DROP COLUMN IF EXISTS connection_type")

    if not _table_exists(cr, "nsp_measurement_device_node"):
        return
    if not _table_exists(cr, "nsp_measurement_reader_line"):
        return

    # One contextual Server node per old session/server pair.
    cr.execute(
        """
        INSERT INTO nsp_measurement_device_node
            (session_id, source_node_id, device_type, server_id, sequence,
             power_dbm, read_interval_ms, tid_addr, tid_len)
        SELECT DISTINCT line.session_id,
               'legacy-server-' || line.edge_server_id::text,
               'server', line.edge_server_id, 10, 30, 200, 0, 4
          FROM nsp_measurement_reader_line line
         WHERE line.session_id IS NOT NULL AND line.edge_server_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM nsp_measurement_device_node node
                WHERE node.session_id = line.session_id
                  AND node.device_type = 'server'
                  AND node.server_id = line.edge_server_id
           )
        """
    )

    # One Controller node per old session/controller pair, parented to its old Server.
    cr.execute(
        """
        INSERT INTO nsp_measurement_device_node
            (session_id, source_node_id, device_type, controller_id, parent_id, sequence,
             power_dbm, read_interval_ms, tid_addr, tid_len)
        SELECT DISTINCT ON (line.session_id, line.controller_id)
               line.session_id,
               'legacy-controller-' || line.controller_id::text,
               'controller', line.controller_id, server_node.id, 10, 30, 200, 0, 4
          FROM nsp_measurement_reader_line line
          JOIN nsp_measurement_device_node server_node
            ON server_node.session_id = line.session_id
           AND server_node.device_type = 'server'
           AND server_node.server_id = line.edge_server_id
         WHERE line.session_id IS NOT NULL AND line.controller_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM nsp_measurement_device_node node
                WHERE node.session_id = line.session_id
                  AND node.device_type = 'controller'
                  AND node.controller_id = line.controller_id
           )
         ORDER BY line.session_id, line.controller_id, line.id
        """
    )

    # Preserve contextual Reader configuration and attach it to the Controller node.
    cr.execute(
        """
        INSERT INTO nsp_measurement_device_node
            (session_id, source_node_id, device_type, reader_id, parent_id, sequence,
             power_dbm, read_interval_ms, tid_addr, tid_len)
        SELECT line.session_id,
               'legacy-reader-line-' || line.id::text,
               'reader', line.reader_id, controller_node.id, 10,
               COALESCE(line.reader_power_dbm, 30),
               COALESCE(line.read_interval_ms, 200),
               COALESCE(line.reader_tid_addr, 0),
               COALESCE(line.reader_tid_len, 4)
          FROM nsp_measurement_reader_line line
          JOIN nsp_measurement_device_node controller_node
            ON controller_node.session_id = line.session_id
           AND controller_node.device_type = 'controller'
           AND controller_node.controller_id = line.controller_id
         WHERE line.session_id IS NOT NULL AND line.reader_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM nsp_measurement_device_node node
                WHERE node.session_id = line.session_id
                  AND node.device_type = 'reader'
                  AND node.reader_id = line.reader_id
           )
        """
    )

    # Rebind existing Reader Port rows to their migrated Reader node. Keep the old
    # reader_line_id database column untouched for safe rolling upgrades.
    if _table_exists(cr, "nsp_measurement_reader_port") and _column_exists(
        cr, "nsp_measurement_reader_port", "reader_node_id"
    ):
        cr.execute(
            """
            UPDATE nsp_measurement_reader_port port
               SET reader_node_id = node.id
              FROM nsp_measurement_reader_line line,
                   nsp_measurement_device_node node
             WHERE port.reader_line_id = line.id
               AND node.session_id = line.session_id
               AND node.device_type = 'reader'
               AND node.reader_id = line.reader_id
               AND port.reader_node_id IS NULL
            """
        )
