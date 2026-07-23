# -*- coding: utf-8 -*-
from datetime import datetime, time

from odoo import api, fields, models, tools, _


class ParkingDashboardMetric(models.Model):
    _name = "nsp.parking.dashboard.metric"
    _description = "NSP Parking IT Dashboard Metric"
    _auto = False
    _order = "sequence, id"

    sequence = fields.Integer(string="Sequence", readonly=True)
    code = fields.Char(string="Metric Code", readonly=True)
    name = fields.Char(string="Metric", readonly=True)
    category = fields.Selection([("traffic", "Traffic"), ("parking", "Parking State"), ("alert", "Alerts"), ("device", "Devices"), ("config", "Configuration")], string="Category", readonly=True)
    value_int = fields.Integer(string="Value", readonly=True)
    severity = fields.Selection([("info", "Info"), ("normal", "Normal"), ("warning", "Warning"), ("critical", "Critical")], string="Severity", readonly=True)
    help_text = fields.Char(string="Meaning", readonly=True)
    generated_at = fields.Datetime(string="Generated At", readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
CREATE OR REPLACE VIEW nsp_parking_dashboard_metric AS
WITH today_tx AS (
    SELECT event_type, status, error_code
      FROM nsp_parking_transaction
     WHERE event_time >= date_trunc('day', now())
), latest_vehicle_event AS (
    SELECT DISTINCT ON (vehicle_id) vehicle_id, event_type, status, event_time, id
      FROM nsp_parking_transaction
     WHERE vehicle_id IS NOT NULL AND status = 'allowed'
     ORDER BY vehicle_id, event_time DESC, id DESC
), metric_rows AS (
    SELECT 10 AS id, 10 AS sequence, 'check_in_today' AS code, 'Xe vào hôm nay' AS name, 'traffic' AS category, COUNT(*)::integer AS value_int, 'normal' AS severity, 'Số lượt xe vào hợp lệ trong ngày.' AS help_text FROM today_tx WHERE event_type = 'check_in' AND status = 'allowed'
    UNION ALL SELECT 20, 20, 'check_out_today', 'Xe ra hôm nay', 'traffic', COUNT(*)::integer, 'normal', 'Số lượt xe ra hợp lệ trong ngày.' FROM today_tx WHERE event_type = 'check_out' AND status = 'allowed'
    UNION ALL SELECT 30, 30, 'inside_now', 'Xe đang trong bãi', 'parking', COUNT(*)::integer, 'info', 'Ước tính xe còn trong bãi dựa trên sự kiện allowed gần nhất của từng xe.' FROM latest_vehicle_event WHERE event_type = 'check_in'
    UNION ALL SELECT 40, 40, 'denied_today', 'Sự kiện bị từ chối hôm nay', 'alert', COUNT(*)::integer, CASE WHEN COUNT(*) > 0 THEN 'warning' ELSE 'normal' END, 'Tổng số sự kiện ra/vào bị từ chối trong ngày.' FROM today_tx WHERE status = 'denied'
    UNION ALL SELECT 60, 60, 'missing_user_tid_today', 'Thiếu thẻ người dùng hôm nay', 'alert', COUNT(*)::integer, CASE WHEN COUNT(*) > 0 THEN 'critical' ELSE 'normal' END, 'Lượt Check-out không phát hiện được User RFID trong cửa sổ ghép thẻ.' FROM today_tx WHERE error_code = 'missing_user_tid'
    UNION ALL SELECT 70, 70, 'auth_error_today', 'Lỗi xác thực hôm nay', 'alert', COUNT(*)::integer, CASE WHEN COUNT(*) > 0 THEN 'critical' ELSE 'normal' END, 'Vehicle/User RFID chưa được gán hoặc người dùng không phải chủ xe và không có quyền mượn hợp lệ.' FROM today_tx WHERE error_code IN ('vehicle_not_found','user_not_assigned','unauthorized_vehicle_user')
    UNION ALL SELECT 80, 80, 'controller_offline', 'Controller offline/error', 'device', COUNT(*)::integer, CASE WHEN COUNT(*) > 0 THEN 'critical' ELSE 'normal' END, 'Controller đang offline, blocked, revoked hoặc error.' FROM nsp_controller WHERE COALESCE(active, true) = true AND (status IS NULL OR status IN ('offline','error','block','revoked'))
    UNION ALL SELECT 90, 90, 'device_offline', 'Reader/device offline/degraded', 'device', COUNT(*)::integer, CASE WHEN COUNT(*) > 0 THEN 'critical' ELSE 'normal' END, 'RFID reader hoặc thiết bị đang offline hoặc degraded.' FROM nsp_device WHERE status IS NULL OR status IN ('offline','degraded')
)
SELECT id, sequence, code, name, category, value_int, severity, help_text, now()::timestamp AS generated_at FROM metric_rows
        """)

    @api.model
    def _today_start(self):
        return fields.Datetime.to_string(datetime.combine(fields.Date.context_today(self), time.min))

    def _domain_for_code(self, code):
        today = self._today_start()
        if code == "check_in_today": return "nsp.parking.transaction", [("event_time", ">=", today), ("event_type", "=", "check_in"), ("status", "=", "allowed")]
        if code == "check_out_today": return "nsp.parking.transaction", [("event_time", ">=", today), ("event_type", "=", "check_out"), ("status", "=", "allowed")]
        if code == "denied_today": return "nsp.parking.transaction", [("event_time", ">=", today), ("status", "=", "denied")]
        if code == "missing_user_tid_today": return "nsp.parking.transaction", [("event_time", ">=", today), ("error_code", "=", "missing_user_tid")]
        if code == "auth_error_today": return "nsp.parking.transaction", [("event_time", ">=", today), ("error_code", "in", ["vehicle_not_found", "user_not_assigned", "unauthorized_vehicle_user"])]
        if code == "inside_now":
            self.env.cr.execute(
                """
                SELECT vehicle_id
                  FROM (
                        SELECT DISTINCT ON (vehicle_id)
                               vehicle_id, event_type
                          FROM nsp_parking_transaction
                         WHERE vehicle_id IS NOT NULL
                           AND status = 'allowed'
                         ORDER BY vehicle_id, event_time DESC, id DESC
                       ) latest
                 WHERE event_type = 'check_in'
                """
            )
            vehicle_ids = [row[0] for row in self.env.cr.fetchall()]
            return "nsp.vehicle", [("id", "in", vehicle_ids or [0])]
        if code == "controller_offline": return "nsp.controller", [("active", "=", True), ("status", "in", ["offline", "error", "block", "revoked"])]
        if code == "device_offline": return "nsp.device", [("status", "in", ["offline", "degraded"])]
        return "nsp.parking.transaction", []

    def action_open_records(self):
        self.ensure_one()
        model, domain = self._domain_for_code(self.code)
        return {"type": "ir.actions.act_window", "name": self.name or _("Dashboard Records"), "res_model": model, "view_mode": "list,form", "domain": domain, "target": "current"}
