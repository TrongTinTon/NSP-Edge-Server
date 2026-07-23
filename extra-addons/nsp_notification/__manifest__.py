{
    'name': 'NSP Notification',
    'summary': 'Operational and security alerts for NSP',
    'description': 'Minimal NSP notification center for parking events and operational/device security alerts.',
    'version': '19.0.2.0.0',
    'sequence': 40,
    'author': 'BKU Team',
    'category': 'Services',
    'depends': ['base', 'bus', 'nsp_core', 'nsp_user'],
    'installable': True,
    'application': True,
    'auto_install': False,
    'data': [
        'security/ir.model.access.csv',
        'views/notification_views.xml',
        'views/menu_views.xml',
    ],
    'license': 'LGPL-3',
}
