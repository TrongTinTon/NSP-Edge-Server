{
    "name": "NSP RFID Assignment",
    "summary": "Immutable RFID assignment history for Users and Vehicles",
    "description": (
        "Assign and revoke canonical RFID Tags while preserving an immutable audit history."
    ),
    "version": "19.0.2.0.0",
    "sequence": 25,
    "author": "BKU Team",
    "category": "Services",
    "depends": [
        "base",
        "mail",
        "web",
        "nsp_core",
        "nsp_user",
        "nsp_vehicle",
        "nsp_rfid",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/user_views.xml",
        "views/vehicle_views.xml",
        "views/rfid_tag_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "nsp_rfid_assignment/static/src/xml/rfid_scan_field.xml",
            "nsp_rfid_assignment/static/src/js/rfid_scan_field.js",
            "nsp_rfid_assignment/static/src/scss/rfid_scan_field.scss",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "LGPL-3",
}
