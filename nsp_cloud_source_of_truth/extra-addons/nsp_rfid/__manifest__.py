{
    'name': 'NSP RFID',
    'summary': 'RFID Tag Whitelist for NSP',
    'description': 'Maintain the authoritative Cloud whitelist of normalized RFID TIDs.',
    'version': '19.0.4.0.0',
    'sequence': 20,
    'author': 'BKU Team',
    'category': 'Services',
    'depends': ['base', 'nsp_core'],
    'installable': True,
    'application': False,
    'auto_install': False,
    'data': [
        'security/ir.model.access.csv',
        'views/rfid_tag_views.xml',
        'views/menu_views.xml',
    ],
    'license': 'LGPL-3',
}
