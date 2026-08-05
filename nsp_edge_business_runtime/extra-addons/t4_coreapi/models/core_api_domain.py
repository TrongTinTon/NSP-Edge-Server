# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.http import request


class CoreApiDomain(models.Model):
    _name = 'core.api.domain'
    _description = 'API Host Domain'
    _order = 'sequence, name'

    name = fields.Char(required=True)
    protocol = fields.Selection(
        [('http', 'HTTP'), ('https', 'HTTPS')],
        string='Protocol',
        default='https',
        required=True,
        help='Protocol used to build the API server root URL.',
    )
    hostname = fields.Char(
        string='Hostname',
        help='Host name or host:port used by clients, e.g. cloud_web:8069, localhost:8070, api.example.com. '
             'Do not enter a service code, version, or API route here.',
    )
    base_url = fields.Char(
        string='Public Base URL',
        compute='_compute_base_url',
        help='Protocol + hostname used by integrations and NSP Sync jobs (server root only).',
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    is_default = fields.Boolean(
        string='Default Domain',
        default=False,
        help='Used when the HTTP Host header does not match any configured hostname.',
    )
    description = fields.Text()

    _hostname_unique = models.Constraint(
        'unique(hostname)',
        'Hostname must be unique (empty hostname is allowed only once for the default domain).',
    )

    @api.model
    def _normalize_hostname_input(self, hostname=False, protocol=False):
        """Accept flexible host input but store only host[:port].

        Users often paste http://host:port/service/v1/route. API Host Domains are
        server roots, so keep the host[:port] and move the scheme into protocol.
        """
        raw = (hostname or '').strip()
        proto = (protocol or 'https').strip().lower()
        if not raw:
            return proto if proto in ('http', 'https') else 'https', False
        candidate = raw if '://' in raw else 'dummy://%s' % raw
        parsed = urlsplit(candidate)
        if parsed.scheme in ('http', 'https'):
            proto = parsed.scheme
        host = (parsed.netloc or parsed.path or '').strip().strip('/')
        # If the user typed host/path without scheme, urlsplit puts everything in path.
        # Keep only host[:port].
        if '/' in host:
            host = host.split('/', 1)[0]
        return proto if proto in ('http', 'https') else 'https', host or False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Normal form creation passes hostname explicitly. Quick-create from
            # Many2one only passes ``name``; when the user types a URL/host in
            # NSP Sync, treat that value as the hostname so the newly created
            # record immediately satisfies the Sync Job domain.
            raw_hostname = vals.get('hostname')
            if not raw_hostname and not vals.get('is_default'):
                raw_name = (vals.get('name') or '').strip()
                if raw_name and (('://' in raw_name) or (':' in raw_name) or ('.' in raw_name) or raw_name in ('localhost', '127.0.0.1')):
                    raw_hostname = raw_name
            protocol, hostname = self._normalize_hostname_input(raw_hostname, vals.get('protocol'))
            vals['protocol'] = protocol
            vals['hostname'] = hostname
            if hostname and (not vals.get('name') or vals.get('name') == raw_hostname):
                vals['name'] = hostname
        return super().create(vals_list)

    @api.model
    def name_create(self, name):
        """Allow NSP Sync users to create a Remote Server URL inline.

        Typing ``http://localhost:8070`` or ``cloud_web:8069`` in a Many2one
        creates an explicit API Host Domain with hostname populated. Without
        this, Odoo quick-create would only set ``name`` and the record would not
        be selectable by NSP Sync because hostname stays empty.
        """
        raw = (name or '').strip()
        protocol, hostname = self._normalize_hostname_input(raw, False)
        vals = {
            'name': hostname or raw,
            'protocol': protocol,
            'hostname': hostname,
            'active': True,
            'is_default': False,
        }
        rec = self.create(vals)
        return rec.id, rec.display_name

    def write(self, vals):
        vals = dict(vals)
        if 'hostname' in vals or 'protocol' in vals:
            for rec in self:
                raw_host = vals.get('hostname', rec.hostname)
                raw_protocol = vals.get('protocol', rec.protocol)
                protocol, hostname = rec._normalize_hostname_input(raw_host, raw_protocol)
                single_vals = dict(vals, protocol=protocol, hostname=hostname)
                super(CoreApiDomain, rec).write(single_vals)
            return True
        return super().write(vals)

    @api.onchange('hostname', 'protocol')
    def _onchange_hostname_protocol(self):
        for rec in self:
            protocol, hostname = rec._normalize_hostname_input(rec.hostname, rec.protocol)
            rec.protocol = protocol
            rec.hostname = hostname

    @api.depends('protocol', 'hostname')
    def _compute_base_url(self):
        """Build the public origin URL from protocol and hostname."""
        web_base = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
        for rec in self:
            host = (rec.hostname or '').strip()
            if host:
                rec.base_url = '%s://%s' % (rec.protocol or 'https', host)
            else:
                rec.base_url = web_base

    @api.constrains('hostname')
    def _check_hostname(self):
        """Keep host flexible, but prevent values that are not server roots."""
        for rec in self:
            host = (rec.hostname or '').strip()
            if not host:
                continue
            if any(ch.isspace() for ch in host):
                raise ValidationError(_('Hostname must not contain spaces.'))
            if '/' in host:
                raise ValidationError(_('Hostname must be host or host:port only. Do not include service code, version, or route path.'))

    @api.constrains('is_default')
    def _check_single_default(self):
        """Allow only one default host domain."""
        for rec in self.filtered('is_default'):
            other = self.search([
                ('is_default', '=', True),
                ('id', '!=', rec.id),
            ], limit=1)
            if other:
                raise ValidationError(_(
                    'Only one default API host domain is allowed (already: "%s").',
                    other.name,
                ))

    @api.model
    def get_default(self):
        """Return the default host domain record."""
        domain = self.search([('is_default', '=', True)], limit=1)
        if domain:
            return domain
        return self.search([], order='sequence, id', limit=1)

    @api.model
    def get_from_request(self, httprequest=None):
        """Resolve host domain from the HTTP Host header."""
        req = httprequest or (request.httprequest if request else None)
        host_header = ''
        if req:
            host_header = (req.host or '').strip().lower()
        host_only = host_header.split(':')[0] if host_header else ''
        if host_header:
            domains = self.sudo().search([('hostname', '!=', False), ('active', '=', True)])
            for domain in domains:
                configured = (domain.hostname or '').strip().lower()
                configured_only = configured.split(':')[0]
                if configured in (host_header, host_only) or configured_only in (host_header, host_only):
                    return domain
        return self.sudo().get_default()
