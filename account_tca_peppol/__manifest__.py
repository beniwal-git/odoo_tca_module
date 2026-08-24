# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.

{
    'name': 'TCA Peppol E-Invoicing (UAE PINT AE)',
    'version': '17.0.3.0.0',
    'category': 'Accounting/Accounting',
    'summary': 'UAE PINT AE e-invoicing via TCA Access Point',
    'description': """
        Integrates Odoo with the TCA (The Connect Arabia) Peppol Access Point.
        Replaces Odoo's native account_peppol module for UAE businesses.

        Features:
        - PINT AE (UAE-specific Peppol CIUS) XML generation
        - Outbound invoice/credit note submission to TCA
        - Inbound invoice/credit note receipt from TCA via webhook
        - OAuth2 client credentials authentication per company
        - Real-time delivery status tracking
        - Multi-company support
        - Odoo 17 + 18 compatible
    """,
    'author': 'TCA - The Connect Arabia',
    'website': 'https://taxcomplianceagent.com/',
    'depends': [
        'account',
        'account_edi_ubl_cii',
    ],
    # No external Python dependencies. PINT AE validation runs as pure-Python
    # rules at Confirm (account_move._tca_validate_xml_pipeline), and the TCA
    # Access Point runs the authoritative PINT AE schematron server-side on
    # submission (the inline-JSON /api/v1/invoices/ endpoint validates
    # synchronously — 400 with a per-field error list on bad content). A
    # previous build shipped an optional saxonche/Saxon-C local schematron
    # re-check; it was removed — the native GraalVM runtime it needs is
    # unreliable on some hosted deployments and added no coverage over the
    # Python rules + the AP's server-side check.
    #
    # l10n_ae (UAE chart of accounts) is NOT a hard dependency — the addon works without
    # it. Installing l10n_ae is strongly recommended for UAE companies as it provides the
    # correct VAT tax groups and account structure expected by UAE e-invoicing.
    #
    # account_peppol uses the Odoo IAP proxy and conflicts with TCA's direct AP
    # integration. Both cannot be installed simultaneously.
    'conflicts': ['account_peppol'],
    'data': [
        'security/ir.model.access.csv',
        'data/pint_ae_templates.xml',
        'data/cron.xml',
        'views/res_config_settings_views.xml',
        'views/res_company_views.xml',
        'views/account_move_views.xml',
        'views/res_partner_views.xml',
        'views/account_tax_views.xml',
    ],
    'license': 'OPL-1',
    'auto_install': False,
    'installable': True,
    'application': False,
    'post_init_hook': '_post_init_migrate_invoice_type_code',
    'images': [],
    # Test files — discovered by Odoo's test runner via tests/__init__.py
    # Run with:  ./odoo-bin -i account_tca_peppol --test-enable --stop-after-init
    # Or tagged: --test-tags account_tca_peppol
}
