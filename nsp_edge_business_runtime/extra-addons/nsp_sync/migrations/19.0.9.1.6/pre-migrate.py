# -*- coding: utf-8 -*-


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    row = cr.fetchone()
    return bool(row and row[0])


def migrate(cr, version):
    """Adopt a pre-created Friendship route before XML data is loaded.

    The Edge sync catalogue is self-healing. After new Python code is copied and
    Odoo is restarted, the scheduler/auth flow can create the new Friendship
    ``ir.actions.core_api`` descriptor before the module data upgrade runs. That
    record has no ``nsp_sync.api_friendships`` external ID yet. Loading
    ``sync_route_definitions.xml`` would then try to create a second descriptor
    with the same route/endpoint code and hit the Core API uniqueness constraint.

    Bind the existing outbound descriptor to the canonical external ID first so
    the normal XML loader updates that record instead of creating a duplicate.
    """
    required = ("ir_model", "ir_model_data", "ir_actions_core_api")
    if not all(_table_exists(cr, table) for table in required):
        return

    cr.execute("SELECT id FROM ir_model WHERE model = 'nsp.sync.job' LIMIT 1")
    row = cr.fetchone()
    if not row:
        return
    sync_job_model_id = row[0]

    # Already bound: the XML loader will update the referenced action normally.
    cr.execute(
        """
        SELECT res_id
          FROM ir_model_data
         WHERE module = 'nsp_sync'
           AND name = 'api_friendships'
           AND model = 'ir.actions.core_api'
         LIMIT 1
        """
    )
    if cr.fetchone():
        return

    cr.execute(
        """
        SELECT action.id
          FROM ir_actions_core_api action
         WHERE action.model_id = %s
           AND (
                trim(BOTH '/' FROM COALESCE(action.route_suffix, '')) = 'edge/friendships/snapshot'
                OR COALESCE(action.endpoint_code, '') = 'nsp_edge_friendships_snapshot'
           )
         ORDER BY action.id
         LIMIT 1
        """,
        (sync_job_model_id,),
    )
    row = cr.fetchone()
    if not row:
        return

    cr.execute(
        """
        INSERT INTO ir_model_data (module, name, model, res_id, noupdate)
        VALUES ('nsp_sync', 'api_friendships', 'ir.actions.core_api', %s, FALSE)
        """,
        (row[0],),
    )
