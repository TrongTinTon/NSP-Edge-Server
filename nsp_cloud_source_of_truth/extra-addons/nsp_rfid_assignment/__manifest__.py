{
    "name": "NSP RFID Assignment",
    "summary": "Assign and revoke RFID Tags from User and Vehicle forms",
    "description": "Cloud source of truth for RFID assignments managed directly from NSP User and used to build the Edge runtime projection.",
    "version": "19.0.1.2.1",
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
    "installable": True,
    "application": False,
    "auto_install": False,
    "assets": {
        "web.assets_backend": [
            "nsp_rfid_assignment/static/src/xml/rfid_scan_field.xml",
            "nsp_rfid_assignment/static/src/js/rfid_scan_field.js",
            "nsp_rfid_assignment/static/src/scss/rfid_scan_field.scss",
        ],
    },
    "data": [
        "security/ir.model.access.csv",
        "views/user_views.xml",
        "views/vehicle_views.xml",
    ],
    "license": "LGPL-3",
}
