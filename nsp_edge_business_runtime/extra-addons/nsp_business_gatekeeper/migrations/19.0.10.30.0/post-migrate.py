# -*- coding: utf-8 -*-
"""Finalize Controller-boundary refactor after the new registry is loaded."""


def migrate(cr, version):
    # The cron XML historically used noupdate=1. Update the existing cron row
    # explicitly so upgraded databases stop calling the removed Device-master
    # liveness method and expire Reader Observation records instead.
    cr.execute(
        """
        SELECT data.res_id
          FROM ir_model_data data
         WHERE data.module = 'nsp_business_gatekeeper'
           AND data.name = 'ir_cron_nsp_device_report_offline'
           AND data.model = 'ir.cron'
         LIMIT 1
        """
    )
    row = cr.fetchone()
    if row:
        cr.execute("SELECT id FROM ir_model WHERE model = 'nsp.reader.observation' LIMIT 1")
        model_row = cr.fetchone()
        if model_row:
            cr.execute(
                """
                UPDATE ir_cron
                   SET model_id = %s,
                       code = 'model.cron_mark_offline_observations()'
                 WHERE id = %s
                """,
                (model_row[0], row[0]),
            )
