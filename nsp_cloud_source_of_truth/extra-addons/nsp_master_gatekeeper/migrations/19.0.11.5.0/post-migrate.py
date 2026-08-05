# -*- coding: utf-8 -*-


def migrate(cr, version):
    """Remove obsolete Calibration references from operational Lanes.

    Lane Timeline and event sequences are self-contained. Calibration sessions
    remain temporary acquisition data and may be deleted independently.
    """
    cr.execute(
        """
        ALTER TABLE nsp_parking_lane
            DROP COLUMN IF EXISTS calibration_source_id CASCADE,
            DROP COLUMN IF EXISTS calibration_result_id CASCADE
        """
    )
