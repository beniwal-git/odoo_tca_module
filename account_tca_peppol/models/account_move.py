# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
TCA-specific fields and overrides on account.move.

State machine (tca_move_state):
  not_sent   → uploading   Triggered when user sends via TCA
  uploading  → submitted   S3 + POST /api/v1/invoices/ succeeded
  submitted  → processing  Status poll: status = 1 (Processing)
  processing → delivered   Status poll: status = 2 + c3_mls_status = 4 (Accepted)
  delivered  → received    Status poll: c5_mls_status = 4 (Accepted)
  * → error                Status poll: status = 4 (Failed)
  * → rejected             Status poll: status = 3 (Rejected)

Cancel block: invoices in processing/delivered/received states cannot be cancelled.
_get_ubl_cii_builder_from_xml_tree: PINT AE CustomizationID routed to our builder.
"""

import logging
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .. import constants
from ..services.tca_api import TcaTransientError, TcaValidationError

_logger = logging.getLogger(__name__)

# States that block cancellation (document is in-flight or completed)
_CANCEL_BLOCKED_STATES = frozenset(['processing', 'delivered', 'buyer_confirmed'])

# UAE Emirates codes (ibr-128-ae)
_UAE_EMIRATES = list(constants.UAE_EMIRATES)

# BTAE-03: Credit note reason codes (AE-CreditReason code list per UAE VAT Decree-Law)
CREDIT_NOTE_REASONS = [
    ('DL8.61.1.A', 'Supply was cancelled'),
    ('DL8.61.1.B', 'Tax treatment changed'),
    ('DL8.61.1.C', 'Consideration altered / Bad debt relief'),
    ('DL8.61.1.D', 'Goods/services returned'),
    ('DL8.61.1.E', 'Tax charged or applied in error'),
    ('VD', 'Volume Discount (no preceding invoice reference required)'),
]

# TCA Invoice Status integer codes (from API spec)
TCA_STATUS_PROCESSING = 1
TCA_STATUS_COMPLETED  = 2
TCA_STATUS_REJECTED   = 3
TCA_STATUS_FAILED     = 4

# TCA C3 MLS status integer codes
TCA_C3_ACCEPTED            = 4   # Delivered to buyer AP
TCA_C3_REJECTED            = 5
TCA_C3_UNABLE_TO_DELIVER   = 6

# TCA C5 MLS status integer codes
TCA_C5_ACCEPTED            = 4   # Buyer confirmed receipt

# PINT AE CustomizationID — must be checked before the BIS3 urn:cen.eu prefix check
PINT_AE_CUSTOMIZATION_ID = constants.PINT_AE_CUSTOMIZATION_ID
PINT_AE_PROFILE_ID = constants.PINT_AE_PROFILE_ID


class AccountMove(models.Model):
    _inherit = 'account.move'

    # ── TCA state machine ──────────────────────────────────────────────────────

    tca_move_state = fields.Selection(
        selection=[
            ('not_sent', 'Not Sent'),
            ('uploading', 'Uploading'),
            ('submitted', 'Submitted to TCA'),
            ('processing', 'Processing (In Transit)'),
            ('delivered', 'Delivered to Buyer AP'),
            ('buyer_confirmed', 'Confirmed by Buyer'),
            ('inbound_received', 'Received from Seller'),
            ('error', 'Error'),
            ('rejected', 'Rejected'),
            ('cancelled', 'Cancelled'),
        ],
        string='TCA Peppol Status',
        default='not_sent',
        copy=False,
        tracking=True,
        help=(
            'Lifecycle state of this invoice on the TCA Peppol network.\n'
            'not_sent: not yet submitted\n'
            'uploading: uploading XML to TCA storage\n'
            'submitted: registered with TCA, workflow starting\n'
            'processing: TCA sending to Peppol C3 Access Point\n'
            'delivered: C3 confirmed delivery to buyer AP\n'
            'buyer_confirmed: buyer C5 confirmed receipt (outbound terminal success)\n'
            'inbound_received: document received from Peppol network (inbound)\n'
            'error: submission or delivery error — see chatter\n'
            'rejected: rejected by Peppol network or buyer AP'
        ),
    )
    tca_invoice_uuid = fields.Char(
        string='TCA Invoice UUID',
        copy=False,
        readonly=True,
        help='UUID assigned by TCA after successful submission. Used for status polling and webhook matching.',
    )
    tca_submission_error = fields.Text(
        string='TCA Last Error',
        copy=False,
        readonly=True,
        help='Last error message received from TCA. Cleared on successful resubmission.',
    )
    tca_last_submission_id = fields.Char(
        string='Last TCA Submission ID',
        copy=False,
        readonly=True,
        help='The invoice_number sent to TCA on the most recent attempt — same as '
             'this record\'s name.',
    )
    tca_is_inbound = fields.Boolean(
        string='TCA Inbound',
        default=False,
        copy=False,
        readonly=True,
        help='True if this invoice was received via TCA Peppol (direction = RECEIVED).',
    )

    # Related: company.tca_is_active surfaced on the move so view conditions can
    # reference it directly (Odoo view attrs cannot traverse Many2one chains).
    tca_company_is_active = fields.Boolean(
        related='company_id.tca_is_active',
        readonly=True,
        store=False,
    )

    tca_create_einvoice = fields.Boolean(
        string='Create E-Invoice (UAE)',
        default=True,
        copy=True,
        help=(
            'Per-document opt-out of PINT AE e-invoicing. On by default for '
            'eligible documents. Untick to skip PINT AE validation and TCA '
            'submission entirely for this specific invoice/credit note — use '
            'for the rare document that must NOT go through TCA even though '
            'the company and partner are otherwise configured for it.'
        ),
    )

    # ── Inbound accept/reject ──────────────────────────────────────────────────

    tca_inbound_status = fields.Selection(
        selection=[
            ('pending', 'Pending Review'),
            ('accepted', 'Accepted'),
            ('rejected', 'Rejected'),
        ],
        string='Inbound Decision',
        copy=False,
        tracking=True,
        help='Buyer decision on an inbound invoice received via TCA Peppol.',
    )
    tca_reject_reason = fields.Text(
        string='Rejection Reason',
        copy=False,
        help='Reason for rejecting this inbound invoice. Logged in chatter.',
    )

    # ── Buyer Participant ID ─────────────────────────────────────────────────

    # PINT AE predefined endpoints (BIS Section 1.5.3, eas=0235).
    # Used when the document does not need to reach a real Peppol receiver:
    # the participant ID is overridden to one of these so TCA reports to C5
    # (FTA platform) only.
    _PREDEFINED_DEEMED = constants.PREDEFINED_DEEMED             # Deemed Supply (BTAE-02 pos 2 = 1)
    _PREDEFINED_NOT_SUBJECT = constants.PREDEFINED_NOT_SUBJECT   # Buyer not subject to UAE e-invoicing
    _PREDEFINED_EXPORT_NO_PEPPOL = constants.PREDEFINED_EXPORT_NO_PEPPOL  # Export, receiver not in Peppol (BTAE-02 pos 8 = 1)
    # Treat it as equivalent to "anonymous / not-in-Peppol buyer" for backward compat.
    _LEGACY_PLACEHOLDER_PARTICIPANT = constants.LEGACY_PLACEHOLDER_PARTICIPANT
    _ANON_BUYER_PIDS = constants.ANON_BUYER_PIDS
    # Backward-compat alias — kept so external callers / docs using the old name still work.
    _NON_UAE_DEFAULT_PARTICIPANT_ID = _LEGACY_PLACEHOLDER_PARTICIPANT

    tca_buyer_participant_id = fields.Char(
        string='Buyer Participant ID',
        copy=True,
        compute='_compute_tca_buyer_participant_id',
        store=True,
        readonly=False,
        help=(
            'Peppol Participant ID of the buyer.\n'
            'For UAE buyers: their Peppol Participant ID or full TRN.\n'
            'For non-UAE / out-of-scope cases, PINT AE BIS 1.5.3 mandates a predefined endpoint:\n'
            '  9900000097 — Deemed Supply\n'
            '  9900000098 — Buyer not subject to UAE e-invoicing\n'
            '  9900000099 — Export, receiver not registered in Peppol\n'
            'Auto-populated from the customer record + transaction flags; editable per invoice.'
        ),
    )

    @api.model
    def _tca_resolve_buyer_participant_id(self, partner, flags):
        """
        Apply BIS 1.5.3 routing to determine the Peppol Participant ID for a
        buyer. Pure function — does NOT mutate any record. Called by both the
        @api.depends compute below and the @api.onchange handler in this model,
        so the routing rules live in exactly one place.

        Routing precedence:
          1. Deemed Supply flag set (BTAE-02 pos 2)         → 9900000097
          2. UAE buyer                                      → peppol_endpoint (or '')
          3. Foreign buyer + Export flag set (BTAE-02 pos 8)→ 9900000099
          4. Foreign buyer (no special flag)                → 9900000098
          5. Partner without country                        → ''

        Args:
            partner:  res.partner record (typically the commercial_partner_id)
            flags:    8-char BTAE-02 binary string, e.g. '01000000' = Deemed Supply

        Returns:
            The resolved participant ID string. Never None.
        """
        flags = (flags or '00000000').ljust(8, '0')

        # (1) Deemed Supply — predefined endpoint regardless of buyer
        if flags[1] == '1':
            return self._PREDEFINED_DEEMED

        if not partner.country_id:
            return ''

        if partner.country_id.code == 'AE':
            return partner.peppol_endpoint or ''

        if flags[7] == '1':
            # (3) Export, receiver not registered in Peppol
            return self._PREDEFINED_EXPORT_NO_PEPPOL
        # (4) Foreign buyer not otherwise subject to UAE e-invoicing
        return self._PREDEFINED_NOT_SUBJECT

    @api.depends(
        'partner_id', 'partner_id.peppol_endpoint', 'partner_id.country_id',
        'tca_transaction_type_flags',
    )
    def _compute_tca_buyer_participant_id(self):
        """
        Auto-populate Buyer Participant ID per BIS 1.5.3. The compute
        re-evaluates when flags or partner change ONLY if the current value
        is a predefined/legacy endpoint (i.e. it was auto-set, not user-set).
        Custom values entered by the user are preserved.

        Actual routing logic lives in _tca_resolve_buyer_participant_id —
        shared with the @api.onchange so the rules cannot drift.
        """
        for move in self:
            if not move.partner_id:
                # Customer removed — nothing left to route to. Clears even a
                # manually-typed value, same as the other buyer-derived
                # fields below: with no buyer, none of this data still means
                # anything, and leaving it behind is misleading.
                move.tca_buyer_participant_id = ''
                continue
            current = (move.tca_buyer_participant_id or '').strip()
            # Preserve user-set values (anything not in the auto-set predefined set).
            if current and current not in self._ANON_BUYER_PIDS:
                continue
            partner = move.partner_id.commercial_partner_id
            move.tca_buyer_participant_id = self._tca_resolve_buyer_participant_id(
                partner, move.tca_transaction_type_flags,
            )

    # ── PINT AE XML fields ────────────────────────────────────────────────────

    # ── BTAE-02 transaction-type flag booleans (user-facing checkboxes) ──────
    # These 7 booleans represent positions 1-7 of the BTAE-02 ProfileExecutionID
    # binary string. Position 8 (Export) is auto-detected by the XML builder
    # from the buyer's country — never user-set.
    # tca_transaction_type_flags (below) is COMPUTED from these.

    tca_flag_free_trade_zone = fields.Boolean(
        string='Free Trade Zone', copy=True,
        help='Tick if the supply involves a UAE Free Trade Zone.',
    )
    tca_flag_deemed_supply = fields.Boolean(
        string='Deemed Supply', copy=True,
        help='Tick for deemed-supply scenarios (e.g. goods for own use). '
             'Buyer participant ID auto-switches to predefined endpoint 9900000097.',
    )
    tca_flag_margin_scheme = fields.Boolean(
        string='Margin Scheme', copy=True,
        help='Tick for second-hand goods / margin-scheme transactions.',
    )
    tca_flag_summary_invoice = fields.Boolean(
        string='Summary Invoice', copy=True,
        help='Tick for an invoice consolidating multiple supplies over a period. '
             'Requires Invoice Period Start/End.',
    )
    tca_flag_continuous_supply = fields.Boolean(
        string='Continuous Supply', copy=True,
        help='Tick for subscriptions / recurring supplies. '
             'Requires Invoice Period Start/End, Contract Reference, and Billing Frequency.',
    )
    tca_flag_disclosed_agent = fields.Boolean(
        string='Disclosed Agent Billing', copy=True,
        help='Tick when invoicing as a disclosed agent on behalf of a principal '
             'Requires Principal TRN.',
    )
    tca_flag_ecommerce = fields.Boolean(
        string='E-commerce', copy=True,
        help='Tick for online-channel transactions.',
    )
    tca_flag_export = fields.Boolean(
        string='Export', copy=True,
        help='Tick when this is an export supply. '
             'Manual flag — NOT auto-detected from the buyer\'s country: a '
             'foreign buyer alone does not make a supply an "export" (e.g. it '
             'may be out-of-scope / not-subject-to-VAT instead). Composes '
             'with the other special flags.',
    )

    # ── Section expand/collapse toggle ────────────────────────────────────────
    # Acts as the "expand" switch for the Transaction Type (Optional) group.
    # Auto-set to True whenever any flag above is on, so opening an existing
    # invoice with flags already configured shows them expanded.
    tca_show_special_flags = fields.Boolean(
        string='Use Special Type',
        compute='_compute_tca_show_special_flags',
        store=True,
        readonly=False,
        copy=True,
        help='Toggle on to reveal special transaction-type checkboxes. '
             'Leave off for standard tax invoices (the most common case).',
    )

    @api.depends(
        'tca_flag_free_trade_zone', 'tca_flag_deemed_supply',
        'tca_flag_margin_scheme', 'tca_flag_summary_invoice',
        'tca_flag_continuous_supply', 'tca_flag_disclosed_agent',
        'tca_flag_ecommerce', 'tca_flag_export',
    )
    def _compute_tca_show_special_flags(self):
        """Auto-expand the section whenever any flag is on. Preserves a
        manual True so the user can keep it open with no flags ticked yet,
        and preserves a manual False (the default) when no flag is on."""
        for move in self:
            if any((
                move.tca_flag_free_trade_zone,
                move.tca_flag_deemed_supply,
                move.tca_flag_margin_scheme,
                move.tca_flag_summary_invoice,
                move.tca_flag_continuous_supply,
                move.tca_flag_disclosed_agent,
                move.tca_flag_ecommerce,
                move.tca_flag_export,
            )):
                move.tca_show_special_flags = True
            elif not move.tca_show_special_flags:
                # No flags AND not explicitly toggled on by the user.
                move.tca_show_special_flags = False

    tca_transaction_type_flags = fields.Char(
        string='Transaction Type Flags',
        size=8,
        compute='_compute_tca_transaction_type_flags',
        store=True,
        copy=True,
        help=(
            'BTAE-02: 8-digit binary flag string for the PINT AE ProfileExecutionID. '
            'Composed automatically from the eight flag checkboxes above (positions 1-8). '
            '"00000000" = standard tax invoice.'
        ),
    )

    @api.depends(
        'tca_flag_free_trade_zone',
        'tca_flag_deemed_supply',
        'tca_flag_margin_scheme',
        'tca_flag_summary_invoice',
        'tca_flag_continuous_supply',
        'tca_flag_disclosed_agent',
        'tca_flag_ecommerce',
        'tca_flag_export',
    )
    def _compute_tca_transaction_type_flags(self):
        """Compose the 8-char BTAE-02 string from the 8 user-facing booleans.
        Export (position 8) is a manual flag — NOT auto-detected from the
        buyer's country (a foreign buyer alone doesn't make a supply an
        export; that wrongly blocked legitimate out-of-scope/not-subject
        cases in the past)."""
        for move in self:
            move.tca_transaction_type_flags = ''.join((
                '1' if move.tca_flag_free_trade_zone else '0',
                '1' if move.tca_flag_deemed_supply else '0',
                '1' if move.tca_flag_margin_scheme else '0',
                '1' if move.tca_flag_summary_invoice else '0',
                '1' if move.tca_flag_continuous_supply else '0',
                '1' if move.tca_flag_disclosed_agent else '0',
                '1' if move.tca_flag_ecommerce else '0',
                '1' if move.tca_flag_export else '0',
            ))

    tca_payment_means_ids = fields.One2many(
        'tca.payment.means', 'move_id',
        string='Payment Means (IBT-081)',
        copy=True,
        default=lambda self: [(0, 0, {'tca_payment_means_code': '1'})],
        help=(
            'IBT-081 / IBG-16: one or more payment means for this invoice '
            '(e.g. part cash, part credit card) — each line renders as its '
            'own cac:PaymentMeans element. Required per ibr-191-ae except on '
            'credit notes, where PaymentMeans is not emitted at all.'
        ),
    )
    tca_credit_note_reason = fields.Selection(
        selection=CREDIT_NOTE_REASONS,
        string='Credit Note Reason (BTAE-03)',
        copy=False,
        help=(
            'BTAE-03: Mandatory reason code for all UAE credit notes (IBR-158-AE Fatal rule).\n'
            'Must be set before generating PINT AE XML for any credit note.\n'
            '"VD" (Volume Discount) is the only reason that does NOT require a '
            'preceding invoice reference (IBG-03).'
        ),
    )

    # ── Derived booleans for view visibility (kept for backward compat) ──────
    # These now delegate to the new user-facing flag booleans. View conditions
    # can use either the derived ones or the new flag fields directly.
    tca_is_agent_billing = fields.Boolean(
        compute='_compute_tca_derived_flag_booleans', string='Is Agent Billing',
    )
    tca_is_summary_or_continuous = fields.Boolean(
        compute='_compute_tca_derived_flag_booleans',
    )
    tca_is_continuous = fields.Boolean(
        compute='_compute_tca_derived_flag_booleans',
    )
    # tca_is_export now derives from buyer country (export auto-detected)
    tca_is_export = fields.Boolean(compute='_compute_tca_is_export')
    tca_buyer_is_uae = fields.Boolean(compute='_compute_tca_buyer_is_uae')

    @api.depends(
        'tca_flag_disclosed_agent', 'tca_flag_summary_invoice',
        'tca_flag_continuous_supply',
    )
    def _compute_tca_derived_flag_booleans(self):
        for move in self:
            move.tca_is_agent_billing = move.tca_flag_disclosed_agent
            move.tca_is_continuous = move.tca_flag_continuous_supply
            move.tca_is_summary_or_continuous = (
                move.tca_flag_summary_invoice or move.tca_flag_continuous_supply
            )

    @api.depends('tca_flag_export')
    def _compute_tca_is_export(self):
        """Export is the manual tca_flag_export toggle, not the buyer's
        country — a foreign buyer alone doesn't make a supply an export (it
        may be out-of-scope / not-subject-to-VAT instead). Drives visibility
        of the Export Declaration Number field."""
        for move in self:
            move.tca_is_export = move.tca_flag_export

    @api.depends('partner_id', 'partner_id.commercial_partner_id.country_id')
    def _compute_tca_buyer_is_uae(self):
        for move in self:
            partner = move.partner_id.commercial_partner_id
            move.tca_buyer_is_uae = bool(partner.country_id and partner.country_id.code == 'AE')

    tca_principal_id = fields.Char(
        string='Principal TRN (BTAE-14)',
        copy=True,
        help=(
            'Tax Registration Number of the Principal in a Disclosed Agent Billing '
            'arrangement (UC5 / UC13).\n'
            'Mandatory when Disclosed Agent flag set.\n'
            'Carried over to credit notes — same principal usually applies.'
        ),
    )
    # ── Invoice Type Code (6 PINT AE variants) ───────────────────────────────
    # 389/261 = self-billing (buyer issues on behalf of supplier). The XML's
    # actual cbc:InvoiceTypeCode/cbc:CreditNoteTypeCode element still carries
    # the bare UNCL1001 code (380/381) — see tca_uncl1001_code, computed via
    # _SELF_BILLING_TO_UNCL1001 below. Self-billing is distinguished only via
    # CustomizationID/ProfileID (see PINT_AE_SELFBILLING_* in
    # account_edi_xml_pint_ae.py), not the InvoiceTypeCode value itself.

    _TYPE_INVOICE_TO_REFUND = {'380': '381', '389': '261', '480': '81'}
    _TYPE_REFUND_TO_INVOICE = {'381': '380', '261': '389', '81': '480'}

    tca_invoice_type_code = fields.Selection(
        selection=[
            ('380', '380 — Tax Invoice'),
            ('381', '381 — Tax Credit Note'),
            ('389', '389 - Self-Billing Tax Invoice'),
            ('261', '261 - Self-Billing Tax Credit Note'),
            ('480', '480 — Out-of-Scope Invoice'),
            ('81', '81 — Out-of-Scope Credit Note'),
        ],
        string='Invoice Type Code',
        compute='_compute_tca_invoice_type_code',
        store=True,
        readonly=False,
        copy=True,
        help=(
            'PINT AE invoice type code.\n'
            '380: Tax Invoice — standard sale with UAE VAT\n'
            '381: Tax Credit Note — reverses a 380\n'
            '389: Self-Billing Tax Invoice: buyer issues 380 on behalf of supplier (UC4)\n'
            '261: Self-Billing Tax Credit Note: buyer issues 381 on behalf of supplier (UC5)\n'
            '480: Out-of-Scope Invoice — not subject to UAE VAT\n'
            '81: Out-of-Scope Credit Note — reverses a 480\n'
            'Self-billing variants emit the standard 380/381 UNCL1001 code with '
            'the urn:peppol:pint:selfbilling-1@ae-1 customization.'
        ),
    )

    tca_uncl1001_code = fields.Char(
        compute='_compute_tca_uncl1001_code',
        store=False,
        string='UNCL1001 Code',
        help='Actual UNCL1001 document type code emitted in the XML (380/381/480/81). '
             'Strips the _sb suffix from self-billing variants.',
    )

    # ── User-facing OOS toggle ────────────────────────────────────────────────
    # Drives _compute_tca_invoice_type_code: when ticked, the resolved code
    # flips to 480 (Commercial Invoice) for invoices or 81 (OOS Credit Note)
    # for refunds. Default off — most invoices are Tax Invoices subject to
    # UAE VAT. Shown on the invoice form; hidden / readonly for inbound moves
    # (the OOS classification of a received document is fixed by the seller).
    tca_is_out_of_scope = fields.Boolean(
        string='Out of Scope (Commercial Invoice)',
        # No default= here — it would be injected into create() vals ahead
        # of computation (same mechanism documented on the create() override
        # below for tca_invoice_type_code), silently skipping the compute
        # and breaking the OOS-mirrors-onto-reversal behavior. The compute's
        # own else-branch already defaults to False when there's no reversal.
        compute='_compute_tca_is_out_of_scope',
        store=True,
        readonly=False,
        recursive=True,
        copy=True,
        help='Tick to issue a Commercial Invoice — a document NOT subject to '
             'UAE VAT '
             'Examples: financial services, supplies outside the UAE VAT scope, '
             'transactions with non-residents. Leave unticked for standard Tax '
             'Invoices (codes 380 / 381).',
    )

    @api.depends('reversed_entry_id', 'reversed_entry_id.tca_is_out_of_scope')
    def _compute_tca_is_out_of_scope(self):
        """A credit note always mirrors the OOS classification of the
        invoice it reverses — 381 must reverse a 380, 81 must reverse a 480,
        never mixed. Only auto-set for actual reversals; everything else
        (including a manual edit on a non-reversal) is left to the user's
        existing value (readonly=False compute preserves explicit writes)."""
        for move in self:
            if move.reversed_entry_id:
                move.tca_is_out_of_scope = move.reversed_entry_id.tca_is_out_of_scope
            elif not move.tca_is_out_of_scope:
                move.tca_is_out_of_scope = False

    @api.onchange('tca_is_out_of_scope')
    def _onchange_tca_is_out_of_scope(self):
        """
        When the user ticks "Out of Scope":

          (1) Clear forbidden BTAE-02 flags. PINT AE ibr-157-ae: Out-of-Scope
              documents cannot also be Deemed Supply / Margin Scheme / Summary
              Invoice. The form hides those checkboxes when OOS is on, but the
              user might have ticked one BEFORE turning OOS on.

          (2) Strip forbidden line taxes. An OOS invoice is outside the UAE
              VAT regime, so it cannot carry standard-rate (S) or
              reverse-charge (AE) taxes — these would force Odoo to post VAT
              entries the law says don't exist. We strip any tax that is
              explicitly S/AE OR has a non-zero percent rate (catches
              uncategorized 5% taxes).

          (3) Auto-apply an Out-of-Scope tax on lines left without taxes.
              PINT AE rule ibr-sr-58 makes line tax category MANDATORY; for
              OOS documents the required value is 'O'. Search the company
              for a tax with tca_tax_category='O' and 0% rate matching the
              move's direction (sale / purchase); apply it to product lines
              that have no tax. If no OOS tax is configured, surface a clear
              warning telling the user to set one up.

          A single warning dialog summarizes all three actions.
        """
        if not self.tca_is_out_of_scope:
            return

        # ── (1) Forbidden BTAE-02 flags ──────────────────────────────────────
        self.tca_flag_deemed_supply = False
        self.tca_flag_margin_scheme = False
        self.tca_flag_summary_invoice = False

        # ── (2) Strip forbidden taxes ────────────────────────────────────────
        def _is_forbidden_for_oos(tax):
            cat = (getattr(tax, 'tca_tax_category', '') or '')
            if cat in ('S', 'AE'):
                return True
            return tax.amount_type == 'percent' and tax.amount != 0.0

        removed_per_line = []
        for line in self.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
            forbidden = line.tax_ids.filtered(_is_forbidden_for_oos)
            if not forbidden:
                continue
            label = line.name or (line.product_id and line.product_id.name) or _('(unnamed line)')
            removed_per_line.append(
                '%s — %s' % (label, ', '.join(forbidden.mapped('name')))
            )
            line.tax_ids = line.tax_ids - forbidden

        # ── (3) Auto-apply OOS tax to lines without any tax remaining ───────
        # Lines that still carry a (now zero-rated / exempt) tax are left
        # alone — the user's existing setup is assumed valid. The OOS tax is
        # auto-created in the company's chart if missing.
        type_tax_use = 'sale' if self.move_type in ('out_invoice', 'out_refund') else 'purchase'
        oos_tax = self.env['account.tax']._tca_ensure_oos_tax(self.company_id, type_tax_use)

        applied_to_lines = []
        for line in self.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
            if line.tax_ids:
                continue  # Has a tax already (must be zero-rated/exempt after strip) — keep
            line.tax_ids = oos_tax
            label = line.name or (line.product_id and line.product_id.name) or _('(unnamed line)')
            applied_to_lines.append(label)

        # ── Assemble single combined warning if any of the three acted ──────
        sections = []
        if removed_per_line:
            sections.append(_(
                'Removed taxes (UAE FTA: Out-of-Scope invoices cannot carry VAT):\n%s',
                '\n'.join(f'  • {r}' for r in removed_per_line),
            ))
        if applied_to_lines:
            sections.append(_(
                'Auto-applied "%(name)s" (Out-of-Scope, 0%% rate, category O) to '
                'satisfy PINT AE rule ibr-sr-58 (line tax category is mandatory):\n%(list)s',
                name=oos_tax.name,
                list='\n'.join(f'  • {label}' for label in applied_to_lines),
            ))

        if sections:
            return {
                'warning': {
                    'title': _('Tax adjustments for Out-of-Scope invoice'),
                    'message': '\n\n'.join(sections),
                }
            }

    # Computed booleans for view visibility (Odoo 17 cannot do slice/in on Selection in invisible)
    tca_show_credit_note_fields = fields.Boolean(
        compute='_compute_tca_type_visibility', store=False,
    )
    tca_is_out_of_scope_type = fields.Boolean(
        compute='_compute_tca_type_visibility', store=False,
    )

    @api.depends('move_type', 'tca_is_out_of_scope')
    def _compute_tca_invoice_type_code(self):
        """
        Resolve the PINT AE document type code from the move's direction plus
        the user-facing OOS toggle:

            (out_invoice / in_invoice, not OOS) → '380'  Tax Invoice
            (out_invoice / in_invoice,     OOS) → '480'  Commercial Invoice (OOS)
            (out_refund  / in_refund,  not OOS) → '381'  Tax Credit Note
            (out_refund  / in_refund,      OOS) → '81'   OOS Credit Note

        Self-billing variants ('380_sb', '381_sb') aren't user-exposed and are
        preserved if already set (set via dev mode / data import / future UI).

        Inbound moves are not touched — their type code is set by the XML
        importer from the actual `<cbc:InvoiceTypeCode>` / `<CreditNoteTypeCode>`
        carried in the received document, and the OOS classification is a
        property of the seller's invoice, not something the buyer can flip.
        """
        for move in self:
            # Inbound: importer is the source of truth — don't recompute.
            if move.tca_is_inbound:
                continue
            # Preserve self-billing variants (no _sb checkbox UI yet).
            if move.tca_invoice_type_code in ('389', '261'):
                continue

            if move.move_type in ('out_invoice', 'in_invoice'):
                move.tca_invoice_type_code = '480' if move.tca_is_out_of_scope else '380'
            elif move.move_type in ('out_refund', 'in_refund'):
                move.tca_invoice_type_code = '81' if move.tca_is_out_of_scope else '381'
            else:
                move.tca_invoice_type_code = False

    # Self-billing type codes map to their bare UNCL1001 equivalent — the
    # XML/JSON still emits 380/381, only the CustomizationID differs (see
    # tca_invoice_type_code's help text).
    _SELF_BILLING_TO_UNCL1001 = {'389': '380', '261': '381'}

    @api.depends('tca_invoice_type_code')
    def _compute_tca_uncl1001_code(self):
        for move in self:
            code = move.tca_invoice_type_code or ''
            move.tca_uncl1001_code = self._SELF_BILLING_TO_UNCL1001.get(code, code or False)

    @api.depends('tca_invoice_type_code')
    def _compute_tca_type_visibility(self):
        for move in self:
            code = move.tca_invoice_type_code or ''
            move.tca_show_credit_note_fields = code in ('381', '261', '81')
            move.tca_is_out_of_scope_type = code in ('480', '81')

    @api.onchange('tca_invoice_type_code')
    def _onchange_tca_invoice_type_code(self):
        """Prevent mismatched type codes (e.g. credit note code on an invoice)."""
        code = self.tca_invoice_type_code
        if not code or not self.move_type:
            return
        is_refund = self.move_type in ('out_refund', 'in_refund')
        if is_refund and code in self._TYPE_INVOICE_TO_REFUND:
            self.tca_invoice_type_code = self._TYPE_INVOICE_TO_REFUND[code]
            return {'warning': {
                'title': _('Invalid Type Code'),
                'message': _('Credit notes cannot use an invoice type code. Reset to %s.', self.tca_invoice_type_code),
            }}
        if not is_refund and code in self._TYPE_REFUND_TO_INVOICE:
            self.tca_invoice_type_code = self._TYPE_REFUND_TO_INVOICE[code]
            return {'warning': {
                'title': _('Invalid Type Code'),
                'message': _('Invoices cannot use a credit note type code. Reset to %s.', self.tca_invoice_type_code),
            }}

    @api.depends('company_id', 'tca_create_einvoice', 'move_type')
    def _compute_currency_id(self):
        """
        EXTENDS account.move.
        PINT AE mandates AED-denominated invoices. Force the currency to AED
        on draft outbound (sale) documents for a TCA-active company with
        e-invoicing enabled. Only touches drafts — never overrides the
        currency of an already-posted move.
        """
        super()._compute_currency_id()
        aed = self.env.ref('base.AED', raise_if_not_found=False)
        if not aed:
            return
        for move in self:
            if (
                move.state == 'draft'
                and move.company_id.tca_is_active
                and move.tca_create_einvoice
                and move.is_sale_document()
            ):
                move.currency_id = aed

    @api.model_create_multi
    def create(self, vals_list):
        """
        EXTENDS account.move.
        Defensive flip: when the create vals carry both move_type and an
        opposite-pair tca_invoice_type_code (e.g. Odoo's reversal wizard copies
        '380' from the original invoice into a new out_refund), auto-flip the
        type code so it matches the move_type direction.

        The @api.depends('move_type') compute can't handle this on its own:
        Odoo preserves explicit values for readonly=False compute-stored fields
        during create — the recompute is skipped when the field is supplied
        directly in the create vals.
        """
        aed = self.env.ref('base.AED', raise_if_not_found=False)
        for vals in vals_list:
            move_type = vals.get('move_type')
            type_code = vals.get('tca_invoice_type_code')
            if move_type and type_code:
                is_refund = move_type in ('out_refund', 'in_refund')
                if is_refund and type_code in self._TYPE_INVOICE_TO_REFUND:
                    vals['tca_invoice_type_code'] = self._TYPE_INVOICE_TO_REFUND[type_code]
                elif not is_refund and type_code in self._TYPE_REFUND_TO_INVOICE:
                    vals['tca_invoice_type_code'] = self._TYPE_REFUND_TO_INVOICE[type_code]

            # AED currency lock: same "explicit value bypasses the compute"
            # issue as tca_invoice_type_code above — when currency_id is
            # explicitly present in vals (as it usually is from the web
            # client, which sends the onchange-populated value), the
            # _compute_currency_id override never fires for this record.
            if aed and 'currency_id' in vals and move_type in ('out_invoice', 'out_refund'):
                company_id = vals.get('company_id') or self.env.company.id
                company = self.env['res.company'].browse(company_id)
                create_einvoice = vals.get('tca_create_einvoice', True)
                if company.tca_is_active and create_einvoice:
                    vals['currency_id'] = aed.id
        return super().create(vals_list)

    tca_is_self_billing = fields.Boolean(
        string='Self-Billing (UC4/UC5)',
        compute='_compute_tca_is_self_billing',
        store=True,
        copy=True,
        help=(
            'True when invoice type is a self-billing variant — '
            'buyer issues the invoice on behalf of the supplier. '
            'Derived from tca_invoice_type_code (the _sb variants).\n'
            'Sets CustomizationID to selfbilling variant and ProfileID to selfbilling in PINT AE XML.'
        ),
    )

    @api.depends('tca_invoice_type_code')
    def _compute_tca_is_self_billing(self):
        # Stored codes are bare '389'/'261' (no _sb suffix — see
        # tca_invoice_type_code's help text and _SELF_BILLING_TO_UNCL1001).
        for move in self:
            move.tca_is_self_billing = (move.tca_invoice_type_code or '') in ('389', '261')
    tca_contract_value = fields.Char(
        string='Contract Value (BTAE-05)',
        copy=True,
        help=(
            'BTAE-05: Contract value description for ContractDocumentReference/DocumentDescription.\n'
            'Example: "AED 1000000". Used in Continuous Supply (UC11) invoices.'
        ),
    )
    tca_billing_frequency = fields.Selection(
        selection=[
            ('DLY', 'Daily'),
            ('WKY', 'Weekly'),
            ('Q15', 'Once in 15 days'),
            ('MTH', 'Monthly'),
            ('Q45', 'Once in 45 days'),
            ('Q60', 'Once in 60 days'),
            ('QTR', 'Quarterly'),
            ('YRL', 'Yearly'),
            ('HYR', 'Half-Yearly'),
            ('OTH', 'Others'),
        ],
        string='Billing Frequency',
        copy=True,
        help=(
            'BTAE-06: Frequency of billing for Continuous Supply (UC11) invoices.\n'
            'Rendered as InvoicePeriod/Description. When "OTH", an Invoice Note is required.\n'
            'Carried over to credit notes for Continuous Supply.'
        ),
    )
    tca_export_declaration_number = fields.Char(
        string='Export Declaration Number',
        copy=True,
        help=(
            'Export declaration number for Exports.\n'
            'Rendered as StatementDocumentReference/ID.\n'
            'Carried over to export credit notes — same declaration usually applies.'
        ),
    )
    tca_incoterms = fields.Char(
        string='Incoterms',
        size=3,
        copy=True,
        help=(
            'Incoterms code for Exports.\n'
            'Rendered as Delivery/DeliveryTerms/ID with schemeID="Incoterms".\n'
            'Example: CIF, FOB, EXW.'
        ),
    )

    # ── Missing PINT AE fields (document level) ─────────────────────────────

    tca_buyer_reference = fields.Char(
        string='Buyer Reference (IBT-010)',
        copy=True,
        help='IBT-010: A reference assigned by the buyer (e.g. purchase order number). '
             'Carried over to credit notes — the same PO usually applies.',
    )
    tca_project_reference = fields.Char(
        string='Project Reference (IBT-011)',
        copy=True,
        help='IBT-011: Identifier of the project the invoice relates to.',
    )
    tca_contract_reference = fields.Char(
        string='Contract Reference (IBT-012)',
        copy=True,
        help='IBT-012: Identifier of the contract the invoice relates to.',
    )
    tca_buyer_accounting_ref = fields.Char(
        string='Buyer Accounting Ref (IBT-019)',
        copy=True,
        help='IBT-019: A reference used by the buyer for internal accounting routing.',
    )
    tca_tax_point_date = fields.Date(
        string='Tax Point Date (IBT-007)',
        copy=False,
        help='IBT-007: Date when VAT becomes applicable (if different from invoice date).',
    )
    tca_invoice_period_start = fields.Date(
        string='Invoice Period Start (IBT-073)',
        copy=False,
        help='IBT-073: Start date of the invoicing period. Required for Summary/Continuous invoices.',
    )
    tca_invoice_period_end = fields.Date(
        string='Invoice Period End (IBT-074)',
        copy=False,
        help='IBT-074: End date of the invoicing period. Required for Summary/Continuous invoices.',
    )
    tca_delivery_date = fields.Date(
        string='Delivery Date (IBT-072)',
        copy=False,
        help='IBT-072: Actual delivery date of goods or services.',
    )
    tca_delivery_party_trn = fields.Char(
        string='Deliver-to Party TRN (BTAE-23)',
        copy=True,
        help='BTAE-23: TRN/TIN of the delivery recipient (for triangular sales).',
    )
    tca_delivery_street = fields.Char(
        string='Delivery Street',
        copy=False,
        help='ibr-142-ae: delivery address street, mandatory when E-commerce '
             '(UC9) is ticked. Falls back to the shipping partner\'s address '
             'when left blank.',
    )
    tca_delivery_city = fields.Char(
        string='Delivery City',
        copy=False,
        help='ibr-142-ae: delivery address city, mandatory when E-commerce '
             '(UC9) is ticked. Falls back to the shipping partner\'s address '
             'when left blank.',
    )
    tca_delivery_state_id = fields.Many2one(
        'res.country.state',
        string='Delivery Emirate',
        copy=False,
        help='ibr-142-ae/ibr-128-ae: delivery address emirate. Falls back to '
             'the shipping partner\'s emirate when left blank.',
    )
    tca_buyer_beneficiary_id = fields.Char(
        string='FTZ Beneficiary ID (BTAE-01)',
        copy=True,
        help='BTAE-01/ibr-007-ae: Free Trade Zone beneficiary identifier — '
             'mandatory when the Free Trade Zone (UC8) flag is ticked.',
    )

    # ── Buyer Emirate (per invoice override) ──────────────────────────────────

    tca_buyer_emirate = fields.Selection(
        selection=[(e, e) for e in _UAE_EMIRATES],
        string='Buyer Emirate',
        compute='_compute_tca_buyer_emirate',
        store=True,
        readonly=False,
        copy=False,
        help=(
            'UAE emirate of the buyer (CountrySubentity, ibr-128-ae). '
            'Auto-fills from the customer record when set; editable per invoice. '
            'Mandatory when the buyer country is UAE.'
        ),
    )

    # ── Format constraints ────────────────────────────────────────────────────

    _RE_TRN_15 = re.compile(r'^\d{15}$')
    _RE_FLAGS_8 = re.compile(r'^[01]{8}$')
    # Backward-compat alias for any external code that still references this name.
    _NON_UAE_PARTICIPANT_PLACEHOLDER = _LEGACY_PLACEHOLDER_PARTICIPANT

    @api.constrains('tca_buyer_participant_id', 'partner_id')
    def _check_tca_buyer_participant_id_format(self):
        # UAE Peppol Participant ID — strictly 10 digits starting with "1".
        # The 15-digit TRN is a separate identifier (PartyTaxScheme/CompanyID),
        # not a Peppol endpoint.
        # The 3 PINT AE predefined endpoints (BIS 1.5.3, 9900000097/98/99) start
        # with "99" so they don't match the regex — they bypass this check via
        # the _ANON_BUYER_PIDS set.
        re_uae_format = re.compile(r'^1[0-9]{9}$')
        for move in self:
            pid = (move.tca_buyer_participant_id or '').strip()
            if not pid or pid in self._ANON_BUYER_PIDS:
                continue
            partner = move.partner_id.commercial_partner_id
            # Only enforce UAE format when buyer is in UAE
            if not (partner.country_id and partner.country_id.code == 'AE'):
                continue
            if not re_uae_format.match(pid):
                raise ValidationError(_(
                    '"Buyer Participant ID" for UAE customers must be either:\n'
                    '  • 10-digit Peppol Participant ID: starts with 1 (e.g. 1234567890), or\n'
                    '  • One of the PINT AE predefined endpoints (9900000097/98/99).\n'
                    'The 15-digit TRN goes in the customer\'s "Tax ID" field, not here.\n'
                    'Current: "%s".',
                    pid,
                ))

    @api.constrains('tca_transaction_type_flags')
    def _check_tca_transaction_type_flags_format(self):
        for move in self:
            flags = (move.tca_transaction_type_flags or '').strip()
            if not flags:
                continue  # required-check handled at posting
            if not self._RE_FLAGS_8.match(flags):
                raise ValidationError(_(
                    '"Transaction Type Flags" must be exactly 8 digits, each 0 or 1. '
                    'Example: "00000000" for standard, "00000001" for export. Current: "%s".',
                    flags,
                ))

    @api.constrains('tca_principal_id')
    def _check_tca_principal_id_format(self):
        for move in self:
            pid = (move.tca_principal_id or '').strip()
            if not pid:
                continue
            if not self._RE_TRN_15.match(pid):
                raise ValidationError(_(
                    '"Principal TRN" must be exactly 15 digits. Current: "%s".', pid,
                ))

    @api.constrains('tca_delivery_party_trn')
    def _check_tca_delivery_party_trn_format(self):
        for move in self:
            trn = (move.tca_delivery_party_trn or '').strip()
            if not trn:
                continue
            if not self._RE_TRN_15.match(trn):
                raise ValidationError(_(
                    '"Deliver-to Party TRN" must be exactly 15 digits. Current: "%s".', trn,
                ))

    @api.depends('partner_id', 'partner_id.tca_emirate', 'partner_id.state_id')
    def _compute_tca_buyer_emirate(self):
        for move in self:
            if not move.partner_id:
                move.tca_buyer_emirate = False  # customer removed — clear
                continue
            if move.tca_buyer_emirate:
                continue  # user-set or previously computed — preserve
            partner = move.partner_id.commercial_partner_id
            emirate = (
                getattr(partner, 'tca_emirate', '')
                or (partner.state_id and partner.state_id.code)
                or ''
            )
            if emirate in _UAE_EMIRATES:
                move.tca_buyer_emirate = emirate

    # ── Buyer Legal Registration (per invoice override) ──────────────────────

    tca_buyer_legal_id_type = fields.Selection(
        selection=[
            ('TL', 'Trade License (Commercial)'),
            ('EID', 'Emirates ID'),
            ('PAS', 'Passport'),
            ('CD', 'Cabinet Decision'),
        ],
        string='Buyer Legal ID Type (BTAE-16)',
        compute='_compute_tca_buyer_legal_fields',
        store=True,
        readonly=False,
        copy=False,
        help=(
            'Type of buyer legal registration document. '
            'Auto-fills from the customer record; editable per invoice. '
            'TL=Trade License, EID=Emirates ID, PAS=Passport, CD=Cabinet Decision.'
        ),
    )
    tca_buyer_trade_license = fields.Char(
        string='Buyer Trade License / Reg. ID (IBT-047)',
        compute='_compute_tca_buyer_legal_fields',
        store=True,
        readonly=False,
        copy=False,
        help=(
            'Buyer legal registration identifier (Trade License / Emirates ID / Passport / CD ref). '
            'Auto-fills from the customer record; editable per invoice.'
        ),
    )
    tca_buyer_legal_authority = fields.Char(
        string='Buyer Issuing Authority (BTAE-11)',
        compute='_compute_tca_buyer_legal_fields',
        store=True,
        readonly=False,
        copy=False,
        help=(
            'Issuing authority for the buyer Trade License (e.g. "DED - Dubai"). '
            'Mandatory when Buyer Legal ID Type is TL. Auto-fills from customer record.'
        ),
    )
    tca_buyer_passport_country_id = fields.Many2one(
        'res.country',
        string='Buyer Passport Country (BTAE-19)',
        compute='_compute_tca_buyer_legal_fields',
        store=True,
        readonly=False,
        copy=False,
        help=(
            'Country that issued the buyer passport. '
            'Mandatory when Buyer Legal ID Type is PAS. Auto-fills from customer record.'
        ),
    )

    @api.depends(
        'partner_id',
        'partner_id.tca_legal_id_type',
        'partner_id.tca_trade_license',
        'partner_id.tca_legal_authority',
        'partner_id.tca_passport_country_id',
        'partner_id.company_registry',
        'partner_id.vat',
    )
    def _compute_tca_buyer_legal_fields(self):
        for move in self:
            if not move.partner_id:
                # Customer removed — clear, same as the other buyer fields.
                move.tca_buyer_legal_id_type = False
                move.tca_buyer_trade_license = False
                move.tca_buyer_legal_authority = False
                move.tca_buyer_passport_country_id = False
                continue
            partner = move.partner_id.commercial_partner_id
            # Each field: don't overwrite if user already set on this invoice
            if not move.tca_buyer_legal_id_type and partner.tca_legal_id_type:
                move.tca_buyer_legal_id_type = partner.tca_legal_id_type
            if not move.tca_buyer_trade_license:
                move.tca_buyer_trade_license = (
                    partner.tca_trade_license
                    or partner.company_registry
                    or partner.vat
                    or False
                )
            if not move.tca_buyer_legal_authority and partner.tca_legal_authority:
                move.tca_buyer_legal_authority = partner.tca_legal_authority
            if not move.tca_buyer_passport_country_id and partner.tca_passport_country_id:
                move.tca_buyer_passport_country_id = partner.tca_passport_country_id

    # ──────────────────────────────────────────────────────────────────────────
    # ONCHANGE: ensure TCA buyer fields populate immediately when the partner
    # is set in the form — including the case where the partner was just
    # created via the Many2one "Save & Close" popup, where the @api.depends
    # compute chain occasionally fails to fire against fresh related data.
    # ──────────────────────────────────────────────────────────────────────────

    @api.onchange('partner_id')
    def _onchange_partner_id_tca(self):
        """
        Mirror the partner-derived compute logic as an explicit onchange.
        Runs in the form UI on every partner_id change, so values appear
        without needing the user to re-select the customer.

        Preserves user-edited values on the invoice (only fills empty fields)
        when a customer is set or changed — that part is unchanged. When the
        customer is removed entirely, the buyer fields are cleared instead
        of being left stale from whichever partner was previously selected.
        """
        if not self.partner_id:
            self.tca_buyer_participant_id = ''
            self.tca_buyer_emirate = False
            self.tca_buyer_legal_id_type = False
            self.tca_buyer_trade_license = False
            self.tca_buyer_legal_authority = False
            self.tca_buyer_passport_country_id = False
            return

        partner = self.partner_id.commercial_partner_id

        # ── Buyer Participant ID (BIS 1.5.3 routing) ──────────────────────────
        # Same routing as the compute — both delegate to the shared helper
        # so the rules live in one place and cannot drift apart.
        current_pid = (self.tca_buyer_participant_id or '').strip()
        if not current_pid or current_pid in self._ANON_BUYER_PIDS:
            resolved = self._tca_resolve_buyer_participant_id(
                partner, self.tca_transaction_type_flags,
            )
            # Preserve a previously-set non-empty value if the helper returns ''
            # (e.g. partner has no country yet — common during draft creation).
            if resolved or not current_pid:
                self.tca_buyer_participant_id = resolved

        # ── Buyer Emirate ─────────────────────────────────────────────────────
        if not self.tca_buyer_emirate:
            emirate = (
                getattr(partner, 'tca_emirate', '')
                or (partner.state_id and partner.state_id.code)
                or ''
            )
            if emirate in _UAE_EMIRATES:
                self.tca_buyer_emirate = emirate

        # ── Buyer Legal fields ────────────────────────────────────────────────
        if not self.tca_buyer_legal_id_type and partner.tca_legal_id_type:
            self.tca_buyer_legal_id_type = partner.tca_legal_id_type
        if not self.tca_buyer_trade_license:
            self.tca_buyer_trade_license = (
                partner.tca_trade_license
                or partner.company_registry
                or partner.vat
                or False
            )
        if not self.tca_buyer_legal_authority and partner.tca_legal_authority:
            self.tca_buyer_legal_authority = partner.tca_legal_authority
        if not self.tca_buyer_passport_country_id and partner.tca_passport_country_id:
            self.tca_buyer_passport_country_id = partner.tca_passport_country_id

    # ──────────────────────────────────────────────────────────────────────────
    # COMPUTED HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    def _tca_is_send_eligible(self):
        """
        Returns True if this invoice can be submitted (or resubmitted) to TCA.
        An invoice is eligible when:
          - the company has TCA integration active
          - the partner is configured with a PINT AE format (ubl_pint_ae)
          - the invoice is in 'posted' state
          - the move is outbound (not a vendor bill received via TCA)
        """
        self.ensure_one()
        return (
            self.company_id.tca_is_active
            and self.tca_create_einvoice
            and self.state == 'posted'
            and not self.tca_is_inbound
            and self.partner_id.commercial_partner_id.ubl_cii_format == 'ubl_pint_ae'
            and self.tca_move_state in ('not_sent', 'error', 'rejected')
        )

    # ──────────────────────────────────────────────────────────────────────────
    # CANCEL BLOCK
    # ──────────────────────────────────────────────────────────────────────────

    def button_cancel(self):
        """
        EXTENDS account.move.
        Block cancellation if any invoice is processing, delivered, or received.
        For non-blocked invoices, set tca_move_state to 'cancelled'.
        """
        blocked = self.filtered(lambda m: m.tca_move_state in _CANCEL_BLOCKED_STATES)
        if blocked:
            names = ', '.join(blocked[:5].mapped('name'))
            raise UserError(_(
                'Cannot cancel invoice(s) %s: they have already been submitted to the '
                'TCA Peppol network and cannot be retracted.\n\n'
                'To correct an error, issue a credit note instead.',
                names
            ))
        # Mark non-submitted invoices as cancelled in TCA state
        for move in self:
            if move.tca_move_state in ('not_sent', 'error', 'rejected', 'uploading', 'submitted', 'inbound_received'):
                move.tca_move_state = 'cancelled'
        return super().button_cancel()

    def button_draft(self):
        """
        EXTENDS account.move.
        Block reset-to-draft for in-flight/completed invoices.
        Reset TCA state to 'not_sent' for error/rejected invoices.
        """
        blocked = self.filtered(lambda m: m.tca_move_state in _CANCEL_BLOCKED_STATES)
        if blocked:
            names = ', '.join(blocked[:5].mapped('name'))
            raise UserError(_(
                'Cannot reset invoice(s) %s to draft: they have already been submitted to the '
                'TCA Peppol network.\n\n'
                'Issue a credit note to correct any errors.',
                names
            ))
        result = super().button_draft()
        # Reset TCA state so the invoice is eligible for re-submission after fixing
        for move in self:
            if move.tca_move_state in ('error', 'rejected', 'cancelled', 'not_sent'):
                move.tca_move_state = 'not_sent'
                move.tca_submission_error = False
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # PRE-POSTING VALIDATION — block posting for UAE data errors
    # ──────────────────────────────────────────────────────────────────────────

    def _tca_validate_mandatory_fields(self):
        """
        Standalone mandatory field validation for PINT AE.
        Returns a list of error message strings. Empty list = all OK.
        Does NOT depend on the XML builder pipeline — reads invoice fields directly.
        """
        self.ensure_one()
        errors = []
        invoice = self
        supplier = invoice.company_id.partner_id.commercial_partner_id
        customer = invoice.partner_id.commercial_partner_id

        # ── Document level ───────────────────────────────────────────────────
        if not invoice.tca_invoice_type_code:
            errors.append(
                '"Invoice Type Code" is required. '
                'Select the invoice type (e.g. 380) in the "Invoice & Buyer" section on the invoice form.'
            )

        if not invoice.invoice_date:
            errors.append('"Invoice Date" is required.')

        if not invoice.currency_id:
            errors.append('"Currency" is required.')
        elif invoice.currency_id.name != 'AED':
            errors.append(
                '[pint_ae_currency_aed] PINT AE requires AED-denominated invoices. '
                f'Current currency: "{invoice.currency_id.name}".'
            )

        type_code = invoice.tca_invoice_type_code or ''
        is_credit_note = type_code in ('381', '261', '81')

        # IBT-009: Payment Due Date — mandatory for ALL invoice types incl. credit notes
        if not invoice.invoice_date_due and not invoice.invoice_payment_term_id:
            errors.append('"Due Date" or "Payment Terms" is required.')

        # ── IBT-010: Buyer Reference ─────────────────────────────────────────
        # Optional per PINT AE BIS — no schematron rule enforces it.
        # The field remains on the form for buyer-PO traceability but is not
        # required for Confirm or for valid PINT AE XML.

        # ── Buyer Participant ID ─────────────────────────────────────────────
        if not invoice.tca_buyer_participant_id:
            errors.append(
                '"Buyer Participant ID" is required. '
                'Enter the buyer\'s Peppol Participant ID in the "Invoice & Buyer" section.'
            )

        # ── Transaction type flags ───────────────────────────────────────────
        flags = (invoice.tca_transaction_type_flags or '').strip()
        if not flags:
            errors.append(
                '"Transaction Type Flags" is required. '
                'Set it to "00000000" for standard invoices in the "Transaction Type" section.'
            )
        elif len(flags) != 8 or not all(c in '01' for c in flags):
            errors.append(
                f'"Transaction Type Flags" must be exactly 8 digits of 0 or 1. Current: "{flags}".'
            )

        # ── Credit note reason ───────────────────────────────────────────────
        if is_credit_note and not invoice.tca_credit_note_reason:
            errors.append(
                '"Credit Note Reason" is required for credit notes. '
                'Set it in the "Invoice & Buyer" section.'
            )

        # [pint_ae_cn_preceding] Credit notes must reference the original
        # invoice unless the reason is Volume Discount (VD — IBG-03 exempt).
        if is_credit_note and invoice.tca_credit_note_reason != 'VD' and not invoice.reversed_entry_id:
            errors.append(
                '[pint_ae_cn_preceding] This credit note has no "Reversal of" (preceding invoice) '
                'reference. PINT AE requires it unless the reason is "VD — Volume Discount".'
            )

        # [pint_ae_oos_flags] ibr-157-ae: OOS documents (480/81) cannot also
        # be Deemed Supply / Margin Scheme / Summary Invoice. The onchange on
        # tca_is_out_of_scope clears these proactively, but this is the hard
        # gate in case flags were set another way (e.g. import, direct write).
        if invoice.tca_is_out_of_scope and len(flags) == 8 and (
            flags[1] == '1' or flags[2] == '1' or flags[3] == '1'
        ):
            errors.append(
                '[pint_ae_oos_flags] Out-of-Scope documents cannot also be Deemed Supply, '
                'Margin Scheme, or Summary Invoice. Untick the conflicting flag(s).'
            )

        # ── Disclosed agent → Principal TRN ──────────────────────────────────
        if len(flags) == 8 and flags[5] == '1' and not invoice.tca_principal_id:
            errors.append(
                'Disclosed Agent flag is set — "Principal TRN" is required. '
                'Set it in the "Transaction Type" section.'
            )

        # [ibr-007-ae] Free Trade Zone → Beneficiary ID
        if len(flags) == 8 and flags[0] == '1' and not invoice.tca_buyer_beneficiary_id:
            errors.append(
                '[ibr-007-ae] Free Trade Zone flag is set — "FTZ Beneficiary ID" is required. '
                'Set it in the "Additional Details" section.'
            )

        # ── Summary / Continuous → Invoice Period ────────────────────────────
        if len(flags) == 8 and flags[3] == '1':
            if not getattr(invoice, 'tca_invoice_period_start', None) or not getattr(invoice, 'tca_invoice_period_end', None):
                errors.append(
                    'Summary Invoice flag is set — "Invoice Period Start" and "End" dates are required.'
                )
        if len(flags) == 8 and flags[4] == '1':
            if not getattr(invoice, 'tca_invoice_period_start', None) or not getattr(invoice, 'tca_invoice_period_end', None):
                errors.append(
                    'Continuous Supply flag is set — "Invoice Period Start" and "End" dates are required.'
                )
            if not invoice.tca_contract_reference:
                errors.append(
                    'Continuous Supply flag is set — "Contract Reference" is required.'
                )
            # [pint_ae_oth_note] Billing Frequency "Others" requires a note.
            if invoice.tca_billing_frequency == 'OTH' and not invoice.narration:
                errors.append(
                    '[pint_ae_oth_note] Billing Frequency is "Others" — an "Invoice Note" is required.'
                )

        # [pint_ae_ecommerce_delivery] / [pint_ae_export_delivery] ibr-142-ae /
        # ibr-152-ae: delivery address required when E-commerce or Export.
        if len(flags) == 8 and (flags[6] == '1' or flags[7] == '1'):
            ship = invoice.partner_shipping_id or invoice.partner_id
            has_delivery_addr = bool(
                (invoice.tca_delivery_street or (ship and ship.street))
                and (invoice.tca_delivery_city or (ship and ship.city))
            )
            if not has_delivery_addr:
                code = 'pint_ae_ecommerce_delivery' if flags[6] == '1' else 'pint_ae_export_delivery'
                errors.append(
                    f'[{code}] A delivery address is required for E-commerce/Export invoices. '
                    'Set "Delivery Street"/"Delivery City" in the "Additional Details" section, '
                    'or set a delivery address on the customer.'
                )

        # [pint_ae_payment_means] ibr-191-ae: Payment Means Code required
        # except on credit notes and Deemed Supply.
        if (
            not is_credit_note
            and not invoice.tca_flag_deemed_supply
            and not invoice.tca_payment_means_ids
        ):
            errors.append(
                '[pint_ae_payment_means] At least one "Payment Means" (IBT-081) line is '
                'required for this document (not a credit note, not Deemed Supply). '
                'Add one in the "Invoice & Buyer" section.'
            )

        # [pint_ae_card_account] ibr-066-ae: an invoice may carry at most ONE
        # CardAccount block — block confirm if 2+ payment-means lines both
        # have card details (PAN/holder name) filled in.
        card_detail_lines = invoice.tca_payment_means_ids.filtered(
            lambda pm: pm.tca_card_pan or pm.tca_card_holder_name
        )
        if len(card_detail_lines) > 1:
            errors.append(
                '[pint_ae_card_account] "Payment Means": only ONE line may carry card '
                'details (Card Number / Card Holder Name) — PINT AE allows at most one '
                'CardAccount per invoice (ibr-066-ae). Clear the card details on all but '
                'one line.'
            )

        # [pint_ae_payment_mandate] ibr-067-ae: an invoice may carry at most
        # ONE PaymentMandate block — block confirm if 2+ payment-means lines
        # both have mandate details (mandate ref/payer account) filled in.
        mandate_detail_lines = invoice.tca_payment_means_ids.filtered(
            lambda pm: pm.tca_mandate_id or pm.tca_payer_account_id
        )
        if len(mandate_detail_lines) > 1:
            errors.append(
                '[pint_ae_payment_mandate] "Payment Means": only ONE line may carry '
                'mandate details (Mandate Reference / Payer Account) — PINT AE allows '
                'at most one PaymentMandate per invoice (ibr-067-ae). Clear the mandate '
                'details on all but one line.'
            )

        # ── Seller (company) mandatory fields ────────────────────────────────
        if not supplier.name:
            errors.append('Your company name (IBT-027) is missing. Set it in Settings → Companies.')

        if not supplier.vat and getattr(supplier, 'peppol_eas', '') == '0235':
            errors.append('Your company\'s "Tax ID" (TRN, IBT-031) is missing. Set it in Settings → Companies.')

        if not supplier.street:
            errors.append('Your company\'s "Street" (IBT-035) address is missing.')

        if not supplier.city:
            errors.append('Your company\'s "City" (IBT-037) is missing.')

        if not supplier.country_id:
            errors.append('Your company\'s "Country" (IBT-040) is missing.')

        if supplier.country_id and supplier.country_id.code == 'AE':
            emirate = getattr(supplier, 'tca_emirate', '') or (supplier.state_id and supplier.state_id.code) or ''
            if emirate not in ('AUH', 'DXB', 'SHJ', 'UAQ', 'FUJ', 'AJM', 'RAK'):
                errors.append(
                    'Your company\'s "Emirate" must be set to one of: '
                    'AUH, DXB, SHJ, UAQ, FUJ, AJM, RAK.'
                )

        if not getattr(supplier, 'peppol_eas', None) or not getattr(supplier, 'peppol_endpoint', None):
            errors.append('Your company\'s "Peppol EAS" and "Peppol Endpoint" (IBT-034) are missing.')

        # ibr-134-ae: Seller TRN (IBT-031) required, except for OOS / certain CN types
        type_code = invoice.tca_invoice_type_code or ''
        is_oos = type_code in ('480', '81')
        if not is_oos and not supplier.vat:
            errors.append(
                '[ibr-134-ae] Your company\'s "Tax ID" (TRN, IBT-031) is required. '
                'Set it in Settings → Companies. (Required unless invoice type is Out-of-Scope.)'
            )

        # IBT-030: Seller legal registration ID
        seller_legal_reg = (
            getattr(supplier, 'tca_trade_license', None)
            or supplier.company_registry
            or supplier.vat
        )
        if not seller_legal_reg:
            errors.append(
                'Your company\'s "Trade License / Registration ID" (IBT-030) is missing. '
                'Set it on the company partner record → "E-Invoicing" tab.'
            )

        # ibr-181-ae: BTAE-15 Seller Legal ID Type required when EAS=0235 + legal reg ID provided
        if (
            getattr(supplier, 'peppol_eas', '') == '0235'
            and seller_legal_reg
            and not getattr(supplier, 'tca_legal_id_type', None)
        ):
            errors.append(
                '[ibr-181-ae] Your company\'s "Legal ID Type" (BTAE-15) is required. '
                'Set it to TL / EID / PAS / CD on the company partner record → "E-Invoicing" tab.'
            )

        # Seller authority required when type=TL
        if getattr(supplier, 'tca_legal_id_type', '') == 'TL' and not getattr(supplier, 'tca_legal_authority', None):
            errors.append(
                'Your company\'s "Issuing Authority" (BTAE-12) is required when Legal ID Type is Trade License. '
                'Set it on the company partner record → "E-Invoicing" tab.'
            )

        # Seller passport country required when type=PAS
        if getattr(supplier, 'tca_legal_id_type', '') == 'PAS' and not getattr(supplier, 'tca_passport_country_id', None):
            errors.append(
                'Your company\'s "Passport Issuing Country" (BTAE-18) is required when Legal ID Type is Passport. '
                'Set it on the company partner record → "E-Invoicing" tab.'
            )

        # ibr-141-ae: Tax point date must be strictly before invoice date
        if invoice.tca_tax_point_date and invoice.invoice_date:
            if invoice.tca_tax_point_date >= invoice.invoice_date:
                errors.append(
                    '[ibr-141-ae] "Tax Point Date" (IBT-007) must be strictly before "Invoice Date" (IBT-002). '
                    f'Tax point: {invoice.tca_tax_point_date}, Invoice date: {invoice.invoice_date}.'
                )

        # ── Buyer mandatory fields ───────────────────────────────────────────
        # Match official schematron scope: UAE-specific buyer checks fire only when
        # buyer is a UAE party AND buyer participant ID is not the placeholder
        # the buyer participant ID is one of the PINT AE predefined endpoints
        # (9900000097/98/99) or the legacy 1XXXXXXXXX placeholder.
        # Foreign-buyer (export) flow: minimal checks only.

        if not customer.name:
            errors.append('Customer name (IBT-044) is missing.')

        if not customer.country_id:
            errors.append(f'Customer "{customer.name}" is missing a "Country" (IBT-055).')

        # IBT-049: Buyer Peppol electronic address — always required for Peppol routing
        if not getattr(customer, 'peppol_eas', None) or not getattr(customer, 'peppol_endpoint', None):
            errors.append(
                f'Customer "{customer.name}" is missing "Peppol EAS" and/or "Peppol Endpoint" (IBT-049). '
                'Open the customer record → "Accounting" tab.'
            )

        # ── Branch: UAE buyer vs foreign buyer ────────────────────────────────
        buyer_is_uae = customer.country_id and customer.country_id.code == 'AE'
        buyer_pid = (invoice.tca_buyer_participant_id or '').strip()
        buyer_is_anonymous = buyer_pid in self._ANON_BUYER_PIDS

        if buyer_is_uae and not buyer_is_anonymous:
            # Strict UAE buyer checks — match ibr-149-ae, ibr-128-ae, ibr-143-ae

            # IBT-048: Buyer VAT identifier (TRN)
            if not customer.vat and getattr(customer, 'peppol_eas', '') == '0235':
                errors.append(
                    f'Customer "{customer.name}" is missing "Tax ID" (TRN, IBT-048). '
                    'Set it on the customer record.'
                )

            # IBT-050: Buyer street (ibr-143/144-ae for AE party)
            if not customer.street:
                errors.append(f'Customer "{customer.name}" is missing "Street" (IBT-050).')

            # IBT-052: Buyer city
            if not customer.city:
                errors.append(f'Customer "{customer.name}" is missing "City" (IBT-052).')

            # ibr-128-ae: Buyer Emirate when country=AE
            if invoice.tca_buyer_emirate not in _UAE_EMIRATES:
                errors.append(
                    '"Buyer Emirate" is required for UAE customers. '
                    'Set it in the "Invoice & Buyer" section '
                    '(AUH/DXB/SHJ/UAQ/FUJ/AJM/RAK), or set it once on the customer record.'
                )

            # ibr-149-ae: Buyer legal reg ID (IBT-047) when EAS=0235 + endpoint != placeholder
            if not invoice.tca_buyer_trade_license:
                errors.append(
                    '"Buyer Trade License / Reg. ID" (IBT-047) is required. '
                    'Set it in the "Buyer Legal" section, '
                    'or set it once on the customer record.'
                )

            # BTAE-16: Buyer legal ID type
            if not invoice.tca_buyer_legal_id_type:
                errors.append(
                    '"Buyer Legal ID Type" (BTAE-16) is required. '
                    'Set it to TL / EID / PAS / CD in the "Buyer Legal" section.'
                )

            # ibr-101-ae: Buyer authority required when type=TL
            if invoice.tca_buyer_legal_id_type == 'TL' and not invoice.tca_buyer_legal_authority:
                errors.append(
                    '"Buyer Issuing Authority" (BTAE-11) is required when Legal ID Type is Trade License. '
                    'Set it in the "Buyer Legal" section.'
                )

            # ibr-010-ae: Buyer passport country required when type=PAS
            if invoice.tca_buyer_legal_id_type == 'PAS' and not invoice.tca_buyer_passport_country_id:
                errors.append(
                    '"Buyer Passport Country" (BTAE-19) is required when Legal ID Type is Passport. '
                    'Set it in the "Buyer Legal" section.'
                )
        # else: foreign / anonymous buyer — UAE-specific buyer checks skip.
        # Schematron rules ibr-149-ae and friends won't fire either, so XML still passes.

        # ── Invoice lines ────────────────────────────────────────────────────
        product_lines = invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        if not product_lines:
            errors.append('The invoice has no lines. Add at least one product or service line.')

        for line in product_lines:
            label = line.name or (line.product_id and line.product_id.name) or f'Line {line.sequence}'

            # IBT-129: Invoiced quantity
            if not line.quantity:
                errors.append(f'Line "{label}": "Quantity" (IBT-129) is required and cannot be zero.')
                break

            # IBT-130: Unit of measure
            if not line.product_uom_id:
                errors.append(f'Line "{label}": "Unit of Measure" (IBT-130) is required.')
                break

            # IBT-153: Item name
            if not line.name and not (line.product_id and line.product_id.name):
                errors.append(f'Line {line.sequence}: "Description" or product name (IBT-153) is required.')
                break

            if not line.tax_ids:
                errors.append(f'Line "{label}": at least one Tax must be applied.')
                break

            # [pint_ae_vat_category] ibr-sr-58: every line needs a UAE VAT
            # Category (S/E/O/AE/Z/N) on at least one of its taxes — this is
            # universal, not just for Out-of-Scope documents (that check is
            # separate, below, and further restricts which categories are
            # allowed). A line using a tax with no category set silently
            # drops vat_category_code from the outbound submission and TCA
            # rejects it as "This field is required".
            if not any(getattr(t, 'tca_tax_category', '') for t in line.tax_ids):
                errors.append(
                    f'[pint_ae_vat_category] Line "{label}": the tax applied has no '
                    '"UAE VAT Category" set. Pick one of the six PINT AE taxes '
                    '(Settings → Accounting → Taxes) rather than a generic one.'
                )
                break

            # [pint_ae_oos_vat] For Out-of-Scope documents, every line tax
            # must carry a VAT category, restricted to the categories valid
            # for that OOS type (480: E/O/Z; 81 credit note: E/O).
            if type_code in ('480', '81'):
                allowed = {'E', 'O', 'Z'} if type_code == '480' else {'E', 'O'}
                missing_cat = any(not getattr(t, 'tca_tax_category', '') for t in line.tax_ids)
                if missing_cat:
                    errors.append(
                        f'[pint_ae_oos_vat_missing] Line "{label}": every tax on an '
                        'Out-of-Scope invoice must have a UAE VAT Category set.'
                    )
                    break
                bad_cat = any(
                    getattr(t, 'tca_tax_category', '') not in allowed for t in line.tax_ids
                )
                if bad_cat:
                    errors.append(
                        f'[pint_ae_oos_vat] Line "{label}": Out-of-Scope invoices may only use '
                        f'VAT categories {"/".join(sorted(allowed))} — found a different category.'
                    )
                    break

            # ibr-184-ae: HS Code is mandatory only for Reverse-Charge (AE)
            # lines — not simply because the commodity type is Goods/Both.
            # (Service Accounting Code is not client-side mandatory; TCA's
            # server-side schematron may still require it.)
            has_rc = any(getattr(t, 'tca_tax_category', '') == 'AE' for t in line.tax_ids)
            if has_rc and not line.tca_hs_code:
                errors.append(f'Line "{label}": Reverse Charge tax — "HS Code" is mandatory.')
                break
            if has_rc and not line.tca_rc_description:
                errors.append(f'Line "{label}": Reverse Charge tax — "Goods/Services Type" is mandatory.')
                break

            # ibr-167-ae: an Exempt (E) category line needs an exemption
            # reason somewhere — the line override or the tax's own code.
            if line.tca_line_needs_exemption_reason:
                errors.append(
                    f'[ibr-167-ae] Line "{label}": Exempt (E) VAT category — '
                    '"VAT Exemption Reason" is mandatory (set it on the line or on the tax).'
                )
                break

        return errors

    def _tca_validate_xml_pipeline(self):
        """
        Runs the local PINT AE validation pipeline — the pure-Python rule
        replica (builder._export_invoice_constraints, ~30 rules mirroring
        the official schematron). This is the authoritative local gate;
        TCA's Access Point runs the official PINT AE schematron server-side
        as the final compliance check on submission (the inline-JSON
        endpoint validates synchronously — see _tca_submit_outbound).
        Returns a list of error messages (empty = all OK).
        """
        self.ensure_one()
        errors = []
        builder = self.env['account.edi.xml.ubl_pint_ae']

        try:
            vals = builder._export_invoice_vals(self)
            constraints = builder._export_invoice_constraints(self, vals)
            # Parent returns {key: None} for passed checks — filter None values
            for v in constraints.values():
                if v:
                    errors.append(v)
        except Exception as exc:
            _logger.exception('TCA: failed to build PINT AE vals for validation')
            errors.append(_('Internal error building PINT AE vals: %s', exc))

        return errors

    def _tca_build_submission_id(self):
        """
        The invoice_number sent to TCA for a submission attempt: the
        record's own name, unchanged — no per-attempt uniquifying suffix.

        Retry policy: this method is only ever called from contexts gated by
        _tca_is_send_eligible(), which restricts to tca_move_state in
        ('not_sent', 'error', 'rejected'). Once TCA has accepted the
        document (state moves past 'submitted'), retries are blocked
        upstream — so we never re-submit a record TCA has already processed.
        A resubmit after a TCA-side rejection/failure reuses the same
        invoice_number; TCA's own duplicate handling (see
        tca_api._execute_request's 409/400-"exists" handling) covers the
        case where that resubmit turns out to already be on file.
        """
        self.ensure_one()
        return self.name

    # ──────────────────────────────────────────────────────────────────────────
    # JSON SUBMISSION BUILDER — inline-JSON outbound flow (ASP JSON schema §9)
    #
    # TCA validates and builds the UBL XML server-side from this `detail`
    # tree; there is no client-side XML build or S3 upload for submission
    # (the QWeb-template XML builder is still used locally for Tier-1 Python
    # constraint validation and for inbound-document parsing).
    #
    # No self-billing swap here: 17.0 does not build out self-billing (the
    # `_sb` type-code variants exist as dormant fields only) — seller is
    # always the company's own partner, buyer is always the counterpart.
    # ──────────────────────────────────────────────────────────────────────────

    _TCA_ZERO_VAT_CATEGORIES = ('Z', 'AE', 'E', 'O', 'N')
    # Categories where the VAT RATE (IBT-152 / IBT-119) must be ABSENT
    # entirely, not zero — schematron ibr-119-ae / aligned-ibrp-e-05.
    _TCA_NO_RATE_CATEGORIES = ('E', 'O')

    def _tca_product_lines(self):
        """Product lines only (display_type == 'product') — the same filter
        repeated throughout validation and the JSON/XML builders."""
        self.ensure_one()
        return self.invoice_line_ids.filtered(lambda l: l.display_type == 'product')

    def _tca_json_seller_buyer(self):
        """Return (seller_partner, buyer_partner): seller = supplier
        (company), buyer = customer. Always this way round — no self-billing
        swap (see class docstring above)."""
        self.ensure_one()
        company_partner = self.company_id.partner_id.commercial_partner_id
        counterpart = self.partner_id.commercial_partner_id
        return company_partner, counterpart

    @staticmethod
    def _tca_json_peppol_id(raw):
        """Format a participant id as §9 requires: `{scheme}:{identifier}`.
        Stored bare (the XML builder adds the schemeID attribute
        separately); JSON needs the scheme inline. Default to the UAE EAS
        (0235) when no scheme is already present."""
        raw = (raw or '').strip()
        if not raw or ':' in raw:
            return raw
        return f'{constants.UAE_EAS}:{raw}'

    @staticmethod
    def _tca_json_uom_code(uom):
        """UNECE Recommendation 20 unit code for a UoM record. Falls back to
        'C62' (piece/unit) if the core account_edi_ubl_cii helper isn't
        available on this Odoo version, so a missing helper degrades
        gracefully instead of crashing the whole submission."""
        if not uom:
            return 'C62'
        getter = getattr(uom, '_get_unece_code', None)
        if not getter:
            return 'C62'
        try:
            return getter() or 'C62'
        except Exception:
            return 'C62'

    def _tca_json_party(self, partner, peppol_id, is_buyer):
        """Layer 2 party object (§9 sending_party / receiving_party).

        For the buyer, invoice-form overrides (tca_buyer_*) win over the
        partner record — mirrors the XML builder's legal-id precedence so a
        legal id typed on the invoice actually reaches the wire."""
        self.ensure_one()

        # ── Emirate / subdivision ────────────────────────────────────────
        if is_buyer and self.tca_buyer_emirate:
            emirate = self.tca_buyer_emirate
        elif hasattr(partner, '_tca_emirate'):
            emirate = partner._tca_emirate() or ''
        else:
            emirate = partner.tca_emirate or ''

        # Peppol endpoint split into id + scheme (API keeps them separate).
        raw_pid = self._tca_json_peppol_id(peppol_id)
        if ':' in raw_pid:
            eas_scheme, eas_addr = raw_pid.split(':', 1)
        else:
            eas_scheme, eas_addr = constants.UAE_EAS, raw_pid

        # ── Tax identifiers ──────────────────────────────────────────────
        # vat_identifier (IBT-031) = the 15-char VAT TRN = partner.vat.
        # The 10-digit TIN (IBT-032) is derived by TCA from the 0235
        # endpoint (electronic_address) — not sent separately here.
        vat_identifier = partner.vat or ''

        # ── Legal registration id (IBT-030, buyer overrides win) ─────────
        if is_buyer:
            trade_license = (
                self.tca_buyer_trade_license
                or partner.tca_trade_license
                or partner.company_registry
                or vat_identifier
            )
            legal_type = self.tca_buyer_legal_id_type or partner.tca_legal_id_type
            legal_authority = self.tca_buyer_legal_authority or partner.tca_legal_authority
            passport_country = (
                self.tca_buyer_passport_country_id.code
                if self.tca_buyer_passport_country_id
                else (
                    partner.tca_passport_country_id.code if partner.tca_passport_country_id else ''
                )
            )
        else:
            trade_license = partner.tca_trade_license or partner.company_registry or vat_identifier
            legal_type = partner.tca_legal_id_type
            legal_authority = partner.tca_legal_authority
            passport_country = (
                partner.tca_passport_country_id.code if partner.tca_passport_country_id else ''
            )

        party = {
            'name': partner.name or '',
            'electronic_address': eas_addr,
            'electronic_address_scheme': eas_scheme,
            'address_line_1': partner.street or '',
            'city': partner.city or '',
            'country_subdivision': emirate,
            'country_code': partner.country_id.code or '',
        }
        if partner.street2:
            party['address_line_2'] = partner.street2
        if partner.zip:
            party['postal_zone'] = partner.zip
        if vat_identifier:
            party['vat_identifier'] = vat_identifier
            party['tax_scheme'] = 'VAT'
        if trade_license:
            party['legal_registration_identifier'] = trade_license
        if legal_type:
            party['legal_registration_identifier_type'] = legal_type
        if legal_authority:
            party['legal_registration_authority'] = legal_authority
        if passport_country:
            party['passport_country'] = passport_country
        if partner.tca_legal_form:
            party['additional_legal_info'] = partner.tca_legal_form
        return party

    def _tca_json_line(self, line, seq):
        """Layer 4 — one invoice_lines[] entry (§9)."""
        vat_tax = next((t for t in line.tax_ids if t.tca_tax_category), None)
        cat = vat_tax.tca_tax_category if vat_tax else ''
        rate = vat_tax.amount if vat_tax else 0.0
        net = line.price_subtotal
        # VAT amount must be 0 for Z/AE/E/O/N per §9 (buyer self-accounts,
        # exempt, out-of-scope, or additional-VAT-not-added — see
        # ibr-108-ae); use the actual line delta otherwise.
        vat_amt = (
            0.0
            if cat in self._TCA_ZERO_VAT_CATEGORIES
            else (line.price_total - line.price_subtotal)
        )
        item_name = line.name or (line.product_id.name if line.product_id else '') or ''
        commodity = line.tca_effective_commodity_type or ''
        # Net unit price (after line discount); Odoo price_unit is pre-discount.
        net_unit = line.price_unit * (1 - (line.discount or 0.0) / 100.0)

        vat_info = {
            'vat_category_code': cat,
            'tax_scheme': 'VAT',
        }
        # VAT rate (IBT-152) must be ABSENT for E/O — ibr-119-ae. Present
        # (incl. 0) for S/Z/AE/N.
        if cat not in self._TCA_NO_RATE_CATEGORIES:
            vat_info['vat_rate'] = round(rate, 2)
        if cat == 'E':
            # Per-line override wins; fall back to the reason code on the tax.
            reason_code = (line.tca_vat_exemption_reason_code or '').strip() or (
                vat_tax.tca_exemption_reason_code if vat_tax else ''
            )
            if reason_code:
                vat_info['vat_exemption_reason_code'] = reason_code
            if vat_tax and vat_tax.tca_exemption_reason:
                vat_info['vat_exemption_reason_text'] = vat_tax.tca_exemption_reason

        d = {
            'line_id': str(seq),
            'invoiced_quantity': line.quantity,
            'invoiced_quantity_unit_of_measure_code': self._tca_json_uom_code(line.product_uom_id),
            'line_net_amount': round(net, 2),
            'item_net_price': round(net_unit, 2),
            'item_gross_price': round(line.price_unit, 2),
            'item_price_base_quantity': 1,
            'item_name': item_name,
            'item_description': item_name,  # IBT-154 mandatory — mirror name
            'item_type': commodity,  # BTAE-13 G/S/B
            'line_amount_in_aed': round(net + vat_amt, 2),  # BTAE-10
            'vat_info': [vat_info],
        }
        # BTAE-08 (VAT line amount) must be ABSENT on Exempt lines —
        # schematron ibr-163-ae. Emit it for every other category (0 is
        # valid for Z/O/AE/N).
        if cat != 'E':
            d['vat_line_amount_in_aed'] = round(vat_amt, 2)  # BTAE-08
        # HS (goods) / SAC (services) go in their own arrays.
        if commodity in ('G', 'B') and line.tca_hs_code:
            d['classifications'] = [{
                'classification_identifier': line.tca_hs_code,
                'classification_identifier_scheme': 'HS',
            }]
        if commodity in ('S', 'B') and line.tca_service_accounting_code:
            d['service_accounting_codes'] = [
                {'code': line.tca_service_accounting_code, 'scheme_identifier': 'SAC'}
            ]
        if cat == 'AE':
            if line.tca_rc_description:
                d['type_of_goods_or_services'] = line.tca_rc_description
            if line.tca_standard_item_id:
                d['item_standard_identifier'] = line.tca_standard_item_id
                d['item_standard_identifier_scheme'] = line.tca_standard_item_scheme or '0160'
        if line.tca_line_note:
            d['note'] = line.tca_line_note
        return d

    def _tca_json_lines(self):
        self.ensure_one()
        return [
            self._tca_json_line(line, seq)
            for seq, line in enumerate(self._tca_product_lines(), start=1)
        ]

    def _tca_json_vat_breakdown(self):
        """Layer 5 — one entry per unique (category, rate) across lines."""
        self.ensure_one()
        groups = {}
        for line in self._tca_product_lines():
            vat_tax = next((t for t in line.tax_ids if t.tca_tax_category), None)
            cat = vat_tax.tca_tax_category if vat_tax else ''
            rate = vat_tax.amount if vat_tax else 0.0
            key = (cat, rate)
            g = groups.setdefault(key, {
                'vat_category_code': cat,
                'tax_scheme_code': 'VAT',
                'taxable_amount': 0.0,
                'tax_amount': 0.0,
            })
            if cat not in self._TCA_NO_RATE_CATEGORIES:
                g['vat_category_rate'] = round(rate, 2)
            g['taxable_amount'] += line.price_subtotal
            if cat not in self._TCA_ZERO_VAT_CATEGORIES:
                g['tax_amount'] += line.price_total - line.price_subtotal
        # Accumulated in a Python float loop above — round once at the end
        # rather than per-add, so intermediate rounding doesn't drift the sum.
        for g in groups.values():
            g['taxable_amount'] = round(g['taxable_amount'], 2)
            g['tax_amount'] = round(g['tax_amount'], 2)
        return list(groups.values())

    def _tca_json_totals(self):
        """Layer 5 — totals (real API leaf names)."""
        self.ensure_one()
        return {
            'sum_of_invoice_line_net_amount': round(self.amount_untaxed, 2),
            'invoice_total_amount_without_vat': round(self.amount_untaxed, 2),
            'invoice_total_vat_amount': round(self.amount_tax, 2),
            'invoice_total_amount_with_vat': round(self.amount_total, 2),
            'amount_due_for_payment': round(self.amount_total, 2),
        }

    def _tca_build_json_detail(self):
        """Build the full PINT AE `detail` tree for the inline-JSON
        submission mode (ASP JSON schema §9). Returns a plain dict ready to
        json-encode."""
        self.ensure_one()
        is_credit_note = (self.tca_uncl1001_code or '') in ('381', '81')
        seller, buyer = self._tca_json_seller_buyer()

        # Transaction-type code — reuse the XML builder's BTAE-02 logic so
        # the two paths can never drift apart.
        builder = self.env['account.edi.xml.ubl_pint_ae']
        transaction_type_code = builder._get_profile_execution_id(self)

        detail = {
            'issue_date': self.invoice_date.isoformat() if self.invoice_date else '',
            'invoice_type_code': self.tca_uncl1001_code or '',
            'transaction_type_code': transaction_type_code,
            'invoice_currency_code': self.currency_id.name or 'AED',
            'process_control': {
                'profile_id': PINT_AE_PROFILE_ID,
                'customization_id': PINT_AE_CUSTOMIZATION_ID,
            },
            'seller': self._tca_json_party(seller, seller.peppol_endpoint, is_buyer=False),
            'buyer': self._tca_json_party(buyer, self.tca_buyer_participant_id, is_buyer=True),
            'lines': self._tca_json_lines(),
            'vat_breakdowns': self._tca_json_vat_breakdown(),
            'totals': self._tca_json_totals(),
        }

        # ── Layer 1 conditional header fields ───────────────────────────────
        if (
            self.invoice_date_due
            and self.amount_total > 0
            and not is_credit_note
            and not self.tca_flag_deemed_supply
        ):
            detail['payment_due_date'] = self.invoice_date_due.isoformat()
        if self.tca_tax_point_date and not is_credit_note:
            detail['tax_point_date'] = self.tca_tax_point_date.isoformat()
        if self.tca_buyer_reference:
            detail['buyer_reference'] = self.tca_buyer_reference
        if self.tca_buyer_accounting_ref:
            detail['accounting_cost'] = self.tca_buyer_accounting_ref
        if self.invoice_payment_term_id:
            detail['payment_terms'] = self.invoice_payment_term_id.name
        if self.narration:
            # narration is HTML on account.move; strip to plain text.
            note_text = re.sub(r'<[^>]+>', ' ', self.narration or '').strip()
            if note_text:
                detail['note'] = note_text

        # FTZ beneficiary id (BTAE-01) lives on the buyer party.
        if self.tca_flag_free_trade_zone and self.tca_buyer_beneficiary_id:
            detail['buyer']['beneficiary_identifier'] = self.tca_buyer_beneficiary_id
        # Disclosed-agent principal (BTAE-14) — detail-root key.
        if self.tca_flag_disclosed_agent and self.tca_principal_id:
            detail['principal_identifier'] = self.tca_principal_id

        # ── Layer 3 references / periods / delivery / payment ───────────────
        references = {}
        if self.tca_contract_reference:
            references['contract_id'] = self.tca_contract_reference
        if self.tca_contract_value:
            references['contract_value'] = self.tca_contract_value
        if self.tca_project_reference:
            references['project'] = self.tca_project_reference
        if self.tca_export_declaration_number:
            references['customs_ref'] = self.tca_export_declaration_number
        if is_credit_note and self.tca_credit_note_reason:
            references['credit_note_reason_code'] = self.tca_credit_note_reason
        # Preceding invoice — mandatory for credit notes unless reason is VD.
        if is_credit_note and self.tca_credit_note_reason != 'VD' and self.reversed_entry_id:
            references['preceding_invoices'] = [{
                'id': self.reversed_entry_id.name or '',
                'issue_date': (
                    self.reversed_entry_id.invoice_date.isoformat()
                    if self.reversed_entry_id.invoice_date else ''
                ),
            }]
        if references:
            detail['references'] = references

        # delivery — mandatory when ecommerce or export. Prefer the explicit
        # tca_delivery_* fields; fall back to the shipping partner.
        if self.tca_flag_ecommerce or self.tca_is_export:
            ship = self.partner_shipping_id or self.partner_id
            if self.tca_delivery_state_id:
                sub = constants.UAE_STATE_CODE_TO_EMIRATE.get(
                    self.tca_delivery_state_id.code, self.tca_delivery_state_id.code,
                )
            else:
                sub = ship._tca_emirate() if hasattr(ship, '_tca_emirate') else ''
            delivery = {
                'address': {
                    'address_line_1': self.tca_delivery_street or ship.street or '',
                    'city': self.tca_delivery_city or ship.city or '',
                    'country_subdivision': sub or '',
                    'country_code': (
                        ship.country_id.code if ship.country_id
                        else (buyer.country_id.code if buyer.country_id else '')
                    ) or '',
                }
            }
            if self.tca_delivery_date:
                delivery['actual_delivery_date'] = self.tca_delivery_date.isoformat()
            if self.tca_incoterms:
                delivery['incoterms'] = self.tca_incoterms
            if self.tca_delivery_party_trn:
                delivery['party_identifier'] = self.tca_delivery_party_trn
            detail['delivery'] = delivery

        # invoicing_period (IBG-14) — mandatory when Summary; also Continuous.
        if self.tca_invoice_period_start or self.tca_invoice_period_end:
            s = self.tca_invoice_period_start.isoformat() if self.tca_invoice_period_start else ''
            e = self.tca_invoice_period_end.isoformat() if self.tca_invoice_period_end else ''
            obj = {}
            if s:
                obj['start_date'] = s
            if e:
                obj['end_date'] = e
            freq = (
                self.tca_billing_frequency
                if self.tca_flag_continuous_supply and self.tca_billing_frequency
                else ''
            )
            if freq:
                obj['frequency_of_billing'] = freq
            detail['invoicing_period'] = dict(obj)

        # payment_instructions — required for all doc types except credit
        # notes / deemed supply. IBT-081 payment means type code, one entry
        # per tca_payment_means_ids line (an invoice may declare several).
        if not is_credit_note and not self.tca_flag_deemed_supply and self.tca_payment_means_ids:
            detail['payment_instructions'] = [
                {'payment_means_type_code': pm.tca_payment_means_code}
                for pm in self.tca_payment_means_ids
            ]

        # Strip empty strings / None / empty containers so TCA does not
        # render empty UBL elements. Numeric 0 / 0.0 is KEPT.
        return self._tca_prune_empty(detail)

    @staticmethod
    def _tca_prune_empty(value):
        """Recursively drop '' / None / empty dict / empty list from a JSON
        structure. Keeps 0, 0.0 and False (meaningful values)."""
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                pv = AccountMove._tca_prune_empty(v)
                if pv is None or pv == '' or pv == {} or pv == []:
                    continue
                out[k] = pv
            return out
        if isinstance(value, (list, tuple)):
            out = [AccountMove._tca_prune_empty(v) for v in value]
            return [v for v in out if not (v is None or v == '' or v == {} or v == [])]
        return value

    def _tca_submit_outbound(self):
        """
        Submit this posted invoice/credit note to TCA via the inline-JSON
        endpoint. Atomic: raises UserError on any PERMANENT failure so the
        caller can roll back super()._post() — UAE FTA compliance requires
        TCA to ACCEPT the document before it is recorded in the books.

        Single call: POST /api/v1/invoices/ with the PINT AE `detail` tree
        (no XML build, no S3 upload). Validation is synchronous — 201 means
        validated + queued, 400 means content rejected (TcaValidationError,
        nothing posted).

        Robustness (kept from the pre-JSON flow, not present in the 19.0
        version this was ported from — see docs/PORTING_17_vs_19.md P2.7):
          - Row lock (SELECT ... FOR UPDATE NOWAIT) guards against a
            concurrent double-submit of the same invoice.
          - Transient errors (network/timeout/5xx) leave tca_move_state at
            'submitted' rather than 'error', so the retry cron picks the
            invoice back up instead of requiring a manual resend.
        """
        self.ensure_one()
        api_svc = self.env['tca.api.service']
        company = self.company_id

        from psycopg2 import OperationalError
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    'SELECT id FROM account_move WHERE id = %s FOR UPDATE NOWAIT',
                    [self.id],
                )
        except OperationalError as exc:
            raise UserError(_(
                'Invoice %s is being submitted by another process — try again shortly.',
                self.name or '(draft)',
            )) from exc

        # 1. Build the PINT AE detail tree from this move.
        detail = self._tca_build_json_detail()

        # 2. invoice_number for this submission (the record's own name).
        submission_id = self._tca_build_submission_id()

        # 3. Submit (synchronous validation).
        self.tca_move_state = 'uploading'
        try:
            result = api_svc.submit_invoice_json(
                company=company,
                name=submission_id,
                invoice_number=submission_id,
                detail=detail,
            )
        except TcaValidationError as exc:
            # Content rejected — surface the per-field list; leave unposted.
            self.write({
                'tca_move_state': 'error',
                'tca_submission_error': '\n'.join(exc.tca_field_errors) or str(exc),
            })
            raise UserError(_(
                'TCA rejected this invoice — fix these and confirm again:\n\n%s',
                '\n'.join(f'• {e}' for e in exc.tca_field_errors) or str(exc),
            )) from exc
        except TcaTransientError as exc:
            # Network/timeout/5xx — leave 'submitted' so the retry cron
            # picks it back up instead of dead-ending in 'error'.
            self.write({
                'tca_move_state': 'submitted',
                'tca_submission_error': str(exc),
            })
            self._message_log(body=_(
                'TCA Peppol: transient error on submission, will retry: %s', exc,
            ))
            raise UserError(_(
                'Cannot confirm — TCA is temporarily unreachable (%s). The invoice will '
                'be retried automatically; you can also use "Retry TCA" later.', exc,
            )) from exc

        # 4. Duplicate — expected on a resubmit if the prior attempt actually
        #    made it through despite the failure that triggered this retry.
        if result.get('tca_duplicate'):
            self.write({
                'tca_move_state': 'submitted',
                'tca_submission_error': False,
                'tca_last_submission_id': submission_id,
            })
            self._message_log(body=_(
                'TCA: document already registered (duplicate detected on submission "%s"). '
                'Status will sync via cron.', submission_id,
            ))
            return True

        # 5. 201 — validated + queued. Store the TCA id, mark submitted.
        tca_id = result.get('id', '')
        self.write({
            'tca_invoice_uuid': tca_id,
            'tca_move_state': 'submitted',
            'tca_submission_error': False,
            'tca_last_submission_id': submission_id,
        })
        self._message_log(body=_(
            'Submitted to TCA Peppol network (validated on submission). '
            'TCA invoice_number: %(sid)s — TCA ID: %(tid)s',
            sid=submission_id, tid=tca_id,
        ))
        return True

    def _post(self, soft=True):
        """
        EXTENDS account.move.
        Three-phase PINT AE flow at Confirm:
          Phase 1 (pre-post): _tca_validate_mandatory_fields — fast Python checks
                              on partner / invoice fields. Fails → no ledger entry.
          Phase 2 (post-post): _tca_validate_xml_pipeline — local PINT AE rule
                              replica, same as Send & Print wizard. TCA's own
                              schematron runs server-side on submission.
                              Fails → UserError rolls back the super()._post().
          Phase 3 (credit notes only): _tca_submit_outbound — register the document
                              with TCA Peppol synchronously. UAE FTA compliance:
                              no credit note in the books until it reaches Peppol.
                              Fails → UserError rolls back the super()._post().

        Net guarantee: a credit note is either in the books AND on the Peppol
        network, or in neither — never half-committed.

        Scope: sale documents on TCA-active company whose buyer uses ubl_pint_ae.

        Known limitation — sequence gaps on failure:
          super()._post() assigns the document name from the journal's sequence
          via PostgreSQL's nextval(), which is NOT transactional — the sequence
          advance survives a rollback. When Phase 2 or Phase 3 raises UserError,
          the move is rolled back to draft but the consumed sequence number is
          gone. The next successful Confirm picks up the FOLLOWING number,
          leaving a permanent gap.

          UAE FTA expects sequential invoice numbering. In practice, gaps are
          rare (only on actual TCA failures, which should be uncommon in
          production) and can be explained in audits. Proper no-gap behavior
          would require replacing the journal sequence with a row-locked
          counter table that's truly transactional — out of scope for this
          revision. TODO: implement a custom counter for TCA-active journals.
        """
        # ── Phase 1: pre-post fast checks ─────────────────────────────────────
        pint_moves = self.env['account.move']
        for move in self:
            partner = move.partner_id.commercial_partner_id
            if (
                move.company_id.tca_is_active
                and move.tca_create_einvoice
                and move.is_sale_document()
                and partner.ubl_cii_format == 'ubl_pint_ae'
            ):
                errors = move._tca_validate_mandatory_fields()
                if errors:
                    raise UserError(_(
                        'Cannot confirm this invoice — the following issues must be fixed first:\n\n%s',
                        '\n'.join(f'• {v}' for v in errors)
                    ))
                pint_moves |= move

        # ── Standard Odoo posting (assigns sequence + ledger entries) ────────
        result = super()._post(soft=soft)

        # ── Phase 2: full XML validation on the just-posted invoice ──────────
        # Raising here rolls back super()._post() — invoice returns to draft,
        # no ledger entries persist.
        for move in pint_moves:
            xml_errors = move._tca_validate_xml_pipeline()
            if xml_errors:
                raise UserError(_(
                    'Cannot confirm this invoice — PINT AE validation failed:\n\n%s\n\n'
                    'Fix these issues, then try Confirm again.',
                    '\n'.join(f'• {v}' for v in xml_errors)
                ))

        # ── Phase 3: TCA submission for credit notes (atomic with post) ───────
        # UAE FTA requires that a credit note must reach the Peppol network
        # before it can be considered "in the books". On any failure (validation,
        # network, TCA rejection) we raise UserError, rolling back super()._post()
        # so the credit note stays in draft.
        #
        # Batch safety: if multiple credit notes are posted at once and TCA
        # accepts some but rejects a later one, the accepted ones are already
        # on Peppol (HTTP done) but the raise below rolls back the local DB
        # state for ALL of them — creating drift between Odoo and TCA that we
        # cannot reconcile (TCA documents cannot be un-submitted). To prevent
        # this we restrict atomic credit-note posting to a single record per
        # call. Multi-confirm of credit notes must be done one at a time.
        credit_notes = pint_moves.filtered(
            lambda m: m.move_type == 'out_refund'
            and m.tca_move_state in ('not_sent', 'error', 'rejected')
        )
        if len(credit_notes) > 1:
            raise UserError(_(
                'Confirm credit notes one at a time. TCA Peppol submission '
                'happens during Confirm and cannot be safely batched: if TCA '
                'accepts the first credit note and rejects the second, the '
                'first would already be on the Peppol network while local '
                'records are rolled back, leaving Odoo and TCA out of sync.\n\n'
                'Please select a single credit note and Confirm it, then move '
                'on to the next.'
            ))
        if credit_notes:
            move = credit_notes  # exactly one record at this point
            try:
                move._tca_submit_outbound()
            except UserError:
                raise  # propagate already-clear error
            except Exception as exc:
                _logger.exception('TCA: credit-note submission crashed during _post')
                raise UserError(_(
                    'Cannot confirm credit note %(name)s — TCA submission failed.\n\n%(err)s\n\n'
                    'Per UAE FTA regulations, a credit note must be sent to the '
                    'Peppol network before it is recorded in the books. Fix the '
                    'issue and try Confirm again.',
                    name=move.name or '(draft)', err=exc,
                )) from exc

        return result

    # ──────────────────────────────────────────────────────────────────────────
    # INBOUND XML ROUTING — register PINT AE in the builder dispatch table
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _get_ubl_cii_builder_from_xml_tree(self, tree):
        """
        EXTENDS account_edi_ubl_cii.
        Check for PINT AE CustomizationID BEFORE the generic BIS3 prefix check,
        because PINT AE's urn:peppol:pint:billing-1@ae-1 does NOT start with
        urn:cen.eu:en16931:2017 and would otherwise fall through unmatched.
        """
        customization_id = tree.find('{*}CustomizationID')
        if customization_id is not None:
            if customization_id.text == PINT_AE_CUSTOMIZATION_ID:
                return self.env['account.edi.xml.ubl_pint_ae']
        return super()._get_ubl_cii_builder_from_xml_tree(tree)

    # ──────────────────────────────────────────────────────────────────────────
    # STATUS UPDATE (called from webhook and cron)
    # ──────────────────────────────────────────────────────────────────────────

    def _tca_update_state_from_payload(self, payload):
        """
        Update tca_move_state from a TCA status poll response (GET /api/v1/invoices/{id}/).
        Payload keys (all integer codes per API spec):
          status         — 1=Processing, 2=Completed, 3=Rejected, 4=Failed
          c3_mls_status  — 0=N/A, 4=Accepted, 5=Rejected, 6=Unable to Deliver
          c5_mls_status  — 0=N/A, 4=Accepted (buyer confirmed receipt)
          uuid           — the TCA invoice UUID
        """
        self.ensure_one()
        tca_status = payload.get('status')    # int or None
        c3_status = payload.get('c3_mls_status')
        c5_status = payload.get('c5_mls_status')

        old_state = self.tca_move_state

        # Determine new state from most → least granular signal
        # c5 Accepted = buyer has confirmed receipt (terminal success)
        if c5_status == TCA_C5_ACCEPTED:
            new_state = 'buyer_confirmed'
        # status=Completed + c3 Accepted = delivered to buyer AP
        elif tca_status == TCA_STATUS_COMPLETED and c3_status == TCA_C3_ACCEPTED:
            new_state = 'delivered'
        elif tca_status == TCA_STATUS_PROCESSING:
            new_state = 'processing'
        elif tca_status == TCA_STATUS_REJECTED:
            new_state = 'rejected'
        elif tca_status == TCA_STATUS_FAILED:
            new_state = 'error'
        else:
            _logger.warning(
                'TCA: unrecognised status payload for invoice %s: %s', self.name, payload
            )
            return

        if new_state == old_state:
            return  # No change

        self.tca_move_state = new_state
        if new_state in ('error', 'rejected'):
            error_detail = payload.get('error_message') or payload.get('detail') or tca_status
            self.tca_submission_error = error_detail
            self._message_log(
                body=_('TCA Peppol: Invoice %s — status changed to %s. Detail: %s',
                       self.name, new_state.upper(), error_detail)
            )
        else:
            self.tca_submission_error = False
            self._message_log(
                body=_('TCA Peppol: Invoice %s — status updated from %s → %s.',
                       self.name, old_state.upper(), new_state.upper())
            )

        _logger.info(
            'TCA: invoice %s (id=%s) state %s → %s',
            self.name, self.id, old_state, new_state
        )

    # ──────────────────────────────────────────────────────────────────────────
    # CRON: fallback status polling
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _cron_tca_sync_outbound_status(self):
        """
        Fallback polling cron — runs every 15 minutes.
        Syncs status for outbound invoices in 'submitted' or 'processing' state.

        Strategy (G-1 optimisation):
          1. Call list_processing_outbound() once per company to get the set of
             TCA invoice IDs that TCA still reports as status=1 (Processing).
          2. Any Odoo invoice NOT in that set must have changed state on TCA's side
             (completed, rejected, failed) — poll those individually.
          3. Invoices still in TCA's processing list are left unchanged (no extra call).

        This reduces API calls from N (one per pending invoice) to 1 + M where M
        is the number of invoices that have transitioned out of the processing state.
        Falls back to per-invoice polling if list_processing_outbound() fails.
        """
        active_companies = self.env['res.company'].search([('tca_is_active', '=', True)])
        api_svc = self.env['tca.api.service']

        for company in active_companies:
            pending_invoices = self.env['account.move'].search([
                ('company_id', '=', company.id),
                ('tca_move_state', 'in', ['submitted', 'processing']),
                ('tca_invoice_uuid', '!=', False),
            ], limit=100)

            if not pending_invoices:
                continue

            # ── Step 1: Fetch IDs TCA still considers in-flight ───────────────
            still_processing_ids = set()
            try:
                result = api_svc.list_processing_outbound(company, limit=200)
                tca_list = result.get('results', result) if isinstance(result, dict) else result
                still_processing_ids = {str(item.get('id', '')) for item in tca_list if item.get('id')}
            except Exception as exc:
                _logger.warning(
                    'TCA cron: list_processing_outbound failed for company %s (%s), '
                    'falling back to per-invoice poll',
                    company.id, exc
                )
                # Fallback: poll all pending invoices individually
                for invoice in pending_invoices:
                    self._tca_poll_single_invoice(api_svc, company, invoice)
                continue

            # ── Step 2: Poll only invoices that have moved out of processing ──
            for invoice in pending_invoices:
                uuid = str(invoice.tca_invoice_uuid or '')
                if uuid in still_processing_ids:
                    # TCA still processing — no state change, skip API call
                    continue
                # Invoice has changed state on TCA side — poll for final status
                self._tca_poll_single_invoice(api_svc, company, invoice)

    @api.model
    def _tca_poll_single_invoice(self, api_svc, company, invoice):
        """Poll TCA for a single invoice's current status and update state."""
        try:
            payload = api_svc.get_invoice_status(company, invoice.tca_invoice_uuid)
            invoice._tca_update_state_from_payload(payload)
        except Exception as exc:
            is_transient = isinstance(exc, TcaTransientError)
            if is_transient:
                # Transient — leave state unchanged, cron will retry next run
                _logger.warning(
                    'TCA cron: transient error polling invoice %s (uuid=%s), will retry: %s',
                    invoice.name, invoice.tca_invoice_uuid, exc
                )
            else:
                # Permanent — mark as error so user investigates
                _logger.error(
                    'TCA cron: permanent error polling invoice %s (uuid=%s): %s',
                    invoice.name, invoice.tca_invoice_uuid, exc
                )
                invoice.write({
                    'tca_move_state': 'error',
                    'tca_submission_error': str(exc),
                })
                invoice._message_log(body=_(
                    'TCA cron: status poll failed — %s', exc
                ))

    # First-ever run for a company has no stored cursor — look back this far
    # so nothing sent before the module/webhook was set up gets missed.
    # Bounded window still satisfies the API's "created_at window required"
    # contract; subsequent runs narrow it to since-last-successful-run.
    _TCA_INBOUND_FIRST_RUN_LOOKBACK_DAYS = 30

    @api.model
    def _cron_tca_pull_inbound_invoices(self):
        """
        Fallback polling cron — pulls inbound invoices from TCA (direction=2).
        Normally inbound invoices arrive via webhook (DOCUMENT_RECEIVED).
        This cron is a safety net for missed webhooks.

        Improvements over the naive implementation:
          G-2: env.cr.commit() between each import — one parse failure does not
               roll back other successfully imported invoices.
          G-3: On UBL parse failure, create a bare draft vendor bill stub with the
               raw XML attached so accounting staff can manually process it.
          G-4: Follow DRF pagination (next URL) to import ALL inbound invoices,
               not just the first page of 50.
          G-6: Cursor-based tracking — created_at_from/created_at_to window
               (REQUIRED by the API on every list call) plus an after=<id>
               cursor from the last successful run. A stale after (TCA no
               longer recognises the id) 400s rather than returning an empty
               page — caught and retried once without it, same window.

        Deduplication: tca_invoice_uuid — already-imported invoices are skipped.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        active_companies = self.env['res.company'].search([('tca_is_active', '=', True)])
        api_svc = self.env['tca.api.service']
        now = fields.Datetime.now()

        for company in active_companies:
            time_key = f'tca.{company.id}.last_inbound_sync_time'
            after_key = f'tca.{company.id}.last_inbound_after_id'
            last_sync_time = ICP.get_param(time_key, '')
            last_after_id = ICP.get_param(after_key, '') or None

            if last_sync_time:
                created_at_from = last_sync_time
            else:
                lookback = now - timedelta(days=self._TCA_INBOUND_FIRST_RUN_LOOKBACK_DAYS)
                created_at_from = lookback.strftime('%Y-%m-%dT%H:%M:%SZ')
            created_at_to = now.strftime('%Y-%m-%dT%H:%M:%SZ')

            try:
                self._tca_pull_inbound_for_company(
                    api_svc, company, ICP, time_key, after_key,
                    created_at_from, created_at_to, last_after_id,
                )
            except Exception as exc:
                _logger.error(
                    'TCA cron: failed to pull inbound invoices for company %s: %s',
                    company.id, exc
                )

    @api.model
    def _tca_pull_inbound_for_company(self, api_svc, company, ICP, time_key, after_key,
                                       created_at_from, created_at_to, after_id):
        """
        Pull and import all new inbound invoices for one company.
        Paginates through all result pages (G-4).
        Updates both cursor params after a successful run (G-6).
        """
        try:
            result = api_svc.list_inbound_invoices(
                company, created_at_from, created_at_to, after=after_id, limit=50
            )
        except Exception as exc:
            if after_id:
                # Stale after cursor (TCA no longer recognises that id) 400s
                # instead of an empty page — retry once without it, same
                # window. tca_invoice_uuid dedup below covers any overlap.
                _logger.warning(
                    'TCA cron: list_inbound_invoices with after=%s failed for '
                    'company %s (%s) — retrying without cursor', after_id, company.id, exc
                )
                result = api_svc.list_inbound_invoices(
                    company, created_at_from, created_at_to, after=None, limit=50
                )
            else:
                raise

        page_invoices = result.get('results', result) if isinstance(result, dict) else result
        next_url = result.get('next') if isinstance(result, dict) else None
        last_seen_id = after_id  # advances to the last id actually returned, in API order

        while True:
            for tca_invoice in page_invoices:
                tca_id = tca_invoice.get('id')
                xml_location_path = tca_invoice.get('document_location_path') or tca_invoice.get('invoice_xml_location_path')

                if not tca_id:
                    continue
                last_seen_id = tca_id

                # Deduplication — belt-and-suspenders alongside the window/cursor
                existing = self.env['account.move'].search([
                    ('tca_invoice_uuid', '=', tca_id),
                    ('company_id', '=', company.id),
                ], limit=1)
                if existing:
                    continue

                if not xml_location_path:
                    # List endpoint may omit xml path — fetch detail (same as webhook).
                    # Backend returns it on single GET via `invoice_xml_location_path`.
                    try:
                        detail = api_svc.get_invoice_status(company, tca_id)
                        xml_location_path = (
                            detail.get('document_location_path')
                            or detail.get('invoice_xml_location_path')
                        )
                    except Exception as exc:
                        _logger.warning(
                            'TCA cron: failed to fetch detail for inbound id=%s: %s',
                            tca_id, exc
                        )
                        continue
                    if not xml_location_path:
                        _logger.warning(
                            'TCA cron: inbound invoice id=%s has no XML path even after detail fetch, skipping',
                            tca_id
                        )
                        continue

                # G-2: commit between imports — isolate failures.
                # After commit, the ORM cache must be invalidated: records held
                # in-memory may be stale relative to other concurrent transactions
                # that ran while we were doing HTTP work for this iteration.
                self._tca_import_inbound_invoice(
                    company, tca_id, xml_location_path, api_svc
                )
                self.env.cr.commit()  # noqa: B012 — intentional mid-cron commit
                self.env.invalidate_all()

            # G-4: follow pagination
            if not next_url:
                break
            try:
                result = api_svc._http_get_url(company, next_url)
                page_invoices = result.get('results', [])
                next_url = result.get('next')
            except Exception as exc:
                _logger.error('TCA cron: pagination fetch failed (%s) — stopping', exc)
                break

        # G-6: advance both cursors — window start becomes this run's end,
        # after becomes the last invoice id actually seen (API return order).
        ICP.set_param(time_key, created_at_to)
        if last_seen_id and last_seen_id != after_id:
            ICP.set_param(after_key, last_seen_id)
        _logger.info(
            'TCA cron: updated inbound cursor for company %s (time=%s, after=%s)',
            company.id, created_at_to, last_seen_id
        )

    def _tca_import_inbound_invoice(self, company, tca_id, xml_location_path, api_svc=None):
        """
        Import a single inbound TCA invoice as a vendor bill in Odoo.
        Called by the webhook controller (after fetching invoice details) and the fallback cron.

        tca_id            — TCA invoice ID (stored as tca_invoice_uuid)
        xml_location_path — S3 URI from invoice_xml_location_path field in TCA response
        api_svc           — optional pre-resolved tca.api.service reference

        Returns the created account.move record or None on failure.
        """
        if api_svc is None:
            api_svc = self.env['tca.api.service']

        try:
            xml_bytes = api_svc.download_inbound_xml(company, xml_location_path)
        except Exception as exc:
            _logger.error(
                'TCA: failed to download XML for id %s (path=%s): %s',
                tca_id, xml_location_path, exc
            )
            # No stub created — cron will retry on next run (cursor doesn't advance).
            # Log to company partner chatter so admins are aware.
            company.partner_id._message_log(body=_(
                'TCA Peppol: failed to download inbound invoice XML (ID: %s). '
                'Error: %s. Will retry on next cron run.',
                tca_id, exc,
            ))
            return None

        # Find a purchase journal for this company
        journal = self.env['account.journal'].search([
            ('type', '=', 'purchase'),
            ('company_id', '=', company.id),
        ], limit=1)
        if not journal:
            _logger.error('TCA: no purchase journal found for company %s', company.id)
            return None

        # Create attachment
        filename = f'tca_inbound_{tca_id}.xml'
        from base64 import b64encode
        try:
            attachment = self.env['ir.attachment'].create({
                'name': filename,
                'datas': b64encode(xml_bytes),
                'res_model': 'account.journal',
                'res_id': journal.id,
                'type': 'binary',
                'mimetype': 'application/xml',
            })
        except Exception as exc:
            _logger.error('TCA: attachment creation failed for id %s: %s', tca_id, exc)
            return None

        # Use Odoo's standard UBL import pipeline.
        # _create_document_from_attachment routes via _get_ubl_cii_builder_from_xml_tree
        # which correctly routes PINT AE CustomizationID to our builder.
        try:
            move = journal.with_context(
                default_move_type='in_invoice',
                default_journal_id=journal.id,
            )._create_document_from_attachment(attachment.id)
        except Exception as exc:
            _logger.error('TCA: UBL import failed for id %s: %s', tca_id, exc)
            # G-3: create a bare draft vendor bill stub so the document is not lost.
            # Accounting staff can manually complete the record using the attached XML.
            move = self.env['account.move'].create({
                'move_type': 'in_invoice',
                'journal_id': journal.id,
                'company_id': company.id,
                'tca_invoice_uuid': tca_id,
                'tca_move_state': 'inbound_received',
                'tca_is_inbound': True,
                'tca_inbound_status': 'pending',
                'ref': f'TCA-{tca_id}',
            })
            attachment.write({'res_model': 'account.move', 'res_id': move.id})
            move._message_log(
                body=_(
                    'TCA Peppol: UBL parse failed for inbound invoice (ID: %s). '
                    'The raw XML is attached. Please fill in the details manually.',
                    tca_id
                )
            )
            _logger.warning(
                'TCA: created stub vendor bill for id %s after UBL parse failure', tca_id
            )
            return move

        if move:
            move.sudo().write({
                'tca_invoice_uuid': tca_id,
                'tca_move_state': 'inbound_received',
                'tca_is_inbound': True,
                'tca_inbound_status': 'pending',
            })
            move._message_log(
                body=_('Invoice imported from TCA Peppol network (ID: %s).', tca_id)
            )
        else:
            _logger.warning('TCA: _create_document_from_attachment returned empty for id %s', tca_id)
            move = self.env['account.move'].create({
                'move_type': 'in_invoice',
                'journal_id': journal.id,
                'company_id': company.id,
                'tca_invoice_uuid': tca_id,
                'tca_move_state': 'inbound_received',
                'tca_is_inbound': True,
                'tca_inbound_status': 'pending',
                'ref': f'TCA-{tca_id}',
            })
            attachment.write({'res_model': 'account.move', 'res_id': move.id})
            move._message_log(body=_(
                'TCA Peppol: import returned empty for inbound invoice (ID: %s). '
                'The raw XML is attached. Please fill in the details manually.', tca_id
            ))

        return move

    # ──────────────────────────────────────────────────────────────────────────
    # MANUAL RESEND
    # ──────────────────────────────────────────────────────────────────────────

    def action_tca_resend(self):
        """
        Resend a failed/rejected invoice to TCA. Always opens the Send &
        Print wizard for a fresh submission — TCA's dedicated /resubmit/
        endpoint only accepts the legacy XML/S3 (source_file_path) contract,
        which this module no longer uses (see docs/PORTING_17_vs_19.md P2.2).
        A resend is simply a new inline-JSON POST /invoices/ with the same
        invoice_number (_tca_build_submission_id) — relies on TCA's own
        duplicate handling if it turns out this exact submission already
        made it through despite the prior error.
        """
        self.ensure_one()
        if self.tca_move_state not in ('error', 'rejected'):
            raise UserError(_(
                'Invoice %s cannot be resent — current TCA state is "%s".',
                self.name, self.tca_move_state
            ))

        # Do NOT pre-mutate tca_move_state / tca_submission_error here: if the
        # user opens the wizard and then cancels, the previous error context
        # should remain visible. The wizard's submission code clears these
        # fields on success (and the Send-eligible / enable_tca checks both
        # accept 'error' and 'rejected' states, so no reset is needed).
        return {
            'name': _('Send & Print'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.move.send',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'active_ids': [self.id],
                'active_model': 'account.move',
                'default_move_ids': [self.id],
            },
        }

    # ──────────────────────────────────────────────────────────────────────────
    # INBOUND: ACCEPT / REJECT
    # ──────────────────────────────────────────────────────────────────────────

    def action_tca_accept_inbound(self):
        """
        Accept an inbound invoice received via TCA Peppol.
        Posts the vendor bill (commits to ledger) and marks it accepted.
        Only available on draft inbound invoices with tca_is_inbound=True.
        """
        self.ensure_one()
        if not self.tca_is_inbound:
            raise UserError(_('This action is only available for invoices received via TCA Peppol.'))
        if self.state != 'draft':
            raise UserError(_('Only draft invoices can be accepted. Current state: %s', self.state))

        self.action_post()
        self.tca_inbound_status = 'accepted'
        self._message_log(body=_('Inbound invoice accepted and posted to ledger.'))

        # TODO: When TCA adds an Invoice Response endpoint, send AP (Accepted)
        # response back to the seller via:
        #   api_svc.send_invoice_response(company, tca_id, response_code='AP')

        return True

    def action_tca_reject_inbound(self):
        """
        Reject an inbound invoice received via TCA Peppol.
        Opens a simple wizard to capture the rejection reason,
        then cancels the vendor bill.
        """
        self.ensure_one()
        if not self.tca_is_inbound:
            raise UserError(_('This action is only available for invoices received via TCA Peppol.'))
        if self.state not in ('draft', 'posted'):
            raise UserError(_('Cannot reject an invoice in state: %s', self.state))

        return {
            'name': _('Reject Inbound Invoice'),
            'type': 'ir.actions.act_window',
            'res_model': 'tca.inbound.reject.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_move_id': self.id,
            },
        }

    def _tca_apply_rejection(self, reason):
        """
        Apply rejection to an inbound invoice. Called by the reject wizard.
        If the bill was already posted, resets to draft first, then cancels.
        """
        self.ensure_one()
        self.tca_inbound_status = 'rejected'
        self.tca_reject_reason = reason

        if self.state == 'posted':
            self.button_draft()
        if self.state == 'draft':
            self.button_cancel()

        self._message_log(body=_(
            'Inbound invoice rejected.\nReason: %s', reason
        ))

        # TODO: When TCA adds an Invoice Response endpoint, send RE (Rejected)
        # response back to the seller via:
        #   api_svc.send_invoice_response(company, tca_id, response_code='RE',
        #                                  reason=reason)

        _logger.info('TCA: inbound invoice %s rejected. Reason: %s', self.name, reason)
