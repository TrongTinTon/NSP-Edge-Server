# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from urllib.parse import urlparse, urlunparse
from lxml import etree


class NSPConnectionBlacklist(models.Model):
    _name = 'nsp.connection.blacklist'
    _description = 'NSP Connection URL Blacklist'
    _rec_name = 'url'
    _order = 'blocked_at desc, id desc'

    url = fields.Char(string='Blocked URL', required=True)
    normalized_url = fields.Char(string='Normalized URL', required=True, index=True, readonly=True, copy=False)
    reason = fields.Char(string='Reason')
    blocked_at = fields.Datetime(string='Blocked At', default=fields.Datetime.now, readonly=True, copy=False)

    _sql_constraints = [
        ('normalized_url_unique', 'unique(normalized_url)', 'This URL already exists in blacklist.'),
    ]


    @api.model
    def get_view(self, view_id=None, view_type='form', **options):
        """Allow manual blacklist entry from the Blacklist list view."""
        result = super().get_view(view_id=view_id, view_type=view_type, **options)
        if view_type in ('list', 'form') and result.get('arch'):
            try:
                arch = etree.fromstring(result['arch'])
                arch.set('create', '1')
                result['arch'] = etree.tostring(arch, encoding='unicode')
            except Exception:
                pass
        return result

    @api.model
    def normalize_url(self, value):
        value = (value or '').strip()
        if not value:
            return False
        if '://' not in value:
            value = 'http://' + value
        try:
            parsed = urlparse(value)
        except Exception:
            return value.strip().lower().rstrip('/')

        scheme = (parsed.scheme or 'http').lower()
        hostname = (parsed.hostname or '').lower()
        if not hostname:
            return value.strip().lower().rstrip('/')
        port = parsed.port
        netloc = hostname
        if port and not ((scheme == 'http' and port == 80) or (scheme == 'https' and port == 443)):
            netloc = '%s:%s' % (hostname, port)
        path = (parsed.path or '').rstrip('/')
        return urlunparse((scheme, netloc, path, '', '', '')).rstrip('/')

    @api.model
    def is_url_blocked(self, value):
        normalized = self.normalize_url(value)
        if not normalized:
            return False
        return bool(self.sudo().search_count([('normalized_url', '=', normalized)]))

    @api.model
    def block_url(self, value, reason=None):
        normalized = self.normalize_url(value)
        if not normalized:
            return self.browse()
        existing = self.sudo().search([('normalized_url', '=', normalized)], limit=1)
        if existing:
            if reason and not existing.reason:
                existing.write({'reason': reason})
            return existing
        return self.sudo().create({
            'url': (value or '').strip(),
            'normalized_url': normalized,
            'reason': reason or _('Blocked from Core API'),
        })

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for vals in vals_list:
            vals = dict(vals or {})
            normalized = self.normalize_url(vals.get('url'))
            if not normalized:
                raise ValidationError(_('Blocked URL is required.'))
            vals['normalized_url'] = normalized
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        vals = dict(vals or {})
        if 'url' in vals:
            normalized = self.normalize_url(vals.get('url'))
            if not normalized:
                raise ValidationError(_('Blocked URL is required.'))
            vals['normalized_url'] = normalized
        return super().write(vals)
