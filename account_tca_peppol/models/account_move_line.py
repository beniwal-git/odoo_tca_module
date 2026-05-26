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

    @api.depends('tca_commodity_type', 'product_id', 'product_id.type')
    def _compute_tca_effective_commodity_type(self):
        for line in self:
            line.tca_effective_commodity_type = (
                line.tca_commodity_type or line._get_default_commodity_type()
            )

    def _get_default_commodity_type(self):
        """
        Infer commodity type from the product when `tca_commodity_type` is not
        set. Only explicit service products are mapped to 'S'; everything else
        — storable products and free-text lines — defaults to 'G' (Goods).
        Rationale: Odoo's product.type itself defaults to 'consu' (Goods), and
        the vast majority of B2B invoice lines are goods. Defaulting to 'S'
        forced every line through the SAC-mandatory branch (ibr-185-ae).
        Users can still override via the "Commodity (UAE)" column.
        """
        self.ensure_one()
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
