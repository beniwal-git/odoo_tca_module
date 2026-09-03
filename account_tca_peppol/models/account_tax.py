# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models

# UAE VAT category codes per PINT AE / UNCL5305 — restricted to the six
# codes valid under the UAE mandate (no EU-only G/K carryover).
UAE_TAX_CATEGORY_SELECTION = [
    ('S',  'S — Standard Rate (5%)'),
    ('E',  'E — Exempt'),
    ('O',  'O — Not Subject to VAT (Out of Scope)'),
    ('AE', 'AE — Reverse Charge'),
    ('Z',  'Z — Zero Rated'),
    ('N',  'N — Standard Rate Additional VAT'),
]

# UAE Article-46 VAT exemption reason codes (IBT-121) — the only codes
# legally valid for an 'E' (Exempt) category tax under the UAE mandate.
UAE_TAX_EXEMPTION_REASON_SELECTION = [
    ('DL8.46.1', 'DL8.46.1 — Financial services'),
    ('DL8.46.2', 'DL8.46.2 — Supply of residential buildings'),
    ('DL8.46.3', 'DL8.46.3 — Supply of bare land'),
    ('DL8.46.4', 'DL8.46.4 — Local passenger transport'),
]


class AccountTax(models.Model):
    """
    Extends account.tax with PINT AE UAE-specific VAT classification fields.

    These fields allow administrators to precisely classify each tax record
    for UAE e-invoicing purposes, overriding Odoo's EU-centric default logic
    in _get_tax_unece_codes().

    IBT-118: TaxCategory/ID          ← tca_tax_category
    IBT-121: TaxExemptionReasonCode   ← tca_exemption_reason_code
    IBT-120: TaxExemptionReason       ← tca_exemption_reason
    """
    _inherit = 'account.tax'

    tca_tax_category = fields.Selection(
        selection=UAE_TAX_CATEGORY_SELECTION,
        string='UAE VAT Category (IBT-118)',
        help=(
            'PINT AE: VAT category code for this tax per UNCL5305 / UAE mandate.\n'
            'When set, overrides Odoo\'s auto-detected category in PINT AE XML.\n'
            'S = Standard (5%), Z = Zero-Rated, E = Exempt, AE = Reverse Charge,\n'
            'G = Export/Free, O = Out of Scope, K = Intra-Community.'
        ),
    )

    tca_exemption_reason_code = fields.Selection(
        selection=UAE_TAX_EXEMPTION_REASON_SELECTION,
        string='UAE Exemption Reason Code (IBT-121)',
        help=(
            'PINT AE IBT-121: one of the four UAE VAT Decree-Law Article 46 '
            'exemption codes. Mandatory when the VAT Category is "E" (Exempt) — '
            'see ibr-167-ae.'
        ),
    )

    tca_exemption_reason = fields.Char(
        string='UAE Exemption Reason Text (IBT-120)',
        size=256,
        help=(
            'PINT AE IBT-120: Human-readable description of why this supply '
            'is exempt or zero-rated under UAE VAT law.\n'
            'Example: "Zero-rated export of goods outside UAE" or '
            '"Exempt under Article 42 of UAE VAT Decree-Law".'
        ),
    )

    # ── Helpers ───────────────────────────────────────────────────────────────

    # Template: (category, rate, sale_label, purchase_label). Rate is the
    # amount_type='percent' value; category O/E/N/Z all bootstrap at 0% since
    # the actual VAT-bearing rate is company/product specific — S is the one
    # rate-carrying template (5%, the standard UAE rate).
    _PINT_TAX_TEMPLATES = [
        ('S',  5.0, _('5% VAT (S)'), _('5% VAT (S) — Purchases')),
        ('E',  0.0, _('0% Exempt (E)'), _('0% Exempt (E) — Purchases')),
        ('O',  0.0, _('0% Out-of-Scope (O)'), _('0% Out-of-Scope (O) — Purchases')),
        ('AE', 0.0, _('0% Reverse Charge (AE)'), _('0% Reverse Charge (AE) — Purchases')),
        ('Z',  0.0, _('0% Zero-Rated (Z)'), _('0% Zero-Rated (Z) — Purchases')),
        ('N',  0.0, _('0% Standard Rate Additional VAT (N)'), _('0% Standard Rate Additional VAT (N) — Purchases')),
    ]

    @api.model
    def _tca_ensure_pint_taxes(self, company):
        """
        Idempotently bootstrap all six canonical PINT AE taxes (S/E/O/AE/Z/N)
        for both sale and purchase on the given company. Safe to call
        repeatedly — matches on (company, type_tax_use, tca_tax_category) and
        skips any category that already has a tax.

        Returns the recordset of taxes that already existed or were just
        created (12 records total: 6 categories × 2 directions).
        """
        tax_group = self.env['account.tax.group'].sudo().search(
            [('company_id', '=', company.id)], limit=1,
        )
        if not tax_group:
            tax_group = self.env['account.tax.group'].sudo().search([], limit=1)

        result = self.env['account.tax']
        for category, rate, sale_label, purchase_label in self._PINT_TAX_TEMPLATES:
            for type_tax_use, label in (('sale', sale_label), ('purchase', purchase_label)):
                existing = self.sudo().search([
                    ('company_id', '=', company.id),
                    ('tca_tax_category', '=', category),
                    ('type_tax_use', '=', type_tax_use),
                ], limit=1)
                if not existing:
                    existing = self.sudo().create({
                        'name': label,
                        'amount': rate,
                        'amount_type': 'percent',
                        'type_tax_use': type_tax_use,
                        'company_id': company.id,
                        'tax_group_id': tax_group.id if tax_group else False,
                        'tca_tax_category': category,
                    })
                result |= existing
        return result

    @api.model
    def _tca_ensure_oos_tax(self, company, type_tax_use='sale'):
        """
        Return the company's 0% Out-of-Scope tax for the given direction,
        bootstrapping the full PINT AE tax set (see _tca_ensure_pint_taxes)
        if it isn't there yet. Idempotent — called both eagerly at TCA
        connection time and lazily from the Out-of-Scope onchange so a user
        who hasn't yet wired the chart of accounts can still tick OOS and
        proceed.

        PINT AE rule ibr-sr-58 requires every line to carry a tax category;
        the value for OOS documents is 'O' with scheme 'VAT' (per the official
        Commercial invoice example).
        """
        existing = self.sudo().search([
            ('company_id', '=', company.id),
            ('tca_tax_category', '=', 'O'),
            ('type_tax_use', '=', type_tax_use),
        ], limit=1)
        if existing:
            return existing
        taxes = self._tca_ensure_pint_taxes(company)
        return taxes.filtered(
            lambda t: t.tca_tax_category == 'O' and t.type_tax_use == type_tax_use
        )[:1]
