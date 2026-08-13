# -*- coding: utf-8 -*-


def migrate(cr, version):
    # Applied/Configured belongs to downstream Lane/Parking configuration, not the
    # Lane Calibration lifecycle. Historical rows become Completed.
    cr.execute("UPDATE nsp_measurement_session SET status = 'completed' WHERE status = 'applied'")
    # workflow_status is stored. Populate it explicitly because post-migrate SQL
    # changes do not trigger the ORM compute dependency on status.
    cr.execute(
        """
        UPDATE nsp_measurement_session
           SET workflow_status = CASE status
               WHEN 'draft' THEN 'draft'
               WHEN 'ready' THEN 'released'
               WHEN 'running' THEN 'running'
               WHEN 'completed' THEN 'completed'
               ELSE 'stopped'
           END
        """
    )
    cr.execute("ALTER TABLE IF EXISTS nsp_measurement_session DROP COLUMN IF EXISTS applied_at")
