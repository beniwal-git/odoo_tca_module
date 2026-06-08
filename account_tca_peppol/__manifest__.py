# Part of TCA. See LICENSE file for full copyright and licensing details.

{
    'name': 'Suntech E-Invoicing UAE',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Accounting',
    'summary': 'UAE PINT AE e-invoicing via TCA Access Point',
    'description': """
        Integrates Odoo with the TCA (Tax Compliance Agent) Peppol Access Point.
        Replaces Odoo's native account_peppol module for UAE businesses.

        Features:
        - PINT AE (UAE-specific Peppol CIUS) XML generation
        - Outbound invoice/credit note submission to TCA
        - Inbound invoice/credit note receipt from TCA via webhook
        - OAuth2 client credentials authentication per company
        - Real-time delivery status tracking
        - Multi-company support
        - Odoo 19 compatible
    """,
    'author': 'Suntech - Tax Compliance Agent',
    'website': 'https://taxcomplianceagent.com/',
    'support': 'support@taxcomplianceagent.com',  # TODO confirm real support inbox
    'depends': [
        'account',
        'account_edi_ubl_cii',
        'l10n_ae',
    ],
    # saxonche powers PINT AE schematron validation (services/
    # schematron_validator.py). It is soft-imported — the module still
    # installs without it, but schematron validation is silently skipped.
    # Declaring it here surfaces a clear "missing dependency" error at
    # install time instead of failing quietly. odoo.sh picks it up from
    # the repo-root requirements.txt.
    'external_dependencies': {
        'python': ['saxonche'],
    },
    # l10n_ae (UAE chart of accounts) is a hard dependency — tests load the 'ae'
    # chart template, and module operations mid-test are forbidden by Odoo 19's
    # test runner. Making it a depend ensures install before tests run.
    #
    # account_peppol uses the Odoo IAP proxy and conflicts with TCA's direct AP
    # integration. Both cannot be installed simultaneously.
    'conflicts': ['account_peppol'],
    'data': [
        'security/ir.model.access.csv',
        'data/cron.xml',
        'views/res_config_settings_views.xml',
        'views/account_move_views.xml',
        'views/res_partner_views.xml',
        'views/res_company_views.xml',
        'views/account_tax_views.xml',
        'wizard/account_move_send_wizard_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'account_tca_peppol/static/src/scss/account_move_form.scss',
        ],
    },
    'license': 'OPL-1',
    'auto_install': False,
    'installable': True,
    'application': False,
    'images': ['static/description/banner.png'],
    # Test files — discovered by Odoo's test runner via tests/__init__.py
    # Run with:  ./odoo-bin -i account_tca_peppol --test-enable --stop-after-init
    # Or tagged: --test-tags account_tca_peppol
}
