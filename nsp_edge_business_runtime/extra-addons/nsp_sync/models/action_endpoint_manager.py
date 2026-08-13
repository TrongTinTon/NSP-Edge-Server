# -*- coding: utf-8 -*-

from odoo import _, models


class ActionEndpointManager(models.Model):
    _inherit = "action.endpoint.manager"

    def action_generate_core_api_action(self):
        """Handle the NSP outbound route catalogue without fake @endpoint methods.

        T4 Core API normally generates actions by introspecting server-side
        ``@endpoint`` declarations. ``nsp.sync.job`` is deliberately an outbound
        client transport model, so the NSP Sync manager owns declarative remote
        route descriptors instead.  Keep the normal Core API behavior for every
        other endpoint manager.
        """
        self.ensure_one()
        nsp_manager = self.env.ref(
            "nsp_sync.action_endpoint_manager_nsp_sync_routes",
            raise_if_not_found=False,
        )
        if not nsp_manager or self.id != nsp_manager.id:
            return super().action_generate_core_api_action()

        resolved = self.env["nsp.sync.job"].sudo()._ensure_sync_action_definitions()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("NSP Sync Remote Routes"),
                "message": _("Outbound API Actions synchronized: %s route(s).") % len(resolved),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }
