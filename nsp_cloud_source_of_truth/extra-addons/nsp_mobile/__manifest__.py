{
    'name': 'NSP Mobile',
    'summary': 'Cloud Mobile API for NSP business identities with Odoo credential authentication',
    'description': (
        'Cloud-only NSP Mobile API. Authentication uses an internal res.users credential linked one-to-one to an active nsp.user business identity. '
        'Mobile Tokens remain bound to the nsp.user identity, session and device. The module exposes profile, vehicles, Parking Logs, '
        'friendships, vehicle borrowing and notifications.'
    ),
    'version': '19.0.5.0.3',
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
