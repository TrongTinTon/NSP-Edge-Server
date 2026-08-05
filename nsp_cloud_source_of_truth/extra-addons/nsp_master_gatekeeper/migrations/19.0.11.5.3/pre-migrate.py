# -*- coding: utf-8 -*-


def migrate(cr, version):
    """Allow the Apply Configuration wizard to open before destination selection.

    The wizard record and its selected timeline lines are created before the
    modal is displayed. Parking Layout and Lane are therefore intentionally
    selected by the user after creation and validated when Save is pressed.
    """
    cr.execute(
        """
        ALTER TABLE IF EXISTS nsp_measurement_apply_lane_wizard
            ALTER COLUMN parking_area_id DROP NOT NULL,
            ALTER COLUMN lane_id DROP NOT NULL
        """
    )
