# -*- coding: utf-8 -*-


def migrate(cr, version):
    # Remove the obsolete Configured/Applied lifecycle state.
    cr.execute("UPDATE nsp_measurement_session SET status = 'completed' WHERE status = 'applied'")
    # workflow_status is stored. Populate it explicitly because direct SQL above
    # does not trigger the ORM compute dependency on status.
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
