# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.addons.t4_coreapi.utils import endpoint, get_params, get_body


class NspNotificationApiService(models.AbstractModel):
    _name = "nsp.notification.api.service"
    _description = "NSP Notification Core API Service"

    @api.model
    def _ok(self, payload=None, message="Request processed successfully.", status_code=200):
        data = {"ok": True, "success": True}
        if isinstance(payload, dict):
            data.update(payload)
        elif payload is not None:
            data["result"] = payload
        return {"status_code": status_code, "message": message, "data": data}

    @api.model
    def _error(self, message, status_code=400, **extra):
        data = {"ok": False, "success": False, "error": message}
        data.update(extra)
        return {"status_code": status_code, "message": message, "data": data}

    @api.model
    def _payload(self):
        try:
            body = get_body(self) or {}
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}

    @api.model
    def _params(self):
        try:
            params = get_params(self) or {}
        except Exception:
            params = {}
        return params if isinstance(params, dict) else {}

    @api.model
    def _resolve_nsp_user(self, data):
        User = self.env["nsp.user"].sudo()
        user_id = data.get("user_id")
        user_code = data.get("user_code")
        email = data.get("email")
        if user_id:
            try:
                user = User.browse(int(user_id)).exists()
                if user:
                    return user
            except Exception:
                pass
        if user_code:
            user = User.search([("user_code", "=", str(user_code).strip().upper())], limit=1)
            if user:
                return user
        if email:
            user = User.search([("email", "=", str(email).strip())], limit=1)
            if user:
                return user
        return User.browse()

    @api.model
    def _resolve_odoo_user(self, data):
        Users = self.env["res.users"].sudo()
        odoo_user_id = data.get("odoo_user_id")
        login = data.get("login")
        if odoo_user_id:
            try:
                user = Users.browse(int(odoo_user_id)).exists()
                if user:
                    return user
            except Exception:
                pass
        if login:
            return Users.search([("login", "=", str(login).strip())], limit=1)
        return Users.browse()

    @endpoint("NSP Mobile Register Push Token", route_suffix="mobile/push/register-token", methods="POST", code="nsp_mobile_push_register_token")
    def api_register_token(self):
        data = self._payload()
        user = self._resolve_nsp_user(data)
        odoo_user = self._resolve_odoo_user(data)
        if not user and not odoo_user:
            return self._error("Missing or invalid user_id/user_code/odoo_user_id/login.", 400)
        if not data.get("device_uid"):
            return self._error("device_uid is required.", 400)
        if not data.get("push_token") and data.get("provider_type", "fcm") != "in_app":
            return self._error("push_token is required for native push providers.", 400)
        device = self.env["nsp.push.device"].sudo().register_device(
            user=user,
            odoo_user=odoo_user,
            device_uid=data.get("device_uid"),
            platform=data.get("platform") or "android",
            provider_type=data.get("provider_type") or "fcm",
            push_token=data.get("push_token") or False,
            app_version=data.get("app_version") or False,
        )
        return self._ok({
            "device_id": device.id,
            "device_uid": device.device_uid,
            "status": device.status,
            "provider_type": device.provider_type,
            "server_time": fields.Datetime.to_string(fields.Datetime.now()),
        }, message="Push device registered.")

    @endpoint("NSP Mobile Unregister Push Token", route_suffix="mobile/push/unregister-token", methods="POST", code="nsp_mobile_push_unregister_token")
    def api_unregister_token(self):
        data = self._payload()
        device_uid = str(data.get("device_uid") or "").strip()
        if not device_uid:
            return self._error("device_uid is required.", 400)
        device = self.env["nsp.push.device"].sudo().search([("device_uid", "=", device_uid)], limit=1)
        if not device:
            return self._ok({"device_uid": device_uid, "status": "not_found"}, message="Device is not registered.")
        device.action_revoke()
        return self._ok({"device_id": device.id, "status": device.status}, message="Push device unregistered.")

    @endpoint("NSP Mobile Push Heartbeat", route_suffix="mobile/push/heartbeat", methods="POST", code="nsp_mobile_push_heartbeat")
    def api_push_heartbeat(self):
        data = self._payload()
        device_uid = str(data.get("device_uid") or "").strip()
        if not device_uid:
            return self._error("device_uid is required.", 400)
        device = self.env["nsp.push.device"].sudo().search([("device_uid", "=", device_uid)], limit=1)
        if not device:
            return self._error("Device is not registered.", 404)
        vals = {"last_seen_at": fields.Datetime.now()}
        if data.get("app_version"):
            vals["app_version"] = data.get("app_version")
        if device.status != "revoked":
            vals["status"] = "active"
        device.write(vals)
        return self._ok({"device_id": device.id, "server_time": fields.Datetime.to_string(fields.Datetime.now())}, message="Heartbeat accepted.")

    @api.model
    def _notification_domain_for_mobile(self, data):
        user = self._resolve_nsp_user(data)
        odoo_user = self._resolve_odoo_user(data)
        parts = []
        if user:
            parts.append(("target_user_id", "=", user.id))
        if odoo_user:
            parts.append(("recipient_user_id", "=", odoo_user.id))
        if not parts:
            return None
        domain = [("active", "=", True)]
        if len(parts) == 1:
            domain += parts
        else:
            domain += ["|"] * (len(parts) - 1) + parts
        state = data.get("state")
        if state in ("unread", "read", "archived"):
            domain.append(("state", "=", state))
        since_id = data.get("since_id")
        if since_id:
            try:
                domain.append(("id", ">", int(since_id)))
            except Exception:
                pass
        return domain

    @endpoint("NSP Mobile Notifications List", route_suffix="mobile/notifications/list", methods="GET,POST", code="nsp_mobile_notifications_list")
    def api_notifications_list(self):
        data = self._payload()
        data.update(self._params())
        domain = self._notification_domain_for_mobile(data)
        if domain is None:
            return self._error("Missing or invalid user_id/user_code/odoo_user_id/login.", 400)
        try:
            limit = min(max(int(data.get("limit") or 50), 1), 200)
        except Exception:
            limit = 50
        notifications = self.env["nsp.notification"].sudo().search(domain, order="event_time desc, id desc", limit=limit)
        return self._ok({
            "notifications": [rec._mobile_payload() for rec in notifications],
            "count": len(notifications),
            "server_time": fields.Datetime.to_string(fields.Datetime.now()),
        })

    @endpoint("NSP Mobile Notification Ack", route_suffix="mobile/notifications/ack", methods="POST", code="nsp_mobile_notifications_ack")
    def api_notifications_ack(self):
        data = self._payload()
        device_uid = str(data.get("device_uid") or "").strip()
        notification_id = data.get("notification_id")
        if not notification_id:
            return self._error("notification_id is required.", 400)
        domain = [("notification_id", "=", int(notification_id))]
        if device_uid:
            device = self.env["nsp.push.device"].sudo().search([("device_uid", "=", device_uid)], limit=1)
            if device:
                domain.append(("device_id", "=", device.id))
        deliveries = self.env["nsp.push.delivery"].sudo().search(domain)
        deliveries.action_mark_acked()
        return self._ok({"notification_id": int(notification_id), "acked": len(deliveries)})

    @endpoint("NSP Mobile Notification Read", route_suffix="mobile/notifications/read", methods="POST", code="nsp_mobile_notifications_read")
    def api_notifications_read(self):
        data = self._payload()
        notification_id = data.get("notification_id")
        if not notification_id:
            return self._error("notification_id is required.", 400)
        notification = self.env["nsp.notification"].sudo().browse(int(notification_id)).exists()
        if not notification:
            return self._error("Notification not found.", 404)
        notification.action_mark_read()
        return self._ok({"notification_id": notification.id, "state": notification.state}, message="Notification marked as read.")

    @endpoint("NSP Mobile Notifications Read All", route_suffix="mobile/notifications/read-all", methods="POST", code="nsp_mobile_notifications_read_all")
    def api_notifications_read_all(self):
        data = self._payload()
        domain = self._notification_domain_for_mobile(data)
        if domain is None:
            return self._error("Missing or invalid user_id/user_code/odoo_user_id/login.", 400)
        domain.append(("state", "=", "unread"))
        notifications = self.env["nsp.notification"].sudo().search(domain, limit=1000)
        notifications.action_mark_read()
        return self._ok({"updated": len(notifications)}, message="Notifications marked as read.")

    @api.model
    def _parking_monitor_domain(self, data):
        domain = [
            ("active", "=", True),
            ("monitor_channel", "=", "parking_monitor"),
            ("parking_transaction_id", "!=", False),
        ]
        since_id = data.get("since_id")
        if since_id:
            try:
                domain.append(("id", ">", int(since_id)))
            except Exception:
                pass
        gate_id = data.get("gate_id")
        if gate_id:
            try:
                domain.append(("gate_id", "=", int(gate_id)))
            except Exception:
                pass
        branch_id = data.get("branch_id")
        if branch_id:
            try:
                domain.append(("branch_id", "=", int(branch_id)))
            except Exception:
                pass
        lane_id = data.get("lane_id")
        if lane_id:
            try:
                domain.append(("parking_transaction_id.lane_id", "=", int(lane_id)))
            except Exception:
                pass
        lane_code = str(data.get("lane_code") or "").strip()
        if lane_code:
            domain.append(("parking_transaction_id.lane_code", "=ilike", lane_code))
        direction = str(data.get("direction") or "").strip().lower()
        if direction in ("entry", "exit"):
            domain.append(("parking_transaction_id.direction", "=", direction))
        status_filter = str(data.get("status") or "").strip().lower()
        if status_filter in ("allowed", "denied"):
            domain.append(("parking_transaction_id.status", "=", status_filter))
        return domain

    @endpoint("NSP Parking Monitor Events", route_suffix="parking-monitor/events", methods="GET,POST", code="nsp_parking_monitor_events")
    def api_parking_monitor_events(self):
        """Return parking monitor notifications for external monitor clients.

        Notification is the delivery layer; the linked nsp.parking.transaction
        remains the business source of truth.
        """
        data = self._payload()
        data.update(self._params())
        try:
            limit = min(max(int(data.get("limit") or 30), 1), 100)
        except Exception:
            limit = 30
        try:
            since_id = int(data.get("since_id") or 0)
        except Exception:
            since_id = 0
        domain = self._parking_monitor_domain(data)
        records = self.env["nsp.notification"].sudo().search(domain, order="event_time desc, id desc", limit=limit)
        records = records.sorted(key=lambda rec: (rec.event_time or fields.Datetime.now(), rec.id))
        events = [rec._parking_monitor_payload() for rec in records]
        return self._ok({
            "events": events,
            "count": len(events),
            "last_event_id": max([event["id"] for event in events], default=since_id),
            "source_model": "nsp.notification",
            "business_source_model": "nsp.parking.transaction",
            "server_time": fields.Datetime.to_string(fields.Datetime.now()),
        })
