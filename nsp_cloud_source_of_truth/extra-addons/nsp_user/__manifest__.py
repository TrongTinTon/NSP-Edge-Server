{
    "name": "NSP User",
    "summary": "NSP business identities and friendships",
    "description": (
        "Manage NSP business identities independently from optional Odoo Web accounts."
    ),
    "version": "19.0.16.0.0",
    "sequence": 10,
    "author": "BKU Team",
    "category": "Services",
    "depends": ["base", "mail", "nsp_core"],
    "data": [
        "security/ir.model.access.csv",
        "views/user_views.xml",
        "views/friendship_views.xml",
        "views/menu_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "LGPL-3",
}
