# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class NspController(models.Model):
    _name = 'nsp.controller'
    _table = 'nsp_controller'
    _description = 'NSP Infrastructure Node'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _rec_name = 'controller_id'
    _order = 'parent_path, controller_name, id'
    _parent_name = 'parent_id'
    _parent_store = True

    controller_id = fields.Char(
        string='Server Code', required=True, index=True, tracking=True,
        help='Stable NSP node code. This is the operational identity of the Edge Server or Controller and is independent from the shared Core API Application Server Code.',
    )
    controller_name = fields.Char(string='Controller Name', default='NSP Gatekeeper Controller', tracking=True)
    node_type = fields.Selection([
        ('edge_server', 'Edge Server'),
        ('controller', 'Controller'),
    ], string='Node Type', required=True, default='controller', index=True, tracking=True)
    branch_id = fields.Many2one(
        'nsp.branch', string='Branch / Site', index=True, ondelete='restrict', tracking=True,
        help='Site/Branch where this Edge Server or Controller operates. Child Controllers inherit the Branch from their Edge Server parent when left blank.',
    )

    parent_id = fields.Many2one(
        'nsp.controller', string='Edge Server', index=True, ondelete='restrict', tracking=True,
        domain="[('node_type', '=', 'edge_server'), ('id', '!=', id)]",
        help='Edge Server that manages this Controller.',
    )
    child_ids = fields.One2many('nsp.controller', 'parent_id', string='Managed Controllers')
    parent_path = fields.Char(index=True)
    child_count = fields.Integer(compute='_compute_child_count')

    url = fields.Char(string='Controller API URL', help='Optional endpoint used by the parent server to call this node.')
    api_key_hash = fields.Char(string='Hashed API Key', copy=False, groups='base.group_system')
    session_token = fields.Char(string='Session Token', copy=False, groups='base.group_system')
    session_expiry = fields.Datetime(string='Session Expiry', copy=False)

    connected = fields.Boolean(string='Connected', default=False, index=True)
    timestamp = fields.Datetime(string='Last Heartbeat')
    active = fields.Boolean(string='Active', default=True, index=True)
    status = fields.Selection([
        ('online', 'Online'),
        ('offline', 'Offline'),
        ('block', 'Blocked'),
        ('revoked', 'Revoked'),
        ('error', 'Error'),
    ], string='Status', default='offline', index=True, tracking=True)
    last_error = fields.Text(string='Last Error', readonly=True, copy=False)

    core_api_application_id = fields.Many2one(
        'core.api.application', string='Core API Application', copy=False, index=True,
        ondelete='set null', tracking=True, domain="[('state', '=', 'active')]",
        help='Cloud Core API credential owned by an Edge Server. Controllers inherit API scope from their parent Edge Server.',
    )
    application_client_id = fields.Char(related='core_api_application_id.client_id', string='Client ID', readonly=True)
    application_server_code = fields.Char(related='core_api_application_id.service_code', string='Application Server Code', readonly=True)


    device_id = fields.One2many('nsp.device', 'controller_id', string='Managed Devices')
    gate_m2m_ids = fields.Many2many(
        'nsp.gate', 'nsp_gate_controller_rel', 'controller_id', 'gate_id',
        string='Gate Memberships', readonly=True,
    )
    last_device_report_at = fields.Datetime(string='Last Device Report')
    paired_machine_id = fields.Char(
        string='Paired Machine ID', readonly=True, copy=False, index=True,
        help='Machine identity delivered through an approved Controller Pairing Request.',
    )
    paired_at = fields.Datetime(string='Paired At', readonly=True, copy=False)
    pairing_request_ids = fields.One2many(
        'nsp.controller.pairing.request', 'controller_id', string='Pairing Requests', readonly=True,
    )

    _sql_constraints = [
        ('controller_id_unique', 'unique(controller_id)', 'Server Code must be unique.'),
    ]

    def _migrate_legacy_application_links(self):
        """Move the former Core API -> Controller link to the new owner model.

        Older releases stored ``controller_url_id`` on ``core.api.application``.
        The new architecture stores the relation only on ``nsp.controller``.
        Migrate existing assignments, then remove the obsolete FK column so it
        can no longer block Controller archival/deletion at database level.
        """
        cr = self.env.cr
        cr.execute("""
            SELECT EXISTS (
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'core_api_application'
                   AND column_name = 'controller_url_id'
            ),
            EXISTS (
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'nsp_controller'
                   AND column_name = 'core_api_application_id'
            )
        """)
        legacy_column, target_column = cr.fetchone()
        if not (legacy_column and target_column):
            return

        cr.execute("""
            UPDATE nsp_controller AS controller
               SET core_api_application_id = application.id
              FROM core_api_application AS application
             WHERE application.controller_url_id = controller.id
               AND (controller.core_api_application_id IS NULL
                    OR controller.core_api_application_id = application.id)
               AND NOT EXISTS (
                    SELECT 1
                      FROM nsp_controller AS other
                     WHERE other.id != controller.id
                       AND other.core_api_application_id = application.id
               )
        """)
        cr.execute("ALTER TABLE core_api_application DROP COLUMN IF EXISTS controller_url_id CASCADE")

    def _auto_init(self):
        """Migrate legacy Core API links before normal model initialization."""
        self._migrate_legacy_application_links()
        return super()._auto_init()

    @api.depends('child_ids')
    def _compute_child_count(self):
        for record in self:
            record.child_count = len(record.child_ids)

    @api.constrains('parent_id')
    def _check_parent_recursion(self):
        if not self._check_recursion():
            raise ValidationError(_('A Controller cannot be its own parent or descendant.'))

    @api.constrains('parent_id', 'node_type', 'branch_id')
    def _check_parent_controller_scope(self):
        for record in self:
            if record.node_type == 'edge_server' and record.parent_id:
                raise ValidationError(_('An Edge Server must not have a Parent Controller.'))
            if record.node_type == 'controller' and record.parent_id and record.parent_id.node_type != 'edge_server':
                raise ValidationError(_('A Controller parent should be an Edge Server node.'))
            if record.parent_id and record.parent_id.branch_id and record.branch_id and record.branch_id != record.parent_id.branch_id:
                raise ValidationError(_('Child Controller Branch must match its Parent Edge Server Branch.'))

    # Multiple Controllers / Edge Servers can intentionally share one Core API Application.
    # Do not enforce one-Application-per-node. The operational identity is
    # controller_id/edge_server_code supplied in payload/header.

    @api.model_create_multi
    def create(self, vals_list):
        Application = self.env['core.api.application'].sudo()
        prepared = []
        for source in vals_list:
            values = dict(source)
            parent = self.browse(values.get('parent_id')).exists() if values.get('parent_id') else self.browse()
            if parent and parent.branch_id and not values.get('branch_id'):
                values['branch_id'] = parent.branch_id.id
            if values.get('node_type') == 'edge_server':
                values['parent_id'] = False
            elif values.get('node_type') == 'controller':
                values['core_api_application_id'] = False
            application = (
                Application.browse(values.get('core_api_application_id')).exists()
                if values.get('core_api_application_id') else Application.browse()
            )
            if application:
                service_code = (application.service_code or '').strip()
                if not service_code:
                    raise ValidationError(_('The selected Core API Application has no Server Code.'))
            if values.get('controller_id'):
                values['controller_id'] = str(values['controller_id']).strip()
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if values.get('parent_id'):
            parent = self.browse(values.get('parent_id')).exists()
            if parent and parent.branch_id and not values.get('branch_id'):
                values['branch_id'] = parent.branch_id.id
        if values.get('node_type') == 'edge_server':
            values['parent_id'] = False
        effective_node_type = values.get('node_type') or (self.node_type if len(self) == 1 else False)
        if effective_node_type == 'controller':
            values['core_api_application_id'] = False
        if values.get('core_api_application_id'):
            application = self.env['core.api.application'].sudo().browse(values['core_api_application_id']).exists()
            if application:
                service_code = (application.service_code or '').strip()
                if not service_code:
                    raise ValidationError(_('The selected Core API Application has no Server Code.'))
        if values.get('controller_id'):
            values['controller_id'] = str(values['controller_id']).strip()
        return super().write(values)

    @api.model
    def get_for_application(self, application):
        if not application:
            return self.browse()
        records = self.sudo().search([('core_api_application_id', '=', application.id)], limit=2)
        # Shared Applications intentionally map to multiple nodes. In that case
        # callers must resolve by controller_id/edge_server_code, not by app.
        return records[:1] if len(records) == 1 else self.browse()

    def _generate_gatekeeper_routes(self, application):
        """Generate routes needed by Controller and Edge Server applications.

        NSP Gatekeeper owns runtime and business synchronization routes used by
        the Edge Server and its Controllers.
        """
        application = application.exists()
        if not application:
            raise UserError(_('Assign or create a Core API Application first.'))
        result = {'created': 0, 'updated': 0, 'applications': len(application)}
        xmlids = [
            'nsp_gatekeeper.action_endpoint_manager_nsp_gatekeeper',
        ]
        for xmlid in xmlids:
            manager = self.env.ref(xmlid, raise_if_not_found=False)
            if not manager:
                continue
            generated = manager.sudo()._generate_core_api_routes_for_applications(application) or {}
            result['created'] += int(generated.get('created', 0) or 0)
            result['updated'] += int(generated.get('updated', 0) or 0)
        return result

    def action_create_core_api_application(self):
        self.ensure_one()
        if self.node_type != 'edge_server':
            raise UserError(_('Core API Application is configured only on an Edge Server.'))
        if self.core_api_application_id:
            return self.action_open_core_api_application()
        Application = self.env['core.api.application'].sudo()
        service_code = (self.controller_id or '').strip()
        existing = Application.search([('service_code', '=', service_code)], limit=1) if service_code else Application.browse()
        if existing:
            application = existing
        else:
            application = Application.create({
                'name': '%s / NSP' % (self.controller_name or self.controller_id),
                'service_code': service_code or Application._generate_service_code(self.controller_name or 'nsp-server'),
                'state': 'active',
                'notes': _('Issued from NSP node %s.') % (self.controller_id or self.display_name),
            })
        self.write({'core_api_application_id': application.id})
        self._generate_gatekeeper_routes(application)
        if application.credentials_pending:
            return application.action_view_credentials()
        return self.action_open_core_api_application()

    def action_open_core_api_application(self):
        self.ensure_one()
        if not self.core_api_application_id:
            raise UserError(_('Assign or create a Core API Application first.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Core API Application'),
            'res_model': 'core.api.application',
            'res_id': self.core_api_application_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_generate_application_routes(self):
        self.ensure_one()
        if self.node_type != 'edge_server':
            raise UserError(_('Core API routes are generated from the Edge Server Application.'))
        if not self.core_api_application_id:
            raise UserError(_('Assign or create a Core API Application first.'))
        result = self._generate_gatekeeper_routes(self.core_api_application_id) or {}
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _('Core API Routes'),
                'message': _('Generated %(created)s and updated %(updated)s NSP route(s).',
                             created=result.get('created', 0), updated=result.get('updated', 0)),
                'type': 'success', 'sticky': False,
            },
        }

    def action_open_child_controllers(self):
        self.ensure_one()
        action = self.env.ref('nsp_gatekeeper.action_nsp_controllers').sudo().read()[0]
        action.update({
            'name': _('Managed Controllers'),
            'domain': [('parent_id', '=', self.id)],
            'context': {'default_parent_id': self.id, 'default_node_type': 'controller', 'default_branch_id': self.branch_id.id},
        })
        return action

    def action_open_pairing_requests(self):
        self.ensure_one()
        if self.node_type == 'edge_server':
            domain = [('edge_server_id', '=', self.id)]
            context = {'default_edge_server_id': self.id}
        else:
            domain = [('controller_id', '=', self.id)]
            context = {'default_controller_id': self.id, 'default_edge_server_id': self.parent_id.id}
        return {
            'type': 'ir.actions.act_window',
            'name': _('Controller Pairing Requests'),
            'res_model': 'nsp.controller.pairing.request',
            'view_mode': 'list,form',
            'domain': domain,
            'context': context,
        }

    def unlink(self):
        if self.env.context.get('nsp_force_delete_controller'):
            return super().unlink()
        if self.child_ids:
            raise UserError(_('Archive or move child Controllers before archiving this parent.'))
        self.write({
            'active': False, 'connected': False, 'status': 'revoked',
            'session_token': False, 'session_expiry': False,
            'timestamp': fields.Datetime.now(),
        })
        return True

    def action_archive(self):
        if self.filtered('child_ids'):
            raise UserError(_('Archive or move child Controllers before archiving this parent.'))
        self.write({'active': False, 'connected': False, 'status': 'revoked'})
        return True

    def action_unarchive(self):
        self.write({'active': True, 'status': 'offline', 'connected': False})
        return True

    @api.model
    def cron_mark_offline_controllers(self):
        try:
            timeout_sec = int(self.env['ir.config_parameter'].sudo().get_param(
                'nsp_gatekeeper.controller_heartbeat_timeout_sec', '120'
            ) or '120')
        except Exception:
            timeout_sec = 120
        timeout_sec = max(30, timeout_sec)
        self.env.cr.execute("""
            UPDATE nsp_controller
               SET status='offline', connected=false
             WHERE COALESCE(status, 'offline') NOT IN ('offline', 'revoked')
               AND (timestamp IS NULL OR timestamp < (NOW() AT TIME ZONE 'UTC') - (%s || ' seconds')::interval)
        """, (str(timeout_sec),))
        self.env.cr.execute("""
            UPDATE nsp_device AS device
               SET status='offline'
              FROM nsp_controller AS controller
             WHERE device.controller_id = controller.id
               AND COALESCE(device.status, 'offline') != 'offline'
               AND COALESCE(controller.status, 'offline') = 'offline'
        """)
        return True
