# Part of TCA. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models

# UAE VAT category codes per PINT AE / UNCL5305 — UAE mandate allows only these six.
UAE_TAX_CATEGORY_SELECTION = [
    ('S',  'S — Standard Rate (5%)'),
    ('E',  'E — Exempt from Tax'),
    ('O',  'O — Services Outside Scope / Not Subject to Tax'),
    ('AE', 'AE — VAT Reverse Charge'),
    ('Z',  'Z — Zero Rated'),
    ('N',  'N — Standard Rate Additional VAT'),
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
            'S = Standard (5%), E = Exempt, O = Out of Scope, AE = Reverse Charge,\n'
            'Z = Zero-Rated, N = Standard Rate Additional VAT.'
        ),
    )

    tca_exemption_reason_code = fields.Char(
        string='UAE Exemption Reason Code (IBT-121)',
        size=64,
        help=(
            'PINT AE IBT-121: Code from the AE-Exempt code list explaining why '
            'this tax is exempt or zero-rated (e.g. "VATEX-AE-SPEC").\n'
            'Mandatory when tca_tax_category is Z or E.\n'
            'Leave blank to use the Odoo default (EU codes — not valid for UAE).'
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

    @api.model
    def _tca_ensure_oos_tax(self, company, type_tax_use='sale'):
        """
        Return (creating if missing) the company's 0% Out-of-Scope tax for the
        given direction. Idempotent — called both eagerly at TCA connection
        time and lazily from the Out-of-Scope onchange so a user who hasn't
        yet wired the chart of accounts can still tick OOS and proceed.

        PINT AE rule ibr-sr-58 requires every line to carry a tax category;
        the value for OOS documents is 'O' with scheme 'VAT' (per the official
        Commercial invoice example). A 0% tax with tca_tax_category='O' is
        the minimum Odoo object that satisfies that contract.
        """
        existing = self.sudo().search([
            ('company_id', '=', company.id),
            ('tca_tax_category', '=', 'O'),
            ('amount', '=', 0.0),
            ('type_tax_use', '=', type_tax_use),
        ], limit=1)
        if existing:
            return existing

        # tax_group_id is required on account.tax. Reuse the company's
        # existing 0%/zero tax group if any, else fall back to the first
        # tax group visible to this company.
        tax_group = self.env['account.tax.group'].sudo().search(
            [('company_id', '=', company.id)], limit=1,
        )
        if not tax_group:
            tax_group = self.env['account.tax.group'].sudo().search([], limit=1)

        label = _('0%% Out-of-Scope (UAE)') if type_tax_use == 'sale' \
            else _('0%% Out-of-Scope (UAE) — Purchases')
        return self.sudo().create({
            'name': label,
            'description': _('Out-of-Scope supply — no UAE VAT (PINT AE category O).'),
            'amount': 0.0,
            'amount_type': 'percent',
            'type_tax_use': type_tax_use,
            'company_id': company.id,
            'tax_group_id': tax_group.id if tax_group else False,
            'tca_tax_category': 'O',
        })
