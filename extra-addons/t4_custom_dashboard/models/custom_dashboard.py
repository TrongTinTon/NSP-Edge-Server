# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
import json
import logging

from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)


class CustomDashboard(models.Model):
    _name = 'custom.dashboard'
    _description = 'T4 Custom Dashboard Configuration'
    _order = 'sequence, name'

    name = fields.Char(string='Dashboard Name', required=True)
    description = fields.Text(string='Description')
    user_id = fields.Many2one('res.users', string='Owner', required=True, default=lambda self: self.env.user)
    is_shared = fields.Boolean(string='Shared', default=False, help='Cho phép người dùng khác xem bảng điều khiển này')
    is_default = fields.Boolean(string='Default Dashboard', default=False)
    is_role_bound = fields.Boolean(
        string='Role-Bound',
        default=False,
        help='Dashboard này gắn cố định vào menu role qua client action — '
             'không được set is_default, không được xóa.',
    )
    sequence = fields.Integer(string='Sequence', default=10)
    active = fields.Boolean(string='Active', default=True)
    widget_config = fields.Text(string='Widget Configuration', required=True, default='[]')
    company_id = fields.Many2one('res.company', string='Company', default=lambda self: self.env.company)

    _name_user_unique = models.Constraint(
        'UNIQUE(name, user_id, company_id)',
        'Dashboard name must be unique per user and company!',
    )

    # ---------------------------------------------------------------------
    # CRUD
    # ---------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        if not isinstance(vals_list, list):
            vals_list = [vals_list]
        for vals in vals_list:
            if vals.get('is_default'):
                user_id = vals.get('user_id', self.env.user.id)
                company_id = vals.get('company_id', self.env.company.id)
                self.search([
                    ('user_id', '=', user_id),
                    ('company_id', '=', company_id),
                ]).write({'is_default': False})
        return super().create(vals_list)

    def _check_role_bound_manage_access(self):
        if self.filtered('is_role_bound') and not (
            self.env.user.has_group('t4_custom_dashboard.group_dashboard_manager')
            or self.env.user.has_group('base.group_system')
        ):
            raise AccessError(_('Role-bound dashboards are managed by Dashboard Managers only.'))

    def write(self, vals):
        self._check_role_bound_manage_access()
        if vals.get('is_default'):
            for record in self:
                self.search([
                    ('user_id', '=', record.user_id.id),
                    ('company_id', '=', record.company_id.id),
                    ('id', '!=', record.id),
                ]).write({'is_default': False})
        return super().write(vals)

    def unlink(self):
        self._check_role_bound_manage_access()
        return super().unlink()

    # ---------------------------------------------------------------------
    # Schema v1/v2 helpers
    # ---------------------------------------------------------------------

    def get_widget_config_json(self):
        """Return raw parsed JSON (có thể là list v1 hoặc dict v2)."""
        self.ensure_one()
        try:
            return json.loads(self.widget_config or '[]')
        except Exception:
            return []

    def get_widget_config_normalized(self):
        """Return dict {version, searchPanel, widgets} với defaults đầy đủ.

        - Schema v1 (list) → {version: 1, searchPanel: None, widgets: list}
        - Schema v2 (dict) → giữ nguyên, fill defaults nếu thiếu key
        """
        self.ensure_one()
        raw = self.get_widget_config_json()
        return self._normalize_config(raw)

    @staticmethod
    def _normalize_config(raw):
        if isinstance(raw, list):
            return {'version': 1, 'searchPanel': None, 'widgets': raw}
        if isinstance(raw, dict):
            return {
                'version': raw.get('version', 2),
                'searchPanel': raw.get('searchPanel'),
                'widgets': raw.get('widgets') or [],
            }
        return {'version': 2, 'searchPanel': None, 'widgets': []}

    def set_widget_config_json(self, config_data):
        """Set widget config — accept dict (v2) hoặc list (legacy)."""
        self.ensure_one()
        if isinstance(config_data, list):
            payload = {'version': 2, 'searchPanel': None, 'widgets': config_data}
        elif isinstance(config_data, dict):
            payload = {
                'version': 2,
                'searchPanel': config_data.get('searchPanel'),
                'widgets': config_data.get('widgets') or [],
            }
        else:
            payload = {'version': 2, 'searchPanel': None, 'widgets': []}
        self.widget_config = json.dumps(payload)

    # ---------------------------------------------------------------------
    # API
    # ---------------------------------------------------------------------

    @api.model
    def get_user_dashboards(self):
        domain = [
            '|',
            ('user_id', '=', self.env.user.id),
            ('is_shared', '=', True),
            ('company_id', 'in', [False, self.env.company.id]),
        ]
        dashboards = self.sudo().search(domain)
        return [{
            'id': d.id,
            'name': d.name,
            'description': d.description,
            'is_default': d.is_default,
            'is_shared': d.is_shared,
            'is_owner': d.user_id.id == self.env.user.id,
            'owner_name': d.user_id.name,
        } for d in dashboards]

    @api.model
    def get_default_dashboard(self, dashboard_xml_id=None):
        """Load dashboard mặc định cho user hiện tại.

        Nếu truyền `dashboard_xml_id` (resolve qua env.ref), load đúng dashboard
        đó — phục vụ menu role-specific (Trang Chủ Tồn Kho / Sale / Kế Toán /
        Thu Mua). Fallback: search dashboard is_default theo user + shared.
        """
        if dashboard_xml_id:
            dashboard = self.sudo().env.ref(dashboard_xml_id, raise_if_not_found=False)
            if (
                dashboard
                and dashboard._name == self._name
                and dashboard.active
            ):
                return self._serialize_dashboard(dashboard)

        dashboard = self.sudo().search([
            '|',
            ('user_id', '=', self.env.user.id),
            ('is_shared', '=', True),
            ('is_default', '=', True),
            ('company_id', '=', self.env.company.id),
            ('active', '=', True),
            ('is_role_bound', '=', False),
        ], limit=1)
        if not dashboard:
            return None
        return self._serialize_dashboard(dashboard)

    # ------------------------------------------------------------------
    # Resolve placeholder ``__uid__`` trong widget embed (kanban_embed)
    # ------------------------------------------------------------------
    # KanbanEmbed pass thẳng ``embed.domain`` (JSON list) cho controller view,
    # KHÔNG eval ``uid`` như ``ir.actions.act_window``. Để dashboard role-bound
    # filter ``[('user_id', '=', uid)]`` per-user chính xác, ta thay token
    # ``"__uid__"`` trong JSON bằng ``self.env.uid`` (int) ở server-side trước
    # khi gửi xuống client. Đặt ở BASE để mọi module (t4_som / t4_spm / t4_crm
    # / t4_sti...) dùng được mà không cần copy override.
    _T4_UID_TOKEN = "__uid__"

    @api.model
    def _t4_resolve_uid_placeholder(self, value):
        """Đệ quy thay token ``__uid__`` (string) trong list/tuple/dict bằng
        env.uid (int)."""
        if value == self._T4_UID_TOKEN:
            return self.env.uid
        if isinstance(value, list):
            return [self._t4_resolve_uid_placeholder(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self._t4_resolve_uid_placeholder(v) for v in value)
        if isinstance(value, dict):
            return {k: self._t4_resolve_uid_placeholder(v) for k, v in value.items()}
        return value

    def _serialize_dashboard(self, dashboard):
        cfg = dashboard.get_widget_config_normalized()
        widgets = cfg['widgets']
        for widget in widgets:
            embed = widget.get('embed') or {}
            if embed.get('domain'):
                embed['domain'] = self._t4_resolve_uid_placeholder(embed['domain'])
            if embed.get('context'):
                embed['context'] = self._t4_resolve_uid_placeholder(embed['context'])
        return {
            'id': dashboard.id,
            'name': dashboard.name,
            'description': dashboard.description,
            'widgets': widgets,
            'searchPanel': cfg['searchPanel'],
        }

    def action_set_as_default(self):
        self.ensure_one()
        self.write({'is_default': True})
        return True

    def action_duplicate(self):
        self.ensure_one()
        new_dashboard = self.copy({
            'name': f"{self.name} (Copy)",
            'is_default': False,
            'user_id': self.env.user.id,
            'is_shared': False,
        })
        return {'id': new_dashboard.id, 'name': new_dashboard.name}

    def export_dashboard(self):
        self.ensure_one()
        cfg = self.get_widget_config_normalized()
        return {
            'name': self.name,
            'description': self.description,
            'widgets': cfg['widgets'],
            'searchPanel': cfg['searchPanel'],
            'version': '2.0',
        }

    @api.model
    def import_dashboard(self, config_data, dashboard_name=None):
        try:
            name = dashboard_name or config_data.get('name', 'Imported Dashboard')
            existing = self.search([
                ('user_id', '=', self.env.user.id),
                ('name', '=', name),
            ])
            if existing:
                counter = 1
                while self.search([
                    ('user_id', '=', self.env.user.id),
                    ('name', '=', f"{name} ({counter})"),
                ]):
                    counter += 1
                name = f"{name} ({counter})"

            # Hỗ trợ cả schema v1 (chỉ có widgets) lẫn v2 (widgets + searchPanel)
            payload = {
                'version': 2,
                'searchPanel': config_data.get('searchPanel'),
                'widgets': config_data.get('widgets') or [],
            }

            dashboard = self.create({
                'name': name,
                'description': config_data.get('description', ''),
                'widget_config': json.dumps(payload),
                'user_id': self.env.user.id,
                'company_id': self.env.company.id,
            })
            return {'id': dashboard.id, 'name': dashboard.name}
        except Exception as e:
            _logger.error('Error importing dashboard: %s', e)
            raise
