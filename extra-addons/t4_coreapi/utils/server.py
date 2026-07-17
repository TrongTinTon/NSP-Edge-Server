import logging
import secrets
import hashlib
from datetime import datetime, timedelta
from odoo import fields
from odoo.http import request
from .zeroconfMain import ZeroconfNode

_logger = logging.getLogger(__name__)

node = None

SESSION_DURATION_HOURS = 8

SERVER_PORT = 8069

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tls-psk-server")

def start_discovery(service_name="_master", port=8069, discovery_secret=None):
    global node
    if node is not None:
        return {"code": 400, "message": "Zeroconf already running"}
    if not discovery_secret:
        return {"code": 422, "message": "Discovery secret key is required"}

    try:
        node = ZeroconfNode(service_name, port, discovery_secret=discovery_secret)
        _logger.info(f"Secure Zeroconf started and advertised with service {node.service_name} and ip:port of {node.ip}:{node.port}")
        return {"code": 200, "message": "Discovery started successfully"}

    except Exception as e:
        return {"code": 500, "message": f"Error: {str(e)}"}

def stop_discovery():
    global node
    if node is None:
        return {"code": 404, "message": "No active discovery session"}
    try:
        node.close()
        node = None
        _logger.info(f"Zeroconf service has stopped")
        return {"code": 200, "message": "Zeroconf stopped successfully"}

    except Exception as e:
        return {"code": 500, "message": f"Error: {str(e)}"}

# Security helpers
def generate_api_key():
    return secrets.token_hex(32)

def hash_key(key: str):
    return hashlib.sha256(key.encode()).hexdigest()

def create_session(controller):
    token = secrets.token_hex(32)
    expiry = fields.Datetime.now() + timedelta(hours=SESSION_DURATION_HOURS)

    controller.sudo().write({
        "session_token": token,
        "session_expiry": expiry,
        "connected": True,
        "status": "online",
        "timestamp": fields.Datetime.now(),
    })

    return token

def validate_session_token(env, token: str):
    controller = env["nsp.controller"].sudo().search([
        ('session_token', '=', token),
        ('connected', '=', True),
        ('active', '=', True),
        ('status', 'not in', ['revoked', 'block'])
    ], limit=1)

    if not controller:
        return None
    
    if controller.session_expiry and controller.session_expiry < fields.Datetime.now():
        controller.sudo().write({
            "connected": False,
            "status": "offline"
        })
        return None
    
    return controller