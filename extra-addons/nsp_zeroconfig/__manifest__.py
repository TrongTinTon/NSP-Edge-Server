{
    'name': 'NSP Zeroconfig',
    'version': '19.0.8.0.0',
    'summary': 'Secure IPv6 mDNS discovery and Controller Code bootstrap',
    'description': (
        'Advertises the NSP Edge/Local Server over LAN IPv6 mDNS. Controllers are pre-created in '
        'NSP Gatekeeper with a Controller Code and bootstrap directly by signed Controller Code; '
        'there is no approval queue, polling or cancellation workflow.'
    ),
    'category': 'Services',
    'author': 'BKU Team',
    'depends': ['nsp_core', 't4_coreapi', 'nsp_gatekeeper'],
    'external_dependencies': {'python': ['zeroconf', 'psutil']},
    'data': [
        'security/ir.model.access.csv',
        'data/zeroconfig_cron.xml',
        'wizard/nsp_zeroconfig_config_wizard_views.xml',
        'views/menu_views.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
