{
    "name": "NSP User",
    "summary": "NSP business identities and friendships",
    "description": "Manage NSP business identities independently from optional Odoo Web access accounts.",
    "version": "19.0.15.0.1",
    "sequence": 10,
    "author": "BKU Team",
    "category": "Services",
    "depends": [
        "base",
        "nsp_core",
        "mail",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
    "data": [
        "security/ir.model.access.csv",
        "views/user_views.xml",
        "views/friendship_views.xml",
        "views/menu_views.xml",
    ],
    "license": "LGPL-3",
}
