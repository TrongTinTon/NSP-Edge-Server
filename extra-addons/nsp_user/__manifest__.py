{
    'name': 'NSP User',
    'summary': 'NSP master business users, RFID assignments and friendships',
    'description': (
        'Master Business Identity for NSP people. Owns profile/avatar, business user data and '
        'RFID/friendship relations; Mobile identity extends nsp.user, while '
        'res.users is only an optional Odoo Web Access Account. Cloud is the '
        'source of truth and Edge receives only the business data required for runtime.'
    ),
    'version': '19.0.11.0.0',
    'sequence': 10,
    'author': 'BKU Team',
    'category': 'Services',
    'depends': ['base', 'nsp_core', 'mail', 'nsp_rfid'],
    'installable': True,
    'application': False,
    'auto_install': False,
    'data': [
        'security/ir.model.access.csv',
        'views/user_views.xml',
        'views/user_card_views.xml',
        'views/friendship_views.xml',
        'views/menu_views.xml',
    ],
    'license': 'LGPL-3',
}
