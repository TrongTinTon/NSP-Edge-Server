{
    "name": "T4 Custom Dashboard",
    "version": "19.0.2.1.0",
    "category": "Technical",
    "author": "T4tek - Đặng Thành Nhân",
    "summary": "Reusable Python-backed dashboard engine for Odoo",
    "description": """
T4 Custom Dashboard
===================
Reusable dashboard engine with GridStack layouts, statistic cards, charts,
role-bound dashboards, drill-down actions and Python data sources.
""",
    "depends": ["web"],
    "data": [
        "security/dashboard_groups.xml",
        "security/ir.model.access.csv",
        "security/dashboard_rules.xml",
        "views/menu_action.xml",
        "views/custom_dashboard_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "t4_custom_dashboard/static/src/hooks/**/*.js",
            "t4_custom_dashboard/static/src/utils/**/*.js",
            "t4_custom_dashboard/static/src/components/**/*.js",
            "t4_custom_dashboard/static/src/components/**/*.xml",
            "t4_custom_dashboard/static/src/components/**/*.scss",
            "t4_custom_dashboard/static/src/dashboard/**/*.js",
            "t4_custom_dashboard/static/src/dashboard/**/*.xml",
            "t4_custom_dashboard/static/src/dashboard/**/*.scss",
        ],
        "t4_custom_dashboard.custom_dashboard_lib": [
            "/web/static/lib/jquery/jquery.js",
            "/t4_custom_dashboard/static/lib/css/gridstack.min.css",
            "/t4_custom_dashboard/static/lib/js/gridstack-h5.js",
            "/t4_custom_dashboard/static/lib/css/select2.min.css",
            "/t4_custom_dashboard/static/lib/js/select2.min.js",
        ],
        "t4_custom_dashboard.pdf_export_lib": [
            "/t4_custom_dashboard/static/lib/js/html2canvas.min.js",
            "/t4_custom_dashboard/static/lib/js/jspdf.umd.min.js",
        ],
        "web.assets_web_dark": [
            "t4_custom_dashboard/static/src/t4_custom_dashboard.dark.scss",
        ],
    },
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
