# -*- coding: utf-8 -*-


def migrate(cr, version):
    """Remove obsolete/derived Parking configuration columns in 19.0.10.38.0."""
    cr.execute(
        """
        DROP INDEX IF EXISTS nsp_parking_detection_sequence_idx;

        ALTER TABLE nsp_parking_layout_lane
            DROP COLUMN IF EXISTS tolerance_type,
            DROP COLUMN IF EXISTS tolerance_value;

        ALTER TABLE nsp_parking_layout_lane_sequence
            DROP COLUMN IF EXISTS cumulative_time;

        ALTER TABLE nsp_parking_layout_lane_reader_port
            DROP COLUMN IF EXISTS layout_lane_id,
            DROP COLUMN IF EXISTS reader_id;

        ALTER TABLE nsp_measurement_session
            DROP COLUMN IF EXISTS applied_at;

        ALTER TABLE nsp_measurement_target_line
            DROP COLUMN IF EXISTS vehicle_id CASCADE;

        ALTER TABLE nsp_parking_layout_lane_reader_config
            DROP COLUMN IF EXISTS source_type;
        """
    )
