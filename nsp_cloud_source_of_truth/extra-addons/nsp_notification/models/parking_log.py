# -*- coding: utf-8 -*-
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class ParkingLogNotification(models.Model):
    _inherit = "nsp.parking.log"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        Notification = self.env["nsp.notification"].sudo()
        for parking_log in records:
            try:
                with self.env.cr.savepoint():
                    Notification.notify_parking_log(parking_log)
            except Exception:
                # Notification delivery must never block immutable Parking Log ingest.
                _logger.exception(
                    "Unable to create Cloud parking notification for Parking Log %s",
                    parking_log.log_uid or parking_log.id,
                )
        return records
