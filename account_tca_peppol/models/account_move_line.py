# Part of TCA. See LICENSE file for full copyright and licensing details.

import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class AccountMoveLine(models.Model):
    """
    Extends account.move.line with UAE PINT AE-specific fields:
      - tca_commodity_type: G (Goods) or S (Services) — BTAE-13, mandatory
      - tca_hs_code: HS or CPV classification code — IBT-158, optional
      - tca_rc_description: Reverse charge goods/services type — BTAE-09
      - tca_service_accounting_code: Service accounting code — BTAE-17
      - tca_lot_number: Lot number for exports — BTAE-24
      - tca_per_unit_amount: Per-unit amount for margin/e-commerce — PerUnitAmount
    """
    _inherit = 'account.move.line'

    tca_commodity_type = fields.Selection(
        selection=[
            ('G', 'Goods'),
            ('S', 'Services'),
            ('B', 'Both'),
        ],
        string='Commodity Type (UAE)',
        help=(
            'BTAE-13: Whether this line item is a Good (G), Service (S), or Both (B). '
            'Mandatory for PINT AE (UAE Peppol) invoices.'
        ),
        default=False,
    )
    tca_hs_code = fields.Char(
        string='HS / CPV Code',
        size=30,
        help=(
            'IBT-158: Harmonised System (HS) or CPV classification code for this item. '
            'Optional but recommended for goods imports/exports. '
            'Example HS code: 88098432324. Will be output with listID="HS".'
        ),
    )
    tca_rc_description = fields.Char(
        string='Goods/Services Type (BTAE-09)',
        help=(
            'BTAE-09: Description of the type of goods or services supplied. '
            'Mandatory when the VAT category on this line is AE (Reverse Charge — UC6). '
            'Must be a code from the GoodsType code list, e.g. "DL8.48.3.1" (Crude oil).'
        ),
    )
    tca_service_accounting_code = fields.Char(
        string='Service Accounting Code (BTAE-17)',
        help=(
            'BTAE-17: Numeric service classification code. Optional at confirm '
            'time; TCA\'s server schematron (ibr-185-ae / ibr-186-ae) may '
            'require it when the line is Services or Both. Common sources: '
            'UN CPC (5 digits, e.g. 84111), India GST SAC (6 digits, e.g. '
            '998311), or your own internal scheme. Rendered as '
            'AdditionalItemIdentification with schemeID="SAC".'
        ),
    )
    tca_lot_number = fields.Char(
        string='Lot Number (BTAE-24)',
        help=(
            'BTAE-24: Lot number for export items (UC14). '
            'Rendered as Item/ItemInstance/LotIdentification/LotNumberID.'
        ),
    )
    tca_vat_exemption_reason_code = fields.Char(
        string='VAT Exemption Reason Code (IBT-186)',
        help=(
            'IBT-186: reason this line is exempt from VAT. Mandatory when the '
            'line VAT category is E (Exempt) — schematron ibr-167-ae.\n'
            'Per-line override: takes precedence over the exemption reason set '
            'on the tax record. Leave blank to fall back to the tax value.\n'
            'UAE Article-46 exempt supplies: DL8.46.1 financial services, '
            'DL8.46.2 residential units, DL8.46.3 bare land, DL8.46.4 local '
            'passenger transport.'
        ),
    )
    tca_per_unit_amount = fields.Float(
        string='Per Unit Amount',
        digits='Product Price',
        help=(
            'Per-unit taxable amount for margin scheme (UC15) and e-commerce (UC13). '
            'Rendered as ClassifiedTaxCategory/PerUnitAmount.'
        ),
    )

    tca_seller_item_id = fields.Char(
        string='Seller Item ID (IBT-155)',
        help='IBT-155: Identifier assigned to the item by the seller.',
    )
    tca_buyer_item_id = fields.Char(
        string='Buyer Item ID (IBT-156)',
        help='IBT-156: Identifier assigned to the item by the buyer.',
    )
    tca_standard_item_id = fields.Char(
        string='Standard Item ID / GTIN (IBT-157)',
        help='IBT-157: Standardised item identifier (e.g. GTIN/EAN). Scheme ID defaults to 0160.',
    )
    tca_standard_item_scheme = fields.Char(
        string='Standard Item Scheme ID',
        size=10,
        help='IBT-157-1: Scheme identifier for the standard item ID. Default 0160 = GTIN.',
    )
    tca_order_line_ref = fields.Char(
        string='Order Line Ref (IBT-132)',
        help='IBT-132: Reference to the corresponding line in the purchase order.',
    )
    tca_line_period_start = fields.Date(
        string='Line Period Start (IBT-134)',
        copy=False,
        help='IBT-134: Start date of the delivery period for this line.',
    )
    tca_line_period_end = fields.Date(
        string='Line Period End (IBT-135)',
        copy=False,
        help='IBT-135: End date of the delivery period for this line.',
    )
    tca_line_note = fields.Char(
        string='Line Note (IBT-127)',
        help='IBT-127: Free-text note relevant to this invoice line.',
    )

    # ── Effective commodity type (cached) ─────────────────────────────────────
    # Validation, the XML builder and the import path all need the same
    # resolved Goods/Services classification per line. Computing it once
    # via a stored compute removes the duplicate inference (was called
    # twice per line on every Confirm — validation + XML build).
    tca_effective_commodity_type = fields.Selection(
        selection=[
            ('G', 'Goods'),
            ('S', 'Services'),
            ('B', 'Both'),
        ],
        compute='_compute_tca_effective_commodity_type',
        store=True,
        help='Resolved Goods/Services classification for this line: the user-set '
             'tca_commodity_type if any, otherwise inferred from the product type. '
             'Used by validation and XML emission so inference happens once.',
    )

    # ── Exempt-line flag (drives the "reason required" UI) ────────────────────
    # True when the line carries an Exempt (E) VAT tax but no exemption reason
    # is available yet — neither on the line nor as a default on the tax. The
    # view binds the reason field's `required` to this, so it turns mandatory
    # the moment an Exempt tax is picked. The server-side confirm gate
    # (_tca_check_lines, ibr-167-ae) enforces the same rule authoritatively.
    tca_line_needs_exemption_reason = fields.Boolean(
        compute='_compute_tca_line_needs_exemption_reason',
        help='Internal: the line is Exempt (E) and still lacks a VAT exemption '
             'reason code (IBT-186) on either the line or the tax.',
    )

    @api.depends('tax_ids', 'tax_ids.tca_tax_category',
                 'tax_ids.tca_exemption_reason_code', 'tca_vat_exemption_reason_code')
    def _compute_tca_line_needs_exemption_reason(self):
        for line in self:
            exempt_tax = line.tax_ids.filtered(
                lambda t: t.tca_tax_category == 'E')[:1]
            has_reason = bool((line.tca_vat_exemption_reason_code or '').strip()) or (
                bool(exempt_tax) and bool(exempt_tax.tca_exemption_reason_code))
            line.tca_line_needs_exemption_reason = bool(exempt_tax) and not has_reason

    @api.depends('product_id')
    def _compute_product_uom_id(self):
        """Extend upstream: fall back to "Units" when the line has no product.
        PINT AE IBT-130 (Unit of Measure) is mandatory — free-text lines would
        otherwise have an empty product_uom_id and fail the compliance gate."""
        super()._compute_product_uom_id()
        default_uom = self.env.ref('uom.product_uom_unit', raise_if_not_found=False)
        if not default_uom:
            return
        for line in self.filtered(lambda l: not l.product_uom_id and l.display_type == 'product'):
            line.product_uom_id = default_uom

    @api.model_create_multi
    def create(self, vals_list):
        """Belt-and-suspenders: ensure product_uom_id is set on free-text product lines."""
        default_uom = self.env.ref('uom.product_uom_unit', raise_if_not_found=False)
        if default_uom:
            for vals in vals_list:
                if vals.get('display_type', 'product') == 'product' and not vals.get('product_uom_id') and not vals.get('product_id'):
                    vals['product_uom_id'] = default_uom.id
        return super().create(vals_list)

    @api.depends('tca_commodity_type', 'tca_hs_code', 'tca_service_accounting_code',
                 'product_id', 'product_id.type')
    def _compute_tca_effective_commodity_type(self):
        for line in self:
            line.tca_effective_commodity_type = (
                line.tca_commodity_type or line._get_default_commodity_type()
            )

    def _get_default_commodity_type(self):
        """
        Infer commodity type when `tca_commodity_type` is not explicitly set.

        TCA requires every line to be classified: Goods → HS code (IBT-158),
        Services → SAC (BTAE-17), Both → both. So we infer from the code the
        user actually entered — providing a SAC means the line is a service,
        providing an HS code means goods:

          · HS + SAC          → 'B' (Both)
          · SAC only          → 'S' (Services)
          · HS only           → 'G' (Goods)
          · neither, service product → 'S'
          · neither, otherwise       → 'G' (Odoo product.type defaults to
                                             'consu' = goods; most B2B lines
                                             are goods)

        Users can still override via the "Commodity (UAE)" column.
        """
        self.ensure_one()
        has_hs = bool((self.tca_hs_code or '').strip())
        has_sac = bool((self.tca_service_accounting_code or '').strip())
        if has_hs and has_sac:
            return 'B'
        if has_sac:
            return 'S'
        if has_hs:
            return 'G'
        if self.product_id and self.product_id.type == 'service':
            return 'S'
        return 'G'

    # ── Format constraints ────────────────────────────────────────────────────

    _RE_HS_CODE = re.compile(r'^\d{6,12}$')
    _RE_DIGITS = re.compile(r'^\d+$')

    @api.constrains('tca_hs_code')
    def _check_tca_hs_code_format(self):
        for line in self:
            code = (line.tca_hs_code or '').strip()
            if not code:
                continue
            if not self._RE_HS_CODE.match(code):
                raise ValidationError(_(
                    '"HS / CPV Code" must be 6 to 12 digits. Current: "%s".', code,
                ))

    @api.constrains('tca_service_accounting_code')
    def _check_tca_service_accounting_code_format(self):
        for line in self:
            code = (line.tca_service_accounting_code or '').strip()
            if not code:
                continue
            if not self._RE_DIGITS.match(code):
                raise ValidationError(_(
                    '"Service Accounting Code" must contain digits only. Current: "%s".', code,
                ))
