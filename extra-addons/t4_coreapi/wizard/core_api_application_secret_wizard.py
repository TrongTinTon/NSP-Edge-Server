# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class CoreApiApplicationSecretWizard(models.TransientModel):
    _name = 'core.api.application.secret.wizard'
    _description = 'Application Credentials (shown once)'

    application_id = fields.Many2one('core.api.application', required=True, ondelete='cascade')
    client_id = fields.Char(readonly=True)
    client_secret = fields.Char(readonly=True)

    def unlink(self):
        """Refresh the application form when the popup is closed."""
        apps = self.application_id
        res = super().unlink()
        for app in apps:
            app._notify_application_form_reload()
        return res

    def action_confirm(self):
        """Mark credentials as viewed, refresh the form, and close the popup."""
        self.ensure_one()
        application = self.application_id
        application.sudo().write({'credentials_pending': False})
        application._clear_pending_secret()
        application._notify_application_form_reload()
        return {'type': 'ir.actions.act_window_close'}
