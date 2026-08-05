{
    'name': 'NSP Mobile',
    'summary': 'Cloud Mobile API using standard Odoo User authentication and permissions',
    'description': (
        'Cloud-only NSP Mobile API. Authentication uses an internal res.users account with a mandatory one-to-one nsp.user profile and '
        'standard Odoo Groups, ACLs and Record Rules. The module issues device-bound Mobile '
        'Tokens for profile, vehicles, parking history, friends, vehicle borrowing and notifications.'
    ),
    'version': '19.0.5.0.0',
    'sequence': 45,
    'author': 'BKU Team',
    'category': 'Services',
    'depends': [
        'base',
        'nsp_core',
        't4_coreapi',
        'nsp_user',
        'nsp_vehicle',
        'nsp_master_gatekeeper',
        'nsp_notification',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/odoo_user_self_service_rules.xml',
        'data/mobile_core_api_data.xml',
        'views/mobile_device_views.xml',
        'views/mobile_session_views.xml',
        'views/menu_views.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
