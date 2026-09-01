# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class TcaPaymentMeans(models.Model):
    """
    One PaymentMeans instruction on an invoice — PINT AE / IBG-16 is 0..n,
    i.e. an invoice may declare several payment means (e.g. AED 500 in
    cash + AED 300 by credit card). Each line maps to one <cac:PaymentMeans>
    element in the outbound XML (see
    account.edi.xml.ubl_pint_ae._get_invoice_payment_means_vals_list).

    Card detail fields (tca_card_pan / tca_card_holder_name) map to
    cac:CardAccount (IBG-18) — PINT AE caps CardAccount at ONE per
    document (ibr-066-ae). Mandate detail fields (tca_mandate_id /
    tca_payer_account_id) map to cac:PaymentMandate (IBG-19) — capped at
    ONE per document too (ibr-067-ae). Both caps are enforced in
    account_move._tca_validate_mandatory_fields, not here.

    Per the PINT AE spec, none of these detail fields are mandatory even
    when their corresponding code (54/55 for card, 49 for direct debit) is
    selected — only the code itself is required (ibr-049-ae). No client-side
    "must fill X" beyond that is added here; fields stay optional.
    """
    _name = 'tca.payment.means'
    _description = 'TCA Payment Means (IBT-081 / IBG-16)'
    _order = 'sequence, id'

    move_id = fields.Many2one(
        'account.move', string='Invoice', required=True,
        ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    tca_payment_means_code = fields.Selection(
        selection=[
            ('1', '1 - Instrument not defined'),
            ('10', '10 - In cash'),
            ('20', '20 - Cheque'),
            ('21', '21 - Banker drafter'),
            ('30', '30 - Credit transfer'),
            ('49', '49 - Direct debit'),
            ('54', '54 - Credit card'),
            ('55', '55 - Debit card'),
            ('68', '68 - Online payment service'),
        ],
        string='Payment Means Code (IBT-081)',
        required=True,
        default='1',
        help=(
            'IBT-081: UNCL4461 payment means code, emitted at '
            'cac:PaymentMeans/cbc:PaymentMeansCode.'
        ),
    )
    # ── Card details (IBG-18 / cac:CardAccount) — only meaningful for
    # codes 54 (Credit card) / 55 (Debit card). At most ONE line per
    # invoice may have these filled in — ibr-066-ae.
    tca_card_pan = fields.Char(
        string='Card Number (PAN)', size=30,
        help=(
            'IBG-18: Primary Account Number of the card used for payment. '
            'Use a masked value (e.g. ************1234) — never store the '
            'full PAN. Only ONE payment-means line per invoice may carry '
            'card details (PINT AE ibr-066-ae).'
        ),
    )
    tca_card_holder_name = fields.Char(
        string='Card Holder Name',
        help='IBG-18: name of the payment card holder. Optional.',
    )
    # ── Mandate details (IBG-19 / cac:PaymentMandate) — only meaningful for
    # code 49 (Direct debit). At most ONE line per invoice may have these
    # filled in — ibr-067-ae.
    tca_mandate_id = fields.Char(
        string='Mandate Reference',
        help=(
            'IBG-19: identifier of the direct-debit mandate authorising this '
            'payment. Only ONE payment-means line per invoice may carry '
            'mandate details (PINT AE ibr-067-ae).'
        ),
    )
    tca_payer_account_id = fields.Char(
        string='Payer Account (IBAN)',
        help='IBG-19: the account the direct debit will be drawn from. Optional.',
    )

    @api.constrains('tca_payment_means_code', 'tca_card_pan', 'tca_card_holder_name')
    def _check_card_details_scope(self):
        for line in self:
            if line.tca_payment_means_code not in ('54', '55') and (
                line.tca_card_pan or line.tca_card_holder_name
            ):
                raise ValidationError(
                    'Card details (PAN / Card Holder Name) can only be set on a '
                    '"Credit card" (54) or "Debit card" (55) payment means line.'
                )

    @api.constrains('tca_payment_means_code', 'tca_mandate_id', 'tca_payer_account_id')
    def _check_mandate_details_scope(self):
        for line in self:
            if line.tca_payment_means_code != '49' and (
                line.tca_mandate_id or line.tca_payer_account_id
            ):
                raise ValidationError(
                    'Mandate details (Mandate Reference / Payer Account) can only be '
                    'set on a "Direct debit" (49) payment means line.'
                )
