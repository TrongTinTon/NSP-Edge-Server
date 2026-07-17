# -*- coding: utf-8 -*-
"""Unauthenticated bootstrap routes for Controller pairing."""
import json
import logging

from odoo import http, fields
from odoo.exceptions import ValidationError, UserError
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


def _json_body():
    try:
        raw = request.httprequest.get_data(as_text=True) or ""
        data = json.loads(raw) if raw else {}
    except Exception:
        raise ValidationError("Invalid JSON body.")
    if not isinstance(data, dict):
        raise ValidationError("JSON object is required.")
    return data


def _response(payload, status=200):
    return Response(
        json.dumps(payload, ensure_ascii=False, default=str),
        status=status,
        content_type="application/json; charset=utf-8",
    )


def _success(data=None, status=200):
    return _response({"success": True, "data": data or {}}, status)


def _error(code, message, status=400, details=None):
    return _response({
        "success": False,
        "error_code": code,
        "message": message,
        "details": details or {},
    }, status)


class NspControllerPairingController(http.Controller):

    @http.route(
        "/nsp/zeroconfig/pairing/request",
        type="http", auth="none", methods=["POST"], csrf=False,
        save_session=False,
    )
    def pairing_request(self, **kwargs):
        try:
            data = _json_body()
            pairing, token = request.env["nsp.controller.pairing.request"].sudo().create_public_request(data)
            return _success({
                "pairing_request_uid": pairing.pairing_request_uid,
                "pairing_token": token,
                "pairing_status": pairing.pairing_status,
                "expires_at": fields.Datetime.to_string(pairing.expires_at),
                "poll_interval_seconds": 5,
            }, 202)
        except ValidationError as exc:
            text = str(exc)
            code = "invalid_payload"
            if "machine_id is required" in text:
                code = "missing_machine_id"
            elif "already paired with a Controller" in text:
                code = "machine_already_paired"
            elif "active pairing request" in text:
                code = "machine_already_pairing"
            elif "Exactly one active Edge Server" in text:
                code = "edge_server_not_configured"
            return _error(code, text, 400)
        except Exception:
            _logger.exception("Cannot create Controller Pairing Request")
            return _error("pairing_request_failed", "Cannot create pairing request", 500)

    @http.route(
        "/nsp/zeroconfig/pairing/status",
        type="http", auth="none", methods=["POST"], csrf=False,
        save_session=False,
    )
    def pairing_status(self, **kwargs):
        try:
            data = _json_body()
            allowed = {"pairing_request_uid", "pairing_token", "machine_id"}
            unknown = sorted(set(data) - allowed)
            if unknown:
                return _error("invalid_payload", "Unsupported field(s): %s" % ", ".join(unknown), 400)
            uid = str(data.get("pairing_request_uid") or "").strip()
            token = str(data.get("pairing_token") or "").strip()
            machine_id = str(data.get("machine_id") or "").strip()
            if not uid:
                return _error("pairing_request_not_found", "pairing_request_uid is required", 400)
            pairing = request.env["nsp.controller.pairing.request"].sudo().search([
                ("pairing_request_uid", "=", uid),
            ], limit=1)
            if not pairing:
                return _error("pairing_request_not_found", "Pairing request was not found", 404)
            pairing.expire_if_needed()
            if not pairing.check_pairing_token(token):
                return _error("invalid_pairing_token", "Invalid pairing token", 401)
            if machine_id != pairing.machine_id:
                return _error("machine_id_mismatch", "machine_id does not match the pairing request", 403)
            if pairing.pairing_status == "expired":
                return _error("pairing_request_expired", "Pairing request has expired", 410)
            if pairing.pairing_status == "cancelled":
                return _error("pairing_request_cancelled", "Pairing request was cancelled", 409)
            try:
                payload = pairing.delivery_payload_once()
            except UserError as exc:
                return _error("pairing_credential_unavailable", str(exc), 409)
            if payload.get("pairing_status") in ("pending", "approved") and not payload.get("client_secret"):
                payload["poll_interval_seconds"] = 5
            return _success(payload)
        except ValidationError as exc:
            return _error("invalid_payload", str(exc), 400)
        except Exception:
            _logger.exception("Cannot read Controller Pairing status")
            return _error("pairing_status_failed", "Cannot read pairing status", 500)

    @http.route(
        "/nsp/zeroconfig/pairing/cancel",
        type="http", auth="none", methods=["POST"], csrf=False,
        save_session=False,
    )
    def pairing_cancel(self, **kwargs):
        try:
            data = _json_body()
            allowed = {"pairing_request_uid", "pairing_token", "machine_id"}
            unknown = sorted(set(data) - allowed)
            if unknown:
                return _error("invalid_payload", "Unsupported field(s): %s" % ", ".join(unknown), 400)
            uid = str(data.get("pairing_request_uid") or "").strip()
            pairing = request.env["nsp.controller.pairing.request"].sudo().search([
                ("pairing_request_uid", "=", uid),
            ], limit=1)
            if not pairing:
                return _error("pairing_request_not_found", "Pairing request was not found", 404)
            if not pairing.check_pairing_token(str(data.get("pairing_token") or "")):
                return _error("invalid_pairing_token", "Invalid pairing token", 401)
            if str(data.get("machine_id") or "").strip() != pairing.machine_id:
                return _error("machine_id_mismatch", "machine_id does not match the pairing request", 403)
            pairing.action_cancel()
            return _success({"pairing_status": "cancelled"})
        except UserError as exc:
            return _error("invalid_pairing_status", str(exc), 409)
        except ValidationError as exc:
            return _error("invalid_payload", str(exc), 400)
        except Exception:
            _logger.exception("Cannot cancel Controller Pairing Request")
            return _error("pairing_cancel_failed", "Cannot cancel pairing request", 500)
