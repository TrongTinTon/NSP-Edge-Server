# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.nsp_core.utils import new_management_code


class Device(models.Model):
    """Stable RFID Reader identity on Edge.

    Reader identity is deliberately independent from Controller, Parking Layout,
    Lane and Lane Calibration. Physical Controller<->Reader association exists
    only in contextual runtime configuration / observation records.
    """

    _name = "nsp.device"
    _description = "NSP RFID Reader Identity"
    _rec_name = "name"
    _order = "serial_number, device_code, id"

    whitelist_id = fields.Many2one(
        "nsp.device.whitelist", string="Device Whitelist", readonly=True, copy=False,
        ondelete="set null", index=True,
    )
    name = fields.Char(string="Reader Name", required=True, default="RFID Reader", index=True)
    serial_number = fields.Char(
        string="Serial", required=True, copy=False, index=True,
        help="Physical Reader SDK SerialNumber. Globally unique on this Edge database.",
    )
    device_code = fields.Char(
        string="Device Code", required=True, readonly=True, copy=False, index=True,
        default=lambda self: new_management_code("DEV"),
    )
    active = fields.Boolean(default=True, index=True)
    cloud_removed = fields.Boolean(default=False, readonly=True, index=True, copy=False)

    # Observation projection only. These fields are not ownership/configuration.
    status = fields.Selection(
        [("online", "Online"), ("offline", "Offline"), ("degraded", "Degraded")],
        string="Observed Status", compute="_compute_observation_summary", search="_search_status",
    )
    last_seen = fields.Datetime(string="Last Seen", compute="_compute_observation_summary")
    firmware_version = fields.Char(string="Firmware Version", compute="_compute_observation_summary")
    runtime_power_dbm = fields.Integer(
        string="Observed Power (dBm)", compute="_compute_observation_summary",
    )
    runtime_read_interval_ms = fields.Integer(
        string="Observed Read Interval ms", compute="_compute_observation_summary",
    )
    runtime_ports_json = fields.Text(
        string="Observed Ports", compute="_compute_observation_summary",
    )
    observation_count = fields.Integer(
        string="Controller Observations", compute="_compute_observation_summary",
    )

    _sql_constraints = [
        ("serial_number_unique", "unique(serial_number)", "Reader Serial must be unique."),
        ("device_code_unique", "unique(device_code)", "Reader Device Code must be unique."),
    ]

    @api.model
    def _normalize_serial(self, value):
        return str(value or "").strip().upper()

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model
    def _serial_conflict(self, serial, exclude_ids=None):
        normalized = self._normalize_serial(serial)
        if not normalized:
            return self.browse()
        domain = [("serial_number", "=", normalized)]
        if exclude_ids:
            domain.append(("id", "not in", list(exclude_ids)))
        return self.with_context(active_test=False).search(domain, limit=1)

    @api.model
    def _raise_serial_conflict(self, serial, conflict=None):
        normalized = self._normalize_serial(serial)
        if conflict:
            raise ValidationError(_(
                "Reader Serial '%(serial)s' already exists on Reader '%(reader)s'. "
                "Reader identity is global and cannot be owned by a Controller."
            ) % {"serial": normalized, "reader": conflict.display_name})
        raise ValidationError(_(
            "Reader Serial '%s' is entered more than once. Reader Serial must be globally unique."
        ) % normalized)

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        serials = set()
        codes = set()
        for source in vals_list:
            vals = dict(source)
            # Reject legacy ownership/config fields explicitly so old callers cannot
            # silently recreate the previous topology model.
            forbidden = {
                "controller_id", "power_dbm", "read_interval_ms", "tid_addr", "tid_len",
                "last_seen", "firmware_version", "runtime_power_dbm",
                "runtime_read_interval_ms", "runtime_ports_json", "status",
            } & set(vals)
            if forbidden:
                raise ValidationError(_(
                    "Reader Master does not accept contextual/runtime field(s): %s"
                ) % ", ".join(sorted(forbidden)))
            serial = self._normalize_serial(vals.get("serial_number"))
            if not serial:
                raise ValidationError(_("Reader Serial is required."))
            if serial in serials:
                self._raise_serial_conflict(serial)
            serials.add(serial)
            code = self._normalize_code(vals.get("device_code") or new_management_code("DEV"))
            if not code or code in codes:
                raise ValidationError(_("Reader Device Code is required and must be unique."))
            codes.add(code)
            vals["serial_number"] = serial
            vals["name"] = str(vals.get("name") or serial or "RFID Reader").strip()
            vals["device_code"] = code
            prepared.append(vals)

        existing = self.with_context(active_test=False).search([
            "|", ("serial_number", "in", sorted(serials)), ("device_code", "in", sorted(codes)),
        ], limit=1)
        if existing:
            if existing.serial_number in serials:
                self._raise_serial_conflict(existing.serial_number, existing)
            raise ValidationError(_("Reader Device Code '%s' already exists.") % existing.device_code)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        forbidden = {
            "controller_id", "power_dbm", "read_interval_ms", "tid_addr", "tid_len",
            "last_seen", "firmware_version", "runtime_power_dbm",
            "runtime_read_interval_ms", "runtime_ports_json", "status",
        } & set(values)
        if forbidden:
            raise ValidationError(_(
                "Reader Master does not accept contextual/runtime field(s): %s"
            ) % ", ".join(sorted(forbidden)))
        if "serial_number" in values:
            serial = self._normalize_serial(values.get("serial_number"))
            if not serial:
                raise ValidationError(_("Reader Serial is required."))
            if len(self) > 1:
                self._raise_serial_conflict(serial)
            conflict = self._serial_conflict(serial, exclude_ids=self.ids)
            if conflict:
                self._raise_serial_conflict(serial, conflict)
            values["serial_number"] = serial
        if "name" in values:
            values["name"] = str(values.get("name") or "").strip() or "RFID Reader"
        if "device_code" in values:
            code = self._normalize_code(values.get("device_code"))
            if not code:
                raise ValidationError(_("Reader Device Code is required."))
            conflict = self.with_context(active_test=False).search([
                ("device_code", "=", code), ("id", "not in", self.ids),
            ], limit=1)
            if conflict:
                raise ValidationError(_("Reader Device Code '%s' already exists.") % code)
            values["device_code"] = code
        return super().write(values)

    @api.constrains("serial_number", "device_code")
    def _check_declaration(self):
        for reader in self:
            if not self._normalize_serial(reader.serial_number):
                raise ValidationError(_("Reader Serial is required."))
            if not self._normalize_code(reader.device_code):
                raise ValidationError(_("Device Code is required."))

    @api.model
    def _observation_rank(self, observation):
        candidates = [
            value for value in (
                observation.last_detection_at,
                observation.last_seen_at,
                observation.last_reported_at,
            ) if value
        ]
        return max(candidates) if candidates else fields.Datetime.to_datetime("1970-01-01 00:00:00")

    @api.model
    def _latest_observation_by_serial(self, serials):
        normalized = {self._normalize_serial(value) for value in serials if value}
        if not normalized:
            return {}, {}
        observations = self.env["nsp.reader.observation"].sudo().search([
            ("serial_number", "in", sorted(normalized)),
        ])
        latest = {}
        counts = {}
        for observation in observations:
            serial = self._normalize_serial(observation.serial_number)
            counts[serial] = counts.get(serial, 0) + 1
            current = latest.get(serial)
            if not current or self._observation_rank(observation) > self._observation_rank(current):
                latest[serial] = observation
        return latest, counts

    def _compute_observation_summary(self):
        latest, counts = self._latest_observation_by_serial(self.mapped("serial_number"))
        for reader in self:
            serial = self._normalize_serial(reader.serial_number)
            observation = latest.get(serial)
            reader.observation_count = counts.get(serial, 0)
            if not observation:
                reader.status = "offline"
                reader.last_seen = False
                reader.firmware_version = False
                reader.runtime_power_dbm = 0
                reader.runtime_read_interval_ms = 0
                reader.runtime_ports_json = "[]"
                continue
            reader.status = observation.status or "offline"
            times = [
                value for value in (
                    observation.last_detection_at,
                    observation.last_seen_at,
                    observation.last_reported_at,
                ) if value
            ]
            reader.last_seen = max(times) if times else False
            reader.firmware_version = observation.firmware_version or False
            reader.runtime_power_dbm = int(observation.power_dbm or 0)
            reader.runtime_read_interval_ms = int(observation.read_interval_ms or 0)
            reader.runtime_ports_json = observation.ports_json or "[]"

    @api.model
    def _search_status(self, operator, value):
        if operator not in ("=", "!=", "in", "not in"):
            raise ValidationError(_("Observed Reader Status supports only equality filters."))
        values = value if operator in ("in", "not in") and isinstance(value, (list, tuple)) else [value]
        statuses = {str(item or "").strip().lower() for item in values}
        observations = self.env["nsp.reader.observation"].sudo().search([])
        latest = {}
        for observation in observations:
            serial = self._normalize_serial(observation.serial_number)
            current = latest.get(serial)
            if not current or self._observation_rank(observation) > self._observation_rank(current):
                latest[serial] = observation
        matching_serials = {
            serial for serial, observation in latest.items()
            if str(observation.status or "offline").lower() in statuses
        }
        # No observation is equivalent to offline from the Reader Master view.
        if "offline" in statuses:
            all_serials = set(self.with_context(active_test=False).search([]).mapped("serial_number"))
            matching_serials |= {self._normalize_serial(s) for s in all_serials if self._normalize_serial(s) not in latest}
        positive = operator in ("=", "in")
        return [("serial_number", "in" if positive else "not in", sorted(matching_serials))]

    def runtime_profile_for_controller(self, controller):
        """Return one physical Reader profile derived from Edge runtime contexts.

        Parking Layout and Lane Calibration may both reference the same Reader.
        Parking profiles must be identical across logical Lanes. An active Lane
        Calibration profile is an explicit execution override for Reader-level
        parameters. Port/Antenna topology stays on Edge and is never part of the
        Controller execution contract.
        """
        self.ensure_one()
        if not controller:
            return False

        ports = set()
        parking_profiles = set()
        parking_configs = self.env["nsp.parking.layout.lane.reader.config"].sudo().search([
            ("reader_id", "=", self.id),
            ("layout_lane_id.controller_id", "=", controller.id),
            ("layout_lane_id.active", "=", True),
            ("layout_lane_id.parking_area_id.state", "in", ["operational", "maintenance", "blocked"]),
        ])
        for config in parking_configs:
            parking_profiles.add((
                int(config.power_dbm or 0),
                int(config.read_interval_ms or 0),
                int(config.tid_start_address or 0),
                int(config.tid_length or 0),
            ))
            ports.update(int(value) for value in config.port_ids.mapped("port_no") if int(value or 0) > 0)
        if len(parking_profiles) > 1:
            raise ValidationError(_(
                "Reader %(reader)s has conflicting physical parameters across active Parking Lane Configurations."
            ) % {"reader": self.display_name})

        calibration_profiles = set()
        nodes = self.env["nsp.measurement.device.node"].sudo().search([
            ("device_type", "=", "reader"),
            ("reader_id", "=", self.id),
            ("parent_id.device_type", "=", "controller"),
            ("parent_id.controller_id", "=", controller.id),
            ("session_id.status", "in", ["ready", "running"]),
        ])
        for node in nodes:
            calibration_profiles.add((
                int(node.power_dbm or 0),
                int(node.read_interval_ms or 0),
                int(node.tid_addr or 0),
                int(node.tid_len or 0),
            ))
            ports.update(
                int(value) for value in node.reader_port_ids.mapped("port_no") if int(value or 0) > 0
            )
        if len(calibration_profiles) > 1:
            raise ValidationError(_(
                "Reader %(reader)s has conflicting active Lane Calibration profiles."
            ) % {"reader": self.display_name})

        if not parking_profiles and not calibration_profiles:
            return False
        # Lane Calibration is a technical execution context, not Parking business.
        # When present it owns the Reader-level commands for the calibration run.
        profile = next(iter(calibration_profiles or parking_profiles))
        return {
            "reader_id": self.id,
            "serial_number": self.serial_number or "",
            "technical_code": self.device_code or "",
            "power_dbm": profile[0],
            "read_interval_ms": profile[1],
            "tid_start_address": profile[2],
            "tid_length": profile[3],
            # Kept only for Edge-side validation/diagnostics. Controller must not
            # receive or use this set to filter physical SDK detections.
            "ports": sorted(ports),
        }

    def build_controller_config_payload(self, controller):
        """Return the minimal physical configuration consumed by Controller."""
        self.ensure_one()
        profile = self.runtime_profile_for_controller(controller)
        if not profile:
            return False
        return {
            "serial_number": profile["serial_number"],
            "reader_parameters": {
                "power_dbm": profile["power_dbm"],
                "read_interval_ms": profile["read_interval_ms"],
                "tid_start_address": profile["tid_start_address"],
                "tid_length": profile["tid_length"],
            },
        }
