# Part of TCA. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models

# UAE VAT category codes per PINT AE / UNCL5305 — UAE mandate allows only these six.
UAE_TAX_CATEGORY_SELECTION = [
    ('S', 'S — Standard Rate (5%)'),
    ('E', 'E — Exempt from Tax'),
    ('O', 'O — Services Outside Scope / Not Subject to Tax'),
    ('AE', 'AE — VAT Reverse Charge'),
    ('Z', 'Z — Zero Rated'),
    ('N', 'N — Standard Rate Additional VAT'),
]

# UAE VAT exemption reason codes (BTAE / IBT-186) — the only four exempt
# supplies under UAE VAT Decree-Law Article 46, per the PINT AE
# `Aligned-TaxExemptionCodes.gc` code list. Required on any tax whose
# category is 'E' (schematron ibr-167-ae). The shipped example XMLs use a
# stale EU placeholder (`VATEX-AE-EDU`, education — which is zero-rated, not
# exempt, in the UAE); the .gc code list is authoritative, so we bind here.
UAE_TAX_EXEMPTION_REASON_SELECTION = [
    ('DL8.46.1', 'DL8.46.1 — Certain financial services'),
    ('DL8.46.2', 'DL8.46.2 — Supply of residential units (lease or sale)'),
    ('DL8.46.3', 'DL8.46.3 — Bare land'),
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
            "When set, overrides Odoo's auto-detected category in PINT AE XML.\n"
            'S = Standard (5%), E = Exempt, O = Out of Scope, AE = Reverse Charge,\n'
            'Z = Zero-Rated, N = Standard Rate Additional VAT.'
        ),
    )

    tca_exemption_reason_code = fields.Selection(
        selection=UAE_TAX_EXEMPTION_REASON_SELECTION,
        string='UAE Exemption Reason Code (IBT-121)',
        help=(
            'PINT AE IBT-186/121: the UAE VAT-law reason this supply is exempt. '
            'Rendered as TaxExemptionReasonCode.\n'
            'MANDATORY when the VAT category is E (Exempt) — schematron '
            'ibr-167-ae rejects an exempt line without it.\n'
            'Only the four Article-46 exempt supplies are valid: financial '
            'services, residential units, bare land, local passenger transport.'
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

    # ── Canonical PINT AE tax bootstrap ──────────────────────────────────────
    # UAE PINT AE recognises six tax category codes (UAE_TAX_CATEGORY_SELECTION
    # above). On TCA activation we materialise one account.tax per
    # (direction × category) per company, so users can wire invoice lines
    # to compliance-tagged taxes without manual setup. The invoice tax picker
    # (account_move_views.xml) is domain-filtered to taxes with
    # tca_tax_category set — these six are what the user sees per direction.
    #
    # `l10n_ae`'s per-emirate taxes are left intact (they may be referenced
    # by existing posted moves) but hidden from the picker by the same domain.

    # (category, rate, sale label, purchase label)
    _PINT_TAX_TEMPLATES = (
        ('S', 5.0, '5% VAT — Standard Rated', '5% VAT — Standard Rated (Input)'),
        ('E', 0.0, 'VAT Exempt', 'VAT Exempt (Input)'),
        ('O', 0.0, '0% Out of Scope (UAE)', '0% Out of Scope (UAE) — Purchases'),
        ('AE', 5.0, '5% VAT — Reverse Charge', '5% VAT — Reverse Charge (Input)'),
        ('Z', 0.0, '0% VAT — Zero Rated', '0% VAT — Zero Rated (Input)'),
        (
            'N',
            5.0,
            '5% VAT — Standard Rate Additional',
            '5% VAT — Standard Rate Additional (Input)',
        ),
    )

    @api.model
    def _tca_ensure_pint_taxes(self, company):
        """Materialise the six PINT AE taxes (S/E/O/AE/Z/N) per direction
        (sale + purchase) for `company`. Returns the full set. Idempotent —
        uniqueness key is (company, type_tax_use, tca_tax_category) so
        re-running on TCA reconnect / module upgrade is a no-op.

        Reverse-charge tax repartition (AE) is not auto-wired here — the
        seller- vs buyer-side repartition lines depend on the user's chart
        of accounts. Users configure those on the created tax themselves.
        """
        # tax_group_id is required. Reuse the company's first tax group;
        # fall back to any visible group (l10n_ae usually seeds one).
        tax_group = self.env['account.tax.group'].sudo().search(
            [('company_id', '=', company.id)],
            limit=1,
        ) or self.env['account.tax.group'].sudo().search([], limit=1)

        taxes = self.env['account.tax']
        for category, rate, sale_name, purchase_name in self._PINT_TAX_TEMPLATES:
            for direction, label in (('sale', sale_name), ('purchase', purchase_name)):
                existing = self.sudo().search(
                    [
                        ('company_id', '=', company.id),
                        ('type_tax_use', '=', direction),
                        ('tca_tax_category', '=', category),
                    ],
                    limit=1,
                )
                if existing:
                    taxes |= existing
                    continue
                taxes |= self.sudo().create(
                    {
                        'name': label,
                        'amount': rate,
                        'amount_type': 'percent',
                        'type_tax_use': direction,
                        'company_id': company.id,
                        'tax_group_id': tax_group.id if tax_group else False,
                        'tca_tax_category': category,
                    }
                )
        return taxes

    @api.model
    def _tca_ensure_oos_tax(self, company, type_tax_use='sale'):
        """Return the canonical 0% Out-of-Scope tax for `company` in the
        given direction. Bootstraps the full six if missing. Called from the
        OOS-toggle onchange so a user can tick "Out of Scope" without first
        configuring the chart of accounts (PINT AE rule ibr-sr-58 requires
        a tax category on every line)."""
        self._tca_ensure_pint_taxes(company)
        return self.sudo().search(
            [
                ('company_id', '=', company.id),
                ('tca_tax_category', '=', 'O'),
                ('type_tax_use', '=', type_tax_use),
            ],
            limit=1,
        )
