{
    'name': 'NSP Notification',
    'summary': 'Operational and security alerts for NSP',
    'description': 'Minimal NSP notification center for operational and device security alerts.',
    'version': '19.0.1.0.1',
    'sequence': 40,
    'author': 'BKU Team',
    'category': 'Services',
    'depends': ['base', 'mail', 'bus', 'nsp_core'],
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
