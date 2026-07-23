# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError
from odoo.addons.nsp_core.utils import new_management_code


class NspParkingArea(models.Model):
    """Server-owned parking topology and operational configuration.

    Parking topology remains on Cloud/Edge. Controllers receive only the
    technical configuration of the Readers and antenna ports they manage.
    """

    _name = "nsp.parking.area"
    _description = "NSP Parking Operation Configuration"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "name"
    _order = "branch_id, name, id"

    name = fields.Char(string="Parking Area Name", required=True, tracking=True)
    code = fields.Char(
        string="Parking Area Code",
        required=True,
        readonly=True,
        tracking=True,
        copy=False,
        index=True,
        default=lambda self: new_management_code("PARK"),
    )
    branch_id = fields.Many2one(
        "nsp.branch",
        string="Branch",
        required=True,
        ondelete="restrict",
        tracking=True,
        index=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft / Configuring"),
            ("operational", "Operational"),
            ("maintenance", "Maintenance"),
            ("blocked", "Blocked"),
        ],
        string="State",
        default="draft",
        required=True,
        tracking=True,
        index=True,
    )

    lane_ids = fields.One2many(
        "nsp.parking.lane", "parking_area_id", string="Parking Lanes"
    )
    lane_antenna_mapping_ids = fields.Many2many(
        "nsp.parking.lane.antenna.mapping",
        string="Parking Lane Antenna Mapping",
        compute="_compute_topology",
    )
    edge_server_ids = fields.Many2many(
        "nsp.edge.server",
        string="Edge Servers",
        compute="_compute_topology",
        help="Edge Servers derived from Controllers assigned to active lanes.",
    )
    controller_ids = fields.Many2many(
        "nsp.controller",
        string="Controllers",
        compute="_compute_topology",
        search="_search_controllers",
        help="Controllers assigned to active parking lanes.",
    )
    reader_ids = fields.Many2many(
        "nsp.device",
        string="Readers",
        compute="_compute_topology",
        help="Readers whose antenna ports are mapped to this parking operation.",
    )
    antenna_ids = fields.Many2many(
        "nsp.device.antenna",
        string="Antennas",
        compute="_compute_topology",
        help="Reader antenna ports mapped to this parking operation.",
    )

    edge_server_count = fields.Integer(compute="_compute_counts")
    controller_count = fields.Integer(compute="_compute_counts")
    reader_count = fields.Integer(compute="_compute_counts")
    antenna_count = fields.Integer(compute="_compute_counts")
    lane_count = fields.Integer(compute="_compute_counts")
    whitelist_count = fields.Integer(compute="_compute_whitelist_count")

    _sql_constraints = [
        ("code_unique", "unique(code)", "Parking Area Code must be unique."),
    ]

    @api.depends(
        "lane_ids.active",
        "lane_ids.controller_id",
        "lane_ids.controller_id.edge_server_id",
        "lane_ids.antenna_mapping_ids.antenna_ref_id",
        "lane_ids.antenna_mapping_ids.antenna_ref_id.device_id",
    )
    def _compute_topology(self):
        for rec in self:
            active_lanes = rec.lane_ids.filtered("active")
            mappings = active_lanes.mapped("antenna_mapping_ids")
            controllers = active_lanes.mapped("controller_id")
            readers = controllers.mapped("device_ids")
            rec.lane_antenna_mapping_ids = mappings
            rec.reader_ids = readers
            rec.antenna_ids = readers.mapped("antennas_ids")
            rec.controller_ids = controllers
            rec.edge_server_ids = controllers.mapped("edge_server_id")

    @api.model
    def _search_controllers(self, operator, value):
        return [("lane_ids.controller_id", operator, value)]

    @api.depends(
        "edge_server_ids",
        "controller_ids",
        "reader_ids",
        "antenna_ids",
        "lane_ids.active",
    )
    def _compute_counts(self):
        for rec in self:
            rec.edge_server_count = len(rec.edge_server_ids)
            rec.controller_count = len(rec.controller_ids)
            rec.reader_count = len(rec.reader_ids)
            rec.antenna_count = len(rec.antenna_ids)
            rec.lane_count = len(rec.lane_ids.filtered("active"))

    def _compute_whitelist_count(self):
        count = self.env["nsp.device.whitelist"].sudo().search_count([])
        for rec in self:
            rec.whitelist_count = count

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        Branch = self.env["nsp.branch"].sudo()
        default_branch = (
            Branch.get_default_branch()
            if hasattr(Branch, "get_default_branch")
            else Branch.search([], limit=1)
        )
        prepared = []
        for source in vals_list:
            vals = dict(source)
            if not vals.get("branch_id") and default_branch:
                vals["branch_id"] = default_branch.id
            vals["code"] = self._normalize_code(
                vals.get("code") or new_management_code("PARK")
            )
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "code" in values:
            values["code"] = self._normalize_code(values.get("code"))
        return super().write(values)

    def _controller_payload(self):
        """Return Reader technical configuration required by Edge.

        This is Cloud/Edge configuration. The Controller-facing API removes
        server-only inventory fields and returns only serial number, Reader
        parameters and antenna technical settings.
        """
        self.ensure_one()
        result = []
        for controller in self.controller_ids.sorted(
            key=lambda item: (item.controller_id or "", item.id)
        ):
            devices = self.reader_ids.filtered(
                lambda item: item.controller_id == controller
            ).sorted(key=lambda item: (item.serial_number or "", item.id))
            result.append(
                {
                    "controller_code": controller.controller_id,
                    "devices": [device._build_edge_config_payload() for device in devices],
                }
            )
        return result

    def _lane_payload(self):
        """Return parking topology for Cloud/Edge synchronization."""
        self.ensure_one()
        result = []
        lanes = self.lane_ids.filtered("active").sorted(
            key=lambda item: (item.lane_no, item.id)
        )
        for lane in lanes:
            mappings = []
            for mapping in lane.antenna_mapping_ids.sorted(
                key=lambda item: (
                    item.zone or "",
                    item.serial_number or "",
                    item.antenna_no,
                    item.id,
                )
            ):
                mapping_payload = {
                    "serial_number": mapping.serial_number or "",
                    "antenna_no": int(mapping.antenna_no or 0),
                }
                if lane.direction == "both":
                    mapping_payload["zone"] = mapping.zone
                mappings.append(mapping_payload)
            lane_payload = {
                "lane_code": lane.code,
                "lane_name": lane.name,
                "lane_no": int(lane.lane_no or 0),
                "controller_code": lane.controller_id.controller_id,
                "direction": lane.direction,
                "grouping_window_seconds": int(lane.grouping_window_seconds or 3),
                "repeat_suppression_seconds": int(lane.repeat_suppression_seconds or 1),
                "antenna_mappings": mappings,
            }
            if lane.direction == "both":
                lane_payload["transition_window_seconds"] = int(
                    lane.transition_window_seconds or 10
                )
            result.append(lane_payload)
        return result

    def _operational_issues(self):
        self.ensure_one()
        issues = []
        lanes = self.lane_ids.filtered("active")
        if not lanes:
            return [_('Configure at least one active Parking Lane.')]

        for lane in lanes:
            lane_name = lane.display_name or lane.name or _("Lane")
            if not lane.controller_id:
                issues.append(
                    _("Lane %(lane)s must have a Controller.") % {"lane": lane_name}
                )
                continue

            mappings = lane.antenna_mapping_ids
            if not mappings:
                issues.append(
                    _("Lane %(lane)s must have at least one antenna mapping.")
                    % {"lane": lane_name}
                )
                continue

            if lane.direction == "both":
                mapped_zones = set(mappings.mapped("zone"))
                missing_zones = {"outside", "inside"} - mapped_zones
                if missing_zones:
                    issues.append(
                        _("Two-way Lane %(lane)s requires antenna mappings for zones: %(zones)s.")
                        % {
                            "lane": lane_name,
                            "zones": ", ".join(sorted(missing_zones)),
                        }
                    )
            elif mappings.filtered("zone"):
                issues.append(
                    _("One-way Lane %(lane)s must not configure antenna zones.")
                    % {"lane": lane_name}
                )

            wrong_scope = mappings.filtered(
                lambda item: item.controller_id != lane.controller_id
            )
            if wrong_scope:
                issues.append(
                    _("All antennas of lane %(lane)s must belong to Controller %(controller)s.")
                    % {
                        "lane": lane_name,
                        "controller": lane.controller_id.controller_id,
                    }
                )

            invalid = mappings.filtered(lambda item: not item.device_id._is_whitelisted())
            if invalid:
                issues.append(
                    _("Lane %(lane)s contains a Reader that is not in Device Whitelist.")
                    % {"lane": lane_name}
                )
        return issues

    def _open_related_action(self, action_xmlid, records, name, context=None):
        self.ensure_one()
        action = self.env.ref(action_xmlid).sudo().read()[0]
        action.update(
            {
                "name": name,
                "domain": [("id", "in", records.ids)] if records else [],
                "context": dict(context or {}),
            }
        )
        return action

    def action_open_edge_servers(self):
        self.ensure_one()
        return self._open_related_action(
            "nsp_gatekeeper.action_nsp_edge_servers",
            self.edge_server_ids,
            _("Edge Servers"),
        )

    def action_open_controllers(self):
        self.ensure_one()
        context = {}
        if len(self.edge_server_ids) == 1:
            context["default_edge_server_id"] = self.edge_server_ids.id
        return self._open_related_action(
            "nsp_gatekeeper.action_nsp_controllers",
            self.controller_ids,
            _("Controllers"),
            context,
        )

    def action_open_readers(self):
        self.ensure_one()
        context = {}
        if len(self.controller_ids) == 1:
            context["default_controller_id"] = self.controller_ids.id
        return self._open_related_action(
            "nsp_gatekeeper.nsp_device_action", self.reader_ids, _("Readers"), context
        )

    def action_open_antennas(self):
        self.ensure_one()
        context = {}
        if len(self.reader_ids) == 1:
            context["default_device_id"] = self.reader_ids.id
        return self._open_related_action(
            "nsp_gatekeeper.action_nsp_device_antenna",
            self.antenna_ids,
            _("Antennas"),
            context,
        )

    def action_open_device_whitelist(self):
        self.ensure_one()
        action = self.env.ref("nsp_gatekeeper.nsp_device_whitelist_action").sudo().read()[0]
        action.update({"name": _("Device Whitelist"), "domain": [], "context": {}})
        return action

    def action_open_lanes(self):
        self.ensure_one()
        action = self.env.ref("nsp_gatekeeper.action_nsp_parking_lane").sudo().read()[0]
        action.update(
            {
                "name": _("Parking Lanes"),
                "domain": [("parking_area_id", "=", self.id)],
                "context": {"default_parking_area_id": self.id},
            }
        )
        return action

    def action_open_antenna_mappings(self):
        self.ensure_one()
        action = self.env.ref(
            "nsp_gatekeeper.action_nsp_parking_lane_antenna_mapping"
        ).sudo().read()[0]
        action.update(
            {
                "name": _("Parking Lane Antenna Mapping"),
                "domain": [("lane_id.parking_area_id", "=", self.id)],
                "context": {},
            }
        )
        return action

    def action_set_operational(self):
        for rec in self:
            issues = rec._operational_issues()
            if issues:
                raise UserError("\n".join(issues))
        self.write({"state": "operational"})
        return True

    def action_reset_to_draft(self):
        self.write({"state": "draft"})
        return True

    def action_set_maintenance(self):
        self.write({"state": "maintenance"})
        return True

    def action_set_blocked(self):
        self.write({"state": "blocked"})
        return True

    def prepare_sync_payload(self):
        self.ensure_one()
        return {
            "parking_area_code": self.code,
            "parking_area_name": self.name,
            "branch_code": self.branch_id.code or "",
            "state": self.state,
            "controllers": self._controller_payload(),
            "lanes": self._lane_payload(),
        }


class NspParkingLane(models.Model):
    _name = "nsp.parking.lane"
    _description = "NSP Parking Lane"
    _order = "parking_area_id, lane_no, id"
    _rec_name = "display_name"

    name = fields.Char(string="Lane Name", required=True, default="Lane")
    code = fields.Char(
        string="Lane Code",
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda self: new_management_code("LANE"),
    )
    display_name = fields.Char(compute="_compute_display_name", store=True)
    parking_area_id = fields.Many2one(
        "nsp.parking.area",
        string="Parking Area",
        required=True,
        ondelete="cascade",
        index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller",
        string="Controller",
        required=True,
        ondelete="restrict",
        index=True,
        help="Controller that owns every Reader and antenna mapped to this lane.",
    )
    lane_no = fields.Integer(string="Lane No.", default=1, required=True)
    direction = fields.Selection(
        [
            ("entry", "Entry"),
            ("exit", "Exit"),
            ("both", "Two-way"),
        ],
        required=True,
        default="entry",
        index=True,
        help=(
            "Physical operating direction of the lane. Two-way direction is resolved "
            "from Outside/Inside antenna-zone transitions at Edge."
        ),
    )
    transition_window_seconds = fields.Integer(
        string="Transition Window (Seconds)",
        default=10,
        required=True,
        help=(
            "For a Two-way lane, maximum time between detections of the same RFID card "
            "in opposite antenna zones. Outside to Inside resolves Entry; Inside to "
            "Outside resolves Exit."
        ),
    )
    grouping_window_seconds = fields.Integer(
        string="Grouping Window (Seconds)",
        default=3,
        required=True,
        help=(
            "For Check-out, Edge pairs the vehicle with the nearest unused User RFID "
            "detection in this time window. Check-in never requires User RFID."
        ),
    )
    repeat_suppression_seconds = fields.Integer(
        string="Repeat Read Suppression (Seconds)",
        default=1,
        required=True,
        help=(
            "For One-way lanes, Edge suppresses the same RFID card across the whole Lane. "
            "For Two-way lanes, suppression is per antenna so a read in the opposite zone "
            "is preserved for movement resolution. event_uid handles request retries separately."
        ),
    )
    active = fields.Boolean(default=True, index=True)
    antenna_mapping_ids = fields.One2many(
        "nsp.parking.lane.antenna.mapping", "lane_id", string="Antenna Mapping"
    )
    antenna_count = fields.Integer(compute="_compute_antenna_count")

    _sql_constraints = [
        (
            "lane_code_per_area_unique",
            "unique(parking_area_id, code)",
            "Lane Code must be unique within a Parking Area.",
        ),
        (
            "lane_no_per_area_unique",
            "unique(parking_area_id, lane_no)",
            "Lane number must be unique within a Parking Area.",
        ),
        ("lane_no_positive", "CHECK(lane_no > 0)", "Lane number must be greater than zero."),
        (
            "transition_window_positive",
            "CHECK(transition_window_seconds >= 1)",
            "Transition window must be at least one second.",
        ),
        (
            "grouping_window_positive",
            "CHECK(grouping_window_seconds >= 1)",
            "Grouping window must be at least one second.",
        ),
        (
            "repeat_suppression_positive",
            "CHECK(repeat_suppression_seconds >= 1)",
            "Repeat read suppression must be at least one second.",
        ),
    ]


    @api.depends("parking_area_id.name", "name", "lane_no")
    def _compute_display_name(self):
        for rec in self:
            lane_name = rec.name or (_("Lane %s") % (rec.lane_no or ""))
            rec.display_name = "%s / %s" % (
                rec.parking_area_id.name or _("Parking"),
                lane_name,
            )

    @api.depends("antenna_mapping_ids")
    def _compute_antenna_count(self):
        for rec in self:
            rec.antenna_count = len(rec.antenna_mapping_ids)

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["code"] = self._normalize_code(
                vals.get("code") or new_management_code("LANE")
            )
            if vals.get("direction") not in ("entry", "exit", "both"):
                vals["direction"] = "entry"
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "code" in values:
            values["code"] = self._normalize_code(values.get("code"))
        result = super().write(values)
        if "controller_id" in values:
            invalid = self.mapped("antenna_mapping_ids").filtered(
                lambda item: item.controller_id != item.lane_id.controller_id
            )
            if invalid:
                raise ValidationError(
                    _("Existing antenna mappings do not belong to the selected Lane Controller.")
                )
        return result


class NspParkingLaneAntennaMapping(models.Model):
    _name = "nsp.parking.lane.antenna.mapping"
    _description = "NSP Parking Lane Antenna Mapping"
    _order = "lane_id, zone, id"
    _rec_name = "rule_name"

    rule_name = fields.Char(compute="_compute_rule_name")
    lane_id = fields.Many2one(
        "nsp.parking.lane",
        string="Parking Lane",
        required=True,
        ondelete="cascade",
        index=True,
    )
    parking_area_id = fields.Many2one(
        "nsp.parking.area", related="lane_id.parking_area_id", readonly=True
    )
    lane_controller_id = fields.Many2one(
        "nsp.controller", related="lane_id.controller_id", readonly=True
    )
    zone = fields.Selection(
        [("outside", "Outside"), ("inside", "Inside")],
        string="Zone",
        index=True,
        help=(
            "Used only for Two-way lanes. Outside → Inside resolves Entry; "
            "Inside → Outside resolves Exit. Leave empty on one-way lanes."
        ),
    )
    antenna_ref_id = fields.Many2one(
        "nsp.device.antenna",
        string="Antenna",
        required=True,
        ondelete="restrict",
        index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller", related="antenna_ref_id.device_id.controller_id", readonly=True
    )
    device_id = fields.Many2one(
        "nsp.device", related="antenna_ref_id.device_id", readonly=True
    )
    serial_number = fields.Char(related="device_id.serial_number", readonly=True)
    antenna_no = fields.Integer(related="antenna_ref_id.antenna_no", readonly=True)
    minimum_rssi_dbm = fields.Float(
        related="antenna_ref_id.minimum_rssi_dbm", readonly=True
    )

    _sql_constraints = [
        (
            "unique_antenna_mapping",
            "unique(antenna_ref_id)",
            "An antenna can be mapped to only one Parking Lane.",
        ),
    ]

    @api.depends(
        "parking_area_id.name",
        "lane_id.name",
        "lane_id.direction",
        "device_id.name",
        "antenna_no",
        "zone",
    )
    def _compute_rule_name(self):
        for rec in self:
            suffix = rec.zone.title() if rec.zone else rec.lane_id.direction.title()
            rec.rule_name = "%s / %s / %s / Antenna %s / %s" % (
                rec.parking_area_id.name or _("Parking"),
                rec.lane_id.name or _("Lane"),
                rec.device_id.name or rec.serial_number or _("Reader"),
                rec.antenna_no or "",
                suffix or "",
            )

    @api.onchange("lane_id")
    def _onchange_lane_id(self):
        for rec in self:
            if rec.lane_id and rec.lane_id.direction != "both":
                rec.zone = False

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        Lane = self.env["nsp.parking.lane"].sudo()
        for source in vals_list:
            vals = dict(source)
            lane = Lane.browse(vals.get("lane_id")).exists()
            if lane and lane.direction != "both":
                vals["zone"] = False
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        lane = self.env["nsp.parking.lane"].sudo().browse(
            values.get("lane_id")
        ).exists() if values.get("lane_id") else self[:1].lane_id
        if lane and lane.direction != "both":
            values["zone"] = False
        return super().write(values)

    @api.constrains("lane_id", "antenna_ref_id", "zone")
    def _check_mapping(self):
        for rec in self:
            if rec.controller_id != rec.lane_id.controller_id:
                raise ValidationError(
                    _("Antenna must belong to the Controller assigned to this Lane.")
                )
            if not rec.device_id._is_whitelisted():
                rec.device_id._notify_not_whitelisted()
                raise ValidationError(
                    _("Antenna must belong to a Reader in Device Whitelist.")
                )
            if rec.lane_id.direction == "both" and rec.zone not in ("outside", "inside"):
                raise ValidationError(_(
                    "A Two-way Lane antenna mapping requires Zone = Outside or Inside."
                ))
            if rec.lane_id.direction != "both" and rec.zone:
                raise ValidationError(_(
                    "A one-way Lane antenna mapping must not define a zone."
                ))
