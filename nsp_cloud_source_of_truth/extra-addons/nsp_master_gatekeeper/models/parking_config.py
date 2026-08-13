# -*- coding: utf-8 -*-
import json

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError
from odoo.addons.nsp_core.utils import new_management_code


class NspParkingArea(models.Model):
    """Cloud working layout plus immutable published runtime snapshot."""

    _name = "nsp.parking.area"
    _description = "NSP Parking Operation Configuration"
    _rec_name = "name"
    _order = "branch_id, name, id"

    name = fields.Char(string="Parking Layout Name", required=True)
    code = fields.Char(
        string="Parking Layout Code", required=True, readonly=True, copy=False,
        index=True, default=lambda self: new_management_code("PARK"),
    )
    branch_id = fields.Many2one(
        "nsp.branch", string="Branch", required=True,
        ondelete="restrict", index=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft / Configuring"),
            ("operational", "Operational"),
            ("maintenance", "Maintenance"),
            ("blocked", "Blocked"),
        ],
        string="Working State", default="draft", required=True, index=True,
    )
    layout_lane_ids = fields.One2many(
        "nsp.parking.layout.lane", "parking_area_id", string="Lane Configurations",
    )

    # Publication is separate from the editable working state. Returning to Draft
    # does not remove the last published snapshot from Edge.
    published_revision = fields.Integer(
        string="Published Revision", default=0, readonly=True, copy=False, index=True,
    )
    published_at = fields.Datetime(readonly=True, copy=False)
    published_payload_json = fields.Text(readonly=True, copy=False)
    published_edge_server_codes = fields.Char(readonly=True, copy=False)
    is_published = fields.Boolean(compute="_compute_is_published")

    edge_server_ids = fields.Many2many(
        "nsp.edge.server", string="Servers", compute="_compute_topology",
    )
    controller_ids = fields.Many2many(
        "nsp.controller", string="Controllers", compute="_compute_topology",
        search="_search_controllers",
    )
    reader_ids = fields.Many2many(
        "nsp.device", string="Readers", compute="_compute_topology",
    )
    edge_server_count = fields.Integer(compute="_compute_counts")
    controller_count = fields.Integer(compute="_compute_counts")
    reader_count = fields.Integer(compute="_compute_counts")
    lane_count = fields.Integer(compute="_compute_counts")
    whitelist_count = fields.Integer(compute="_compute_whitelist_count")
    ready_lane_count = fields.Integer(
        string="Ready Configurations", compute="_compute_configuration_health",
    )
    incomplete_lane_count = fields.Integer(
        string="Incomplete Configurations", compute="_compute_configuration_health",
    )
    configuration_state = fields.Selection(
        [
            ("empty", "No Lane Configuration"),
            ("incomplete", "Needs Attention"),
            ("ready", "Ready to Publish"),
        ],
        string="Configuration Readiness",
        compute="_compute_configuration_health",
    )
    configuration_summary = fields.Char(
        string="Readiness Summary", compute="_compute_configuration_health",
    )

    _sql_constraints = [
        ("code_unique", "unique(code)", "Parking Layout Code must be unique."),
    ]

    @api.depends("published_payload_json")
    def _compute_is_published(self):
        for record in self:
            record.is_published = bool(record.published_payload_json)

    @api.depends(
        "layout_lane_ids.active",
        "layout_lane_ids.edge_server_id",
        "layout_lane_ids.controller_id",
        "layout_lane_ids.antenna_sequence_ids.reader_id",
        "layout_lane_ids.antenna_sequence_ids.port_no",
    )
    def _compute_topology(self):
        for record in self:
            lanes = record.layout_lane_ids.filtered("active")
            record.edge_server_ids = lanes.mapped("edge_server_id")
            record.controller_ids = lanes.mapped("controller_id")
            record.reader_ids = lanes.mapped("antenna_sequence_ids.reader_id")

    @api.model
    def _search_controllers(self, operator, value):
        return [("layout_lane_ids.controller_id", operator, value)]

    @api.depends(
        "edge_server_ids", "controller_ids", "reader_ids",
        "layout_lane_ids.active",
    )
    def _compute_counts(self):
        for record in self:
            record.edge_server_count = len(record.edge_server_ids)
            record.controller_count = len(record.controller_ids)
            record.reader_count = len(record.reader_ids)
            record.lane_count = len(record.layout_lane_ids.filtered("active"))

    @api.depends(
        "layout_lane_ids.active",
        "layout_lane_ids.configuration_state",
        "layout_lane_ids.configuration_issue",
        "layout_lane_ids.antenna_sequence_ids",
    )
    def _compute_configuration_health(self):
        for record in self:
            active_lanes = record.layout_lane_ids.filtered("active")
            ready_lanes = active_lanes.filtered(
                lambda lane: lane.configuration_state == "ready"
            )
            incomplete_lanes = active_lanes - ready_lanes
            record.ready_lane_count = len(ready_lanes)
            record.incomplete_lane_count = len(incomplete_lanes)
            if not active_lanes:
                record.configuration_state = "empty"
                record.configuration_summary = _("No active Lane Configuration is configured.")
            elif incomplete_lanes:
                record.configuration_state = "incomplete"
                record.configuration_summary = _(
                    "%(ready)s ready · %(incomplete)s need attention"
                ) % {
                    "ready": len(ready_lanes),
                    "incomplete": len(incomplete_lanes),
                }
            else:
                record.configuration_state = "ready"
                record.configuration_summary = _(
                    "All %(count)s active Lanes have a valid Antenna Sequence."
                ) % {"count": len(active_lanes)}

    def _compute_whitelist_count(self):
        count = self.env["nsp.device.whitelist"].sudo().search_count([])
        for record in self:
            record.whitelist_count = count

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            values["code"] = self._normalize_code(
                values.get("code") or new_management_code("PARK")
            )
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "code" in values:
            values["code"] = self._normalize_code(values.get("code"))
        if values.get("branch_id"):
            target_branch = self.env["nsp.branch"].browse(int(values["branch_id"])).exists()
            for record in self:
                mismatched = record.layout_lane_ids.filtered(
                    lambda item: item.lane_id.branch_id != target_branch
                )
                if mismatched:
                    raise ValidationError(_(
                        "Parking Layout Branch cannot be changed while it references Lane(s) from another Branch: %s"
                    ) % ", ".join(mismatched.mapped("lane_id.display_name")))
        return super().write(values)

    def action_open_live_monitor(self):
        self.ensure_one()
        self.check_access("read")
        return {
            "type": "ir.actions.client",
            "name": _("Parking Live Monitor"),
            "tag": "nsp_parking_live_monitor",
            "target": "fullscreen",
            "params": {"parking_area_id": self.id},
        }

    @api.model
    def get_live_monitor_snapshot(self, parking_area_id, limit=16):
        if not (
            self.env.user.has_group("nsp_core.group_nsp_operator")
            or self.env.user.has_group("nsp_core.group_nsp_it_parking")
            or self.env.user.has_group("base.group_system")
        ):
            from odoo.exceptions import AccessError
            raise AccessError(_("You do not have access to the Parking Live Monitor."))
        try:
            parking_area_id = int(parking_area_id or 0)
            limit = min(max(int(limit or 12), 3), 50)
        except (TypeError, ValueError):
            parking_area_id, limit = 0, 12
        area = self.browse(parking_area_id).exists()
        if not area:
            return {"found": False}
        area.check_access("read")
        transactions = self.env["nsp.parking.transaction"].search(
            [
                "|",
                ("parking_area_id", "=", area.id),
                ("layout_lane_id.parking_area_id", "=", area.id),
            ],
            order="event_time desc, id desc", limit=limit,
        )
        return {
            "found": True,
            "parking_area_id": area.id,
            "parking_area_name": area.name,
            "branch_name": area.branch_id.name or "",
            "state": area.state,
            "items": [tx._live_monitor_payload() for tx in transactions[::-1]],
        }

    def _lane_payload(self):
        self.ensure_one()
        payloads = []
        lanes = self.layout_lane_ids.filtered("active").sorted(
            key=lambda item: ((item.lane_id.name or "").casefold(), item.lane_id.code or "", item.id)
        )
        for lane in lanes:
            readers = {}
            antenna_sequence = []
            config_by_reader = {
                config.reader_id.id: config for config in lane.reader_config_ids
            }
            ordered_points = lane.antenna_sequence_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            )
            for business_order, point in enumerate(ordered_points, start=1):
                reader = point.reader_id
                config = config_by_reader.get(reader.id)
                if not config:
                    raise ValidationError(
                        _("Lane %(lane)s has no Device Configuration for %(reader)s.")
                        % {"lane": lane.display_name, "reader": reader.display_name}
                    )
                reader_payload = readers.setdefault(reader.id, {
                    "technical_code": reader.device_code or "",
                    "serial_number": reader.serial_number or "",
                    "reader_name": reader.name or reader.serial_number or "",
                    "reader_parameters": {
                        "power_dbm": int(config.power_dbm or 0),
                        "read_interval_ms": int(config.read_interval_ms or 200),
                        "tid_start_address": int(config.tid_start_address or 0),
                        "tid_length": int(config.tid_length or 4),
                    },
                    "ports": set(),
                })
                reader_payload["ports"].add(int(point.port_no or 0))
                antenna_sequence.append({
                    # ``sequence`` on the Odoo model is only a UI sort priority
                    # (handle widgets legitimately use gaps such as 10, 20, 30).
                    # The published contract owns a dense business order 1..N.
                    "sequence": business_order,
                    "reader_code": reader.device_code or "",
                    "reader_serial_number": reader.serial_number or "",
                    "port_no": int(point.port_no or 0),
                    "duration_from_previous_seconds": (
                        0.0
                        if business_order == 1
                        else float(point.duration_from_previous or 0.0)
                    ),
                })
            payloads.append({
                "lane_code": lane.lane_id.code,
                "lane_name": lane.lane_id.name,
                "server_code": lane.edge_server_id.edge_server_code or "",
                "controller_code": lane.controller_id.controller_id or "",
                "antenna_sequence": antenna_sequence,
                "readers": [
                    {
                        **reader_payload,
                        "ports": [
                            {"port_no": port_no}
                            for port_no in sorted(reader_payload["ports"])
                        ],
                    }
                    for reader_payload in sorted(
                        readers.values(),
                        key=lambda row: (row["serial_number"], row["technical_code"]),
                    )
                ],
            })
        return payloads

    def _build_sync_payload(self, published_state="operational", revision=False):
        self.ensure_one()
        return {
            "parking_area_code": self.code,
            "parking_area_name": self.name,
            "branch_code": self.branch_id.code or "",
            "state": published_state,
            "published_revision": int(revision or self.published_revision or 1),
            "lanes": self._lane_payload(),
        }

    def _validate_sync_payload_contract(self, payload):
        """Reject legacy published snapshots instead of sending mixed schemas."""
        self.ensure_one()
        if not isinstance(payload, dict):
            raise ValidationError(_("Published Parking Layout snapshot must be an object."))
        allowed_root = {
            "parking_area_code", "parking_area_name", "branch_code",
            "state", "published_revision", "lanes",
        }
        unsupported_root = set(payload) - allowed_root
        if unsupported_root:
            raise ValidationError(
                _("Published Parking Layout contains unsupported field(s): %s")
                % ", ".join(sorted(unsupported_root))
            )
        lanes = payload.get("lanes")
        if not isinstance(lanes, list):
            raise ValidationError(_("Published Lane Configurations must be an array."))
        allowed_lane = {
            "lane_code", "lane_name", "server_code", "controller_code",
            "antenna_sequence", "readers",
        }
        required_lane = allowed_lane
        for lane in lanes:
            if not isinstance(lane, dict):
                raise ValidationError(_("Published Lane Configurations must contain objects."))
            unsupported = set(lane) - allowed_lane
            missing = required_lane - set(lane)
            if unsupported or missing:
                details = []
                if unsupported:
                    details.append(_("unsupported: %s") % ", ".join(sorted(unsupported)))
                if missing:
                    details.append(_("missing: %s") % ", ".join(sorted(missing)))
                raise ValidationError(
                    _("Published Parking Layout uses a legacy Lane contract; revise and publish it again (%s).")
                    % "; ".join(details)
                )

            readers = lane.get("readers")
            if not isinstance(readers, list):
                raise ValidationError(_("Published Lane Readers must be an array."))
            allowed_reader = {
                "technical_code", "serial_number", "reader_name",
                "reader_parameters", "ports",
            }
            for reader in readers:
                if not isinstance(reader, dict):
                    raise ValidationError(_("Published Lane Readers must contain objects."))
                unsupported_reader = set(reader) - allowed_reader
                if unsupported_reader:
                    raise ValidationError(
                        _("Published Lane Reader contains unsupported field(s): %s")
                        % ", ".join(sorted(unsupported_reader))
                    )

            sequence = lane.get("antenna_sequence")
            if not isinstance(sequence, list):
                raise ValidationError(_("Published Antenna Sequence must be an array."))
            allowed_point = {
                "sequence", "reader_code", "reader_serial_number",
                "port_no", "duration_from_previous_seconds",
            }
            for point in sequence:
                if not isinstance(point, dict):
                    raise ValidationError(_("Published Antenna Sequence must contain objects."))
                unsupported_point = set(point) - allowed_point
                if unsupported_point:
                    raise ValidationError(
                        _("Published Antenna Sequence contains unsupported field(s): %s")
                        % ", ".join(sorted(unsupported_point))
                    )
        return True

    @api.model
    def _sequence_is_ordered_subsequence(self, smaller, larger):
        """Return True when ``smaller`` can be matched inside ``larger`` in order.

        Shared Reader/Antenna points across logical Lanes are valid. Ambiguity
        exists only when one complete Lane sequence can also satisfy another
        Lane sequence. Reverse sequences such as A1->A3 and A3->A1 remain
        distinct and are explicitly supported.
        """
        if len(smaller) > len(larger):
            return False
        position = 0
        for item in larger:
            if position < len(smaller) and item == smaller[position]:
                position += 1
        return position == len(smaller)

    def _operational_issues(self):
        self.ensure_one()
        issues = []
        lanes = self.layout_lane_ids.filtered("active")
        if not lanes:
            return [_('Configure at least one active Lane Configuration.')]

        valid_sequences = []
        for lane in lanes:
            try:
                lane._validate_lane_assembly()
                lane._validate_antenna_sequence()
                lane._validate_reader_configs()
            except ValidationError as exc:
                issues.append(str(exc))
                continue
            sequence = tuple(
                (point.reader_id.id, int(point.port_no or 0))
                for point in lane.antenna_sequence_ids.sorted(lambda row: (row.sequence or 0, row.id))
            )
            valid_sequences.append((lane, sequence))

        # One physical Reader can participate in multiple logical Lanes, but the
        # Controller can apply only one physical Reader profile at a time. Ports
        # may differ between Lane sequences; Power / Interval / TID settings and
        # the contextual Server+Controller assembly must remain identical.
        reader_profiles = {}
        for lane, _sequence in valid_sequences:
            for config in lane.reader_config_ids:
                profile = (
                    lane.edge_server_id.id,
                    lane.controller_id.id,
                    int(config.power_dbm or 0),
                    int(config.read_interval_ms or 0),
                    int(config.tid_start_address or 0),
                    int(config.tid_length or 0),
                )
                previous = reader_profiles.get(config.reader_id.id)
                if previous and previous[0] != profile:
                    issues.append(_(
                        "Reader %(reader)s is shared by multiple Lane Configurations with "
                        "conflicting Server/Controller or Reader parameters. Shared logical "
                        "Lanes may use different Antenna Sequences, but the physical Reader "
                        "runtime profile must be identical."
                    ) % {"reader": config.reader_id.display_name})
                else:
                    reader_profiles[config.reader_id.id] = (profile, lane)

        # Reader/Antenna points may be shared by multiple logical Lanes. The
        # complete ordered sequence, not an individual port, identifies a Lane.
        for index, (first_lane, first_sequence) in enumerate(valid_sequences):
            for second_lane, second_sequence in valid_sequences[index + 1:]:
                if (
                    self._sequence_is_ordered_subsequence(first_sequence, second_sequence)
                    or self._sequence_is_ordered_subsequence(second_sequence, first_sequence)
                ):
                    issues.append(_(
                        "Antenna Sequences for %(first)s and %(second)s are ambiguous. "
                        "Logical Lanes may share Reader/Antenna points, but each Lane must have "
                        "a distinguishable ordered Antenna Sequence."
                    ) % {
                        "first": first_lane.display_name,
                        "second": second_lane.display_name,
                    })
        return issues

    def _publish(self, target_state):
        self.ensure_one()
        self.check_access("write")
        record = self
        record._validate_parking_state_transition(target_state)
        revision = int(record.published_revision or 0) + 1
        previous_edge_codes = {
            item.strip().upper()
            for item in str(record.published_edge_server_codes or "").split(",")
            if item.strip()
        }

        if target_state == "operational":
            issues = record._operational_issues()
            if issues:
                raise UserError("\n".join(issues))
            payload = record._build_sync_payload(target_state, revision)
            record._validate_sync_payload_contract(payload)
            current_reader_ports = {
                (
                    str(point.get("reader_code") or "").strip().upper(),
                    int(point.get("port_no") or 0),
                )
                for lane_payload in payload.get("lanes", [])
                for point in lane_payload.get("antenna_sequence", [])
            }
            other_layouts = self.search([
                ("id", "!=", record.id),
                ("published_payload_json", "!=", False),
            ])
            conflicts = []
            for other in other_layouts:
                try:
                    other_payload = json.loads(other.published_payload_json)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValidationError(
                        _("Published Parking Layout %(layout)s snapshot is invalid.")
                        % {"layout": other.display_name}
                    ) from exc
                if other_payload.get("state") != "operational":
                    continue
                other_reader_ports = {
                    (
                        str(point.get("reader_code") or "").strip().upper(),
                        int(point.get("port_no") or 0),
                    )
                    for lane_payload in other_payload.get("lanes", [])
                    for point in lane_payload.get("antenna_sequence", [])
                }
                overlap = sorted(current_reader_ports & other_reader_ports)
                if overlap:
                    conflicts.append("%s: %s" % (
                        other.display_name,
                        ", ".join("%s:%s" % item for item in overlap),
                    ))
            if conflicts:
                raise UserError(
                    _("Operational Parking Layout Reader Ports must be exclusive. Conflicts: %s")
                    % "; ".join(conflicts)
                )
            edge_codes = sorted({
                str(lane.get("server_code") or "").strip().upper()
                for lane in payload.get("lanes", [])
                if lane.get("server_code")
            })
        else:
            if not record.published_payload_json:
                raise UserError(_("Publish the Parking Layout before changing its runtime state."))
            payload = dict(record.prepare_sync_payload())
            payload["state"] = target_state
            payload["published_revision"] = revision
            edge_codes = sorted({
                str(lane.get("server_code") or "").strip().upper()
                for lane in payload.get("lanes", [])
                if lane.get("server_code")
            })

        record.with_context(nsp_publishing=True).write({
            "state": target_state,
            "published_revision": revision,
            "published_at": fields.Datetime.now(),
            "published_payload_json": json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "published_edge_server_codes": ",".join(edge_codes),
        })
        affected_codes = previous_edge_codes | set(edge_codes)
        if affected_codes:
            affected_edges = self.env["nsp.edge.server"].sudo().with_context(
                active_test=False
            ).search([("edge_server_code", "in", sorted(affected_codes))])
            affected_edges._bump_config_revision()
        return True

    def prepare_sync_payload(self):
        self.ensure_one()
        if not self.published_payload_json:
            return False
        try:
            payload = json.loads(self.published_payload_json)
        except Exception as exc:
            raise ValidationError(_("Published Parking Layout snapshot is invalid.")) from exc
        self._validate_sync_payload_contract(payload)
        return payload

    def is_published_for_edge(self, edge_code):
        self.ensure_one()
        codes = {
            item.strip().upper()
            for item in str(self.published_edge_server_codes or "").split(",")
            if item.strip()
        }
        return str(edge_code or "").strip().upper() in codes



class NspParkingLane(models.Model):
    """Stable physical Lane identity, independent from Parking Layout versions."""

    _name = "nsp.parking.lane"
    _description = "NSP Lane Master"
    _order = "branch_id, name, code, id"
    _rec_name = "display_name"

    name = fields.Char(string="Lane Name", required=True)
    code = fields.Char(
        string="Lane Code", required=True, readonly=True, copy=False, index=True,
        default=lambda self: new_management_code("LANE"),
    )
    branch_id = fields.Many2one(
        "nsp.branch", string="Branch", required=True,
        ondelete="restrict", index=True,
    )
    active = fields.Boolean(default=True, index=True)
    display_name = fields.Char(compute="_compute_display_name", store=True)
    layout_lane_ids = fields.One2many(
        "nsp.parking.layout.lane", "lane_id", string="Lane Configurations",
        readonly=True,
    )
    layout_count = fields.Integer(
        string="Parking Layouts", compute="_compute_layout_count",
    )

    _sql_constraints = [
        ("parking_lane_code_unique", "unique(code)", "Lane Code must be unique."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            values["code"] = str(
                values.get("code") or new_management_code("LANE")
            ).strip().upper()
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if values.get("code"):
            values["code"] = str(values["code"]).strip().upper()
        if values.get("branch_id"):
            target_branch = self.env["nsp.branch"].browse(int(values["branch_id"])).exists()
            for record in self:
                mismatched = record.layout_lane_ids.filtered(
                    lambda item: item.parking_area_id.branch_id != target_branch
                )
                if mismatched:
                    raise ValidationError(_(
                        "Lane Branch cannot be changed while the Lane is referenced by Parking Layout(s) from another Branch: %s"
                    ) % ", ".join(mismatched.mapped("parking_area_id.display_name")))
        return super().write(values)

    @api.depends("name", "code", "branch_id.name")
    def _compute_display_name(self):
        for record in self:
            label = record.name or record.code or _("Lane")
            record.display_name = "%s / %s" % (record.branch_id.name, label) if record.branch_id else label

    @api.depends("layout_lane_ids")
    def _compute_layout_count(self):
        for record in self:
            record.layout_count = len(record.layout_lane_ids)

    @api.model
    def name_search(self, name="", domain=None, operator="ilike", limit=100):
        search_domain = list(domain or [])
        if name:
            search_domain = [
                "|", "|",
                ("name", operator, name),
                ("code", operator, name),
                ("branch_id.name", operator, name),
            ] + search_domain
        records = self.search(search_domain, limit=limit)
        return [(record.id, record.display_name) for record in records]

    @api.model
    def name_create(self, name):
        lane_name = str(name or "").strip()
        if not lane_name:
            raise UserError(_("Lane Name is required."))
        try:
            branch_id = int(self.env.context.get("default_branch_id") or 0)
        except (TypeError, ValueError) as exc:
            raise UserError(_("Invalid Branch context for Lane creation.")) from exc
        if not branch_id:
            raise UserError(_(
                "Lane is an independent master and requires a Branch. "
                "Select a Parking Layout first or use Create and Edit to select the Branch."
            ))
        record = self.create({"name": lane_name, "branch_id": branch_id})
        return record.id, record.display_name


class NspParkingLayoutLane(models.Model):
    """Contextual Lane configuration owned by one Parking Layout revision."""

    _name = "nsp.parking.layout.lane"
    _description = "NSP Lane Configuration"
    _order = "parking_area_id, sequence, lane_id, id"
    _rec_name = "display_name"

    parking_area_id = fields.Many2one(
        "nsp.parking.area", string="Parking Layout", required=True,
        ondelete="cascade", index=True,
    )
    lane_id = fields.Many2one(
        "nsp.parking.lane", string="Lane", required=True,
        ondelete="restrict", index=True,
    )
    sequence = fields.Integer(default=10)
    display_name = fields.Char(compute="_compute_display_name", store=True)
    lane_name = fields.Char(related="lane_id.name", string="Lane Name", readonly=True)
    lane_code = fields.Char(related="lane_id.code", string="Lane Code", readonly=True)
    branch_id = fields.Many2one(related="parking_area_id.branch_id", store=True, readonly=True)

    edge_server_id = fields.Many2one(
        "nsp.edge.server", string="Server", required=True,
        ondelete="restrict", index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True,
        ondelete="restrict", index=True,
    )
    edge_server_name = fields.Char(
        related="edge_server_id.name", string="Server Name", readonly=True,
    )
    edge_server_status = fields.Selection(
        related="edge_server_id.status", string="Server Status", readonly=True,
    )
    controller_name = fields.Char(
        related="controller_id.controller_name", string="Controller Name", readonly=True,
    )
    controller_status = fields.Selection(
        related="controller_id.status", string="Controller Status", readonly=True,
    )
    device_tree_anchor = fields.Boolean(
        string="NSP Device Tree", compute="_compute_device_tree_anchor",
    )
    antenna_sequence_preview_anchor = fields.Boolean(
        string="Antenna Sequence Preview", compute="_compute_antenna_sequence_preview_anchor",
    )
    active = fields.Boolean(default=True, index=True)
    setup_state = fields.Selection(
        [("draft", "Draft"), ("applied", "Applied")],
        string="Lane Setup State", required=True, default="draft", copy=True, index=True,
        help="Draft Layout-Lane configuration cannot be published to Edge until applied.",
    )
    setup_applied_at = fields.Datetime(string="Lane Setup Applied At", readonly=True, copy=False)
    sequence_point_count = fields.Integer(string="Sequence Points", compute="_compute_sequence_point_count")
    antenna_sequence_ids = fields.One2many(
        "nsp.parking.layout.lane.sequence", "layout_lane_id", string="Antenna Sequence",
    )
    reader_config_ids = fields.One2many(
        "nsp.parking.layout.lane.reader.config", "layout_lane_id",
        string="Applied Reader Configuration", copy=True,
    )
    reader_config_count = fields.Integer(string="Configured Readers", compute="_compute_reader_config_count")
    total_path_duration = fields.Float(string="Total Path Duration", compute="_compute_total_path_duration", digits=(8, 3))
    parking_area_state = fields.Selection(related="parking_area_id.state", string="Layout State", readonly=True)
    configuration_state = fields.Selection(
        [("disabled", "Disabled"), ("incomplete", "Needs Attention"), ("ready", "Ready")],
        string="Readiness", compute="_compute_configuration_health",
    )
    configuration_issue = fields.Char(string="Configuration Check", compute="_compute_configuration_health")

    _sql_constraints = [
        (
            "parking_layout_lane_unique", "unique(parking_area_id, lane_id)",
            "A Lane can be referenced only once in one Parking Layout.",
        ),
    ]

    _DIRECT_CONFIGURATION_FIELDS = frozenset({
        "edge_server_id", "controller_id", "reader_config_ids",
        "antenna_sequence_ids",
    })

    @api.constrains("parking_area_id", "lane_id")
    def _check_branch_scope(self):
        for record in self:
            if (
                record.parking_area_id and record.lane_id
                and record.parking_area_id.branch_id != record.lane_id.branch_id
            ):
                raise ValidationError(_(
                    "Lane %(lane)s belongs to Branch %(lane_branch)s and cannot be used in Parking Layout %(layout)s of Branch %(layout_branch)s."
                ) % {
                    "lane": record.lane_id.display_name,
                    "lane_branch": record.lane_id.branch_id.display_name,
                    "layout": record.parking_area_id.display_name,
                    "layout_branch": record.parking_area_id.branch_id.display_name,
                })

    @api.depends("reader_config_ids", "edge_server_id", "controller_id")
    def _compute_device_tree_anchor(self):
        for record in self:
            record.device_tree_anchor = True

    @api.depends(
        "antenna_sequence_ids.sequence", "antenna_sequence_ids.reader_id",
        "antenna_sequence_ids.port_no", "antenna_sequence_ids.duration_from_previous",
    )
    def _compute_antenna_sequence_preview_anchor(self):
        for record in self:
            record.antenna_sequence_preview_anchor = True

    @api.depends("parking_area_id.name", "lane_id.name")
    def _compute_display_name(self):
        for record in self:
            record.display_name = "%s / %s" % (
                record.parking_area_id.name or _("Parking Layout"),
                record.lane_id.name or _("Lane"),
            )

    @api.model_create_multi
    def create(self, vals_list):
        direct_configuration = bool(self.env.context.get("lane_configuration_form"))
        values_list = [dict(values) for values in vals_list]
        if direct_configuration:
            applied_at = fields.Datetime.now()
            for values in values_list:
                values["setup_state"] = "applied"
                values["setup_applied_at"] = applied_at
        records = super().create(values_list)
        records._normalize_first_sequence_duration()
        # Draft saves are intentionally incomplete-friendly. Publish validates the
        # complete Lane Configuration contract before synchronizing to Edge.
        return records

    def write(self, vals):
        direct_configuration = bool(self.env.context.get("lane_configuration_form"))
        values = dict(vals)
        if direct_configuration and self._DIRECT_CONFIGURATION_FIELDS.intersection(values):
            values.update({"setup_state": "applied", "setup_applied_at": fields.Datetime.now()})
        result = super().write(values)
        if "antenna_sequence_ids" in values:
            self._normalize_first_sequence_duration()
        return result

    def action_open_lane_setup(self):
        self.ensure_one()
        self.check_access("write")
        if self.parking_area_id.state != "draft":
            raise ValidationError(_("Lane Setup can be changed only while Parking Layout is Draft."))
        device_lines = [
            (0, 0, {
                "reader_id": config.reader_id.id,
                "power_dbm": int(config.power_dbm or 0),
                "read_interval_ms": int(config.read_interval_ms or 200),
                "tid_start_address": int(config.tid_start_address or 0),
                "tid_length": int(config.tid_length or 4),
            })
            for config in self.reader_config_ids.sorted(lambda row: (row.reader_id.id, row.id))
        ]
        sequence = self.antenna_sequence_ids.sorted(lambda row: (row.sequence or 0, row.id))
        sequence_lines = [
            (0, 0, {
                "sequence": index,
                "reader_id": point.reader_id.id,
                "port_no": int(point.port_no or 0),
                "duration_ms": int(round(float(point.duration_from_previous or 0.0) * 1000.0)),
            })
            for index, point in enumerate(sequence, start=1)
        ]
        wizard = self.env["nsp.lane.setup.wizard"].create({
            "source_scope": "parking_layout",
            "parking_area_id": self.parking_area_id.id,
            "layout_lane_id": self.id,
            "lane_id": self.lane_id.id,
            "edge_server_id": self.edge_server_id.id,
            "controller_id": self.controller_id.id,
            "device_line_ids": device_lines,
            "sequence_line_ids": sequence_lines,
        })
        return {
            "type": "ir.actions.act_window",
            "name": _("Lane Setup"),
            "res_model": "nsp.lane.setup.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "views": [(self.env.ref("nsp_master_gatekeeper.view_nsp_lane_direction_setup_wizard_form").id, "form")],
            "target": "new",
            "context": dict(self.env.context),
        }

    @api.depends("antenna_sequence_ids.duration_from_previous")
    def _compute_total_path_duration(self):
        for record in self:
            record.total_path_duration = sum(record.antenna_sequence_ids.mapped("duration_from_previous"))

    @api.depends("antenna_sequence_ids")
    def _compute_sequence_point_count(self):
        for record in self:
            record.sequence_point_count = len(record.antenna_sequence_ids)

    @api.depends("reader_config_ids")
    def _compute_reader_config_count(self):
        for record in self:
            record.reader_config_count = len(record.reader_config_ids)

    @api.model
    def _default_reader_config_values(self, reader):
        return {
            "reader_id": reader.id,
            "power_dbm": int(reader.power_dbm or 0),
            "read_interval_ms": int(reader.read_interval_ms or 200),
            "tid_start_address": int(reader.tid_addr or 0),
            "tid_length": int(reader.tid_len or 4),
            "source_type": "reader_defaults",
            "source_reference": False,
            "source_revision": 0,
            "applied_at": fields.Datetime.now(),
        }

    def _validate_reader_configs(self):
        for layout_lane in self:
            sequence_readers = layout_lane.antenna_sequence_ids.mapped("reader_id")
            config_by_reader = {config.reader_id.id: config for config in layout_lane.reader_config_ids}
            missing = sequence_readers.filtered(lambda reader: reader.id not in config_by_reader)
            if missing:
                raise ValidationError(_(
                    "Lane %(lane)s is missing Device Configuration for: %(readers)s"
                ) % {
                    "lane": layout_lane.display_name,
                    "readers": ", ".join(missing.mapped("display_name")),
                })
            # Extra configured Readers are valid. Device Configuration is the
            # infrastructure scope; Antenna Sequence may use any subset of it.
            layout_lane.reader_config_ids._validate_parameter_ranges()
        return True

    @api.depends(
        "active", "setup_state", "edge_server_id", "controller_id",
        "antenna_sequence_ids", "antenna_sequence_ids.sequence",
        "antenna_sequence_ids.reader_id", "antenna_sequence_ids.port_no",
        "antenna_sequence_ids.duration_from_previous", "reader_config_ids",
        "reader_config_ids.reader_id", "reader_config_ids.power_dbm",
        "reader_config_ids.read_interval_ms", "reader_config_ids.tid_start_address",
        "reader_config_ids.tid_length",
    )
    def _compute_configuration_health(self):
        for layout_lane in self:
            if not layout_lane.active:
                layout_lane.configuration_state = "disabled"
                layout_lane.configuration_issue = _("Lane is disabled in this Parking Layout.")
                continue
            issues = []
            if not layout_lane.lane_id or not layout_lane.lane_id.active:
                issues.append(_("Lane master is missing or inactive"))
            if layout_lane.setup_state == "draft":
                issues.append(_("Lane Setup is Draft; save Lane Setup before publishing"))
            if not layout_lane.edge_server_id:
                issues.append(_("Server is missing"))
            if not layout_lane.controller_id:
                issues.append(_("Controller is missing"))
            sequence = layout_lane.antenna_sequence_ids.sorted(lambda row: (row.sequence or 0, row.id))
            if len(sequence) < 2:
                issues.append(_("Antenna Sequence needs at least 2 points"))
            keys = [(row.reader_id.id, int(row.port_no or 0)) for row in sequence]
            if len(keys) != len(set(keys)):
                issues.append(_("Antenna Sequence contains duplicate Reader/Antenna points"))
            if any(float(row.duration_from_previous or 0.0) <= 0.0 for row in sequence[1:]):
                issues.append(_("Antenna Sequence points after the first require positive Max Duration"))
            sequence_reader_ids = set(sequence.mapped("reader_id").ids)
            configured_reader_ids = set(layout_lane.reader_config_ids.mapped("reader_id").ids)
            if not sequence_reader_ids.issubset(configured_reader_ids):
                issues.append(_("Device Configuration is missing one or more Readers used by Antenna Sequence"))
            layout_lane.configuration_state = "incomplete" if issues else "ready"
            layout_lane.configuration_issue = "; ".join(issues) if issues else _("Antenna Sequence is ready.")

    @api.model
    def _validate_whitelist_identity(self, record, type_code, label):
        if (
            not record or not record.active or not record.whitelist_id
            or not record.whitelist_id.active
            or record.whitelist_id.device_type_code != type_code
        ):
            raise ValidationError(_("%(label)s must be an active device from Device Whitelist.") % {"label": label})

    def _validate_lane_assembly(self):
        for record in self:
            if not record.active:
                continue
            if not record.lane_id or not record.lane_id.active:
                raise ValidationError(_("Parking Layout can use only an active Lane master."))
            record._check_branch_scope()
            self._validate_whitelist_identity(record.edge_server_id, "SERVER", _("Server"))
            self._validate_whitelist_identity(record.controller_id, "CONTROLLER", _("Controller"))
        return True

    def _normalize_first_sequence_duration(self):
        first_points = self.env["nsp.parking.layout.lane.sequence"].browse()
        for layout_lane in self:
            ordered_points = layout_lane.antenna_sequence_ids.sorted(lambda row: (row.sequence or 0, row.id))
            if ordered_points:
                first_points |= ordered_points[:1]
        first_points = first_points.filtered(lambda point: float(point.duration_from_previous or 0.0) != 0.0)
        if first_points:
            first_points.with_context(
                skip_first_duration_normalization=True,
            ).write({"duration_from_previous": 0.0})
        return True

    def _validate_antenna_sequence(self):
        for layout_lane in self:
            sequence = layout_lane.antenna_sequence_ids.sorted(lambda row: (row.sequence or 0, row.id))
            if len(sequence) < 2:
                raise ValidationError(_("Lane Antenna Sequence requires at least two Antennas."))
            keys = [(row.reader_id.id, int(row.port_no or 0)) for row in sequence]
            if len(keys) != len(set(keys)):
                raise ValidationError(_("A Reader/Antenna can appear only once in one Lane Antenna Sequence."))
            for row in sequence:
                if not row.reader_id.active:
                    raise ValidationError(_("Every Reader used by Lane Setup must be active."))
                layout_lane._validate_whitelist_identity(row.reader_id, "RFID_READER", _("Reader"))
                if not 1 <= int(row.port_no or 0) <= 16:
                    raise ValidationError(_("Lane Setup Antenna/Port must be an integer from 1 to 16."))
            if any(float(row.duration_from_previous or 0.0) <= 0.0 for row in sequence[1:]):
                raise ValidationError(_("Every Antenna after the first requires a positive Max Duration."))
        return True

class NspParkingLayoutLaneReaderConfig(models.Model):
    _name = "nsp.parking.layout.lane.reader.config"
    _description = "NSP Lane Configuration Reader Settings"
    _order = "layout_lane_id, reader_id, id"
    _rec_name = "reader_id"

    layout_lane_id = fields.Many2one(
        "nsp.parking.layout.lane", string="Lane Configuration", required=True, ondelete="cascade", index=True,
    )
    reader_id = fields.Many2one(
        "nsp.device", string="Reader", required=True, ondelete="restrict", index=True,
    )
    reader_name = fields.Char(related="reader_id.name", string="Reader Name", readonly=True)
    reader_serial_number = fields.Char(related="reader_id.serial_number", string="Serial", readonly=True)
    reader_status = fields.Selection(related="reader_id.status", string="Operational Status", readonly=True)
    power_dbm = fields.Integer(string="Power (dBm)", required=True, default=30)
    read_interval_ms = fields.Integer(string="Read Interval (ms)", required=True, default=200)
    tid_start_address = fields.Integer(string="TID Start Address (Words)", required=True, default=0)
    tid_length = fields.Integer(string="TID Length (Words)", required=True, default=4)
    source_type = fields.Selection(
        [("reader_defaults", "Reader Defaults"), ("lane_calibration", "Lane Calibration"), ("manual", "Manual")],
        string="Source", required=True, default="reader_defaults", readonly=True,
    )
    source_reference = fields.Char(string="Source Reference", readonly=True)
    source_revision = fields.Integer(string="Source Revision", readonly=True)
    applied_at = fields.Datetime(string="Applied At", readonly=True)
    port_summary = fields.Char(string="Ports", compute="_compute_port_summary")

    _sql_constraints = [
        ("layout_lane_reader_config_unique", "unique(layout_lane_id, reader_id)", "A Reader can have only one Applied Configuration per Lane Configuration."),
        ("layout_lane_reader_power_range", "CHECK(power_dbm >= 0 AND power_dbm <= 40)", "Reader Power must be between 0 and 40 dBm."),
        ("layout_lane_reader_interval_range", "CHECK(read_interval_ms > 0 AND read_interval_ms <= 60000)", "Read Interval must be between 1 and 60000 ms."),
        ("layout_lane_reader_tid_addr_nonnegative", "CHECK(tid_start_address >= 0)", "TID Start Address cannot be negative."),
        ("layout_lane_reader_tid_length_positive", "CHECK(tid_length > 0)", "TID Length must be greater than zero."),
    ]

    @api.depends(
        "layout_lane_id.antenna_sequence_ids.reader_id",
        "layout_lane_id.antenna_sequence_ids.port_no", "reader_id",
    )
    def _compute_port_summary(self):
        for config in self:
            ports = sorted({
                int(line.port_no or 0)
                for line in config.layout_lane_id.antenna_sequence_ids
                if line.reader_id == config.reader_id and int(line.port_no or 0) > 0
            })
            config.port_summary = ", ".join("P%s" % port for port in ports)

    def _validate_parameter_ranges(self):
        for config in self:
            if config.power_dbm < 0 or config.power_dbm > 40:
                raise ValidationError(_("Reader Power must be between 0 and 40 dBm."))
            if config.read_interval_ms <= 0 or config.read_interval_ms > 60000:
                raise ValidationError(_("Read Interval must be between 1 and 60000 ms."))
            if config.tid_start_address < 0:
                raise ValidationError(_("TID Start Address cannot be negative."))
            if config.tid_length <= 0:
                raise ValidationError(_("TID Length must be greater than zero."))
        return True

    @api.constrains("power_dbm", "read_interval_ms", "tid_start_address", "tid_length")
    def _check_parameter_ranges(self):
        self._validate_parameter_ranges()

    def write(self, vals):
        values = dict(vals)
        technical_fields = {"power_dbm", "read_interval_ms", "tid_start_address", "tid_length"}
        if technical_fields.intersection(values) and not (
            self.env.context.get("lane_calibration_apply") or self.env.context.get("lane_setup")
        ):
            values.update({
                "source_type": "manual", "source_reference": False,
                "source_revision": 0, "applied_at": fields.Datetime.now(),
            })
        result = super().write(values)
        self._validate_parameter_ranges()
        return result


class NspParkingLayoutLaneSequencePoint(models.Model):
    _name = "nsp.parking.layout.lane.sequence"
    _description = "NSP Lane Configuration Antenna Sequence Point"
    _order = "layout_lane_id, sequence, id"

    layout_lane_id = fields.Many2one(
        "nsp.parking.layout.lane", string="Lane Configuration", required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(
        string="Order", required=True,
        help="UI sort priority only; published order is normalized to 1..N.",
    )
    reader_id = fields.Many2one("nsp.device", string="Reader", required=True, ondelete="restrict")
    port_no = fields.Integer(string="Port", required=True)
    duration_from_previous = fields.Float(
        string="Duration from previous (s)", required=True, digits=(8, 3), default=0.0,
    )
    is_first_point = fields.Boolean(string="First Point", compute="_compute_is_first_point")
    cumulative_time = fields.Float(string="Cumulative Time (s)", compute="_compute_cumulative_time", digits=(8, 3))

    _sql_constraints = [
        ("layout_lane_sequence_order_unique", "unique(layout_lane_id, sequence)", "Antenna Sequence Order must be unique per Lane Configuration."),
        ("layout_lane_sequence_reader_port_unique", "unique(layout_lane_id, reader_id, port_no)", "A Reader/Antenna can appear only once per Lane Configuration Antenna Sequence."),
        ("layout_lane_sequence_positive", "CHECK(sequence > 0)", "Antenna Sequence Order must be greater than zero."),
        ("layout_lane_sequence_port_range", "CHECK(port_no >= 1 AND port_no <= 16)", "Antenna Sequence Port must be between 1 and 16."),
        ("layout_lane_sequence_duration_nonnegative", "CHECK(duration_from_previous >= 0)", "Antenna Sequence Duration cannot be negative."),
    ]

    @api.depends("layout_lane_id.antenna_sequence_ids.sequence")
    def _compute_is_first_point(self):
        for record in self:
            ordered_points = record.layout_lane_id.antenna_sequence_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            ) if record.layout_lane_id else self.browse()
            record.is_first_point = bool(ordered_points and ordered_points[0] == record)

    @api.depends(
        "layout_lane_id.antenna_sequence_ids.sequence",
        "layout_lane_id.antenna_sequence_ids.duration_from_previous",
    )
    def _compute_cumulative_time(self):
        for record in self:
            total = 0.0
            for line in record.layout_lane_id.antenna_sequence_ids.sorted(lambda item: (item.sequence or 0, item.id)):
                total += float(line.duration_from_previous or 0.0)
                if line.id == record.id:
                    record.cumulative_time = total
                    break
            else:
                record.cumulative_time = total

    @api.model_create_multi
    def create(self, vals_list):
        layout_lane_ids = {
            int(values.get("layout_lane_id") or 0)
            for values in vals_list
            if int(values.get("layout_lane_id") or 0) and not int(values.get("sequence") or 0)
        }
        max_sequence = {layout_lane_id: 0 for layout_lane_id in layout_lane_ids}
        if layout_lane_ids:
            rows = self._read_group(
                [("layout_lane_id", "in", sorted(layout_lane_ids))],
                ["layout_lane_id"], ["sequence:max"],
            )
            max_sequence.update({record.id: int(value or 0) for record, value in rows})
        prepared = []
        for source in vals_list:
            values = dict(source)
            layout_lane_id = int(values.get("layout_lane_id") or 0)
            if layout_lane_id and not int(values.get("sequence") or 0):
                max_sequence[layout_lane_id] = max_sequence.get(layout_lane_id, 0) + 1
                values["sequence"] = max_sequence[layout_lane_id]
            prepared.append(values)
        records = super().create(prepared)
        layout_lanes = records.mapped("layout_lane_id")
        if not self.env.context.get("skip_first_duration_normalization"):
            layout_lanes._normalize_first_sequence_duration()
        return records

    def write(self, vals):
        layout_lanes = self.mapped("layout_lane_id")
        result = super().write(vals)
        affected = layout_lanes | self.mapped("layout_lane_id")
        if not self.env.context.get("skip_first_duration_normalization"):
            affected._normalize_first_sequence_duration()
        return result

    def unlink(self):
        layout_lanes = self.mapped("layout_lane_id")
        result = super().unlink()
        if not self.env.context.get("skip_first_duration_normalization"):
            layout_lanes._normalize_first_sequence_duration()
        return result

    @api.constrains("sequence", "reader_id", "port_no", "duration_from_previous", "layout_lane_id")
    def _check_antenna_sequence_point(self):
        for record in self:
            if record.sequence <= 0:
                raise ValidationError(_("Antenna Sequence order must be greater than zero."))
            if record.port_no < 1 or record.port_no > 16:
                raise ValidationError(_("Antenna/Port must be between 1 and 16."))
            if not record.reader_id.active:
                raise ValidationError(_("Antenna Sequence Reader must be active."))
            if float(record.duration_from_previous or 0.0) < 0.0:
                raise ValidationError(_("Antenna Sequence Max Duration cannot be negative."))
