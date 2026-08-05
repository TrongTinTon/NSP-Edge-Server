# -*- coding: utf-8 -*-
from datetime import datetime, time, timedelta
import os

import pytz

from odoo import api, fields, models, _


class NspItDashboardService(models.AbstractModel):
    _name = "nsp.it.dashboard.service"
    _description = "NSP IT Dashboard Service"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def _model_available(self, model_name):
        return model_name in self.env.registry.models

    @api.model
    def _deployment_role(self):
        role = (
            self.env["ir.config_parameter"].sudo().get_param("nsp.deployment_role")
            or os.getenv("NSP_DEPLOYMENT_ROLE")
            or os.getenv("NSP_SERVER_ROLE")
            or "edge_server"
        ).strip().lower()
        return role if role in ("cloud", "edge_server") else "edge_server"

    @api.model
    def _today_start_utc(self):
        tz_name = self.env.user.tz or "UTC"
        try:
            tz = pytz.timezone(tz_name)
        except Exception:
            tz = pytz.UTC
        local_day = fields.Date.context_today(self)
        local_start = tz.localize(datetime.combine(local_day, time.min))
        return local_start.astimezone(pytz.UTC).replace(tzinfo=None)

    @api.model
    def _stat(self, value, ok_text="Healthy", alert_text="Needs attention", installed=True):
        if not installed:
            return {"value": 0, "trend": "neutral", "trend_value": "Module not installed"}
        value = int(value or 0)
        return {
            "value": value,
            "trend": "neutral",
            "trend_value": ok_text if value == 0 else alert_text,
        }

    @api.model
    def _fill_hourly(self, rows, hours, value_keys):
        now = fields.Datetime.to_datetime(fields.Datetime.now()).replace(minute=0, second=0, microsecond=0)
        start = now - timedelta(hours=max(1, int(hours)) - 1)
        by_hour = {fields.Datetime.to_datetime(row[0]).replace(minute=0, second=0, microsecond=0): row[1:] for row in rows}
        labels, dates = [], []
        series_values = [[] for _ in value_keys]
        for index in range(max(1, int(hours))):
            bucket = start + timedelta(hours=index)
            local_bucket = fields.Datetime.context_timestamp(self, bucket)
            labels.append(local_bucket.strftime("%H:%M"))
            dates.append(local_bucket.strftime("%Y-%m-%d %H:00"))
            values = by_hour.get(bucket, tuple(0 for _ in value_keys))
            for idx in range(len(value_keys)):
                series_values[idx].append(int(values[idx] or 0))
        return labels, dates, series_values

    # ------------------------------------------------------------------
    # Headline health cards
    # ------------------------------------------------------------------
    @api.model
    def get_dashboard_edge_unhealthy(self, filters=None, extra_domain=None):
        count = self.env["nsp.edge.server"].sudo().search_count([
            ("active", "=", True), ("status", "!=", "online"),
        ])
        return self._stat(count, alert_text="Edge needs attention")

    @api.model
    def get_dashboard_controller_unhealthy(self, filters=None, extra_domain=None):
        count = self.env["nsp.controller"].sudo().search_count([
            ("active", "=", True), ("status", "!=", "online"),
        ])
        return self._stat(count, alert_text="Controller needs attention")

    @api.model
    def get_dashboard_reader_unhealthy(self, filters=None, extra_domain=None):
        count = self.env["nsp.device"].sudo().search_count([
            ("status", "in", ["offline", "degraded"]),
        ])
        return self._stat(count, alert_text="Reader needs attention")

    @api.model
    def get_dashboard_stale_detections(self, filters=None, extra_domain=None, seconds=30):
        if self._deployment_role() == "cloud":
            return {"value": 0, "trend": "neutral", "trend_value": "Edge-only metric"}
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), seconds=max(10, int(seconds)))
        count = self.env["nsp.parking.detection.event"].sudo().search_count([
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("detected_at", "<", cutoff),
        ])
        return self._stat(count, alert_text="Parking pipeline backlog")

    @api.model
    def get_dashboard_denied_today(self, filters=None, extra_domain=None):
        count = self.env["nsp.parking.transaction"].sudo().search_count([
            ("event_time", ">=", self._today_start_utc()),
            ("status", "=", "denied"),
        ])
        return self._stat(count, ok_text="No denied event today", alert_text="Review denied parking events")

    @api.model
    def get_dashboard_api_errors_24h(self, filters=None, extra_domain=None):
        since = fields.Datetime.subtract(fields.Datetime.now(), hours=24)
        count = self.env["core.api.log"].sudo().search_count([
            ("create_date", ">=", since),
            ("success", "=", False),
        ])
        return self._stat(count, ok_text="API healthy", alert_text="Review Core API failures")

    @api.model
    def get_dashboard_sync_failures(self, filters=None, extra_domain=None):
        if self._deployment_role() == "cloud":
            return {"value": 0, "trend": "neutral", "trend_value": "Edge-only metric"}
        if not self._model_available("nsp.sync.record"):
            return self._stat(0, installed=False)
        count = self.env["nsp.sync.record"].sudo().search_count([
            ("status", "in", ["pending", "failed"]),
        ])
        return self._stat(count, ok_text="Sync queue healthy", alert_text="Local sync queue needs attention")

    @api.model
    def get_dashboard_notification_delivery_failures(self, filters=None, extra_domain=None):
        if not self._model_available("nsp.notification.delivery"):
            return self._stat(0, installed=False)
        Delivery = self.env["nsp.notification.delivery"].sudo()
        stale = fields.Datetime.subtract(fields.Datetime.now(), minutes=5)
        count = Delivery.search_count([
            "|",
            ("state", "=", "failed"),
            "&", ("state", "=", "pending"), ("create_date", "<", stale),
        ])
        return self._stat(count, ok_text="Notification delivery healthy", alert_text="Notification delivery needs attention")

    # ------------------------------------------------------------------
    # Charts
    # ------------------------------------------------------------------
    @api.model
    def get_dashboard_infrastructure_status_chart(self, filters=None, extra_domain=None):
        models_to_check = [
            ("Edge", "nsp.edge.server", [("active", "=", True)]),
            ("Controller", "nsp.controller", [("active", "=", True)]),
            ("Reader", "nsp.device", []),
        ]
        labels, online, unhealthy = [], [], []
        for label, model_name, base_domain in models_to_check:
            Model = self.env[model_name].sudo()
            total = Model.search_count(base_domain)
            if model_name == "nsp.device":
                healthy = Model.search_count(base_domain + [("status", "=", "online")])
            else:
                healthy = Model.search_count(base_domain + [("status", "=", "online")])
            labels.append(label)
            online.append(healthy)
            unhealthy.append(max(0, total - healthy))
        return {
            "labels": labels,
            "series": [
                {"label": "Online", "values": online},
                {"label": "Unhealthy", "values": unhealthy},
            ],
            "suggested_y_label": _("Nodes"),
            "suggested_x_label": _("Infrastructure"),
        }

    @api.model
    def get_dashboard_parking_traffic_hourly(self, filters=None, extra_domain=None, hours=24):
        hours = max(6, min(int(hours or 24), 72))
        since = fields.Datetime.subtract(fields.Datetime.now(), hours=hours)
        self.env.cr.execute(
            """
            SELECT date_trunc('hour', event_time) AS bucket,
                   COUNT(*) FILTER (WHERE event_type='check_in')::integer AS check_in,
                   COUNT(*) FILTER (WHERE event_type='check_out')::integer AS check_out
              FROM nsp_parking_transaction
             WHERE event_time >= %s
               AND status = 'allowed'
             GROUP BY bucket
             ORDER BY bucket
            """,
            [since],
        )
        labels, dates, values = self._fill_hourly(self.env.cr.fetchall(), hours, ("check_in", "check_out"))
        return {
            "labels": labels,
            "dates": dates,
            "series": [
                {"label": "Check-in", "values": values[0]},
                {"label": "Check-out", "values": values[1]},
            ],
            "suggested_y_label": _("Transactions"),
            "suggested_x_label": _("Hour"),
        }

    @api.model
    def get_dashboard_denied_reason_chart(self, filters=None, extra_domain=None, days=7):
        since = fields.Datetime.subtract(fields.Datetime.now(), days=max(1, min(int(days or 7), 31)))
        self.env.cr.execute(
            """
            SELECT COALESCE(error_code, 'unknown') AS reason, COUNT(*)::integer
              FROM nsp_parking_transaction
             WHERE event_time >= %s
               AND status = 'denied'
             GROUP BY COALESCE(error_code, 'unknown')
             ORDER BY COUNT(*) DESC, reason
            """,
            [since],
        )
        rows = self.env.cr.fetchall()
        selection = dict(self.env["nsp.parking.transaction"]._fields["error_code"].selection)
        return {
            "labels": [selection.get(code, code.replace("_", " ").title()) for code, _count in rows],
            "values": [int(count or 0) for _code, count in rows],
            "suggested_y_label": _("Denied transactions"),
            "suggested_x_label": _("Reason"),
        }

    @api.model
    def get_dashboard_api_health_hourly(self, filters=None, extra_domain=None, hours=24):
        hours = max(6, min(int(hours or 24), 72))
        since = fields.Datetime.subtract(fields.Datetime.now(), hours=hours)
        self.env.cr.execute(
            """
            SELECT date_trunc('hour', create_date) AS bucket,
                   COUNT(*)::integer AS requests,
                   COUNT(*) FILTER (WHERE success IS FALSE)::integer AS errors
              FROM core_api_log
             WHERE create_date >= %s
             GROUP BY bucket
             ORDER BY bucket
            """,
            [since],
        )
        labels, dates, values = self._fill_hourly(self.env.cr.fetchall(), hours, ("requests", "errors"))
        return {
            "labels": labels,
            "dates": dates,
            "series": [
                {"label": "Requests", "values": values[0]},
                {"label": "Errors", "values": values[1]},
            ],
            "suggested_y_label": _("API requests"),
            "suggested_x_label": _("Hour"),
        }

    @api.model
    def get_dashboard_sync_health_chart(self, filters=None, extra_domain=None):
        if self._deployment_role() == "cloud":
            return {"labels": ["Edge-only"], "values": [0], "suggested_y_label": _("Jobs")}
        if not self._model_available("nsp.sync.job"):
            return {"labels": ["Not installed"], "values": [0], "suggested_y_label": _("Jobs")}
        Job = self.env["nsp.sync.job"].sudo()
        labels = ["Success", "Failed", "Running", "Idle", "Disabled"]
        states = ["success", "failed", "running", "idle", "disabled"]
        return {
            "labels": labels,
            "values": [Job.search_count([("status", "=", state)]) for state in states],
            "suggested_y_label": _("Sync jobs"),
            "suggested_x_label": _("State"),
        }

    @api.model
    def get_dashboard_mobile_notification_health_chart(self, filters=None, extra_domain=None):
        if self._deployment_role() == "edge_server" and not (
            self._model_available("nsp.mobile.session") or self._model_available("nsp.notification")
        ):
            return {"labels": ["Cloud-only"], "values": [0], "suggested_y_label": _("Records")}
        labels = ["Active Sessions", "Recent Devices", "Unread Notifications", "Failed Delivery", "Stale Pending"]
        values = [0, 0, 0, 0, 0]
        if self._model_available("nsp.mobile.session"):
            values[0] = self.env["nsp.mobile.session"].sudo().search_count([("state", "=", "active")])
        if self._model_available("nsp.mobile.device"):
            cutoff = fields.Datetime.subtract(fields.Datetime.now(), minutes=15)
            values[1] = self.env["nsp.mobile.device"].sudo().search_count([
                ("active", "=", True), ("last_seen_at", ">=", cutoff),
            ])
        if self._model_available("nsp.notification"):
            values[2] = self.env["nsp.notification"].sudo().search_count([
                ("active", "=", True), ("state", "=", "unread"),
            ])
        if self._model_available("nsp.notification.delivery"):
            Delivery = self.env["nsp.notification.delivery"].sudo()
            values[3] = Delivery.search_count([("state", "=", "failed")])
            stale = fields.Datetime.subtract(fields.Datetime.now(), minutes=5)
            values[4] = Delivery.search_count([("state", "=", "pending"), ("create_date", "<", stale)])
        return {
            "labels": labels,
            "values": values,
            "suggested_y_label": _("Records"),
            "suggested_x_label": _("Mobile / Notification"),
        }

    # ------------------------------------------------------------------
    # Drill-down domains for stat_action widgets
    # ------------------------------------------------------------------
    @api.model
    def get_dashboard_domain_unhealthy_edges(self, filters=None, extra_domain=None, **kwargs):
        return [("active", "=", True), ("status", "!=", "online")]

    @api.model
    def get_dashboard_domain_unhealthy_controllers(self, filters=None, extra_domain=None, **kwargs):
        return [("active", "=", True), ("status", "!=", "online")]

    @api.model
    def get_dashboard_domain_unhealthy_readers(self, filters=None, extra_domain=None, **kwargs):
        return [("status", "in", ["offline", "degraded"])]

    @api.model
    def get_dashboard_domain_stale_detections(self, filters=None, extra_domain=None, seconds=30, **kwargs):
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), seconds=max(10, int(seconds)))
        return [("state", "=", "pending"), ("transaction_id", "=", False), ("detected_at", "<", cutoff)]

    @api.model
    def get_dashboard_domain_denied_today(self, filters=None, extra_domain=None, **kwargs):
        return [("event_time", ">=", self._today_start_utc()), ("status", "=", "denied")]

    @api.model
    def get_dashboard_domain_api_errors_24h(self, filters=None, extra_domain=None, **kwargs):
        since = fields.Datetime.subtract(fields.Datetime.now(), hours=24)
        return [("create_date", ">=", since), ("success", "=", False)]
