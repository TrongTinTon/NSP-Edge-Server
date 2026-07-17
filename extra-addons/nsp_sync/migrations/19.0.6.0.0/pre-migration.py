# -*- coding: utf-8 -*-

def migrate(cr, version):
    # The new model allows one active Cloud Authentication per Local Server.
    cr.execute("""
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY local_server_id ORDER BY id) AS rn
              FROM nsp_sync_auth
             WHERE active = TRUE AND local_server_id IS NOT NULL
        )
        UPDATE nsp_sync_auth AS auth
           SET active = FALSE
          FROM ranked
         WHERE auth.id = ranked.id AND ranked.rn > 1
    """)
