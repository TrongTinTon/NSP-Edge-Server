# -*- coding: utf-8 -*-


def migrate(cr, version):
    """Turn Parking Detection history into a minimal pending/error working buffer."""
    cr.execute(
        """
        UPDATE nsp_parking_detection_event
           SET error_code = 'processing_error'
         WHERE state = 'error'
           AND error_code IS NULL;

        DELETE FROM nsp_parking_detection_event
         WHERE state = 'processed';

        DELETE FROM nsp_parking_detection_event
         WHERE error_code = 'layout_revision_superseded';

        DELETE FROM nsp_parking_detection_event
         WHERE error_code IS NOT NULL
           AND detected_at < (NOW() AT TIME ZONE 'UTC') - INTERVAL '24 hours';

        DROP INDEX IF EXISTS nsp_parking_detection_pending_lane_idx;
        DROP INDEX IF EXISTS nsp_parking_detection_pending_vehicle_idx;
        DROP INDEX IF EXISTS nsp_parking_detection_pending_user_idx;
        DROP INDEX IF EXISTS nsp_parking_detection_cleanup_idx;
        DROP INDEX IF EXISTS nsp_parking_detection_parking_log_idx;
        DROP INDEX IF EXISTS nsp_parking_detection_event_parking_log_id_index;
        ALTER TABLE nsp_parking_detection_event
            DROP CONSTRAINT IF EXISTS event_uid_layout_lane_unique;
        ALTER TABLE nsp_parking_detection_event
            DROP CONSTRAINT IF EXISTS nsp_parking_detection_event_event_uid_layout_lane_unique;

        ALTER TABLE nsp_parking_detection_event
            DROP COLUMN IF EXISTS state,
            DROP COLUMN IF EXISTS parking_log_id,
            DROP COLUMN IF EXISTS rssi_dbm;

        CREATE INDEX IF NOT EXISTS nsp_parking_detection_pending_lane_idx
            ON nsp_parking_detection_event (layout_lane_id, detected_at, id)
         WHERE error_code IS NULL AND layout_lane_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS nsp_parking_detection_pending_vehicle_idx
            ON nsp_parking_detection_event
               (layout_lane_id, layout_revision, tid, detected_at, id)
         WHERE error_code IS NULL AND vehicle_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS nsp_parking_detection_pending_user_idx
            ON nsp_parking_detection_event
               (layout_lane_id, layout_revision, detected_at, id)
         WHERE error_code IS NULL AND user_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS nsp_parking_detection_cleanup_idx
            ON nsp_parking_detection_event (detected_at, id)
         WHERE error_code IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS nsp_parking_detection_resolved_uid_lane_unique
            ON nsp_parking_detection_event (event_uid, layout_lane_id)
         WHERE layout_lane_id IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS nsp_parking_detection_unresolved_uid_unique
            ON nsp_parking_detection_event (event_uid)
         WHERE layout_lane_id IS NULL;
        """
    )
