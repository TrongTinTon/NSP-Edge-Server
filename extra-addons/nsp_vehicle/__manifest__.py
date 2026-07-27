{
    'name': 'NSP Vehicle',
    'summary': 'Vehicle management for NSP Gatekeeper',
    'description': (
        'Manage NSP vehicle master data, vehicle photos, RFID card assignments, '
        'authorized borrowers and vehicle configuration. Vehicle ownership uses '
        'nsp.user as the business identity.'
    ),
    'version': '19.0.14.0.0',
    'sequence': 30,
    'author': 'BKU Team',
    'category': 'Services',
    'depends': ['base', 'nsp_core', 'mail', 'nsp_rfid', 'nsp_user'],
    'installable': True,
    'application': True,
    'auto_install': False,
    'data': [
        'security/ir.model.access.csv',
        'data/vehicle_borrow_sequence.xml',
        'data/vehicle_master_data.xml',
        'views/vehicle_master_data_views.xml',
        'views/vehicle_views.xml',
        'views/vehicle_card_views.xml',
        'views/vehicle_borrow_views.xml',
        'views/menu_views.xml',
    ],
    'license': 'LGPL-3',
}
