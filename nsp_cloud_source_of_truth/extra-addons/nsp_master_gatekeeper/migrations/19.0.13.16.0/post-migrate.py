# -*- coding: utf-8 -*-
"""Migrate legacy Lane Calibration scopes/reader assemblies to Device Tree nodes."""


def _table_exists(cr, table_name):
    cr.execute("SELECT to_regclass(%s)", (table_name,))
    return bool(cr.fetchone()[0])


def migrate(cr, version):
    node_table = "nsp_measurement_device_node"
    if not _table_exists(cr, node_table):
        return

    has_server_scope = _table_exists(cr, "nsp_measurement_server_scope")
    has_controller_scope = _table_exists(cr, "nsp_measurement_controller_scope")
    has_reader_line = _table_exists(cr, "nsp_measurement_reader_line")
    has_reader_port = _table_exists(cr, "nsp_measurement_reader_port")

    # Preserve every Server ever referenced by the previous Draft structures.
    server_sources = []
    if has_server_scope:
        server_sources.append(
            "SELECT session_id, edge_server_id FROM nsp_measurement_server_scope "
            "WHERE session_id IS NOT NULL AND edge_server_id IS NOT NULL"
        )
    if has_controller_scope:
        server_sources.append(
            "SELECT session_id, edge_server_id FROM nsp_measurement_controller_scope "
            "WHERE session_id IS NOT NULL AND edge_server_id IS NOT NULL"
        )
    if has_reader_line:
        server_sources.append(
            "SELECT session_id, edge_server_id FROM nsp_measurement_reader_line "
            "WHERE session_id IS NOT NULL AND edge_server_id IS NOT NULL"
        )
    if server_sources:
        cr.execute(
            """
            INSERT INTO nsp_measurement_device_node
                (session_id, device_type, server_id, sequence)
            SELECT DISTINCT src.session_id, 'server', src.edge_server_id, 10
              FROM (%s) src
             WHERE NOT EXISTS (
                    SELECT 1
                      FROM nsp_measurement_device_node node
                     WHERE node.session_id = src.session_id
                       AND node.server_id = src.edge_server_id
             )
            """ % " UNION ".join(server_sources)
        )

    # Preserve every Controller. Existing Server association becomes parent_id;
    # this is contextual topology only and can later be changed independently.
    controller_sources = []
    if has_controller_scope:
        controller_sources.append(
            "SELECT session_id, edge_server_id, controller_id "
            "FROM nsp_measurement_controller_scope "
            "WHERE session_id IS NOT NULL AND controller_id IS NOT NULL"
        )
    if has_reader_line:
        controller_sources.append(
            "SELECT session_id, edge_server_id, controller_id "
            "FROM nsp_measurement_reader_line "
            "WHERE session_id IS NOT NULL AND controller_id IS NOT NULL"
        )
    if controller_sources:
        cr.execute(
            """
            INSERT INTO nsp_measurement_device_node
                (session_id, device_type, controller_id, parent_id, sequence)
            SELECT DISTINCT ON (src.session_id, src.controller_id)
                   src.session_id,
                   'controller',
                   src.controller_id,
                   server_node.id,
                   10
              FROM (%s) src
         LEFT JOIN nsp_measurement_device_node server_node
                ON server_node.session_id = src.session_id
               AND server_node.device_type = 'server'
               AND server_node.server_id = src.edge_server_id
             WHERE NOT EXISTS (
                    SELECT 1
                      FROM nsp_measurement_device_node node
                     WHERE node.session_id = src.session_id
                       AND node.controller_id = src.controller_id
             )
          ORDER BY src.session_id, src.controller_id, server_node.id NULLS LAST
            """ % " UNION ALL ".join(controller_sources)
        )

    # Reader configuration was contextual already. Copy it to the Reader node and
    # use the old Controller association only to initialize parent_id.
    if has_reader_line:
        cr.execute(
            """
            INSERT INTO nsp_measurement_device_node
                (session_id, device_type, reader_id, parent_id, sequence,
                 power_dbm, read_interval_ms, tid_addr, tid_len)
            SELECT line.session_id,
                   'reader',
                   line.reader_id,
                   controller_node.id,
                   10,
                   COALESCE(line.reader_power_dbm, 30),
                   COALESCE(line.read_interval_ms, 200),
                   COALESCE(line.reader_tid_addr, 0),
                   COALESCE(line.reader_tid_len, 4)
              FROM nsp_measurement_reader_line line
         LEFT JOIN nsp_measurement_device_node controller_node
                ON controller_node.session_id = line.session_id
               AND controller_node.device_type = 'controller'
               AND controller_node.controller_id = line.controller_id
             WHERE line.session_id IS NOT NULL
               AND line.reader_id IS NOT NULL
               AND NOT EXISTS (
                    SELECT 1
                      FROM nsp_measurement_device_node node
                     WHERE node.session_id = line.session_id
                       AND node.reader_id = line.reader_id
               )
            """
        )

    # Reader Port keeps its physical row/id. Attach it to the migrated Reader node.
    # The legacy reader_line_id column is intentionally left untouched by migration;
    # Odoo may drop it later after normal schema lifecycle/cleanup.
    if has_reader_line and has_reader_port:
        # reader_line_id was required in the legacy model. Keep the compatibility
        # column nullable so new Reader-node Ports can be inserted after upgrade.
        cr.execute(
            "ALTER TABLE nsp_measurement_reader_port "
            "ALTER COLUMN reader_line_id DROP NOT NULL"
        )
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

    # _parent_store is maintained by ORM for future writes. Initialize parent_path
    # for rows inserted directly by this migration.
    cr.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT id, parent_id, (id::text || '/')::varchar AS path
              FROM nsp_measurement_device_node
             WHERE parent_id IS NULL
            UNION ALL
            SELECT child.id,
                   child.parent_id,
                   (tree.path || child.id::text || '/')::varchar AS path
              FROM nsp_measurement_device_node child
              JOIN tree ON child.parent_id = tree.id
        )
        UPDATE nsp_measurement_device_node node
           SET parent_path = tree.path
          FROM tree
         WHERE node.id = tree.id
        """
    )
