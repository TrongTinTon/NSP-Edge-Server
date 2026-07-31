{
    'name': 'NSP RFID',
    'summary': 'RFID Tag Whitelist for NSP',
    'description': 'Maintain the authoritative whitelist of normalized RFID TIDs. Assignment ownership is managed separately and the tag has no stored type or purpose.',
    'version': '19.0.3.2.0',
    'sequence': 20,
    'author': 'BKU Team',
    'category': 'Services',
    'depends': ['base', 'nsp_core'],
    'installable': True,
    'application': False,
    'auto_install': False,
    'assets': {
        'web.assets_backend': [
            'nsp_rfid/static/src/xml/rfid_scan_field.xml',
            'nsp_rfid/static/src/js/rfid_scan_field.js',
            'nsp_rfid/static/src/scss/rfid_scan_field.scss',
        ],
    },
    'data': [
        'security/ir.model.access.csv',
        'views/rfid_tag_views.xml',
        'views/menu_views.xml',
    ],
    'license': 'LGPL-3',
}
