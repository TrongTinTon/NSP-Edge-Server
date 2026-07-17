# -*- coding: utf-8 -*-
from odoo import api, models


class NspNotificationCleanup(models.AbstractModel):
    _name = "nsp.notification.cleanup"
    _description = "NSP Notification Upgrade Cleanup"

    @api.model
    def cleanup_parking_monitor_artifacts(self):
        """Archive v5 parking monitor artifacts that were put under NSP Notification.

        Parking realtime display belongs to NSP Gatekeeper / NSP Parking Realtime.
        Notification remains responsible for user/mobile/operator notifications only.
        """
        xmlids_to_archive = [
            "nsp_notification.menu_nsp_notification_parking_monitors",
            "nsp_notification.view_nsp_parking_monitor_search",
            "nsp_notification.view_nsp_parking_monitor_list",
            "nsp_notification.view_nsp_parking_monitor_form",
        ]
        for xmlid in xmlids_to_archive:
            rec = self.env.ref(xmlid, raise_if_not_found=False)
            if rec and "active" in rec._fields:
                rec.sudo().write({"active": False})

        action = self.env.ref("nsp_notification.action_nsp_parking_monitor", raise_if_not_found=False)
        if action:
            # Leave the action record for database compatibility, but disconnect menus.
            menus = self.env["ir.ui.menu"].sudo().search([("action", "=", "ir.actions.act_window,%s" % action.id)])
            menus.write({"active": False})

        # Disable Core API routes introduced by v5 under NSP Notification.
        self.env["core.api.endpoint"].sudo().search([
            ("route_suffix", "in", ["monitor/parking/events", "monitor/config", "monitor/heartbeat"])
        ]).write({"route_active": False})
        return True

    @api.model
    def repair_parking_monitor_notification_channel(self):
        """Mark existing parking notifications as parking monitor delivery events.

        Older builds created nsp.notification rows for parking transactions before
        monitor_channel existed. Keep those rows and classify them instead of
        creating duplicate notification records.
        """
        self.env["nsp.notification"].sudo().search([
            ("parking_transaction_id", "!=", False),
            ("notification_type", "in", ["parking_entry", "parking_exit", "parking_denied"]),
            ("monitor_channel", "!=", "parking_monitor"),
        ]).write({"monitor_channel": "parking_monitor"})
        return True
