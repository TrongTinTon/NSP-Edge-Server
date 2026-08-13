# -*- coding: utf-8 -*-


def migrate(cr, version):
    """Remove Detection columns that became non-stored projections in 19.0.10.37.0."""
    cr.execute(
        """
        ALTER TABLE nsp_parking_detection_event
            DROP COLUMN IF EXISTS lane_id CASCADE;
        ALTER TABLE nsp_parking_detection_event
            DROP COLUMN IF EXISTS error_message;
        """
    )
