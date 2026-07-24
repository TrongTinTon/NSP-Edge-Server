{
    "name": "NSP IT Dashboard",
    "summary": "Operational health dashboard for NSP infrastructure, parking, API, sync, mobile and notification",
    "description": """
NSP IT Dashboard
================
Deployment-aware operational dashboard built on T4 Custom Dashboard. It focuses
on infrastructure health, parking pipeline health, Core API, Edge sync, Mobile
sessions/devices and Notification delivery rather than business vanity metrics.
""",
    "version": "19.0.1.0.0",
    "sequence": 50,
    "author": "BKU Team",
    "category": "Services",
    "depends": ["nsp_core", "nsp_gatekeeper", "t4_coreapi", "t4_custom_dashboard"],
    "data": [
        "security/dashboard_security.xml",
        "data/it_dashboard_data.xml",
        "views/it_dashboard_menu.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "LGPL-3",
}
