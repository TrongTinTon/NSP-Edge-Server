# -*- coding: utf-8 -*-
from odoo import api, models, _
from odoo.exceptions import UserError


class NspSyncJob(models.Model):
    _inherit = "nsp.sync.job"

    @api.model
    def _normalize_rfid_tid(self, value):
        return self.env["nsp.rfid.runtime.assignment"]._normalize_tid(value)

    def _runtime_vehicle_assignment_map(self, tids):
        Runtime = self.env["nsp.rfid.runtime.assignment"].sudo()
        assignments = Runtime.search([("tid", "in", list(tids))]) if tids else Runtime.browse()
        return {assignment.tid: assignment for assignment in assignments}

    def _normalize_rfid_runtime_item(self, item):
        if not isinstance(item, dict):
            raise UserError(_("RFID runtime assignment items must be objects."))
        unsupported = sorted(set(item) - {"tid", "assignment"})
        if unsupported:
            raise UserError(
                _("Unsupported RFID runtime assignment field(s): %s")
                % ", ".join(unsupported)
            )

        tid = self._normalize_rfid_tid(item.get("tid"))
        assignment = item.get("assignment")
        if not tid or not isinstance(assignment, dict):
            raise UserError(_("RFID TID and runtime assignment are required."))

        unsupported_assignment = sorted(
            set(assignment) - {"target", "code", "assigned_at"}
        )
        if unsupported_assignment:
            raise UserError(
                _("Unsupported RFID runtime target field(s): %s")
                % ", ".join(unsupported_assignment)
            )

        target = str(assignment.get("target") or "").strip().lower()
        code = str(assignment.get("code") or "").strip().upper()
        if target not in ("user", "vehicle") or not code:
            raise UserError(_("RFID runtime target must contain user/vehicle and code."))
        return {
            "tid": tid,
            "target": target,
            "code": code,
            "assigned_at": self._remote_datetime(assignment.get("assigned_at"))
            if assignment.get("assigned_at") else False,
            "source": item,
        }

    def _apply_rfid_assignment_snapshot(self, data, request_payload=False):
        self.ensure_one()
        if data.get("snapshot_scope") != "rfid_runtime_assignments":
            raise UserError(_("Invalid RFID runtime assignment snapshot scope."))
        if data.get("snapshot_mode") != "replace":
            raise UserError(_("RFID runtime assignment snapshot must use replace mode."))
        items = self._items_from_response(data)
        if not isinstance(items, list):
            raise UserError(_("RFID runtime assignment snapshot must contain an items array."))
        normalized = []
        seen_tids = set()
        seen_targets = set()
        for item in items:
            info = self._normalize_rfid_runtime_item(item)
            target_key = (info["target"], info["code"])
            if info["tid"] in seen_tids:
                raise UserError(_("Duplicate RFID TID: %s") % info["tid"])
            if target_key in seen_targets:
                raise UserError(
                    _("Duplicate RFID runtime target: %(target)s/%(code)s") % info
                )
            seen_tids.add(info["tid"])
            seen_targets.add(target_key)
            normalized.append(info)

        User = self.env["nsp.user"].sudo().with_context(active_test=False)
        Vehicle = self.env["nsp.vehicle"].sudo().with_context(active_test=False)
        Runtime = self.env["nsp.rfid.runtime.assignment"].sudo()

        user_codes = {row["code"] for row in normalized if row["target"] == "user"}
        vehicle_codes = {row["code"] for row in normalized if row["target"] == "vehicle"}
        users = User.search([("user_code", "in", list(user_codes))]) if user_codes else User.browse()
        vehicles = Vehicle.search([("vehicle_code", "in", list(vehicle_codes))]) if vehicle_codes else Vehicle.browse()
        user_by_code = {row.user_code: row for row in users}
        vehicle_by_code = {row.vehicle_code: row for row in vehicles}

        for info in normalized:
            target = user_by_code.get(info["code"]) if info["target"] == "user" else vehicle_by_code.get(info["code"])
            if not target:
                route = "edge/users/snapshot" if info["target"] == "user" else "edge/vehicles/snapshot"
                raise UserError(
                    _("RFID %(tid)s target %(code)s was not found. Run %(route)s first.")
                    % {**info, "route": route}
                )
            if not target.active:
                raise UserError(_("RFID runtime target %(code)s is inactive.") % info)

        existing = Runtime.search([])
        by_tid = {row.tid: row for row in existing}
        by_user = {row.user_id.id: row for row in existing if row.user_id}
        by_vehicle = {row.vehicle_id.id: row for row in existing if row.vehicle_id}
        synced = Runtime.browse()
        counts = {
            "active_assignments": len(normalized),
            "employee_assignments": 0,
            "vehicle_assignments": 0,
        }

        for info in normalized:
            current = by_tid.get(info["tid"], Runtime.browse())
            user = user_by_code.get(info["code"]) if info["target"] == "user" else User.browse()
            vehicle = vehicle_by_code.get(info["code"]) if info["target"] == "vehicle" else Vehicle.browse()
            conflict = by_user.get(user.id) if user else by_vehicle.get(vehicle.id)
            if conflict and conflict != current:
                if conflict.user_id:
                    by_user.pop(conflict.user_id.id, None)
                if conflict.vehicle_id:
                    by_vehicle.pop(conflict.vehicle_id.id, None)
                by_tid.pop(conflict.tid, None)
                conflict.unlink()
            if current:
                if current.user_id:
                    by_user.pop(current.user_id.id, None)
                if current.vehicle_id:
                    by_vehicle.pop(current.vehicle_id.id, None)

            vals = {
                "target_type": info["target"],
                "user_id": user.id if user else False,
                "vehicle_id": vehicle.id if vehicle else False,
                "assigned_at": info["assigned_at"],
            }
            if current:
                self._write_changed(current, vals)
            else:
                current = Runtime.create({"tid": info["tid"], **vals})
            by_tid[info["tid"]] = current
            if user:
                counts["employee_assignments"] += 1
                by_user[user.id] = current
            else:
                counts["vehicle_assignments"] += 1
                by_vehicle[vehicle.id] = current
            synced |= current

        stale = (existing - synced).exists()
        stale_count = len(stale)
        if stale:
            stale.unlink()

        Record = self.env["nsp.sync.record"].sudo()
        for info in normalized:
            assignment = by_tid[info["tid"]]
            Record.mark_result(
                sync_job=self,
                action_code=self.sync_action_code,
                action_name=self.sync_action_name,
                route_suffix=self.route_suffix,
                record=assignment,
                record_key=info["tid"],
                status="synced",
                message="RFID runtime assignment synchronized.",
                payload=request_payload,
                response=info["source"],
                operation="pull",
            )
        return counts, stale_count
