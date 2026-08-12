# -*- coding: utf-8 -*-


def migrate(cr, version):
    # Controller event_uid identifies one physical read. The same physical read
    # may now be projected into multiple logical Lanes, so idempotency is scoped
    # by (event_uid, lane_id).
    cr.execute("""
        ALTER TABLE nsp_parking_detection_event
        DROP CONSTRAINT IF EXISTS nsp_parking_detection_event_event_uid_unique
    """)
