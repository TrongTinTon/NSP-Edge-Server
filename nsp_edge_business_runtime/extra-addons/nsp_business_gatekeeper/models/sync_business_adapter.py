# -*- coding: utf-8 -*-
import logging
from datetime import timedelta, timezone

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services.raw_rfid_tag import normalize_raw_tid

_logger = logging.getLogger(__name__)


class NspSyncBusinessAdapter(models.Model):
    _inherit = "nsp.sync.job"

    def _require_edge_server_record(self):
        """One Edge database represents exactly one Edge node.

        Edge Server is a Cloud master object and is deliberately not stored on Edge.
        Runtime scope is the local database plus auth_id.edge_server_code.
        """
        self.ensure_one()
        return False

    def _managed_runtime_status_scope(self):
        """Return the Controllers and Readers currently managed by this Edge runtime.

        Runtime scope is the union of the applied Parking Layout and active Lane
        Calibration projections. Local master/cache records that are not referenced
        by either projection must not be reported through ``edge/status``.
        """
        self.ensure_one()
        Controller = self.env["nsp.controller"].sudo()
        Device = self.env["nsp.device"].sudo()
        controllers = Controller.browse()
        devices_by_controller = {}

        def register(controller, devices):
            nonlocal controllers
            if not controller or not controller.active or controller.cloud_removed:
                return
            controllers |= controller
            current = devices_by_controller.get(controller.id, Device.browse())
            valid_devices = devices.filtered(
                lambda record: record.active and not record.cloud_removed
            )
            devices_by_controller[controller.id] = current | valid_devices

        LayoutLane = self.env["nsp.parking.layout.lane"].sudo()
        lane_configs = LayoutLane.search([
            ("active", "=", True),
            ("parking_area_id.state", "in", ["operational", "maintenance", "blocked"]),
        ])
        for config in lane_configs:
            register(config.controller_id, config.reader_config_ids.mapped("reader_id"))

        DeviceNode = self.env["nsp.measurement.device.node"].sudo()
        calibration_nodes = DeviceNode.search([
            ("device_type", "=", "reader"),
            ("session_id.status", "in", ["ready", "running"]),
        ])
        for node in calibration_nodes:
            controller_node = node.parent_id if node.parent_id.device_type == "controller" else False
            register(controller_node.controller_id if controller_node else False, node.reader_id)

        return controllers.sorted(
            key=lambda record: (record.controller_id or "", record.id)
        ), devices_by_controller

    def _serialize_edge_server_status(self):
        self.ensure_one()
        managed_controllers, devices_by_controller = self._managed_runtime_status_scope()
        timeout_sec = int(self.env["ir.config_parameter"].sudo().get_param(
            "nsp_business_gatekeeper.reader_observation_timeout_sec", "120"
        ) or 120)
        fresh_after = fields.Datetime.now() - timedelta(seconds=max(timeout_sec, 30))

        controller_ids = managed_controllers.ids
        observations = self.env["nsp.reader.observation"].sudo().search([
            ("controller_id", "in", controller_ids),
        ]) if controller_ids else self.env["nsp.reader.observation"].browse()
        observation_by_key = {
            (record.controller_id.id, str(record.serial_number or "").strip().upper()): record
            for record in observations
        }

        controllers = []
        for controller in managed_controllers:
            devices = []
            managed_devices = devices_by_controller.get(
                controller.id, self.env["nsp.device"].browse()
            )
            for device in managed_devices.sorted(
                key=lambda record: (record.device_code or "", record.id)
            ):
                expected_serial = str(device.serial_number or "").strip().upper()
                observation = observation_by_key.get((controller.id, expected_serial))
                report_fresh = bool(
                    observation
                    and observation.last_reported_at
                    and observation.last_reported_at >= fresh_after
                )
                detection_fresh = bool(
                    observation
                    and observation.last_detection_at
                    and observation.last_detection_at >= fresh_after
                )
                if detection_fresh:
                    # A fresh data-plane detection is stronger evidence than a stale
                    # periodic status report.
                    observed_status = "online"
                elif report_fresh:
                    observed_status = str(observation.status or "offline").lower()
                else:
                    observed_status = "offline"
                if observed_status not in ("online", "offline", "degraded"):
                    observed_status = "offline"

                last_seen_candidates = []
                if observation and observation.last_seen_at:
                    last_seen_candidates.append(observation.last_seen_at)
                if observation and observation.last_detection_at:
                    last_seen_candidates.append(observation.last_detection_at)
                effective_last_seen = max(last_seen_candidates) if last_seen_candidates else False

                runtime_profile = device.runtime_profile_for_controller(controller) or {}
                item = {
                    "reader_code": device.device_code or "",
                    "serial_number": expected_serial,
                    "status": observed_status,
                    "last_seen_at": self._dt(effective_last_seen) if effective_last_seen else False,
                    "last_detection_at": (
                        self._dt(observation.last_detection_at)
                        if observation and observation.last_detection_at else False
                    ),
                    "last_detection_port_no": int(
                        observation.last_detection_port_no or 0
                    ) if observation else 0,
                    "firmware_version": (
                        observation.firmware_version if observation else ""
                    ) or "",
                    "power_dbm": int(
                        observation.power_dbm
                        if observation and observation.power_dbm is not None
                        else runtime_profile.get("power_dbm") or 0
                    ),
                    "read_interval_ms": int(
                        observation.read_interval_ms
                        if observation and observation.read_interval_ms
                        else runtime_profile.get("read_interval_ms") or 200
                    ),
                }
                devices.append(item)

            controller_status = str(controller.status or "offline").lower()
            if controller_status not in ("online", "offline", "error", "block", "revoked"):
                controller_status = "offline"
            controllers.append({
                "controller_code": controller.controller_id or "",
                "status": controller_status,
                "last_seen_at": self._dt(controller.timestamp) if controller.timestamp else False,
                "devices": devices,
            })

        return {
            "record_key": self.edge_server_code,
            "edge_server_code": self.edge_server_code,
            "status": "online",
            "last_seen_at": self._dt(fields.Datetime.now()),
            "controllers": controllers,
        }

    @api.model
    def _serialize_parking_log(self, record):
        """Serialize the final Edge Parking business event using the lean Cloud contract."""
        area = record.parking_area_id
        lane = record.lane_id
        payload = {
            "record_key": record.log_uid,
            "log_uid": record.log_uid,
            "parking_area_code": str(area.code or "").strip().upper() if area else "",
            "lane_code": str(lane.code or "").strip().upper() if lane else "",
            "layout_revision": int(record.layout_revision or 0),
            "event_type": record.event_type,
            "event_time": self._dt(record.event_time),
            "vehicle_tid": record.vehicle_tid or "",
            "vehicle_code": record.vehicle_id.vehicle_code if record.vehicle_id else "",
            "user_tid": record.user_tid or "",
            "user_code": record.user_id.user_code if record.user_id else "",
            "borrow_uid": record.borrow_id.borrow_code if record.borrow_id else "",
            "decision": record.decision if record.decision in ("allowed", "denied") else "denied",
        }
        if payload["decision"] == "denied":
            payload["reason_code"] = record.reason_code or "unknown"
        return payload

    def _push_cursor_domain(self):
        self.ensure_one()
        # Parking Logs are immutable append-only rows; ID is the durable sync cursor.
        last_id = int(self.last_push_record_id or 0)
        return [("id", ">", last_id)] if last_id else []

    def _serialize_push_batch(self, kind):
        self.ensure_one()
        # The concrete HTTP route is the transport contract. Do not depend on
        # nsp_sync's internal push-kind taxonomy here: business modules may add
        # routes before the generic transport module knows their semantic name.
        route = str(self.route_suffix or "").strip().strip("/")
        limit = max(1, min(int(self.batch_size or 100), 1000))
        if route == "edge/status":
            return {
                "items": [self._serialize_edge_server_status()],
                "cursor_at": fields.Datetime.now(),
                "cursor_id": 0,
                "has_more": False,
            }
        if route != "edge/parking-logs":
            raise UserError(_("Unsupported push route: %s") % self.route_suffix)

        records = self.env["nsp.parking.log"].sudo().search(
            self._push_cursor_domain(), order="id asc", limit=limit + 1,
        )
        has_more = len(records) > limit
        selected = records[:limit]
        if selected:
            # Prefetch only relations serialized by the lean Cloud contract.
            selected.mapped("parking_area_id")
            selected.mapped("lane_id")
            selected.mapped("vehicle_id")
            selected.mapped("user_id")
            selected.mapped("borrow_id")
        last = selected[-1:] if selected else selected
        return {
            "items": [self._serialize_parking_log(record) for record in selected],
            "cursor_at": fields.Datetime.now() if last else self.last_push_at,
            "cursor_id": last.id if last else self.last_push_record_id,
            "has_more": has_more,
        }

    def _find_or_create_controller(self, code, name=False):
        self.ensure_one()
        normalized_code = str(code or "").strip().upper()
        Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
        if not normalized_code:
            return Controller.browse()

        controller = Controller.search([
            ("controller_id", "=", normalized_code),
        ], limit=1)
        values = {
            "controller_name": name or (
                controller.controller_name if controller else normalized_code
            ),
            "active": True,
            "cloud_removed": False,
        }
        if controller:
            self._write_changed(controller, values)
            return controller

        values["controller_id"] = normalized_code
        return Controller.create(values)

    @api.model
    def _prepare_apply_cache(self, kind, items):
        """Preload master records used by high-volume pull snapshots."""
        rows = [item for item in (items or []) if isinstance(item, dict)]
        if kind == "device_whitelist":
            technical_codes = {
                str(item.get("technical_code") or "").strip().upper()
                for item in rows
            }
            serial_numbers = {
                str(item.get("serial_number") or "").strip().upper()
                for item in rows
            }
            type_codes = {
                str(item.get("device_type_code") or "").strip().upper()
                for item in rows
            }
            technical_codes.discard("")
            serial_numbers.discard("")
            type_codes.discard("")
            Whitelist = self.env["nsp.device.whitelist"].sudo().with_context(
                active_test=False
            )
            if technical_codes and serial_numbers:
                records = Whitelist.search([
                    "|",
                    ("technical_code", "in", list(technical_codes)),
                    ("serial_number", "in", list(serial_numbers)),
                ])
            elif technical_codes:
                records = Whitelist.search([
                    ("technical_code", "in", list(technical_codes)),
                ])
            elif serial_numbers:
                records = Whitelist.search([
                    ("serial_number", "in", list(serial_numbers)),
                ])
            else:
                records = Whitelist.browse()
            device_types = self.env["nsp.device.type"].sudo().with_context(
                active_test=False
            ).search([
                ("code", "in", list(type_codes)),
            ]) if type_codes else self.env["nsp.device.type"].browse()
            return {
                "records": {record.technical_code: record for record in records},
                "records_by_serial": {
                    record.serial_number: record
                    for record in records
                    if record.serial_number
                },
                "device_types": {record.code: record for record in device_types},
            }

        if kind == "user":
            codes = {str(item.get("user_code") or "").strip().upper() for item in rows}
            codes.discard("")
            records = self.env["nsp.user"].sudo().with_context(active_test=False).search([
                ("user_code", "in", list(codes)),
            ]) if codes else self.env["nsp.user"].browse()
            return {"records": {record.user_code: record for record in records}}

        if kind == "vehicle":
            vehicle_codes = {str(item.get("vehicle_code") or "").strip().upper() for item in rows}
            owner_codes = {str(item.get("owner_user_code") or "").strip().upper() for item in rows}
            vehicle_codes.discard("")
            owner_codes.discard("")
            Vehicle = self.env["nsp.vehicle"].sudo().with_context(active_test=False)
            vehicles = Vehicle.search([
                ("vehicle_code", "in", list(vehicle_codes)),
            ]) if vehicle_codes else Vehicle.browse()
            users = self.env["nsp.user"].sudo().with_context(active_test=False).search([
                ("user_code", "in", list(owner_codes)),
            ]) if owner_codes else self.env["nsp.user"].browse()
            cache = {
                "vehicle_by_code": {record.vehicle_code: record for record in vehicles if record.vehicle_code},
                "user_by_code": {record.user_code: record for record in users},
            }
            master_specs = (
                ("type_by_code", "nsp.vehicle.type", "vehicle_type_code"),
                ("brand_by_code", "nsp.reference.brand", "brand_code"),
                ("model_by_code", "nsp.reference.model", "model_code"),
                ("color_by_code", "nsp.vehicle.color", "color_code"),
            )
            # Clean-code exception: each entry targets a different Odoo model.
            # Every model is queried once with all codes; ORM cannot batch across models.
            for cache_key, model_name, payload_field in master_specs:
                codes = {str(item.get(payload_field) or "").strip().upper() for item in rows}
                codes.discard("")
                records = self.env[model_name].sudo().with_context(active_test=False).search([
                    ("code", "in", list(codes)),
                ]) if codes else self.env[model_name].browse()
                cache[cache_key] = {record.code: record for record in records}
            return cache

        if kind == "vehicle_borrow":
            borrow_codes = {str(item.get("borrow_uid") or "").strip() for item in rows}
            vehicle_codes = {str(item.get("vehicle_code") or "").strip().upper() for item in rows}
            user_codes = {str(item.get("borrower_user_code") or "").strip().upper() for item in rows}
            borrow_codes.discard("")
            vehicle_codes.discard("")
            user_codes.discard("")
            borrows = self.env["nsp.vehicle.borrow"].sudo().search([
                ("borrow_code", "in", list(borrow_codes)),
            ]) if borrow_codes else self.env["nsp.vehicle.borrow"].browse()
            vehicles = self.env["nsp.vehicle"].sudo().with_context(active_test=False).search([
                ("vehicle_code", "in", list(vehicle_codes)),
            ]) if vehicle_codes else self.env["nsp.vehicle"].browse()
            users = self.env["nsp.user"].sudo().with_context(active_test=False).search([
                ("user_code", "in", list(user_codes)),
            ]) if user_codes else self.env["nsp.user"].browse()
            return {
                "borrow_by_code": {record.borrow_code: record for record in borrows},
                "vehicle_by_code": {record.vehicle_code: record for record in vehicles},
                "user_by_code": {record.user_code: record for record in users},
            }
        return {}

    @api.model
    def _apply_device_whitelist(self, item, cache=None):
        """Apply one identity referenced by the released assembly snapshot."""
        if not isinstance(item, dict):
            raise UserError(_("Device Whitelist item must be an object."))
        unsupported = set(item) - {
            "technical_code", "name", "device_type_code", "device_type_name",
            "serial_number", "active",
        }
        if unsupported:
            raise UserError(
                _("Unsupported Device Whitelist field(s): %s")
                % ", ".join(sorted(unsupported))
            )
        technical_code = str(item.get("technical_code") or "").strip().upper()
        if not technical_code:
            raise UserError(_("Device Whitelist Management Code is required."))

        type_code = str(item.get("device_type_code") or "").strip().upper()
        type_name = str(item.get("device_type_name") or type_code).strip()
        if type_code not in {"SERVER", "CONTROLLER", "RFID_READER"}:
            raise UserError(_("Unsupported Device Type Code: %s") % type_code)

        cache = cache or self._prepare_apply_cache("device_whitelist", [item])
        device_type = cache.get("device_types", {}).get(type_code)
        if not device_type:
            raise UserError(_("Device Type %(type)s is not installed on Edge.") % {"type": type_code})
        if type_name and device_type.name != type_name:
            device_type.write({"name": type_name})

        serial = str(item.get("serial_number") or "").strip().upper() or False
        if type_code == "RFID_READER" and not serial:
            raise UserError(_("Serial Number is required for RFID Reader %(code)s.") % {"code": technical_code})
        vals = {
            "technical_code": technical_code,
            "name": str(item.get("name") or serial or technical_code).strip(),
            "device_type_id": device_type.id,
            "serial_number": serial,
            "active": bool(item.get("active", True)),
        }
        Whitelist = self.env["nsp.device.whitelist"].sudo().with_context(active_test=False)
        record_by_code = cache.get("records", {}).get(technical_code)
        record_by_serial = (
            cache.get("records_by_serial", {}).get(serial) if serial else False
        )
        if (
            record_by_code
            and record_by_serial
            and record_by_code != record_by_serial
        ):
            raise UserError(_(
                "Device identity is duplicated on Edge: Management Code %(code)s "
                "and Serial %(serial)s belong to different records."
            ) % {"code": technical_code, "serial": serial})
        record = record_by_code or record_by_serial
        if record:
            previous_code = record.technical_code
            previous_serial = record.serial_number
            self._write_changed(record, vals)
            if previous_code and previous_code != technical_code:
                if cache.get("records", {}).get(previous_code) == record:
                    cache["records"].pop(previous_code, None)
            if previous_serial and previous_serial != serial:
                if cache.get("records_by_serial", {}).get(previous_serial) == record:
                    cache["records_by_serial"].pop(previous_serial, None)
        else:
            record = Whitelist.create(vals)
        cache.setdefault("records", {})[technical_code] = record
        if serial:
            cache.setdefault("records_by_serial", {})[serial] = record
        return record

    @api.model
    def _reconcile_device_whitelist_snapshot(self, items):
        technical_codes = {
            str(item.get("technical_code") or "").strip().upper()
            for item in (items or [])
            if isinstance(item, dict) and str(item.get("technical_code") or "").strip()
        }
        Whitelist = self.env["nsp.device.whitelist"].sudo().with_context(active_test=False)
        stale = Whitelist.search([
            ("technical_code", "not in", list(technical_codes))
        ]) if technical_codes else Whitelist.search([])
        if stale:
            stale.write({"active": False})
        return len(stale)

    @api.model
    def _apply_user(self, item, cache=None):
        if not isinstance(item, dict):
            raise UserError(_("User snapshot item must be an object."))
        unsupported = set(item) - {"user_code", "name", "active"}
        if unsupported:
            raise UserError(
                _("Unsupported User snapshot field(s): %s")
                % ", ".join(sorted(unsupported))
            )
        code = str(item.get("user_code") or "").strip().upper()
        if not code:
            raise UserError(_("User Code is required."))
        cache = cache or self._prepare_apply_cache("user", [item])
        User = self.env["nsp.user"].sudo().with_context(active_test=False)
        user = cache.get("records", {}).get(code)
        vals = {
            "user_code": code,
            "name": str(item.get("name") or code).strip() or code,
            "active": bool(item.get("active", True)),
        }
        if user:
            self._write_changed(user, vals)
            return user
        user = User.create(vals)
        cache.setdefault("records", {})[code] = user
        return user

    @api.model
    def _reconcile_vehicle_master_snapshot(self, model_name, incoming_codes):
        Model = self.env[model_name].sudo().with_context(active_test=False)
        codes = [str(code).strip().upper() for code in incoming_codes if str(code or "").strip()]
        domain = [("code", "not in", codes)] if codes else []
        stale_active = Model.search(domain + [("active", "=", True)])
        if stale_active:
            stale_active.write({"active": False})
        return len(stale_active)

    @api.model
    def _vehicle_master_snapshot_group(self, model_name, items, extra_values=None):
        """Apply one master-data snapshot with bounded queries and writes."""
        rows = items or []
        normalized = []
        seen = set()
        for item in rows:
            if not isinstance(item, dict):
                raise UserError(_("Vehicle Configuration items must be objects."))
            code = str(item.get("code") or "").strip().upper()
            name = str(item.get("name") or "").strip()
            if not code or not name:
                raise UserError(_("Vehicle Configuration Code and Name are required."))
            if code in seen:
                raise UserError(_("Duplicate Vehicle Configuration Code: %s") % code)
            seen.add(code)
            normalized.append((item, code, name))

        Model = self.env[model_name].sudo().with_context(active_test=False)
        existing = Model.search([("code", "in", list(seen))]) if seen else Model.browse()
        by_code = {record.code: record for record in existing}
        creates = []
        create_meta = []
        applied = []
        for item, code, name in normalized:
            vals = {"code": code, "name": name, "active": bool(item.get("active", True))}
            if extra_values:
                vals.update(extra_values(item, code) or {})
            record = by_code.get(code)
            if record:
                self._write_changed(record, vals)
                applied.append((item, record))
            else:
                creates.append(vals)
                create_meta.append(item)
        if creates:
            created = Model.create(creates)
            applied.extend(zip(create_meta, created))
        return applied, [code for _item, code, _name in normalized]

    def _apply_vehicle_config_snapshot(self, data, request_payload=False):
        self.ensure_one()
        if not isinstance(data, dict):
            raise UserError(_("Vehicle Configuration response must be an object."))
        groups = {
            "vehicle_types": data.get("vehicle_types") or [],
            "brands": data.get("brands") or [],
            "models": data.get("models") or [],
            "colors": data.get("colors") or [],
        }
        for group_name, values in groups.items():
            if not isinstance(values, list):
                raise UserError(_("Vehicle Configuration field %s must be an array.") % group_name)

        applied = []
        codes = {}
        with self.env.cr.savepoint():
            type_rows, codes["vehicle_types"] = self._vehicle_master_snapshot_group(
                "nsp.vehicle.type", groups["vehicle_types"]
            )
            applied.extend(("vehicle_type", item, record) for item, record in type_rows)

            brand_rows, codes["brands"] = self._vehicle_master_snapshot_group(
                "nsp.reference.brand", groups["brands"]
            )
            applied.extend(("brand", item, record) for item, record in brand_rows)

            Brand = self.env["nsp.reference.brand"].sudo().with_context(active_test=False)
            brand_codes = {
                str(item.get("brand_code") or "").strip().upper()
                for item in groups["models"] if isinstance(item, dict)
            }
            brand_codes.discard("")
            brands = Brand.search([("code", "in", list(brand_codes))]) if brand_codes else Brand.browse()
            brand_by_code = {record.code: record for record in brands}

            def model_extra(item, _code):
                brand_code = str(item.get("brand_code") or "").strip().upper()
                brand = brand_by_code.get(brand_code) if brand_code else False
                if brand_code and not brand:
                    raise UserError(
                        _("Vehicle Brand %(brand)s was not found for Vehicle Model %(model)s.")
                        % {"brand": brand_code, "model": item.get("code") or "-"}
                    )
                return {"brand_id": brand.id if brand else False}

            model_rows, codes["models"] = self._vehicle_master_snapshot_group(
                "nsp.reference.model", groups["models"], extra_values=model_extra
            )
            applied.extend(("model", item, record) for item, record in model_rows)

            color_rows, codes["colors"] = self._vehicle_master_snapshot_group(
                "nsp.vehicle.color", groups["colors"]
            )
            applied.extend(("color", item, record) for item, record in color_rows)

            removed = {
                "vehicle_types": self._reconcile_vehicle_master_snapshot("nsp.vehicle.type", codes["vehicle_types"]),
                "brands": self._reconcile_vehicle_master_snapshot("nsp.reference.brand", codes["brands"]),
                "models": self._reconcile_vehicle_master_snapshot("nsp.reference.model", codes["models"]),
                "colors": self._reconcile_vehicle_master_snapshot("nsp.vehicle.color", codes["colors"]),
            }

        Record = self.env["nsp.sync.record"].sudo()
        for group_name, item, record in applied:
            Record.mark_result(
                sync_job=self,
                action_code=self.sync_action_code,
                action_name=self.sync_action_name,
                route_suffix=self.route_suffix,
                record=record,
                record_key="%s:%s" % (group_name, record.code),
                status="synced",
                message="Applied Vehicle Configuration snapshot.",
                payload=request_payload,
                response=item,
                operation="pull",
            )
        return [record for _group, _item, record in applied], removed

    @api.model
    def _apply_vehicle(self, item, cache=None):
        code = str(item.get("vehicle_code") or "").strip().upper()
        plate = str(item.get("license_plate") or "").strip().upper()
        if not code or not plate:
            raise UserError(_("Vehicle Code and License Plate are required."))
        cache = cache or self._prepare_apply_cache("vehicle", [item])
        vehicle = cache.get("vehicle_by_code", {}).get(code)

        owner_user_code = str(item.get("owner_user_code") or "").strip().upper()
        if not owner_user_code:
            raise UserError(_("Vehicle Owner User Code is required."))
        owner = cache.get("user_by_code", {}).get(owner_user_code)
        if not owner:
            raise UserError(
                _("Vehicle Owner %(code)s was not found. Run edge/users/snapshot first.")
                % {"code": owner_user_code}
            )

        def master(cache_key, payload_field, label):
            master_code = str(item.get(payload_field) or "").strip().upper()
            if not master_code:
                return False
            record = cache.get(cache_key, {}).get(master_code)
            if not record:
                raise UserError(_("%(label)s %(code)s was not found. Run edge/vehicle-reference/snapshot first.") % {
                    "label": label, "code": master_code,
                })
            return record

        vehicle_type = master("type_by_code", "vehicle_type_code", _("Vehicle Type"))
        brand = master("brand_by_code", "brand_code", _("Vehicle Brand"))
        vehicle_model = master("model_by_code", "model_code", _("Vehicle Model"))
        color = master("color_by_code", "color_code", _("Vehicle Color"))
        if vehicle_model and brand and vehicle_model.brand_id and vehicle_model.brand_id != brand:
            raise UserError(_("Vehicle Model %(model)s does not belong to Brand %(brand)s.") % {
                "model": vehicle_model.code, "brand": brand.code,
            })
        vals = {
            "vehicle_code": code,
            "license_plate": plate,
            "owner_id": owner.id,
            "vehicle_type_id": vehicle_type.id if vehicle_type else False,
            "brand_id": brand.id if brand else False,
            "model_id": vehicle_model.id if vehicle_model else False,
            "color_id": color.id if color else False,
            "active": bool(item.get("active", True)),
        }
        Vehicle = self.env["nsp.vehicle"].sudo().with_context(active_test=False)
        if vehicle:
            old_code = vehicle.vehicle_code
            self._write_changed(vehicle, vals)
            if old_code and old_code != code:
                cache.get("vehicle_by_code", {}).pop(old_code, None)
        else:
            vehicle = Vehicle.create(vals)
        cache.setdefault("vehicle_by_code", {})[code] = vehicle
        return vehicle

    @api.model
    def _apply_vehicle_borrow(self, item, cache=None):
        code = str(item.get("borrow_uid") or "").strip()
        if not code:
            raise UserError(_("Borrow UID is required."))
        cache = cache or self._prepare_apply_cache("vehicle_borrow", [item])
        Borrow = self.env["nsp.vehicle.borrow"].sudo()
        borrow = cache.get("borrow_by_code", {}).get(code)
        vehicle_code = str(item.get("vehicle_code") or "").strip().upper()
        vehicle = cache.get("vehicle_by_code", {}).get(vehicle_code)
        borrower_code = str(item.get("borrower_user_code") or "").strip().upper()
        borrower = cache.get("user_by_code", {}).get(borrower_code)
        if not vehicle or not borrower:
            raise UserError(_("Vehicle and borrower must exist before Vehicle Borrow sync."))
        valid_from = self._remote_datetime(item.get("valid_from")) or fields.Datetime.now()
        valid_to = self._remote_datetime(item.get("valid_to")) or fields.Datetime.to_string(
            fields.Datetime.to_datetime(valid_from) + timedelta(days=1)
        )
        if fields.Datetime.to_datetime(valid_to) <= fields.Datetime.to_datetime(valid_from):
            raise UserError(_("Vehicle Borrow valid_to must be later than valid_from."))
        state = str(item.get("state") or "active").strip().lower()
        if state not in ("active", "returned", "cancelled"):
            raise UserError(_("Invalid Vehicle Borrow state: %s") % state)
        returned_at = (
            self._remote_datetime(item.get("returned_at"))
            if "returned_at" in item and item.get("returned_at")
            else (borrow.returned_at if borrow else False)
        )
        if state in ("active", "cancelled"):
            returned_at = False
        vals = {
            "vehicle_id": vehicle.id,
            "borrower_id": borrower.id,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "state": state,
            "returned_at": returned_at,
        }
        if borrow:
            self._write_changed(borrow.with_context(vehicle_borrow_sync=True), vals)
            return borrow
        vals["borrow_code"] = code
        borrow = Borrow.with_context(vehicle_borrow_sync=True).create(vals)
        cache.setdefault("borrow_by_code", {})[code] = borrow
        return borrow

    def _apply_lane_calibration(self, item):
        """Apply the released Cloud Lane Calibration schema v4 snapshot.

        Cloud master identities and contextual topology are intentionally separate.
        Edge mirrors the released Tree with ``nsp.measurement.device.node`` and
        never reconstructs Lane Calibration scope from Reader ownership fields.
        """
        self.ensure_one()
        if not isinstance(item, dict):
            raise UserError(_("Lane Calibration item must be an object."))
        allowed_outer = {
            "schema_version", "snapshot_id", "lane_calibration_code", "status",
            "desired_state", "revision", "calibration_tag", "devices", "topology",
        }
        unsupported_outer = set(item) - allowed_outer
        if unsupported_outer:
            raise UserError(
                _("Unsupported Lane Calibration field(s): %s")
                % ", ".join(sorted(unsupported_outer))
            )
        if int(item.get("schema_version") or 0) != 4:
            raise UserError(_("Lane Calibration schema_version 4 is required."))

        code = str(item.get("lane_calibration_code") or "").strip().upper()
        if not code:
            raise UserError(_("Calibration Code is required."))
        calibration_payload = item.get("calibration_tag")
        if not isinstance(calibration_payload, dict) or set(calibration_payload) - {"tid"}:
            raise UserError(_("Lane Calibration requires one calibration_tag object containing only TID."))
        try:
            calibration_tid = normalize_raw_tid(calibration_payload.get("tid"))
        except ValueError as exc:
            raise UserError(_("Calibration Tag TID is invalid.")) from exc
        if not calibration_tid:
            raise UserError(_("Calibration Tag TID is required."))

        status = str(item.get("status") or "ready").strip().lower()
        if status not in ("ready", "running", "completed", "applied", "failed", "cancelled"):
            raise UserError(_("Invalid Lane Calibration status: %s") % status)
        try:
            revision = int(item.get("revision") or 1)
        except (TypeError, ValueError) as exc:
            raise UserError(_("Invalid Lane Calibration revision.")) from exc
        if revision <= 0:
            raise UserError(_("Invalid Lane Calibration revision."))

        devices = item.get("devices")
        topology = item.get("topology")
        if not isinstance(devices, dict) or set(devices) - {"servers", "controllers", "readers"}:
            raise UserError(_("Lane Calibration devices payload is invalid."))
        if not isinstance(topology, dict) or set(topology) - {"nodes"}:
            raise UserError(_("Lane Calibration topology payload is invalid."))
        node_payloads = topology.get("nodes")
        if not isinstance(node_payloads, list) or not node_payloads:
            raise UserError(_("Lane Calibration topology must contain Device Nodes."))

        identity_rows = {}
        device_meta = {"server": {}, "controller": {}, "reader": {}}

        def register_identity(values):
            identity_code = values["technical_code"]
            previous = identity_rows.get(identity_code)
            if previous and (
                previous["device_type_code"] != values["device_type_code"]
                or (previous.get("serial_number") or False) != (values.get("serial_number") or False)
            ):
                raise UserError(_("Device identity %s is used with conflicting roles or serials.") % identity_code)
            identity_rows[identity_code] = values

        list_contract = {
            "server": ("servers", {"id", "name", "status"}, "SERVER", "Server"),
            "controller": ("controllers", {"id", "name", "status"}, "CONTROLLER", "Controller"),
            "reader": ("readers", {"id", "name", "serial_number", "status"}, "RFID_READER", "RFID Reader"),
        }
        for device_type, (key, allowed, whitelist_type, label) in list_contract.items():
            rows = devices.get(key) or []
            if not isinstance(rows, list):
                raise UserError(_("Lane Calibration %(device)s devices must be an array.") % {"device": label})
            for row in rows:
                if not isinstance(row, dict) or set(row) - allowed:
                    raise UserError(_("Invalid Lane Calibration %s device payload.") % label)
                device_code = str(row.get("id") or "").strip().upper()
                if not device_code or device_code in device_meta[device_type]:
                    raise UserError(_("Lane Calibration %s identity is missing or duplicated.") % label)
                serial = str(row.get("serial_number") or "").strip().upper() if device_type == "reader" else False
                if device_type == "reader" and not serial:
                    raise UserError(_("Lane Calibration Reader Serial is required."))
                meta = {
                    "code": device_code,
                    "name": str(row.get("name") or serial or device_code).strip(),
                    "serial_number": serial,
                    "status": str(row.get("status") or "").strip().lower(),
                }
                device_meta[device_type][device_code] = meta
                register_identity({
                    "technical_code": device_code,
                    "name": meta["name"],
                    "device_type_code": whitelist_type,
                    "device_type_name": label,
                    "serial_number": serial,
                    "active": True,
                })

        normalized_nodes = []
        by_source_id = {}
        seen_device = {"server": set(), "controller": set(), "reader": set()}
        for row in node_payloads:
            if not isinstance(row, dict):
                raise UserError(_("Lane Calibration topology node must be an object."))
            device_type = str(row.get("device_type") or "").strip().lower()
            if device_type not in ("server", "controller", "reader"):
                raise UserError(_("Lane Calibration topology contains an invalid device_type."))
            allowed = {"node_id", "device_type", "device_id", "parent_node_id", "sequence"}
            if device_type == "reader":
                allowed |= {"configuration", "ports"}
            unsupported = set(row) - allowed
            if unsupported:
                raise UserError(_("Unsupported Lane Calibration topology field(s): %s") % ", ".join(sorted(unsupported)))
            source_id = str(row.get("node_id") or "").strip()
            device_code = str(row.get("device_id") or "").strip().upper()
            parent_source_id = str(row.get("parent_node_id") or "").strip() or False
            if not source_id or source_id in by_source_id:
                raise UserError(_("Lane Calibration node_id is missing or duplicated."))
            if device_code not in device_meta[device_type]:
                raise UserError(_("Lane Calibration topology references an unknown %s device.") % device_type)
            if device_code in seen_device[device_type]:
                raise UserError(_("Lane Calibration topology duplicates device %s.") % device_code)
            seen_device[device_type].add(device_code)
            try:
                sequence = int(row.get("sequence") or 10)
            except (TypeError, ValueError) as exc:
                raise UserError(_("Lane Calibration node sequence is invalid.")) from exc
            normalized = {
                "source_node_id": source_id,
                "device_type": device_type,
                "device_code": device_code,
                "parent_source_node_id": parent_source_id,
                "sequence": sequence,
            }
            if device_type == "reader":
                config = row.get("configuration") or {}
                if not isinstance(config, dict) or set(config) - {"power_dbm", "read_interval_ms", "tid_addr", "tid_len"}:
                    raise UserError(_("Invalid Lane Calibration Reader configuration."))
                try:
                    power = int(config.get("power_dbm") if config.get("power_dbm") is not None else 30)
                    interval = int(config.get("read_interval_ms") or 200)
                    tid_addr = int(config.get("tid_addr") or 0)
                    tid_len = int(config.get("tid_len") or 0)
                except (TypeError, ValueError) as exc:
                    raise UserError(_("Invalid Lane Calibration Reader configuration.")) from exc
                if power < 0 or power > 40 or interval <= 0 or interval > 60000 or tid_addr < 0 or tid_len <= 0:
                    raise UserError(_("Lane Calibration Reader configuration is outside the supported range."))
                ports = row.get("ports") or []
                if not isinstance(ports, list) or not ports:
                    raise UserError(_("Every Lane Calibration Reader requires at least one Reader Port."))
                port_rows, seen_ports = [], set()
                for port in ports:
                    if not isinstance(port, dict) or set(port) - {"port_no", "sequence"}:
                        raise UserError(_("Invalid Lane Calibration Reader Port payload."))
                    try:
                        port_no = int(port.get("port_no") or 0)
                        port_sequence = int(port.get("sequence") or 10)
                    except (TypeError, ValueError) as exc:
                        raise UserError(_("Lane Calibration Reader Port is invalid.")) from exc
                    if port_no < 1 or port_no > 16 or port_no in seen_ports:
                        raise UserError(_("Reader Port must be unique and between 1 and 16."))
                    seen_ports.add(port_no)
                    port_rows.append({"port_no": port_no, "sequence": port_sequence})
                normalized.update({
                    "power_dbm": power,
                    "read_interval_ms": interval,
                    "tid_addr": tid_addr,
                    "tid_len": tid_len,
                    "ports": sorted(port_rows, key=lambda value: (value["sequence"], value["port_no"])),
                })
            normalized_nodes.append(normalized)
            by_source_id[source_id] = normalized

        # Validate the released parent graph before mutating Edge.
        for node in normalized_nodes:
            parent = by_source_id.get(node["parent_source_node_id"]) if node["parent_source_node_id"] else None
            if node["device_type"] == "server":
                if parent:
                    raise UserError(_("Lane Calibration Server node must be a Tree root."))
            elif node["device_type"] == "controller":
                if not parent or parent["device_type"] != "server":
                    raise UserError(_("Lane Calibration Controller node must belong to a Server node."))
            elif not parent or parent["device_type"] != "controller":
                raise UserError(_("Lane Calibration Reader node must belong to a Controller node."))

        Session = self.env["nsp.measurement.session"].sudo().with_context(measurement_sync=True)
        session = Session.search([("measurement_code", "=", code)], limit=1)
        if session and revision < max(int(session.revision or 1), 1):
            _logger.warning(
                "Ignored stale Lane Calibration snapshot: code=%s incoming_revision=%s current_revision=%s",
                code, revision, session.revision,
            )
            return session

        identity_list = list(identity_rows.values())
        identity_cache = self._prepare_apply_cache("device_whitelist", identity_list)
        for row in identity_list:
            self._apply_device_whitelist(row, cache=identity_cache)
        whitelist_by_code = {
            record.technical_code: record
            for record in self.env["nsp.device.whitelist"].sudo().with_context(active_test=False).search([
                ("technical_code", "in", list(identity_rows)),
            ])
        }

        Edge = self.env["nsp.edge.server"].sudo().with_context(active_test=False)
        Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
        Device = self.env["nsp.device"].sudo().with_context(active_test=False)
        edge_by_code = {row.edge_server_code: row for row in Edge.search([])}
        controller_by_code = {row.controller_id: row for row in Controller.search([])}
        reader_by_code = {row.device_code: row for row in Device.search([])}
        reader_by_serial = {row.serial_number: row for row in Device.search([])}

        runtime_by_code = {"server": {}, "controller": {}, "reader": {}}
        # Servers first.
        for code_value, meta in device_meta["server"].items():
            identity = whitelist_by_code.get(code_value)
            if not identity or identity.device_type_code != "SERVER":
                raise UserError(_("Lane Calibration Server identity is missing or invalid."))
            record = edge_by_code.get(code_value)
            vals = {"name": meta["name"] or code_value, "whitelist_id": identity.id, "active": True, "cloud_removed": False}
            if record:
                self._write_changed(record, vals)
            else:
                record = Edge.create({"edge_server_code": code_value, **vals})
                edge_by_code[code_value] = record
            runtime_by_code["server"][code_value] = record

        # Controller runtime ownership is derived from the released topology.
        controller_parent_code = {}
        for node in normalized_nodes:
            if node["device_type"] == "controller":
                parent = by_source_id[node["parent_source_node_id"]]
                controller_parent_code[node["device_code"]] = parent["device_code"]
        for code_value, meta in device_meta["controller"].items():
            identity = whitelist_by_code.get(code_value)
            if not identity or identity.device_type_code != "CONTROLLER":
                raise UserError(_("Lane Calibration Controller identity is missing or invalid."))
            edge = runtime_by_code["server"].get(controller_parent_code.get(code_value))
            if not edge:
                raise UserError(_("Lane Calibration Controller has no released Server parent."))
            record = controller_by_code.get(code_value)
            vals = {"controller_name": meta["name"] or code_value, "whitelist_id": identity.id, "active": True, "cloud_removed": False}
            if record:
                self._write_changed(record, vals)
            else:
                record = Controller.create({"controller_id": code_value, **vals})
                controller_by_code[code_value] = record
            runtime_by_code["controller"][code_value] = record

        reader_parent_code = {}
        for node in normalized_nodes:
            if node["device_type"] == "reader":
                parent = by_source_id[node["parent_source_node_id"]]
                reader_parent_code[node["device_code"]] = parent["device_code"]
        for code_value, meta in device_meta["reader"].items():
            identity = whitelist_by_code.get(code_value)
            if not identity or identity.device_type_code != "RFID_READER":
                raise UserError(_("Lane Calibration Reader identity is missing or invalid."))
            controller = runtime_by_code["controller"].get(reader_parent_code.get(code_value))
            if not controller:
                raise UserError(_("Lane Calibration Reader has no released Controller parent."))
            by_code = reader_by_code.get(code_value)
            by_serial = reader_by_serial.get(meta["serial_number"])
            if by_code and by_serial and by_code != by_serial:
                raise UserError(_("Lane Calibration Reader Code and Serial belong to different Edge records."))
            record = by_code or by_serial
            vals = {
                "name": meta["name"],
                "serial_number": meta["serial_number"],
                "device_code": code_value,
                "whitelist_id": identity.id,
                "active": True,
                "cloud_removed": False,
            }
            if record:
                previous_code, previous_serial = record.device_code, record.serial_number
                self._write_changed(record, vals)
                if previous_code and previous_code != code_value and reader_by_code.get(previous_code) == record:
                    reader_by_code.pop(previous_code, None)
                if previous_serial and previous_serial != meta["serial_number"] and reader_by_serial.get(previous_serial) == record:
                    reader_by_serial.pop(previous_serial, None)
            else:
                record = Device.create(vals)
            reader_by_code[code_value] = record
            reader_by_serial[meta["serial_number"]] = record
            runtime_by_code["reader"][code_value] = record

        effective_status = status
        reset_lifecycle = False
        if session:
            current_revision = max(int(session.revision or 1), 1)
            if revision > current_revision:
                reset_lifecycle = True
            else:
                stale_snapshot_targets = {
                    "running": {"draft", "ready"},
                    "completed": {"draft", "ready", "running"},
                    "applied": {"draft", "ready", "running", "completed", "failed", "cancelled"},
                    "failed": {"draft", "ready", "running"},
                    "cancelled": {"draft", "ready", "running"},
                }
                if status in stale_snapshot_targets.get(session.status, set()):
                    effective_status = session.status

        values = {
            "measurement_code": code,
            "revision": revision,
            "status": effective_status,
            "target_line_ids": [(5, 0, 0), (0, 0, {"tid": calibration_tid})],
        }
        if reset_lifecycle:
            values.update({"started_at": False, "ended_at": False})
        if session:
            session.write(values)
        else:
            session = Session.create(values)

        # Replace the contextual projection atomically for this released revision.
        session.device_node_ids.filtered(lambda node: not node.parent_id).unlink()
        Node = self.env["nsp.measurement.device.node"].sudo().with_context(measurement_sync=True)
        local_by_source = {}
        for device_type in ("server", "controller", "reader"):
            for node in [value for value in normalized_nodes if value["device_type"] == device_type]:
                vals = {
                    "session_id": session.id,
                    "source_node_id": node["source_node_id"],
                    "device_type": device_type,
                    "sequence": node["sequence"],
                }
                if device_type == "server":
                    vals["server_id"] = runtime_by_code["server"][node["device_code"]].id
                elif device_type == "controller":
                    vals.update({
                        "controller_id": runtime_by_code["controller"][node["device_code"]].id,
                        "parent_id": local_by_source[node["parent_source_node_id"]].id,
                    })
                else:
                    vals.update({
                        "reader_id": runtime_by_code["reader"][node["device_code"]].id,
                        "parent_id": local_by_source[node["parent_source_node_id"]].id,
                        "power_dbm": node["power_dbm"],
                        "read_interval_ms": node["read_interval_ms"],
                        "tid_addr": node["tid_addr"],
                        "tid_len": node["tid_len"],
                        "reader_port_ids": [(0, 0, port) for port in node["ports"]],
                    })
                local_by_source[node["source_node_id"]] = Node.create(vals)

        session._require_ready_configuration()
        if effective_status == "applied":
            self._acknowledge_configured_status_records(session)
        return session

    def _apply_items(self, kind, items, request_payload=False):
        self.ensure_one()
        results, failed = [], []
        Record = self.env["nsp.sync.record"].sudo()
        handlers = {
            "user": self._apply_user,
            "vehicle": self._apply_vehicle,
            "vehicle_borrow": self._apply_vehicle_borrow,
            "lane_calibration": self._apply_lane_calibration,
        }
        handler = handlers.get(kind)
        if not handler:
            raise UserError(_("Unsupported pull route: %s") % self.route_suffix)
        normalized_items = items if isinstance(items, list) else []
        cached_kinds = {"user", "vehicle", "vehicle_borrow"}
        apply_cache = self._prepare_apply_cache(kind, normalized_items) if kind in cached_kinds else None
        for index, item in enumerate(normalized_items):
            key = self._record_key_from_item(item)
            try:
                with self.env.cr.savepoint():
                    record = handler(item, cache=apply_cache) if kind in cached_kinds else handler(item)
                key = key or record.display_name or str(record.id)
                Record.mark_result(
                    sync_job=self,
                    action_code=self.sync_action_code,
                    action_name=self.sync_action_name,
                    route_suffix=self.route_suffix,
                    record=record,
                    record_key=key,
                    status="synced",
                    message="Applied by Edge Server.",
                    payload=request_payload,
                    response=item,
                    operation="pull",
                )
                results.append({
                    "index": index,
                    "record_key": key,
                    "record_model": record._name,
                    "record_id": record.id,
                    "success": True,
                })
            except Exception as exc:
                message = str(exc)
                Record.mark_result(
                    sync_job=self,
                    action_code=self.sync_action_code,
                    action_name=self.sync_action_name,
                    route_suffix=self.route_suffix,
                    record_key=key or str(index),
                    status="failed",
                    message=message,
                    payload=request_payload,
                    response=item,
                    operation="pull",
                )
                failed.append({"index": index, "record_key": key, "error": message})
        return results, failed

    def _reconcile_measurement_snapshot(self, items):
        """Stop/remove Edge Measurement runtime records absent from Cloud snapshot.

        A deleted Cloud Measurement must stop physical execution on Edge. Sessions
        that still own observations are kept only as short-lived history and are
        removed by the normal retention cleanup after their observations expire.
        """
        self.ensure_one()
        rows = [item for item in (items or []) if isinstance(item, dict)]
        incoming = {
            str(item.get("lane_calibration_code") or "").strip().upper()
            for item in rows
            if item.get("lane_calibration_code")
        }
        Session = self.env["nsp.measurement.session"].sudo()
        stale = Session.search([("measurement_code", "not in", list(incoming))]) if incoming else Session.search([])
        if not stale:
            return 0
        now = fields.Datetime.now()
        running = stale.filtered(lambda rec: rec.status in ("ready", "running"))
        if running:
            running._apply_status_transition("cancelled", {"ended_at": now})
        disposable = stale.filtered(
            lambda rec: rec.status in ("completed", "applied", "failed", "cancelled") and not rec.event_ids
        )
        if disposable:
            disposable.with_context(measurement_sync=True).unlink()
        return len(stale)

    def _reconcile_business_snapshot(self, kind, items):
        """Archive/remove records absent from full Cloud master snapshots."""
        rows = [item for item in (items or []) if isinstance(item, dict)]

        if kind == "user":
            keys = {
                str(item.get("user_code") or "").strip().upper()
                for item in rows
                if item.get("user_code")
            }
            Model = self.env["nsp.user"].sudo().with_context(active_test=False)
            domain = [("active", "=", True)]
            if keys:
                domain.append(("user_code", "not in", sorted(keys)))
            stale = Model.search(domain)
            stale.write({"active": False})
            return len(stale)

        if kind == "vehicle":
            keys = {
                str(item.get("vehicle_code") or "").strip().upper()
                for item in rows
                if item.get("vehicle_code")
            }
            Model = self.env["nsp.vehicle"].sudo().with_context(active_test=False)
            domain = [("active", "=", True)]
            if keys:
                domain.append(("vehicle_code", "not in", sorted(keys)))
            stale = Model.search(domain)
            stale.write({"active": False})
            return len(stale)

        if kind == "vehicle_borrow":
            keys = {
                str(item.get("borrow_uid") or "").strip()
                for item in rows
                if item.get("borrow_uid")
            }
            Model = self.env["nsp.vehicle.borrow"].sudo()
            domain = [("borrow_code", "not in", sorted(keys))] if keys else []
            stale = Model.search(domain)
            stale.unlink()
            return len(stale)

        return 0

    @api.model
    def _record_key_from_item(self, item):
        if not isinstance(item, dict):
            return False
        for field_name in (
            "record_key", "tid", "borrow_uid", "branch_code", "user_code",
            "vehicle_code", "license_plate", "parking_area_code", "log_uid",
            "lane_calibration_code", "event_uid", "serial_number", "code",
            "controller_code", "edge_server_code",
        ):
            if item.get(field_name):
                return str(item[field_name])
        return False

    @api.model
    def _lane_calibration_event_payload(self, event):
        read_at = False
        if event.read_at:
            parsed = fields.Datetime.to_datetime(event.read_at)
            if parsed:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                else:
                    parsed = parsed.astimezone(timezone.utc)
                millisecond = max(0, min(int(event.read_at_ms or 0), 999))
                parsed = parsed.replace(microsecond=millisecond * 1000)
                read_at = parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        payload = {
            "event_uid": event.event_uid,
            "revision": int(event.revision or 1),
            "serial_number": event.serial_number,
            "port_no": int(event.port_no),
            "tid": event.tid,
            "read_at": read_at,
            "power_dbm": int(
                event.power_dbm
                if event.power_dbm is not None
                else event.session_id._reader_power_for_serial(event.serial_number)
            ),
            "read_interval_ms": int(
                event.read_interval_ms
                if event.read_interval_ms is not None
                else event.session_id._reader_interval_for_serial(event.serial_number)
            ),
        }
        if event.rssi_dbm not in (False, None):
            payload["rssi_dbm"] = float(event.rssi_dbm)
        return payload

    def _pending_lane_calibration_events(self, limit):
        self.ensure_one()
        Record = self.env["nsp.sync.record"].sudo()
        Event = self.env["nsp.measurement.event"].sudo()
        action_code = str(self.sync_action_code or "").strip()
        source_code = str(self.edge_server_code or "NSP").strip() or "NSP"
        synced_keys = Record.search([
            ("source_code", "=", source_code),
            ("sync_action_code", "=", action_code),
            ("operation", "=", "push"),
            ("status", "=", "synced"),
        ]).mapped("record_key")
        domain = [("event_uid", "not in", synced_keys)] if synced_keys else []
        return Event.search(
            domain,
            order="read_at, id",
            limit=max(1, int(limit or 1)),
        )

    def _push_lane_calibration_event_records(self, events, timeout=120):
        self.ensure_one()
        events = events.sudo().exists().sorted(key=lambda event: event.id)
        if not events:
            return {"pushed": 0, "failed": 0, "has_more": False, "message": "No Lane Calibration Events to push."}
        session = events[0].session_id
        events = events.filtered(lambda event: event.session_id == session)
        Record = self.env["nsp.sync.record"].sudo()
        payload = {
            "edge_server_code": self.edge_server_code,
            "lane_calibration_code": session.measurement_code,
            "events": [self._lane_calibration_event_payload(event) for event in events],
        }
        for event in events:
            Record.mark_pending(
                sync_job=self,
                action_code=self.sync_action_code,
                action_name=self.sync_action_name,
                route_suffix=self.route_suffix,
                record=event,
                record_key=event.event_uid,
                message="Waiting for Cloud response.",
                payload=self._lane_calibration_event_payload(event),
                operation="push",
            )
        try:
            data = self._json_or_error(self._post_remote(self.sync_action_id, payload, timeout=timeout))
        except Exception as exc:
            for event in events:
                Record.mark_result(
                    sync_job=self,
                    action_code=self.sync_action_code,
                    action_name=self.sync_action_name,
                    route_suffix=self.route_suffix,
                    record=event,
                    record_key=event.event_uid,
                    status="failed",
                    message=str(exc),
                    payload=self._lane_calibration_event_payload(event),
                    operation="push",
                )
            raise
        result_by_key = {
            str(result.get("record_key") or ""): result
            for result in (data.get("results") or [])
            if isinstance(result, dict)
        }
        reported_failed = int(data.get("failed") or 0)
        failed = 0
        for event in events:
            result = result_by_key.get(event.event_uid)
            rejected = bool(result and result.get("status") in ("rejected", "failed", "error"))
            if not result_by_key and reported_failed:
                rejected = True
            Record.mark_result(
                sync_job=self,
                action_code=self.sync_action_code,
                action_name=self.sync_action_name,
                route_suffix=self.route_suffix,
                record=event,
                record_key=event.event_uid,
                status="failed" if rejected else "synced",
                message=(result or {}).get("message") or ("Rejected by Cloud." if rejected else "Accepted by Cloud."),
                payload=self._lane_calibration_event_payload(event),
                response=result or data,
                operation="push",
            )
            failed += int(rejected)
        if failed:
            reasons = {}
            for result in result_by_key.values():
                if result.get("status") not in ("rejected", "failed", "error"):
                    continue
                code = str(result.get("error_code") or result.get("message") or "rejected").strip()
                reasons[code] = reasons.get(code, 0) + 1
            reason_text = ", ".join(
                "%s: %s" % (code, count)
                for code, count in sorted(reasons.items())
            )
            message = _("Cloud rejected %s Lane Calibration Event(s).") % failed
            if reason_text:
                message += " " + _("Reasons: %s") % reason_text
            raise UserError(message)
        self.last_push_at = fields.Datetime.now()
        return {
            "pushed": len(events),
            "failed": 0,
            "has_more": bool(self._pending_lane_calibration_events(1)),
            "message": "Pushed %s Lane Calibration Event(s)." % len(events),
        }

    def _run_lane_calibration_event_push_once(self):
        self.ensure_one()
        events = self._pending_lane_calibration_events(
            max(1, min(int(self.batch_size or 100), 100))
        )
        return self._push_lane_calibration_event_records(events)

    @api.model
    def _ensure_edge_sync_jobs(self):
        """Repair missing default Sync Jobs for existing Edge connections.

        New route types may be introduced after an Edge connection already
        exists.  Do not require an operator to re-authenticate merely to create
        those jobs: the scheduler and immediate Lane Calibration forwarding can
        self-heal the job set.
        """
        if self._deployment_role() != "edge_server":
            return self.browse()
        Auth = self.env["nsp.sync.auth"].sudo()
        auth_records = Auth.search([])
        if not auth_records:
            return self.browse()
        try:
            return self.sudo().ensure_default_jobs(auth_records)
        except Exception:
            _logger.exception("Unable to repair missing NSP Sync Jobs automatically.")
            return self.browse()

    @api.model
    def _lane_calibration_push_job(self, route_suffix):
        domain = [
            ("active", "=", True),
            ("route_suffix", "=", route_suffix),
            ("direction", "=", "push"),
        ]
        job = self.sudo().search(domain, order="sequence, id", limit=1)
        if not job:
            self._ensure_edge_sync_jobs()
            job = self.sudo().search(domain, order="sequence, id", limit=1)
        return job

    @api.model
    def _acknowledge_configured_status_records(self, session):
        """Close obsolete runtime status retries after Cloud configures a revision."""
        status_job = self._lane_calibration_push_job("edge/lane-calibrations/status")
        if not status_job:
            return 0
        revision = max(int(session.revision or 1), 1)
        record_keys = [
            "%s:R%s:%s" % (session.measurement_code, revision, status)
            for status in self._lane_calibration_runtime_statuses()
        ]
        Record = self.env["nsp.sync.record"].sudo()
        obsolete = Record.search([
            ("sync_action_code", "=", status_job.sync_action_code),
            ("operation", "=", "push"),
            ("record_key", "in", record_keys),
            ("status", "!=", "synced"),
        ])
        for record_key in set(obsolete.mapped("record_key")):
            Record.mark_result(
                sync_job=status_job,
                action_code=status_job.sync_action_code,
                action_name=status_job.sync_action_name,
                route_suffix=status_job.route_suffix,
                record=session,
                record_key=record_key,
                status="synced",
                message="Superseded by the Configured Cloud state.",
                response={"status_sync": {"outcome": "ignored_after_configured"}},
                operation="push",
            )
        return len(obsolete)

    @api.model
    def push_lane_calibration_events_now(self, events):
        job = self._lane_calibration_push_job("edge/lane-calibrations/events")
        if not job:
            _logger.warning(
                "Lane Calibration Event forwarding deferred: no edge/lane-calibrations/events job is available."
            )
            return False
        try:
            job._push_lane_calibration_event_records(events, timeout=3)
            return True
        except Exception:
            _logger.exception("Immediate Lane Calibration Event forwarding failed; fallback retry remains pending.")
            return False

    @api.model
    def _lane_calibration_runtime_statuses(self):
        """Statuses owned and published by Edge runtime."""
        return ("running", "completed", "failed", "cancelled")

    @api.model
    def _lane_calibration_status_payload(self, session):
        status = str(session.status or "draft")
        if status not in self._lane_calibration_runtime_statuses():
            raise UserError(_(
                "Lane Calibration status %(status)s is Cloud-owned and must not be pushed by Edge."
            ) % {"status": status})
        occurred_at = (
            session.started_at
            if status == "running"
            else session.ended_at
        ) or session.write_date or fields.Datetime.now()
        return {
            "edge_server_code": self.edge_server_code,
            "lane_calibration_code": session.measurement_code,
            "revision": max(int(session.revision or 1), 1),
            "status": status,
            "occurred_at": self._iso_utc(occurred_at),
        }

    @api.model
    def _lane_calibration_status_record_key(self, session, payload=False):
        values = payload or self._lane_calibration_status_payload(session)
        return "%s:R%s:%s" % (
            session.measurement_code,
            int(values.get("revision") or session.revision or 1),
            values.get("status") or session.status,
        )

    def _pending_lane_calibration_status_sessions(self, limit):
        self.ensure_one()
        Session = self.env["nsp.measurement.session"].sudo()
        Record = self.env["nsp.sync.record"].sudo()
        sessions = Session.search(
            [("status", "in", self._lane_calibration_runtime_statuses())],
            order="write_date,id",
        )
        if not sessions:
            return sessions
        record_key_by_session = {
            session.id: self._lane_calibration_status_record_key(session)
            for session in sessions
        }
        synced_keys = set(Record.search([
            ("sync_action_code", "=", self.sync_action_code),
            ("operation", "=", "push"),
            ("record_key", "in", list(record_key_by_session.values())),
            ("status", "=", "synced"),
        ]).mapped("record_key"))
        max_items = max(1, int(limit or 1))
        pending_ids = [
            session.id
            for session in sessions
            if record_key_by_session[session.id] not in synced_keys
        ][:max_items]
        return Session.browse(pending_ids)

    def _push_lane_calibration_status_records(self, sessions, timeout=120):
        self.ensure_one()
        runtime_statuses = self._lane_calibration_runtime_statuses()
        sessions = sessions.sudo().exists().filtered(
            lambda session: session.status in runtime_statuses
        ).sorted(key=lambda session: (session.write_date, session.id))
        if not sessions:
            return {
                "pushed": 0,
                "failed": 0,
                "has_more": False,
                "message": "No runtime-owned Lane Calibration Status to push.",
            }
        Record = self.env["nsp.sync.record"].sudo()
        pushed = 0
        for session in sessions:
            payload = self._lane_calibration_status_payload(session)
            record_key = self._lane_calibration_status_record_key(session, payload=payload)
            Record.mark_pending(
                sync_job=self,
                action_code=self.sync_action_code,
                action_name=self.sync_action_name,
                route_suffix=self.route_suffix,
                record=session,
                record_key=record_key,
                message="Waiting for Cloud response.",
                payload=payload,
                operation="push",
            )
            try:
                data = self._json_or_error(self._post_remote(self.sync_action_id, payload, timeout=timeout))
            except Exception as exc:
                Record.mark_result(
                    sync_job=self,
                    action_code=self.sync_action_code,
                    action_name=self.sync_action_name,
                    route_suffix=self.route_suffix,
                    record=session,
                    record_key=record_key,
                    status="failed",
                    message=str(exc),
                    payload=payload,
                    operation="push",
                )
                raise
            Record.mark_result(
                sync_job=self,
                action_code=self.sync_action_code,
                action_name=self.sync_action_name,
                route_suffix=self.route_suffix,
                record=session,
                record_key=record_key,
                status="synced",
                message="Lane Calibration Status accepted by Cloud.",
                payload=payload,
                response=data,
                operation="push",
            )
            pushed += 1
        self.last_push_at = fields.Datetime.now()
        return {
            "pushed": pushed,
            "failed": 0,
            "has_more": bool(self._pending_lane_calibration_status_sessions(1)),
            "message": "Pushed %s Lane Calibration Status record(s)." % pushed,
        }

    def _run_lane_calibration_status_push_once(self):
        self.ensure_one()
        sessions = self._pending_lane_calibration_status_sessions(
            max(1, min(int(self.batch_size or 100), 1000))
        )
        return self._push_lane_calibration_status_records(sessions)

    @api.model
    def push_lane_calibration_status_now(self, session):
        job = self._lane_calibration_push_job("edge/lane-calibrations/status")
        if not job:
            _logger.warning(
                "Lane Calibration Status forwarding deferred: no edge/lane-calibrations/status job is available."
            )
            return False
        try:
            job._push_lane_calibration_status_records(session, timeout=3)
            return True
        except Exception:
            _logger.exception("Immediate Lane Calibration Status forwarding failed; fallback retry remains pending.")
            return False
