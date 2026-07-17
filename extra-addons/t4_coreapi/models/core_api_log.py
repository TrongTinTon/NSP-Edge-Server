# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models


class CoreApiLog(models.Model):
    _name = 'core.api.log'
    _description = 'Core API Request Log'
    _order = 'create_date desc'

    application_id = fields.Many2one('core.api.application', index=True, ondelete='set null')
    client_id = fields.Char(index=True)
    client_instance_id = fields.Char(index=True)
    token_id = fields.Many2one('core.api.token', index=True, ondelete='set null')
    event_type = fields.Selection(
        [('auth', 'Authentication'), ('api', 'API Call')],
        required=True,
        index=True,
    )
    route = fields.Char(required=True, index=True)
    method = fields.Char()
    ip_address = fields.Char(index=True)
    status_code = fields.Integer()
    success = fields.Boolean()
    duration_ms = fields.Float()
    error_message = fields.Text()
    user_agent = fields.Char()

    @api.model
    def log_event(
        self,
        event_type,
        route,
        method,
        ip_address,
        status_code,
        success,
        application=None,
        token=None,
        client_instance_id=None,
        duration_ms=0,
        error_message=None,
        user_agent=None,
    ):
        """Create one audit log row for an auth or API request."""
        return self.sudo().create({
            'application_id': application.id if application else False,
            'client_id': application.client_id if application else False,
            'token_id': token.id if token else False,
            'client_instance_id': client_instance_id or (token.client_instance_id if token else False),
            'event_type': event_type,
            'route': route,
            'method': method,
            'ip_address': ip_address,
            'status_code': status_code,
            'success': success,
            'duration_ms': duration_ms,
            'error_message': error_message,
            'user_agent': user_agent,
        })

    @api.model
    def count_recent(self, domain_extra, minutes=1):
        """Count log rows in the last N minutes for rate limiting."""
        domain = [
            ('create_date', '>=', fields.Datetime.subtract(fields.Datetime.now(), minutes=minutes)),
        ] + domain_extra
        return self.sudo().search_count(domain)

    @api.autovacuum
    def _gc_old_logs(self):
        """Delete request logs older than 90 days."""
        limit = fields.Datetime.subtract(fields.Datetime.now(), days=90)
        old = self.sudo().search([('create_date', '<', limit)])
        if old:
            old.unlink()
