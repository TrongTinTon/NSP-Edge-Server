# -*- coding: utf-8 -*-
"""Remove Reader master columns retired from the NSP device contract."""


def migrate(cr, version):
    # These fields are intentionally removed from nsp.device. Runtime Reader
    # identity uses the master serial_number only; physical connection and
    # antenna topology are not properties of the Reader master anymore.
    cr.execute(
        """
        ALTER TABLE nsp_device
            DROP COLUMN IF EXISTS runtime_detected_serial_number,
            DROP COLUMN IF EXISTS connection_type
        """
    )
