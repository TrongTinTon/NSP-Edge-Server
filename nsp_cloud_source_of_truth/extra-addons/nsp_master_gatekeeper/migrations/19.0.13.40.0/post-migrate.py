# -*- coding: utf-8 -*-


def migrate(cr, version):
    # No data migration is required. This version fixes Lane Calibration
    # event datetime normalization during idempotent Edge -> Cloud retries.
    return
