{
    "name": "NSP RFID",
    "summary": "Canonical RFID Tag Whitelist for NSP",
    "description": (
        "Maintain the authoritative Cloud whitelist of normalized and unique RFID TIDs."
    ),
    "version": "19.0.5.0.0",
    "sequence": 20,
    "author": "BKU Team",
    "category": "Services",
    "depends": ["base", "web", "nsp_core"],
    "data": [
        "security/ir.model.access.csv",
        "views/rfid_tag_views.xml",
        "views/menu_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "nsp_rfid/static/src/js/tid_normalizer.js",
            "nsp_rfid/static/src/js/scan_feedback.js",
            "nsp_rfid/static/src/xml/rfid_tid_scan_field.xml",
            "nsp_rfid/static/src/js/rfid_tid_scan_field.js",
            "nsp_rfid/static/src/scss/rfid_tid_scan_field.scss",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "LGPL-3",
}
