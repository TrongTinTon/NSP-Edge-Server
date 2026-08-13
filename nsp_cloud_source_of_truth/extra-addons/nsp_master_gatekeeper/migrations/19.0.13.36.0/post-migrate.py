# -*- coding: utf-8 -*-


def migrate(cr, version):
    # Timing tolerance was removed from the Parking Layout runtime contract.
    cr.execute("ALTER TABLE nsp_parking_layout_lane DROP COLUMN IF EXISTS tolerance_type")
    cr.execute("ALTER TABLE nsp_parking_layout_lane DROP COLUMN IF EXISTS tolerance_value")
    # Calibration result no longer carries a tolerance knob; it only reports observed timing statistics.
    cr.execute("ALTER TABLE nsp_measurement_result DROP COLUMN IF EXISTS tolerance_percent")
