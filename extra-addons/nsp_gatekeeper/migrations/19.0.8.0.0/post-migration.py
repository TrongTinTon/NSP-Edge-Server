# -*- coding: utf-8 -*-

def migrate(cr, version):
    # Move one existing child Application to the parent Local Server when the
    # parent has none, then remove all Controller-level Application links.
    cr.execute("""
        UPDATE nsp_controller AS parent
           SET core_api_application_id = source.application_id
          FROM (
                SELECT child.parent_id, MIN(child.core_api_application_id) AS application_id
                  FROM nsp_controller AS child
                 WHERE child.node_type = 'controller'
                   AND child.parent_id IS NOT NULL
                   AND child.core_api_application_id IS NOT NULL
                 GROUP BY child.parent_id
               ) AS source
         WHERE parent.id = source.parent_id
           AND parent.node_type = 'local_server'
           AND parent.core_api_application_id IS NULL
    """)
    cr.execute("""
        UPDATE nsp_controller
           SET core_api_application_id = NULL
         WHERE node_type = 'controller'
           AND core_api_application_id IS NOT NULL
    """)
