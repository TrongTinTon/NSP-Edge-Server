# -*- coding: utf-8 -*-
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class ParkingTransactionNotification(models.Model):
    _inherit = "nsp.parking.transaction"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        Notification = self.env["nsp.notification"].sudo()
        for transaction in records:
            try:
                with self.env.cr.savepoint():
                    Notification.notify_parking_transaction(transaction)
            except Exception:
                # Notification delivery must never block the parking transaction.
                _logger.exception(
                    "Unable to create Cloud parking notification for transaction %s",
                    transaction.transaction_uid or transaction.id,
                )
        return records
