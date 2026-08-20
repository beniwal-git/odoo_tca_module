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
_get_import_file_type: PINT AE CustomizationID routed to our builder for inbound XML.
"""

import logging
import re
import uuid
from base64 import b64encode

from odoo import _, api, fields, models
from odoo.addons.account_tca_peppol.constants import (
    ANON_BUYER_PIDS,
    PINT_AE_CUSTOMIZATION_ID,
    PINT_AE_CUSTOMIZATION_IDS,
    PINT_AE_PROFILE_ID,
    PINT_AE_SELFBILLING_CUSTOMIZATION_ID,
    PINT_AE_SELFBILLING_PROFILE_ID,
    UAE_EAS,
    UAE_EMIRATES,
    UAE_STATE_CODE_TO_EMIRATE,
)
from odoo.addons.account_tca_peppol.services.tca_api import TcaTransientError, TcaValidationError
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# States that block cancellation (document is in-flight or completed)
_CANCEL_BLOCKED_STATES = frozenset(['processing', 'delivered', 'buyer_confirmed'])

# BTAE-03: Credit note reason codes (AE-CreditReason code list per UAE VAT Decree-Law)
CREDIT_NOTE_REASONS = [
    ('DL8.61.1.A', 'DL8.61.1.A — Supply was cancelled'),
    ('DL8.61.1.B', 'DL8.61.1.B — Tax treatment changed'),
    ('DL8.61.1.C', 'DL8.61.1.C — Consideration altered / Bad debt relief'),
    ('DL8.61.1.D', 'DL8.61.1.D — Goods/services returned'),
    ('DL8.61.1.E', 'DL8.61.1.E — Tax charged or applied in error'),
    ('VD', 'VD — Volume Discount (no preceding invoice reference required)'),
]

# TCA Invoice Status integer codes (from API spec)
TCA_STATUS_PROCESSING = 1
TCA_STATUS_COMPLETED = 2
TCA_STATUS_REJECTED = 3
TCA_STATUS_FAILED = 4

# TCA C3 MLS status integer codes
TCA_C3_ACCEPTED = 4  # Delivered to buyer AP
TCA_C3_REJECTED = 5
TCA_C3_UNABLE_TO_DELIVER = 6

# TCA C5 MLS status integer codes
TCA_C5_ACCEPTED = 4  # Buyer confirmed receipt


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
        index='btree_not_null',  # join key for the webhook + both polling crons
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
        help='The invoice_number actually sent to TCA on the most recent attempt. '
        'Per UAE FTA compliance, each submission must carry a unique ID; we '
        'compose <record name>-<uuid8> per attempt. Differs from this '
        "record's name — Odoo keeps the canonical invoice number, TCA tracks "
        'each submission with its own ID.',
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

    # Per-document opt-out. Default ON so every issued document is e-invoiced
    # unless the user explicitly turns it off on the form. When False the move
    # behaves like a plain pre-module invoice: PINT AE fields are hidden, the
    # _post() mandatory-field gate is skipped, the AED currency lock is lifted,
    # and the move is never send-eligible (no "Submit via TCA Peppol"). Only
    # consulted for documents WE issue (out_* and self-bills); irrelevant for
    # inbound and plain vendor bills. copy=False so reversals/duplicates start
    # fresh at the default ON.
    tca_create_einvoice = fields.Boolean(
        string='Create E-Invoice',
        default=True,
        copy=False,
        help='When on, this document is validated and submitted as a UAE PINT AE '
        'e-invoice via TCA Peppol. Turn off to issue a plain invoice without '
        'e-invoicing — the PINT AE fields and compliance checks are skipped.',
    )

    # ── Currency lock — AED only for TCA-issued documents ────────────────────
    # UAE PINT AE mandate: every PINT AE document we ISSUE is denominated in
    # AED. No foreign-currency support (no BTAE-20 second-TaxTotal-in-AED
    # handling); the journal/company currency may be anything — we override
    # it on the move. Two cases qualify as "we issue":
    #   · out_invoice / out_refund (our customer invoices/credit notes)
    #   · in_invoice / in_refund on a self-billing journal (we issue on
    #     the supplier's behalf — UC4/UC5).
    # Plain vendor bills (in_*) on non-self-billing journals are untouched —
    # they're inbound from the supplier in the supplier's currency.
    #
    # Enforcement layers:
    #   1. Compute below — sets AED on create / journal change / TCA toggle.
    #   2. View readonly (account_move_views.xml) — locks the field on form.
    #   3. _tca_check_document() — hard-fails _post if currency drifted.
    #
    # The @api.depends here REPLACES the parent's; redeclare upstream's
    # ('journal_id', 'statement_line_id') so super's logic still re-fires on
    # journal change for non-TCA moves.
    @api.depends(
        'journal_id',
        'statement_line_id',
        'journal_id.is_self_billing',
        'move_type',
        'company_id.tca_is_active',
        'tca_create_einvoice',
    )
    def _compute_currency_id(self):
        super()._compute_currency_id()
        aed = self.env.ref('base.AED')
        for move in self:
            if not (move.company_id.tca_is_active and move.tca_create_einvoice):
                continue
            is_issued_by_us = move.move_type in ('out_invoice', 'out_refund') or (
                move.move_type in ('in_invoice', 'in_refund') and move.journal_id.is_self_billing
            )
            if is_issued_by_us:
                move.currency_id = aed

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

    # PINT AE predefined endpoints + anonymous-buyer set are defined in
    # `account_tca_peppol.constants` (PREDEFINED_DEEMED / PREDEFINED_NOT_SUBJECT
    # / PREDEFINED_EXPORT_NO_PEPPOL / LEGACY_PLACEHOLDER_PARTICIPANT /
    # ANON_BUYER_PIDS) — imported at top of file.

    tca_buyer_participant_id = fields.Char(
        string='Buyer Participant ID',
        copy=True,
        compute='_compute_tca_buyer_participant_id',
        store=True,
        readonly=False,
        help=(
            'Peppol Participant ID of the buyer (receiver of this invoice).\n'
            'For UAE buyers: their Peppol Participant ID (10 digits) or full TRN (15 digits).\n'
            'For non-UAE / out-of-scope cases, PINT AE BIS 1.5.3 mandates a predefined endpoint:\n'
            '  9900000097 — Deemed Supply (BTAE-02 pos 2)\n'
            '  9900000098 — Buyer not subject to UAE e-invoicing\n'
            '  9900000099 — Export, receiver not registered in Peppol (BTAE-02 pos 8)\n'
            'Auto-populated from the customer record + transaction flags; editable per invoice.'
        ),
    )
    tca_seller_participant_id = fields.Char(
        string='Seller Participant ID',
        copy=True,
        compute='_compute_tca_seller_participant_id',
        store=True,
        readonly=False,
        help=(
            'Peppol Participant ID of the seller (issuer of this invoice in '
            'real-world terms).\n'
            "For outbound customer invoices: auto-populated from your company's "
            'Peppol Endpoint.\n'
            'For self-bills (in_* on a self-billing journal): auto-populated '
            "from the vendor's Peppol Endpoint. If the vendor has no Peppol "
            'routing (off-network foreign supplier), enter a 10-digit '
            'fallback identifier manually.\n'
            'Editable per invoice — overrides the partner-level value.'
        ),
    )
    tca_buyer_beneficiary_id = fields.Char(
        string='Buyer Beneficiary ID',
        copy=True,
        help=(
            'Buyer additional identification number.\n'
            'Required when the Free Trade Zone flag is set — PINT AE rule '
            'ibr-007-ae. Visible on the form only when Free Trade Zone is '
            'ticked. Emitted at cac:BuyerCustomerParty/cac:Party/'
            'cac:PartyIdentification/cbc:ID in the XML. Carries over to '
            'credit notes.'
        ),
    )

    # ── Delivery address (ibr-142-ae for E-commerce flag) ────────────────────
    # PINT AE requires a complete delivery address (Street + City +
    # CountrySubentity) under cac:Delivery/cac:DeliveryLocation/cac:Address
    # when the E-commerce flag is set. Auto-fill from the shipping party at
    # toggle time; user can override per invoice. Cleared when the flag is
    # unticked. Builder emits these values in the Delivery node when set.
    tca_delivery_street = fields.Char(
        string='Delivery Street',
        copy=True,
        help='Street name of the delivery address. Required when E-commerce '
        'flag is set (ibr-142-ae). Auto-filled from the shipping party. '
        'Carries over to credit notes so a reversal keeps the original '
        'delivery context.',
    )
    tca_delivery_city = fields.Char(
        string='Delivery City',
        copy=True,
        help='City of the delivery address. Required when E-commerce flag '
        'is set (ibr-142-ae). Auto-filled from the shipping party. '
        'Carries over to credit notes.',
    )
    tca_delivery_state_id = fields.Many2one(
        'res.country.state',
        string='Delivery State / Emirate',
        copy=True,
        domain="[('country_id.code', '=', 'AE')]",
        help='State / Emirate of the delivery address. Required when '
        'E-commerce flag is set (ibr-142-ae). Auto-filled from the '
        'shipping party. Carries over to credit notes.',
    )

    @api.model
    def _tca_resolve_buyer_participant_id(self, partner, flags):
        """
        Apply BIS 1.5.3 routing to determine the Peppol Participant ID for a
        buyer. Pure function — does NOT mutate any record. Called by both the
        @api.depends compute below and the @api.onchange handler in this model,
        so the routing rules live in exactly one place.

        Routing precedence:
          1. Deemed Supply OR Export flag set (BTAE-02 pos 2 / pos 8)
                                                      → '' (user fills manually)
          2. Partner without country                 → ''
          3. Non-UAE (foreign) party                 → '' (off Peppol; manual)
          4. UAE party                               → peppol_endpoint (or '')

        Args:
            partner:  res.partner record (typically the commercial_partner_id)
            flags:    8-char BTAE-02 binary string, e.g. '01000000' = Deemed Supply

        Returns:
            The resolved participant ID string. Never None.
        """
        flags = (flags or '00000000').ljust(8, '0')

        # (1) Deemed Supply (pos 2) or Export (pos 8) — the counterparty is off
        # the UAE Peppol network. Leave blank so the user enters the Participant
        # ID manually (Deemed: typically 9900000097; Export: the foreign ID).
        if flags[1] == '1' or flags[7] == '1':
            return ''

        if not partner.country_id:
            return ''

        # (3) Non-UAE party — off the UAE Peppol network. Leave blank for the
        # user to enter the Participant ID manually (no auto endpoint).
        if not partner._tca_is_uae_party():
            return ''

        # (4) UAE party — 10-digit Peppol Participant ID. Do NOT fall back to
        # vat (the TRN is a 15-digit tax id, not a routing endpoint). Missing
        # endpoint surfaces at post-time validation.
        return partner.peppol_endpoint or ''

    @api.depends(
        'partner_id',
        'partner_id.peppol_endpoint',
        'partner_id.country_id',
        'tca_transaction_type_flags',
        'journal_id.is_self_billing',
        'company_id',
        'company_id.partner_id.peppol_endpoint',
    )
    def _compute_tca_buyer_participant_id(self):
        """
        Auto-populate Buyer Participant ID per BIS 1.5.3. The compute
        re-evaluates when flags or party data change ONLY if the current
        value is a predefined/legacy endpoint (i.e. it was auto-set, not
        user-set). Custom values entered by the user are preserved.

        Party resolution:
          · Outbound out_invoice / out_refund   → buyer = customer (partner_id)
          · Self-bill in_invoice / in_refund    → buyer = our company
            (we are the actual buyer; the vendor sits in the supplier slot
            after the BIS3 self-billing swap)
          · Plain vendor bills                  → buyer = customer (legacy;
            these aren't TCA-eligible for outbound but the field is kept
            consistent)

        Actual routing logic lives in _tca_resolve_buyer_participant_id —
        shared with the @api.onchange so the rules cannot drift.
        """
        for move in self:
            # Inline the self-bill check rather than read move.tca_is_self_billing
            # — another compute may not have run yet under the same trigger.
            is_self_bill = (
                move.move_type in ('in_invoice', 'in_refund') and move.journal_id.is_self_billing
            )
            # Self-bill: the buyer is ALWAYS our own organisation and its
            # electronic address MUST be our TIN — TCA rejects otherwise
            # ("buyer.electronic_address must match your organization's TIN").
            # FORCE it; never preserve a stale endpoint left over from when the
            # move was a plain vendor bill (before the journal was flagged
            # self-billing), which would leave the vendor's endpoint in the
            # buyer slot. No legitimate user override exists here.
            if is_self_bill:
                buyer = move.company_id.partner_id.commercial_partner_id
                move.tca_buyer_participant_id = self._tca_resolve_buyer_participant_id(
                    buyer,
                    move.tca_transaction_type_flags,
                )
                continue
            # Non-self-bill: preserve ANY user-set value. We no longer auto-set
            # FTA predefined endpoints (foreign buyers resolve to '' for the
            # user to fill), so a non-blank value is always the user's — never
            # overwrite it, even if it happens to be a predefined 9900000xxx.
            current = (move.tca_buyer_participant_id or '').strip()
            if current:
                continue
            buyer = move.partner_id.commercial_partner_id
            move.tca_buyer_participant_id = self._tca_resolve_buyer_participant_id(
                buyer,
                move.tca_transaction_type_flags,
            )

    @api.depends(
        'partner_id',
        'partner_id.peppol_endpoint',
        'journal_id.is_self_billing',
        'company_id',
        'company_id.partner_id.peppol_endpoint',
    )
    def _compute_tca_seller_participant_id(self):
        """
        Auto-populate Seller Participant ID — the participant ID that lands
        in the AccountingSupplierParty's EndpointID in XML.

        Party resolution:
          · Outbound out_invoice / out_refund   → seller = our company
          · Self-bill in_invoice / in_refund    → seller = vendor (partner_id)
            (vendor sits in the supplier slot after the BIS3 swap)

        Preserves user-set values (only overwrites when blank). No BIS 1.5.3
        government-fallback routing for the seller side — supplier-side
        predefined endpoints aren't part of the spec; if the vendor has no
        peppol_endpoint the field stays blank and the user enters a fallback.
        """
        for move in self:
            current = (move.tca_seller_participant_id or '').strip()
            # Inline the self-bill check rather than read move.tca_is_self_billing
            # — another compute may not have run yet under the same trigger.
            is_self_bill = (
                move.move_type in ('in_invoice', 'in_refund') and move.journal_id.is_self_billing
            )
            if is_self_bill:
                # Seller is the vendor. Drop a stale company endpoint left in
                # the seller slot from the plain-vendor-bill state (before the
                # journal was flagged self-billing), but keep a user-typed
                # fallback for vendors that have no Peppol endpoint of their own.
                company_ep = (
                    move.company_id.partner_id.commercial_partner_id.peppol_endpoint or ''
                ).strip()
                if current and current != company_ep:
                    continue
                seller = move.partner_id.commercial_partner_id
                move.tca_seller_participant_id = seller.peppol_endpoint or ''
                continue
            # Non-self-bill: seller is our company; preserve any user value.
            if current:
                continue
            seller = move.company_id.partner_id.commercial_partner_id
            move.tca_seller_participant_id = seller.peppol_endpoint or ''

    # ── PINT AE XML fields ────────────────────────────────────────────────────

    # ── BTAE-02 transaction-type flag booleans (user-facing checkboxes) ──────
    # These 7 booleans represent positions 1-7 of the BTAE-02 ProfileExecutionID
    # binary string. Position 8 (Export) is auto-detected by the XML builder
    # from the buyer's country — never user-set.
    # tca_transaction_type_flags (below) is COMPUTED from these.

    tca_flag_free_trade_zone = fields.Boolean(
        string='Free Trade Zone',
        copy=True,
        help='Tick if the supply involves a UAE Free Trade Zone (BTAE-02 position 1).',
    )
    tca_flag_deemed_supply = fields.Boolean(
        string='Deemed Supply',
        copy=True,
        help='Tick for deemed-supply scenarios (e.g. goods for own use). '
        'BTAE-02 position 2. Buyer participant ID auto-switches to predefined endpoint 9900000097.',
    )
    tca_flag_margin_scheme = fields.Boolean(
        string='Margin Scheme',
        copy=True,
        help='Tick for second-hand goods / margin-scheme transactions (BTAE-02 position 3).',
    )
    tca_flag_summary_invoice = fields.Boolean(
        string='Summary Invoice',
        copy=True,
        help='Tick for an invoice consolidating multiple supplies over a period (BTAE-02 position 4). '
        'Requires Invoice Period Start/End.',
    )
    tca_flag_continuous_supply = fields.Boolean(
        string='Continuous Supply',
        copy=True,
        help='Tick for subscriptions / recurring supplies (BTAE-02 position 5). '
        'Requires Invoice Period Start/End, Contract Reference, and Billing Frequency.',
    )
    tca_flag_disclosed_agent = fields.Boolean(
        string='Disclosed Agent Billing',
        copy=True,
        help='Tick when invoicing as a disclosed agent on behalf of a principal '
        '(BTAE-02 position 6). Requires Principal TRN.',
    )
    tca_flag_ecommerce = fields.Boolean(
        string='E-commerce',
        copy=True,
        help='Tick for online-channel transactions (BTAE-02 position 7).',
    )
    tca_flag_export = fields.Boolean(
        string='Export',
        copy=True,
        help='Tick for an export supply to a buyer outside the UAE '
        '(BTAE-02 position 8). The counterparty is off the UAE Peppol '
        'network, so enter their Participant ID manually.',
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
        'tca_flag_free_trade_zone',
        'tca_flag_deemed_supply',
        'tca_flag_margin_scheme',
        'tca_flag_summary_invoice',
        'tca_flag_continuous_supply',
        'tca_flag_disclosed_agent',
        'tca_flag_ecommerce',
        'tca_flag_export',
    )
    def _compute_tca_show_special_flags(self):
        """Auto-expand the section whenever any flag is on. Preserves a
        manual True so the user can keep it open with no flags ticked yet,
        and preserves a manual False (the default) when no flag is on."""
        for move in self:
            if any(
                (
                    move.tca_flag_free_trade_zone,
                    move.tca_flag_deemed_supply,
                    move.tca_flag_margin_scheme,
                    move.tca_flag_summary_invoice,
                    move.tca_flag_continuous_supply,
                    move.tca_flag_disclosed_agent,
                    move.tca_flag_ecommerce,
                    move.tca_flag_export,
                )
            ):
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
            'Composed automatically from the seven flag checkboxes above (positions 1-7); '
            'position 8 (Export) is auto-set in the XML output when the buyer is non-UAE. '
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
        Export (position 8) is a manual choice like the others — the user ticks
        it for a supply to a buyer outside the UAE."""
        for move in self:
            move.tca_transaction_type_flags = ''.join(
                (
                    '1' if move.tca_flag_free_trade_zone else '0',
                    '1' if move.tca_flag_deemed_supply else '0',
                    '1' if move.tca_flag_margin_scheme else '0',
                    '1' if move.tca_flag_summary_invoice else '0',
                    '1' if move.tca_flag_continuous_supply else '0',
                    '1' if move.tca_flag_disclosed_agent else '0',
                    '1' if move.tca_flag_ecommerce else '0',
                    '1' if move.tca_flag_export else '0',  # Export (BTAE-02 pos 8)
                )
            )

    tca_credit_note_reason = fields.Selection(
        selection=CREDIT_NOTE_REASONS,
        string='Credit Note Reason',
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
        compute='_compute_tca_derived_flag_booleans',
        string='Is Agent Billing',
    )
    tca_is_summary_or_continuous = fields.Boolean(
        compute='_compute_tca_derived_flag_booleans',
    )
    tca_is_continuous = fields.Boolean(
        compute='_compute_tca_derived_flag_booleans',
    )
    # tca_is_export mirrors the manual Export flag (BTAE-02 pos 8).
    tca_is_export = fields.Boolean(compute='_compute_tca_is_export')
    tca_buyer_is_uae = fields.Boolean(compute='_compute_tca_buyer_is_uae')

    @api.depends(
        'tca_flag_disclosed_agent',
        'tca_flag_summary_invoice',
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
        """Export is a manual user choice (BTAE-02 pos 8). Ticking it reveals
        the Export Declaration Number field and sets the export bit."""
        for move in self:
            move.tca_is_export = move.tca_flag_export

    @api.depends('partner_id', 'partner_id.commercial_partner_id.country_id')
    def _compute_tca_buyer_is_uae(self):
        for move in self:
            partner = move.partner_id.commercial_partner_id
            move.tca_buyer_is_uae = bool(partner._tca_is_uae_party())

    tca_principal_id = fields.Char(
        string='Principal TRN',
        copy=True,
        help=(
            'BTAE-14: Tax Registration Number of the Principal in a Disclosed Agent Billing '
            'arrangement (UC5 / UC13).\n'
            'Mandatory when BTAE-02 position 6 = 1 (Disclosed Agent flag set).\n'
            'Carried over to credit notes — same principal usually applies.'
        ),
    )
    # ── Invoice Type Code (6 PINT AE variants) ───────────────────────────────
    # Stored value is the UNCL1001 code emitted directly in XML:
    #   380 = Tax Invoice               381 = Tax Credit Note
    #   389 = Self-Billing Tax Invoice  261 = Self-Billing Tax Credit Note
    #   480 = Out-of-Scope Invoice       81 = Out-of-Scope Credit Note
    # Self-billing has its own type code (389/261) per current PINT AE spec.
    # The local schematron in `data/schematron/` has been patched to include
    # 389/261 in `ibr-cl-01`'s allowed list.

    _TYPE_INVOICE_TO_REFUND = {'380': '381', '389': '261', '480': '81'}
    _TYPE_REFUND_TO_INVOICE = {'381': '380', '261': '389', '81': '480'}

    tca_invoice_type_code = fields.Selection(
        selection=[
            ('380', '380 — Tax Invoice'),
            ('381', '381 — Tax Credit Note'),
            ('389', '389 — Self-Billing Tax Invoice'),
            ('261', '261 — Self-Billing Tax Credit Note'),
            ('480', '480 — Out-of-Scope Invoice'),
            ('81', '81 — Out-of-Scope Credit Note'),
        ],
        string='Invoice Type Code',
        compute='_compute_tca_invoice_type_code',
        store=True,
        readonly=False,
        copy=True,
        help=(
            'PINT AE UNCL1001 document type code (emitted as-is in XML).\n'
            '380: Tax Invoice — standard sale with UAE VAT\n'
            '381: Tax Credit Note — reverses a 380\n'
            '389: Self-Billing Tax Invoice (buyer issues for supplier — UC4)\n'
            '261: Self-Billing Tax Credit Note (buyer-issued — UC5)\n'
            '480: Out-of-Scope Invoice — not subject to UAE VAT\n'
            '81: Out-of-Scope Credit Note — reverses a 480\n'
            'Self-billing also emits the urn:peppol:pint:selfbilling-1@ae-1 '
            'CustomizationID and triggers the BIS3 supplier/customer swap.'
        ),
    )

    # ── User-facing OOS toggle ────────────────────────────────────────────────
    # Drives _compute_tca_invoice_type_code: when ticked, the resolved code
    # flips to 480 (Commercial Invoice) for invoices or 81 (OOS Credit Note)
    # for refunds. Default off — most invoices are Tax Invoices subject to
    # UAE VAT. Shown on the invoice form; hidden / readonly for inbound moves
    # (the OOS classification of a received document is fixed by the seller).
    tca_is_out_of_scope = fields.Boolean(
        string='Out of Scope (Commercial Invoice)',
        compute='_compute_tca_is_out_of_scope',
        store=True,
        readonly=False,
        copy=True,
        default=False,
        recursive=True,
        help='Tick to issue a Commercial Invoice — a document NOT subject to '
        'UAE VAT (PINT AE code 480, or 81 for credit notes). '
        'Examples: financial services, supplies outside the UAE VAT scope, '
        'transactions with non-residents. Leave unticked for standard Tax '
        'Invoices (codes 380 / 381). On a credit note that reverses an '
        'invoice this value is auto-mirrored from the original (and is '
        'readonly in the UI) so the PINT AE pair stays valid: 381 reverses '
        '380, 81 reverses 480.',
    )

    @api.depends('move_type', 'reversed_entry_id', 'reversed_entry_id.tca_is_out_of_scope')
    def _compute_tca_is_out_of_scope(self):
        """Mirror the original invoice's OOS flag onto a reversing credit note.

        PINT AE pairs invoice types by direction: 381 reverses 380, 81 reverses
        480. Letting the user flip OOS independently on the credit note would
        produce a 380→81 or 480→381 mismatch and a non-compliant XML.

        For credit notes WITH a `reversed_entry_id` we force-mirror the source.
        For everything else (standalone credit notes, plain invoices) the
        compute leaves the field untouched — `readonly=False` keeps it freely
        editable in the UI, and the view adds `readonly="reversed_entry_id"`
        so the toggle is also locked for reversal credit notes.
        """
        for move in self:
            if move.reversed_entry_id and move.move_type in ('out_refund', 'in_refund'):
                move.tca_is_out_of_scope = move.reversed_entry_id.tca_is_out_of_scope

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
            return None

        # ── (1) Forbidden BTAE-02 flags ──────────────────────────────────────
        self.tca_flag_deemed_supply = False
        self.tca_flag_margin_scheme = False
        self.tca_flag_summary_invoice = False

        # ── (2) Strip forbidden taxes ────────────────────────────────────────
        def _is_forbidden_for_oos(tax):
            cat = tax.tca_tax_category or ''
            # S/AE/N are all standard-rated variants → forbidden on OOS.
            if cat in ('S', 'AE', 'N'):
                return True
            return tax.amount_type == 'percent' and tax.amount != 0.0

        removed_per_line = []
        for line in self._tca_product_lines():
            forbidden = line.tax_ids.filtered(_is_forbidden_for_oos)
            if not forbidden:
                continue
            label = line.name or (line.product_id and line.product_id.name) or _('(unnamed line)')
            removed_per_line.append(f'{label} — {", ".join(forbidden.mapped("name"))}')
            line.tax_ids = line.tax_ids - forbidden

        # ── (3) Auto-apply OOS tax to lines without any tax remaining ───────
        # Lines that still carry a (now zero-rated / exempt) tax are left
        # alone — the user's existing setup is assumed valid. The OOS tax is
        # auto-created in the company's chart if missing.
        type_tax_use = 'sale' if self.move_type in ('out_invoice', 'out_refund') else 'purchase'
        oos_tax = self.env['account.tax']._tca_ensure_oos_tax(self.company_id, type_tax_use)

        applied_to_lines = []
        for line in self._tca_product_lines():
            if line.tax_ids:
                continue  # Has a tax already (must be zero-rated/exempt after strip) — keep
            line.tax_ids = oos_tax
            label = line.name or (line.product_id and line.product_id.name) or _('(unnamed line)')
            applied_to_lines.append(label)

        # ── Assemble single combined warning if any of the three acted ──────
        sections = []
        if removed_per_line:
            sections.append(
                _(
                    'Removed taxes (UAE FTA: Out-of-Scope invoices cannot carry VAT):\n%s',
                    '\n'.join(f'  • {r}' for r in removed_per_line),
                )
            )
        if applied_to_lines:
            sections.append(
                _(
                    'Auto-applied "%(name)s" (Out-of-Scope, 0%% rate, category O) to '
                    'satisfy PINT AE rule ibr-sr-58 (line tax category is mandatory):\n%(list)s',
                    name=oos_tax.name,
                    list='\n'.join(f'  • {label}' for label in applied_to_lines),
                )
            )

        if sections:
            return {
                'warning': {
                    'title': _('Tax adjustments for Out-of-Scope invoice'),
                    'message': '\n\n'.join(sections),
                }
            }

    # Computed booleans for view visibility (Odoo 17 cannot do slice/in on Selection in invisible)
    tca_show_credit_note_fields = fields.Boolean(
        compute='_compute_tca_type_visibility',
        store=False,
    )
    tca_is_out_of_scope_type = fields.Boolean(
        compute='_compute_tca_type_visibility',
        store=False,
    )

    @api.depends('move_type', 'tca_is_out_of_scope', 'journal_id.is_self_billing')
    def _compute_tca_invoice_type_code(self):
        """
        Resolve the PINT AE document type code from move direction, OOS
        toggle, and the journal's self-billing flag:

            regular invoice (out_invoice / in_invoice, not OOS, not self-bill)
                                                  → '380'
            regular CN (out_refund / in_refund, not OOS, not self-bill)
                                                  → '381'
            self-bill invoice (in_invoice + journal.is_self_billing)
                                                  → '389'
            self-bill CN (in_refund + journal.is_self_billing)
                                                  → '261'
            OOS invoice (any direction + tca_is_out_of_scope, not self-bill)
                                                  → '480'
            OOS CN    (any direction + tca_is_out_of_scope, not self-bill)
                                                  → '81'

        Self-billing × OOS is mutually exclusive (no `480_sb`/`81_sb` in
        PINT AE) — self-billing wins. Inbound-received moves are not
        touched (importer is the source of truth).
        """
        for move in self:
            if move.tca_is_inbound:
                continue
            is_self_bill = (
                move.move_type in ('in_invoice', 'in_refund') and move.journal_id.is_self_billing
            )
            if move.move_type in ('out_invoice', 'in_invoice'):
                if is_self_bill:
                    move.tca_invoice_type_code = '389'
                else:
                    move.tca_invoice_type_code = '480' if move.tca_is_out_of_scope else '380'
            elif move.move_type in ('out_refund', 'in_refund'):
                if is_self_bill:
                    move.tca_invoice_type_code = '261'
                else:
                    move.tca_invoice_type_code = '81' if move.tca_is_out_of_scope else '381'
            else:
                move.tca_invoice_type_code = False

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
            return None
        is_refund = self.move_type in ('out_refund', 'in_refund')
        if is_refund and code in self._TYPE_INVOICE_TO_REFUND:
            self.tca_invoice_type_code = self._TYPE_INVOICE_TO_REFUND[code]
            return {
                'warning': {
                    'title': _('Invalid Type Code'),
                    'message': _(
                        'Credit notes cannot use an invoice type code. Reset to %s.',
                        self.tca_invoice_type_code,
                    ),
                }
            }
        if not is_refund and code in self._TYPE_REFUND_TO_INVOICE:
            self.tca_invoice_type_code = self._TYPE_REFUND_TO_INVOICE[code]
            return {
                'warning': {
                    'title': _('Invalid Type Code'),
                    'message': _(
                        'Invoices cannot use a credit note type code. Reset to %s.',
                        self.tca_invoice_type_code,
                    ),
                }
            }

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
        for vals in vals_list:
            move_type = vals.get('move_type')
            type_code = vals.get('tca_invoice_type_code')
            if not move_type or not type_code:
                continue
            is_refund = move_type in ('out_refund', 'in_refund')
            if is_refund and type_code in self._TYPE_INVOICE_TO_REFUND:
                vals['tca_invoice_type_code'] = self._TYPE_INVOICE_TO_REFUND[type_code]
            elif not is_refund and type_code in self._TYPE_REFUND_TO_INVOICE:
                vals['tca_invoice_type_code'] = self._TYPE_REFUND_TO_INVOICE[type_code]
        return super().create(vals_list)

    tca_is_self_billing = fields.Boolean(
        string='Self-Billing',
        compute='_compute_tca_is_self_billing',
        store=True,
        copy=True,
        help=(
            'UC4/UC5: True when invoice type is a self-billing variant — '
            'buyer issues the invoice on behalf of the supplier. '
            'Derived from tca_invoice_type_code (the _sb variants).\n'
            'Sets CustomizationID to selfbilling variant and ProfileID to selfbilling in PINT AE XML.'
        ),
    )

    @api.depends('tca_invoice_type_code')
    def _compute_tca_is_self_billing(self):
        # Self-bill = type code is one of the EN16931 self-billing variants
        # (389/261). The type code itself is the source of truth so an
        # inbound importer can override it from the received XML even when
        # we don't see the journal flag.
        for move in self:
            move.tca_is_self_billing = move.tca_invoice_type_code in ('389', '261')

    tca_contract_value = fields.Char(
        string='Contract Value',
        copy=True,
        help=(
            'BTAE-05: Contract value description for ContractDocumentReference/DocumentDescription.\n'
            'Example: "AED 1000000". Used in Continuous Supply invoices.'
        ),
    )
    tca_payment_means_code = fields.Selection(
        selection=[
            ('10', '10 — In cash'),
            ('30', '30 — Credit transfer'),
            ('42', '42 — Payment to bank account'),
            ('48', '48 — Bank card'),
            ('49', '49 — Direct debit'),
            ('57', '57 — Standing agreement'),
            ('ZZZ', 'ZZZ — Mutually defined'),
        ],
        string='Payment Means Code',
        copy=True,
        help=(
            'IBT-081 Payment Means Code (UNCL4461) emitted at '
            'cac:PaymentMeans/cbc:PaymentMeansCode.\n'
            'Required for every PINT AE document EXCEPT credit notes and '
            'Deemed Supply transactions (ibr-191-ae).'
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
            'BTAE-06: Frequency of billing for Continuous Supply invoices.\n'
            'Rendered as InvoicePeriod/Description. When "OTH", an Invoice Note is required.\n'
            'Carried over to credit notes for Continuous Supply.'
        ),
    )
    tca_export_declaration_number = fields.Char(
        string='Export Declaration No.',
        copy=True,
        help=(
            'BTAE-21: Export declaration number for Exports.\n'
            'Rendered as StatementDocumentReference/ID.\n'
            'Carried over to export credit notes — same declaration usually applies.'
        ),
    )
    tca_incoterms = fields.Char(
        string='Incoterms',
        size=3,
        copy=True,
        help=(
            'BTAE-22: Incoterms code for Exports.\n'
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
        string='Deliver-to Party TRN',
        copy=True,
        help='BTAE-23: TRN/TIN of the delivery recipient (for triangular sales).',
    )

    # ── Buyer Emirate (per invoice override) ──────────────────────────────────

    tca_buyer_emirate = fields.Selection(
        selection=[(e, e) for e in UAE_EMIRATES],
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

    @api.constrains('tca_buyer_participant_id', 'partner_id')
    def _check_tca_buyer_participant_id_format(self):
        # UAE Peppol Participant ID — strictly 10 digits starting with "1".
        # The 15-digit TRN is a separate identifier (PartyTaxScheme/CompanyID),
        # not a Peppol endpoint.
        # The 3 PINT AE predefined endpoints (BIS 1.5.3, 9900000097/98/99) start
        # with "99" so they don't match the regex — they bypass this check via
        # the ANON_BUYER_PIDS set.
        re_uae_format = re.compile(r'^1[0-9]{9}$')
        for move in self:
            pid = (move.tca_buyer_participant_id or '').strip()
            if not pid or pid in ANON_BUYER_PIDS:
                continue
            # For self-bills the buyer is our company (vendor is supplier
            # after the XML swap); enforce against that party's country.
            buyer = (
                move.company_id.partner_id.commercial_partner_id
                if move.tca_is_self_billing
                else move.partner_id.commercial_partner_id
            )
            if not buyer._tca_is_uae_party():
                continue
            if not re_uae_format.match(pid):
                raise ValidationError(
                    _(
                        '"Buyer Participant ID" for UAE customers must be either:\n'
                        '  • 10-digit Peppol Participant ID: starts with 1 (e.g. 1234567890), or\n'
                        '  • One of the PINT AE predefined endpoints (9900000097/98/99).\n'
                        'The 15-digit TRN goes in the customer\'s "Tax ID" field, not here.\n'
                        'Current: "%s".',
                        pid,
                    )
                )

    @api.constrains('tca_seller_participant_id', 'partner_id')
    def _check_tca_seller_participant_id_format(self):
        # Mirror the buyer-side format check. For self-bills the seller is
        # the vendor (partner_id); for outbound the seller is our company.
        # FTA predefined endpoints (9900000097/98/99) bypass via ANON_BUYER_PIDS.
        re_uae_format = re.compile(r'^1[0-9]{9}$')
        for move in self:
            pid = (move.tca_seller_participant_id or '').strip()
            if not pid or pid in ANON_BUYER_PIDS:
                continue
            seller = (
                move.partner_id.commercial_partner_id
                if move.tca_is_self_billing
                else move.company_id.partner_id.commercial_partner_id
            )
            if not seller._tca_is_uae_party():
                continue
            if not re_uae_format.match(pid):
                raise ValidationError(
                    _(
                        '"Seller Participant ID" for UAE sellers must be either:\n'
                        '  • 10-digit Peppol Participant ID: starts with 1 (e.g. 1234567890), or\n'
                        '  • One of the PINT AE predefined endpoints (9900000097/98/99).\n'
                        'The 15-digit TRN goes in the "Tax ID" field, not here.\n'
                        'Current: "%s".',
                        pid,
                    )
                )

    @api.constrains('tca_transaction_type_flags')
    def _check_tca_transaction_type_flags_format(self):
        for move in self:
            flags = (move.tca_transaction_type_flags or '').strip()
            if not flags:
                continue  # required-check handled at posting
            if not self._RE_FLAGS_8.match(flags):
                raise ValidationError(
                    _(
                        '"Transaction Type Flags" must be exactly 8 digits, each 0 or 1. '
                        'Example: "00000000" for standard, "00000001" for export. Current: "%s".',
                        flags,
                    )
                )

    @api.constrains('tca_principal_id')
    def _check_tca_principal_id_format(self):
        for move in self:
            pid = (move.tca_principal_id or '').strip()
            if not pid:
                continue
            if not self._RE_TRN_15.match(pid):
                raise ValidationError(
                    _(
                        '"Principal TRN" must be exactly 15 digits. Current: "%s".',
                        pid,
                    )
                )

    @api.constrains('tca_delivery_party_trn')
    def _check_tca_delivery_party_trn_format(self):
        for move in self:
            trn = (move.tca_delivery_party_trn or '').strip()
            if not trn:
                continue
            if not self._RE_TRN_15.match(trn):
                raise ValidationError(
                    _(
                        '"Deliver-to Party TRN" must be exactly 15 digits. Current: "%s".',
                        trn,
                    )
                )

    @api.depends('partner_id', 'partner_id.tca_emirate', 'partner_id.state_id')
    def _compute_tca_buyer_emirate(self):
        for move in self:
            if move.tca_buyer_emirate:
                continue  # user-set or previously computed — preserve
            partner = move.partner_id.commercial_partner_id
            emirate = partner._tca_emirate()
            if emirate in UAE_EMIRATES:
                move.tca_buyer_emirate = emirate

    # ── Buyer Legal Registration (per invoice override) ──────────────────────

    tca_buyer_legal_id_type = fields.Selection(
        selection=[
            ('TL', 'Trade License (Commercial)'),
            ('EID', 'Emirates ID'),
            ('PAS', 'Passport'),
            ('CD', 'Cabinet Decision'),
        ],
        string='Buyer Legal ID Type',
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
        string='Buyer Issuing Authority',
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
        string='Buyer Passport Country',
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
            partner = move.partner_id.commercial_partner_id
            # Each field: don't overwrite if user already set on this invoice
            if not move.tca_buyer_legal_id_type and partner.tca_legal_id_type:
                move.tca_buyer_legal_id_type = partner.tca_legal_id_type
            if not move.tca_buyer_trade_license:
                move.tca_buyer_trade_license = (
                    partner.tca_trade_license or partner.company_registry or partner.vat or False
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

        Preserves user-edited values on the invoice (only fills empty fields).
        """
        if not self.partner_id:
            return

        partner = self.partner_id.commercial_partner_id

        # ── Buyer Participant ID (BIS 1.5.3 routing) ──────────────────────────
        # Same routing as the compute — both delegate to the shared helper
        # so the rules live in one place and cannot drift apart. For
        # self-bills the buyer is OUR company (vendor is supplier after the
        # XML swap); resolve against company.partner_id in that case.
        buyer_party = (
            self.company_id.partner_id.commercial_partner_id
            if self.tca_is_self_billing
            else partner
        )
        # Preserve ANY non-blank value — it is always the user's. Predefined
        # 9900000xxx IDs are NOT treated as "auto-set" here: nothing auto-sets
        # them any more (foreign / deemed buyers resolve to '' for manual
        # entry), so membership in ANON_BUYER_PIDS no longer implies we wrote
        # it. Testing it here would silently discard a hand-typed 9900000098 on
        # the next partner edit — and would contradict the compute above, which
        # preserves it. The elif still refreshes a stale endpoint when the
        # partner itself changed to a foreign party.
        current_pid = (self.tca_buyer_participant_id or '').strip()
        if not current_pid:
            self.tca_buyer_participant_id = self._tca_resolve_buyer_participant_id(
                buyer_party,
                self.tca_transaction_type_flags,
            )
        elif buyer_party.country_id and not buyer_party._tca_is_uae_party():
            # Switched to a foreign counterparty — clear the stale auto-filled
            # endpoint left from a previous UAE partner. The buyer is off the
            # UAE Peppol network, so the user enters the ID manually.
            self.tca_buyer_participant_id = ''

        # ── Buyer Emirate ─────────────────────────────────────────────────────
        if not self.tca_buyer_emirate:
            emirate = partner._tca_emirate()
            if emirate in UAE_EMIRATES:
                self.tca_buyer_emirate = emirate

        # ── Buyer Legal fields ────────────────────────────────────────────────
        if not self.tca_buyer_legal_id_type and partner.tca_legal_id_type:
            self.tca_buyer_legal_id_type = partner.tca_legal_id_type
        if not self.tca_buyer_trade_license:
            self.tca_buyer_trade_license = (
                partner.tca_trade_license or partner.company_registry or partner.vat or False
            )
        if not self.tca_buyer_legal_authority and partner.tca_legal_authority:
            self.tca_buyer_legal_authority = partner.tca_legal_authority
        if not self.tca_buyer_passport_country_id and partner.tca_passport_country_id:
            self.tca_buyer_passport_country_id = partner.tca_passport_country_id

        # Beneficiary ID — auto-fill from the new customer's VAT/TRN when
        # FTZ is on AND the field is currently empty (preserve user edits).
        if self.tca_flag_free_trade_zone and not self.tca_buyer_beneficiary_id:
            self.tca_buyer_beneficiary_id = self._tca_resolve_buyer_beneficiary_id() or False

        # Delivery Address — when E-commerce is on, refill empty fields
        # from the new shipping party (preserve user edits).
        if self.tca_flag_ecommerce:
            party = self.partner_shipping_id or self.partner_id
            if not self.tca_delivery_street and party.street:
                self.tca_delivery_street = party.street
            if not self.tca_delivery_city and party.city:
                self.tca_delivery_city = party.city
            if (
                not self.tca_delivery_state_id
                and party.state_id
                and party.state_id.country_id.code == 'AE'
            ):
                self.tca_delivery_state_id = party.state_id

    def _tca_resolve_buyer_beneficiary_id(self):
        """Return the buyer's TRN/TIN for BTAE-01 (per UAE FTA: Beneficiary
        ID is the buyer's tax registration number). Customer's `vat` for
        outbound, our company's `vat` for self-bills (the buyer is the
        company after the PINT AE party swap). Returns '' when the relevant
        party has no VAT set — user types one manually in that case."""
        self.ensure_one()
        is_self_bill = (
            self.move_type in ('in_invoice', 'in_refund') and self.journal_id.is_self_billing
        )
        buyer = (
            self.company_id.partner_id.commercial_partner_id
            if is_self_bill
            else self.partner_id.commercial_partner_id
        )
        return (buyer.vat or '').strip() if buyer else ''

    @api.onchange('tca_flag_ecommerce')
    def _onchange_tca_flag_ecommerce(self):
        """E-commerce on → auto-fill the three Delivery Address fields from
        the shipping party (or the main partner if no shipping address).
        E-commerce off → clear all three.

        Any value the user typed during the E-commerce session is discarded
        on untick — toggling the flag should land the fields where they
        would have been without it. Subsequent partner changes refill the
        empty fields only (see `_onchange_partner_id_tca`)."""
        if not self.tca_flag_ecommerce:
            self.tca_delivery_street = False
            self.tca_delivery_city = False
            self.tca_delivery_state_id = False
            return
        party = self.partner_shipping_id or self.partner_id
        if not party:
            return
        self.tca_delivery_street = party.street or False
        self.tca_delivery_city = party.city or False
        # Only auto-pick the state if it's a UAE state (the field is
        # domain-restricted to AE — assigning a non-AE state would clear).
        if party.state_id and party.state_id.country_id.code == 'AE':
            self.tca_delivery_state_id = party.state_id
        else:
            self.tca_delivery_state_id = False

    @api.onchange('tca_flag_free_trade_zone')
    def _onchange_tca_flag_free_trade_zone(self):
        """FTZ on → auto-fill the Buyer Beneficiary ID from the buyer's
        TRN/TIN. FTZ off → clear the field. User can override the auto-fill
        by typing manually; partner-change refills only when the field is
        empty (see `_onchange_partner_id_tca`)."""
        if not self.tca_flag_free_trade_zone:
            self.tca_buyer_beneficiary_id = False
            return
        self.tca_buyer_beneficiary_id = self._tca_resolve_buyer_beneficiary_id() or False

    @api.onchange('tca_flag_deemed_supply')
    def _onchange_tca_flag_deemed_supply(self):
        """
        Sync the Buyer Participant ID to the Deemed Supply flag.

          · Deemed ON  → clear the field. The buyer is unknown to us; the
            user types the FTA-assigned predefined endpoint (typically
            9900000097) manually, then the validator's format check accepts
            it on confirm.
          · Deemed OFF → re-fill from the customer's peppol_endpoint
            (or our company's, for self-bills). Any value the user typed
            during the Deemed-on session is discarded — toggling the flag
            should land the field where it would have been without the flag.
        """
        if self.tca_flag_deemed_supply:
            self.tca_buyer_participant_id = ''
            return
        # Resolve the buyer party — same logic as the compute / partner onchange.
        is_self_bill = (
            self.move_type in ('in_invoice', 'in_refund') and self.journal_id.is_self_billing
        )
        buyer = (
            self.company_id.partner_id.commercial_partner_id
            if is_self_bill
            else self.partner_id.commercial_partner_id
        )
        self.tca_buyer_participant_id = (buyer.peppol_endpoint or '') if buyer else ''

    @api.onchange('tca_flag_export')
    def _onchange_tca_flag_export(self):
        """Sync the counterparty's Participant ID to the Export flag.

          · Export ON  → clear it. The counterparty is outside the UAE (off the
            Peppol network); the user enters its Participant ID manually.
          · Export OFF → re-fill from the counterparty's peppol_endpoint.

        The counterparty is the customer on a normal invoice, or the vendor
        (seller slot) on a self-bill — the self-bill buyer is our own UAE
        company and is left untouched.

        Both directions delegate to _tca_resolve_buyer_participant_id rather
        than reading peppol_endpoint directly: the helper already returns ''
        for an export, and it also honours the rules a direct read would
        bypass — Deemed Supply must stay blank, and a foreign counterparty
        never gets an auto-filled endpoint. Writing the endpoint here directly
        re-introduced both bugs (untick Export on a deemed-supply or foreign
        invoice and the ID came back).
        """
        # Build the flags string explicitly instead of trusting
        # tca_transaction_type_flags to have recomputed already — compute
        # ordering within a single onchange is not guaranteed. Position 8 comes
        # from the flag we just toggled; the rest (incl. Deemed Supply at
        # position 2) carries over from the stored string.
        flags = (self.tca_transaction_type_flags or '00000000').ljust(8, '0')
        flags = flags[:7] + ('1' if self.tca_flag_export else '0')

        is_self_bill = (
            self.move_type in ('in_invoice', 'in_refund') and self.journal_id.is_self_billing
        )
        counterparty = self.partner_id.commercial_partner_id
        resolved = (
            self._tca_resolve_buyer_participant_id(counterparty, flags) if counterparty else ''
        )
        if is_self_bill:
            # Counterparty sits in the SELLER slot (the vendor).
            self.tca_seller_participant_id = resolved
        else:
            self.tca_buyer_participant_id = resolved

    # ──────────────────────────────────────────────────────────────────────────
    # COMPUTED HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    def _tca_product_lines(self):
        """Return the recordset of product (non-section, non-note) invoice
        lines on this move. Centralises the
        `invoice_line_ids.filtered(lambda l: l.display_type == 'product')`
        idiom that was repeated 10× across the addon.
        """
        self.ensure_one()
        return self.invoice_line_ids.filtered(lambda line: line.display_type == 'product')

    def _compute_display_send_button(self):
        """
        EXTENDS account.move.
        Upstream shows the Send button only for posted SALE documents
        (out_invoice / out_refund). Self-bills are `in_invoice` / `in_refund`
        on a self-billing journal — we ISSUE them outbound to TCA so they
        also need the Send action. Mirror our send eligibility predicate.
        """
        super()._compute_display_send_button()
        for move in self:
            if not move.display_send_button and move._tca_is_send_eligible():
                move.display_send_button = True

    def _tca_is_send_eligible(self):
        """
        Returns True if this invoice can be submitted (or resubmitted) to TCA.

        Eligible when:
          - the company has TCA integration active
          - the move is posted and not yet TCA-accepted
          - the move was NOT received via TCA inbound
          - AND one of:
              · OUTBOUND: move_type is out_invoice/out_refund AND the
                customer partner has the PINT AE EDI format set
                (`invoice_edi_format == 'ubl_pint_ae'`). The format flag
                signals the customer is reachable on PINT AE.
              · SELF-BILL: move_type is in_invoice/in_refund on a
                self-billing journal. No format requirement on the vendor
                — they may be outside UAE / off the Peppol network; the
                buyer issues to TCA on their behalf regardless.

        The outbound vs self-bill split exists because plain vendor bills
        (in_* on a regular journal) must NOT be queued for TCA outbound.
        """
        self.ensure_one()
        if not (
            self.company_id.tca_is_active
            and self.tca_create_einvoice
            and self.state == 'posted'
            and not self.tca_is_inbound
            and self.tca_move_state in ('not_sent', 'error', 'rejected')
        ):
            return False
        if self.tca_is_self_billing:
            return True
        return (
            self.move_type in ('out_invoice', 'out_refund')
            and self.partner_id.commercial_partner_id.invoice_edi_format == 'ubl_pint_ae'
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
            raise UserError(
                _(
                    'Cannot cancel invoice(s) %s: they have already been submitted to the '
                    'TCA Peppol network and cannot be retracted.\n\n'
                    'To correct an error, issue a credit note instead.',
                    names,
                )
            )
        # Mark non-submitted invoices as cancelled in TCA state
        for move in self:
            if move.tca_move_state in (
                'not_sent',
                'error',
                'rejected',
                'uploading',
                'submitted',
                'inbound_received',
            ):
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
            raise UserError(
                _(
                    'Cannot reset invoice(s) %s to draft: they have already been submitted to the '
                    'TCA Peppol network.\n\n'
                    'Issue a credit note to correct any errors.',
                    names,
                )
            )
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

    # ── CQ11: canonical per-section validators ────────────────────────────
    # `_tca_collect_validation_errors` is the single source of PINT AE
    # validation rules. Two entry points share it:
    #   - `_tca_validate_mandatory_fields` (Phase-1, pre-_post) flattens
    #     the dict to a list[str].
    #   - `account.edi.xml.ubl_pint_ae._export_invoice_constraints`
    #     (render-time) merges the dict into the bis3 constraints dict.
    # Each `_tca_check_*` returns dict[key, message] for stable dedup.

    def _tca_check_document(self):
        self.ensure_one()
        errs = {}
        type_code = self.tca_invoice_type_code or ''
        is_credit_note = type_code in ('381', '261', '81')

        if not self.tca_invoice_type_code:
            errs['pint_ae_type_code'] = _(
                '"Invoice Type Code" is required. '
                'Select the invoice type (e.g. 380) in the "Invoice & Buyer" section on the invoice form.'
            )
        if not self.invoice_date:
            errs['pint_ae_invoice_date'] = _('"Invoice Date" is required.')
        # currency_id is required=True upstream — only worth checking the AED
        # constraint here. Applies to any TCA-issued document: outbound
        # invoices/credit-notes plus self-bills (in_* on a self-billing
        # journal). Plain vendor bills carry the supplier's currency.
        is_issued_by_us = (
            self.move_type in ('out_invoice', 'out_refund') or self.tca_is_self_billing
        )
        if is_issued_by_us and self.currency_id.name != 'AED':
            errs['pint_ae_currency_aed'] = _(
                'PINT AE invoices must be issued in AED. Current currency: %s.',
                self.currency_id.name or '—',
            )
        if not self.invoice_date_due and not self.invoice_payment_term_id:
            errs['pint_ae_due_date_or_term'] = _('"Due Date" or "Payment Terms" is required.')

        # Buyer + Seller Participant IDs — both mandatory on every TCA-issued
        # document. Auto-populated from the partner / company peppol_endpoint
        # when present; the user enters the FTA predefined endpoint
        # (9900000097 / 98 / 99) manually when the party isn't on the
        # Peppol network. The contact-level field is NOT mandatory — the
        # invoice-level field is.
        buyer_pid = (self.tca_buyer_participant_id or '').strip()
        if not buyer_pid:
            errs['pint_ae_buyer_pid'] = _(
                '"Buyer Participant ID" is required. '
                'Enter the buyer\'s Peppol Participant ID in the "Invoice & Buyer" section. '
                'Use one of the FTA predefined endpoints (9900000097 / 9900000098 / '
                "9900000099) if the buyer isn't on the Peppol network."
            )
        elif buyer_pid != '1XXXXXXXXX' and (not buyer_pid.isdigit() or len(buyer_pid) != 10):
            errs['pint_ae_buyer_pid_format'] = _(
                '"Buyer Participant ID" must be exactly 10 digits. '
                'The 15-digit TRN belongs in the "Tax ID" field, not here. Current value: "%s".',
                buyer_pid,
            )

        seller_pid = (self.tca_seller_participant_id or '').strip()
        if not seller_pid:
            errs['pint_ae_seller_pid'] = _(
                '"Seller Participant ID" is required. '
                'Enter the seller\'s Peppol Participant ID in the "Invoice & Buyer" section. '
                'Use one of the FTA predefined endpoints (9900000097 / 9900000098 / '
                "9900000099) if the seller isn't on the Peppol network."
            )
        elif not seller_pid.isdigit() or len(seller_pid) != 10:
            errs['pint_ae_seller_pid_format'] = _(
                '"Seller Participant ID" must be exactly 10 digits. Current value: "%s".',
                seller_pid,
            )

        flags = (self.tca_transaction_type_flags or '').strip()
        if not flags:
            errs['pint_ae_flags_missing'] = _(
                '"Transaction Type Flags" is required. '
                'Set it to "00000000" for standard invoices in the "Transaction Type" section.'
            )
        elif len(flags) != 8 or not all(c in '01' for c in flags):
            errs['pint_ae_flags_format'] = _(
                '"Transaction Type Flags" must be exactly 8 digits of 0 or 1. Current: "%s".',
                flags,
            )

        if is_credit_note and not self.tca_credit_note_reason:
            errs['pint_ae_cn_reason'] = _(
                '"Credit Note Reason" is required for credit notes. '
                'Set it in the "Invoice & Buyer" section.'
            )

        if (
            is_credit_note
            and self.tca_credit_note_reason
            and self.tca_credit_note_reason != 'VD'
            and not self.reversed_entry_id
        ):
            errs['pint_ae_cn_preceding'] = _(
                'This credit note must be linked to the original invoice it corrects. '
                'Use the "Add Credit Note" button from the original invoice, or set the "Reversal Of" field. '
                '(Not required only for Volume Discount "VD" credit notes.)'
            )

        flags_ok = len(flags) == 8 and all(c in '01' for c in flags)
        if flags_ok and flags[0] == '1' and not (self.tca_buyer_beneficiary_id or '').strip():
            errs['pint_ae_ftz_beneficiary'] = _(
                '[ibr-007-ae] Free Trade Zone flag is set — "Buyer Beneficiary ID" is required.'
            )
        if flags_ok and flags[5] == '1' and not self.tca_principal_id:
            errs['pint_ae_principal'] = _(
                'Disclosed Agent flag is set — "Principal TRN" is required. '
                'Set it in the "Transaction Type" section.'
            )
        if (
            flags_ok
            and flags[3] == '1'
            and (not self.tca_invoice_period_start or not self.tca_invoice_period_end)
        ):
            errs['pint_ae_summary_period'] = _(
                '[ibr-138-ae] Summary Invoice flag is set — '
                '"Invoice Period Start" and "End" dates are required.'
            )
        if flags_ok and flags[4] == '1':
            if not self.tca_invoice_period_start or not self.tca_invoice_period_end:
                errs['pint_ae_continuous_period'] = _(
                    'Continuous Supply flag is set — '
                    '"Invoice Period Start" and "End" dates are required.'
                )
            if not self.tca_contract_reference:
                errs['pint_ae_continuous_contract'] = _(
                    'Continuous Supply flag is set — "Contract Reference" is required.'
                )

        # ibr-157-ae: OOS document type codes (480/81) cannot be combined
        # with Deemed Supply / Margin Scheme / Summary Invoice flags.
        if type_code in ('480', '81') and flags_ok:
            incompat = []
            if flags[1] == '1':
                incompat.append('Deemed Supply')
            if flags[2] == '1':
                incompat.append('Margin Scheme')
            if flags[3] == '1':
                incompat.append('Summary Invoice')
            if incompat:
                errs['pint_ae_oos_flags'] = _(
                    '[ibr-157-ae] Out-of-Scope invoice type (%(code)s) cannot be combined '
                    'with: %(flags)s. Either change the invoice type or unset those flags.',
                    code=type_code,
                    flags=', '.join(incompat),
                )

        # ibr-142-ae: E-commerce (pos 7) requires a complete delivery address.
        # The user-facing fields tca_delivery_street / _city / _state_id are
        # auto-filled from the shipping party on flag toggle and editable on
        # the form. Validate those move-level fields.
        if flags_ok and flags[6] == '1':
            missing = []
            if not self.tca_delivery_street:
                missing.append('Street')
            if not self.tca_delivery_city:
                missing.append('City')
            if not self.tca_delivery_state_id:
                missing.append('State / Emirate')
            if missing:
                errs['pint_ae_ecommerce_delivery'] = _(
                    '[ibr-142-ae] E-commerce flag is set — Delivery Address '
                    'fields are required: %s. Fill them in the "Delivery '
                    'Address" section or unset the flag.',
                    ', '.join(missing),
                )

        # ibr-152-ae: Export (pos 8) requires the same delivery address; this
        # flag auto-fires when the buyer is non-UAE, so users typically can't
        # toggle it. Validate the shipping party's address (no dedicated UI
        # fields — Export is a side-effect of the buyer's country).
        if flags_ok and flags[7] == '1':
            delivery_party = self.partner_shipping_id or self.partner_id
            missing = []
            if not delivery_party.street:
                missing.append('Street')
            if not delivery_party.city:
                missing.append('City')
            if not delivery_party.state_id:
                missing.append('State / Emirate')
            if missing:
                errs['pint_ae_export_delivery'] = _(
                    '[ibr-152-ae] Export flag is set — the delivery '
                    'address on "%(party)s" is missing: %(fields)s. '
                    'Complete the address on the shipping address record.',
                    party=delivery_party.display_name or delivery_party.name or '—',
                    fields=', '.join(missing),
                )

        # ibr-191-ae: Payment Means Code (IBT-081) is required, except for
        # credit notes and Deemed Supply transactions.
        is_deemed = flags_ok and flags[1] == '1'
        if not is_credit_note and not is_deemed and not self.tca_payment_means_code:
            errs['pint_ae_payment_means'] = _(
                '[ibr-191-ae] "Payment Means Code" (IBT-081) is required. '
                'Pick a value (e.g. 30 — Credit transfer, 10 — In cash, '
                'ZZZ — Mutually defined) in the "Invoice & Buyer" section.'
            )

        if self.tca_billing_frequency == 'OTH' and not self.narration:
            errs['pint_ae_oth_note'] = _(
                '[ibr-160-ae] Billing frequency is "Others" (OTH) — '
                'an Invoice Note (IBT-022) must be provided to describe the frequency.'
            )

        if (
            self.tca_tax_point_date
            and self.invoice_date
            and self.tca_tax_point_date >= self.invoice_date
        ):
            errs['pint_ae_tax_point_date'] = _(
                '[ibr-141-ae] "Tax Point Date" (IBT-007) must be strictly before "Invoice Date" (IBT-002). '
                'Tax point: %(tp)s, Invoice date: %(d)s.',
                tp=self.tca_tax_point_date,
                d=self.invoice_date,
            )

        return errs

    def _tca_check_supplier(self):
        self.ensure_one()
        errs = {}
        supplier = self.company_id.partner_id.commercial_partner_id
        type_code = self.tca_invoice_type_code or ''
        is_oos = type_code in ('480', '81')

        if not supplier.name:
            errs['pint_ae_supplier_name'] = _(
                'Your company name (IBT-027) is missing. Set it in Settings → Companies.'
            )
        if not getattr(supplier, 'peppol_eas', None) or not getattr(
            supplier, 'peppol_endpoint', None
        ):
            errs['pint_ae_supplier_peppol'] = _(
                'Your company\'s "Peppol EAS" and "Peppol Endpoint" (IBT-034) are missing.'
            )
        if not supplier.vat and getattr(supplier, 'peppol_eas', '') == UAE_EAS:
            errs['pint_ae_supplier_vat'] = _(
                'Your company\'s "Tax ID" (TRN, IBT-031) is missing. Set it in Settings → Companies.'
            )
        if not is_oos and not supplier.vat:
            errs['pint_ae_supplier_vat_required'] = _(
                '[ibr-134-ae] Your company\'s "Tax ID" (TRN, IBT-031) is required. '
                'Set it in Settings → Companies. (Required unless invoice type is Out-of-Scope.)'
            )
        if not supplier.street:
            errs['pint_ae_supplier_street'] = _(
                'Your company\'s "Street" (IBT-035) address is missing.'
            )
        if not supplier.city:
            errs['pint_ae_supplier_city'] = _('Your company\'s "City" (IBT-037) is missing.')
        if not supplier.country_id:
            errs['pint_ae_supplier_country'] = _('Your company\'s "Country" (IBT-040) is missing.')
        if supplier._tca_is_uae_party() and supplier._tca_emirate() not in UAE_EMIRATES:
            errs['pint_ae_supplier_emirate'] = _(
                'Your company\'s "Emirate" must be set to one of: '
                'AUH, DXB, SHJ, UAQ, FUJ, AJM, RAK.'
            )

        seller_legal_reg = supplier.tca_trade_license or supplier.company_registry or supplier.vat
        if not seller_legal_reg:
            errs['pint_ae_supplier_legal_reg'] = _(
                'Your company\'s "Trade License / Registration ID" (IBT-030) is missing. '
                'Set it on the company partner record → "E-Invoicing" tab.'
            )
        if (
            getattr(supplier, 'peppol_eas', '') == UAE_EAS
            and seller_legal_reg
            and not supplier.tca_legal_id_type
        ):
            errs['pint_ae_supplier_legal_id_type'] = _(
                '[ibr-181-ae] Your company\'s "Legal ID Type" is required. '
                'Set it to TL / EID / PAS / CD on the company partner record → "E-Invoicing" tab.'
            )
        if supplier.tca_legal_id_type == 'TL' and not supplier.tca_legal_authority:
            errs['pint_ae_supplier_authority'] = _(
                'Your company\'s "Issuing Authority" is required when Legal ID Type is Trade License. '
                'Set it on the company partner record → "E-Invoicing" tab.'
            )
        if supplier.tca_legal_id_type == 'PAS' and not supplier.tca_passport_country_id:
            errs['pint_ae_supplier_passport_country'] = _(
                'Your company\'s "Passport Issuing Country" is required when Legal ID Type is Passport. '
                'Set it on the company partner record → "E-Invoicing" tab.'
            )
        return errs

    def _tca_check_customer(self):
        self.ensure_one()
        errs = {}
        customer = self.partner_id.commercial_partner_id

        if not customer.name:
            errs['pint_ae_customer_name'] = _('Customer name (IBT-044) is missing.')
        if not customer.country_id:
            errs['pint_ae_customer_country'] = _(
                'Customer "%s" is missing a "Country" (IBT-055).',
                customer.name,
            )
        # Participant-ID enforcement lives on the MOVE field
        # (`tca_buyer_participant_id`, validated in _tca_check_document).
        # The partner's peppol_eas/peppol_endpoint are inputs to that compute;
        # when missing the user fills the predefined govt fallback (97/98/99)
        # directly on the invoice form. We deliberately do not gate confirm
        # on partner.peppol_endpoint so self-bills with off-network foreign
        # vendors and customers without Peppol routing can both proceed.

        buyer_pid = (self.tca_buyer_participant_id or '').strip()
        buyer_is_anonymous = buyer_pid in ANON_BUYER_PIDS
        if customer._tca_is_uae_party() and not buyer_is_anonymous:
            if not customer.vat and getattr(customer, 'peppol_eas', '') == UAE_EAS:
                errs['pint_ae_customer_vat'] = _(
                    'Customer "%s" is missing "Tax ID" (TRN, IBT-048). Set it on the customer record.',
                    customer.name,
                )
            if not customer.street:
                errs['pint_ae_customer_street'] = _(
                    'Customer "%s" is missing "Street" (IBT-050).',
                    customer.name,
                )
            if not customer.city:
                errs['pint_ae_customer_city'] = _(
                    'Customer "%s" is missing "City" (IBT-052).',
                    customer.name,
                )
            if self.tca_buyer_emirate not in UAE_EMIRATES:
                errs['pint_ae_customer_emirate'] = _(
                    '"Buyer Emirate" is required for UAE customers. '
                    'Set it in the "Invoice & Buyer" section '
                    '(AUH/DXB/SHJ/UAQ/FUJ/AJM/RAK), or set it once on the customer record.'
                )
            if not self.tca_buyer_trade_license:
                errs['pint_ae_customer_legal_reg'] = _(
                    '"Buyer Trade License / Reg. ID" (IBT-047) is required. '
                    'Set it in the "Buyer Legal" section, or set it once on the customer record.'
                )
            if not self.tca_buyer_legal_id_type:
                errs['pint_ae_customer_legal_id_type'] = _(
                    '"Buyer Legal ID Type" is required. '
                    'Set it to TL / EID / PAS / CD in the "Buyer Legal" section.'
                )
            if self.tca_buyer_legal_id_type == 'TL' and not self.tca_buyer_legal_authority:
                errs['pint_ae_customer_authority'] = _(
                    '"Buyer Issuing Authority" is required when Legal ID Type is Trade License. '
                    'Set it in the "Buyer Legal" section.'
                )
            if self.tca_buyer_legal_id_type == 'PAS' and not self.tca_buyer_passport_country_id:
                errs['pint_ae_customer_passport_country'] = _(
                    '"Buyer Passport Country" is required when Legal ID Type is Passport. '
                    'Set it in the "Buyer Legal" section.'
                )
        return errs

    def _tca_check_lines(self):
        self.ensure_one()
        errs = {}
        product_lines = self._tca_product_lines()
        if not product_lines:
            errs['pint_ae_no_lines'] = _(
                'The invoice has no lines. Add at least one product or service line.'
            )
            return errs

        for line in product_lines:
            label = (
                line.name
                or (line.product_id and line.product_id.name)
                or _('Line %s', line.sequence)
            )
            if not line.quantity:
                errs[f'pint_ae_line_qty_{line.id}'] = _(
                    'Line "%s": "Quantity" (IBT-129) is required and cannot be zero.',
                    label,
                )
                return errs
            if not line.product_uom_id:
                errs[f'pint_ae_line_uom_{line.id}'] = _(
                    'Line "%s": "Unit of Measure" (IBT-130) is required.',
                    label,
                )
                return errs
            if not line.name and not (line.product_id and line.product_id.name):
                errs[f'pint_ae_line_desc_{line.id}'] = _(
                    'Line %s: "Description" or product name (IBT-153) is required.',
                    line.sequence,
                )
                return errs
            if not line.tax_ids:
                errs[f'pint_ae_line_tax_{line.id}'] = _(
                    'Line "%s": at least one Tax must be applied.',
                    label,
                )
                return errs
            has_rc = any(t.tca_tax_category == 'AE' for t in line.tax_ids)
            # HS Code (IBT-158) is mandatory ONLY under Reverse Charge (RCM).
            # Business rule: non-RCM goods/services lines do not require an HS
            # code at confirmation. SAC (BTAE-17) is likewise left to TCA's own
            # schematron so confirmation is not blocked.
            if has_rc and not line.tca_hs_code:
                errs[f'pint_ae_line_hs_{line.id}'] = _(
                    'Line "%s": Reverse Charge line — "HS Code" (IBT-158) is mandatory.',
                    label,
                )
                return errs
            if has_rc and not line.tca_rc_description:
                errs[f'pint_ae_line_rc_{line.id}'] = _(
                    'Line "%s": Reverse Charge tax — "Goods/Services Type" is mandatory.',
                    label,
                )
                return errs
            # Exempt (E) line MUST carry a VAT exemption reason code (IBT-186,
            # schematron ibr-167-ae). Accept it either on the line (override)
            # or on the tax record — the JSON builder uses the same precedence.
            exempt_tax = next(
                (t for t in line.tax_ids if t.tca_tax_category == 'E'),
                None,
            )
            if exempt_tax:
                reason = (line.tca_vat_exemption_reason_code or '').strip() or (
                    exempt_tax.tca_exemption_reason_code or ''
                )
                if not reason:
                    errs[f'pint_ae_line_exempt_reason_{line.id}'] = _(
                        '[ibr-167-ae] Line "%s": this line is Exempt (E) — a '
                        '"VAT Exemption Reason Code" is required. Enter it on '
                        'the line, or set a default on the tax "%s".',
                        label,
                        exempt_tax.name,
                    )
                    return errs

        type_code = self.tca_invoice_type_code or ''
        if type_code in ('480', '81'):
            allowed = {'E', 'O', 'Z'} if type_code == '480' else {'E', 'O'}
            allowed_str = ', '.join(sorted(allowed))
            for line in product_lines:
                for tax in line.tax_ids:
                    cat = tax.tca_tax_category or ''
                    if not cat:
                        errs['pint_ae_oos_vat_missing'] = _(
                            'Invoice type %s requires all taxes to have a UAE VAT category. '
                            'Tax "%s" on line "%s" has no category set.',
                            type_code,
                            tax.name,
                            line.name or str(line.id),
                        )
                        return errs
                    if cat not in allowed:
                        errs['pint_ae_oos_vat'] = _(
                            'Invoice type %s only allows VAT categories: %s. '
                            'Line "%s" uses tax "%s" with category "%s".',
                            type_code,
                            allowed_str,
                            line.name or str(line.id),
                            tax.name,
                            cat,
                        )
                        return errs

        for line in product_lines:
            for tax in line.tax_ids:
                if tax.tca_tax_category in ('S', 'N') and tax.amount != 5.0:
                    errs['pint_ae_s_rate'] = _(
                        '[ibr-190-ae] Standard rated (%s) VAT must be exactly 5.00%%. '
                        'Tax "%s" has rate %.2f%%.',
                        tax.tca_tax_category,
                        tax.name,
                        tax.amount,
                    )
                    return errs

        return errs

    def _tca_collect_validation_errors(self):
        """Canonical PINT AE validator. Returns dict[key, message].
        Used by both _tca_validate_mandatory_fields (pre-_post) and
        account.edi.xml.ubl_pint_ae._export_invoice_constraints (render-time).
        """
        self.ensure_one()
        errs = {}
        errs.update(self._tca_check_document())
        errs.update(self._tca_check_supplier())
        errs.update(self._tca_check_customer())
        errs.update(self._tca_check_lines())
        return errs

    def _tca_validate_mandatory_fields(self):
        """Phase-1 pre-_post validator. Returns list[str] of error messages.
        Thin wrapper around _tca_collect_validation_errors.
        """
        self.ensure_one()
        return list(self._tca_collect_validation_errors().values())

    def _tca_validate_xml_pipeline(self):
        """
        Renders the PINT AE XML and returns a list of validation error
        messages (empty = all OK). Same check the Send & Print wizard does.

        Odoo 19: builder._export_invoice builds the node tree, runs
        _export_invoice_constraints internally — which merges our canonical
        PINT AE rule set (_tca_collect_validation_errors) into the bis3
        constraints — and returns (xml, errors). Those Python rules are the
        authoritative local gate; TCA's Access Point runs the official PINT AE
        schematron server-side as the final compliance check on submission.
        """
        self.ensure_one()
        errors = []
        builder = self.env['account.edi.xml.ubl_pint_ae']
        try:
            _xml, build_errors = builder._export_invoice(self)
        except Exception as exc:
            _logger.exception('TCA: failed to render PINT AE XML for validation')
            errors.append(_('Internal error rendering PINT AE XML: %s', exc))
            return errors
        for be in build_errors or ():
            if be:
                errors.append(str(be).strip())
        return errors

    def _tca_build_submission_id(self):
        """
        Build a unique invoice_number for a TCA submission attempt.

        UAE FTA compliance rule: the same invoice ID cannot be processed by
        the Peppol network more than once. Every API call to TCA must carry
        a distinct identifier. Composing it as `<record name>-<uuid8>` makes
        each attempt guaranteed-unique without relying on a counter that
        could roll back on transaction failure.

        Retry policy: this method is only ever called from contexts gated by
        _tca_is_send_eligible(), which restricts to tca_move_state in
        ('not_sent', 'error', 'rejected'). Once TCA has accepted the
        document (state moves past 'submitted'), retries are blocked
        upstream — so we never re-submit a record TCA has already processed.
        """
        self.ensure_one()
        return f'{self.name}-{uuid.uuid4().hex[:8]}'

    # ──────────────────────────────────────────────────────────────────────────
    # PINT AE JSON (inline) submission — maps account.move → the TCA `detail`
    # tree. Key names follow the ASP JSON schema §9 Field Reference (human-
    # readable snake_case, one field per IBT/BTAE). The backend injects
    # UUID (BTAE-07), ProfileID/CustomizationID and the VAT tax-scheme codes,
    # so we never send those. Validation is synchronous: POST /invoices/
    # returns 400 with a per-field error dict on bad content, 201 on accept.
    # ──────────────────────────────────────────────────────────────────────────

    # Odoo tca_emirate / partner emirate selection keys already equal the
    # PINT AE subdivision codes (AUH/DXB/SHJ/AJM/UAQ/RAK/FUJ) — no remap needed.
    _TCA_ZERO_VAT_CATEGORIES = ('Z', 'AE', 'E', 'O')
    # Categories where the VAT RATE (IBT-152 / IBT-119) must be ABSENT entirely,
    # not zero — schematron ibr-119-ae / aligned-ibrp-e-05. Note Z and AE still
    # require a rate (0 and 5), so they are NOT in this set.
    _TCA_NO_RATE_CATEGORIES = ('E', 'O')

    def _tca_json_seller_buyer(self):
        """Return (seller_partner, buyer_partner) in the SEMANTIC sense —
        seller = supplier (sending_party), buyer = customer (receiving_party).
        Self-billing swaps who issues but NOT the semantic roles: the peppol_id
        routing swap is already baked into tca_seller/buyer_participant_id."""
        self.ensure_one()
        company_partner = self.company_id.partner_id.commercial_partner_id
        counterpart = self.partner_id.commercial_partner_id
        if self.tca_is_self_billing:
            # We (the buyer/company) issue on the supplier's behalf.
            return counterpart, company_partner
        return company_partner, counterpart

    @staticmethod
    def _tca_json_peppol_id(raw):
        """Format a participant id as §9 requires: `{scheme}:{identifier}`.
        Move-level participant ids are stored bare (the XML builder adds the
        schemeID attribute separately); JSON needs the scheme inline. Default
        to the UAE EAS (0235) when no scheme is already present."""
        raw = (raw or '').strip()
        if not raw or ':' in raw:
            return raw
        return f'{UAE_EAS}:{raw}'

    def _tca_json_party(self, partner, peppol_id, is_buyer):
        """Layer 2 party object (§9 sending_party / receiving_party).

        For the buyer, invoice-form overrides (tca_buyer_*) win over the
        partner record — mirrors the XML builder's _tca_resolve_legal_id so a
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
            eas_scheme, eas_addr = UAE_EAS, raw_pid
        # ── Tax identifiers ──────────────────────────────────────────────
        # TWO distinct ids for a UAE party:
        #   · vat_identifier (IBT-031) = the 15-digit VAT TRN (1…03) = partner.vat
        #   · the 10-digit TIN (IBT-032) is the participant id, already carried
        #     as electronic_address — TCA derives IBT-032 from the 0235 endpoint.
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
            # tax_scheme drives IBT-031-1: must be 'VAT' for a VAT-registered
            # party so the TRN builds as the VAT PartyTaxScheme (IBT-031). The
            # 10-digit TIN (IBT-032) is derived from the 0235 endpoint.
            party['tax_scheme'] = 'VAT'
        if trade_license:
            party['legal_registration_identifier'] = trade_license
        if legal_type:
            # API expects the raw UAE codes TL/EID/PAS/CD (our stored values).
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
        # VAT amount must be 0 for Z / AE / E / O per §9 (buyer self-accounts or
        # no VAT); use the actual line delta otherwise.
        vat_amt = (
            0.0
            if cat in self._TCA_ZERO_VAT_CATEGORIES
            else (line.price_total - line.price_subtotal)
        )
        item_name = line.name or (line.product_id.name if line.product_id else '') or ''
        commodity = line.tca_effective_commodity_type or ''
        # Net unit price (after line discount); Odoo price_unit is pre-discount.
        net_unit = line.price_unit * (1 - (line.discount or 0.0) / 100.0)

        # vat_info — per-line VAT sub-object (canonical keys from GET record).
        vat_info = {
            'vat_category_code': cat,
            'tax_scheme': 'VAT',
        }
        # VAT rate (IBT-152) must be ABSENT for E/O — ibr-119-ae. Present
        # (incl. 0) for S/Z/AE/N.
        if cat not in self._TCA_NO_RATE_CATEGORIES:
            vat_info['vat_rate'] = rate
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
            'invoiced_quantity_unit_of_measure_code': (
                line.product_uom_id._get_unece_code() if line.product_uom_id else 'C62'
            ),
            'line_net_amount': net,
            'item_net_price': net_unit,
            'item_gross_price': line.price_unit,
            'item_price_base_quantity': 1,
            'item_name': item_name,
            'item_description': item_name,  # IBT-154 mandatory — mirror name
            'item_type': commodity,  # BTAE-13 G/S/B
            'line_amount_in_aed': net + vat_amt,  # BTAE-10
            'vat_info': [vat_info],
        }
        # BTAE-08 (VAT line amount) must be ABSENT on Exempt lines — schematron
        # ibr-163-ae. Emit it for every other category (0 is valid for Z/O/AE).
        if cat != 'E':
            d['vat_line_amount_in_aed'] = vat_amt  # BTAE-08 (canonical key)
        # HS (goods) / SAC (services) go in their own arrays.
        if commodity in ('G', 'B') and line.tca_hs_code:
            d['classifications'] = [
                {
                    'classification_identifier': line.tca_hs_code,
                    'classification_identifier_scheme': 'HS',
                }
            ]
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
            g = groups.setdefault(
                key,
                {
                    'vat_category_code': cat,
                    'tax_scheme_code': 'VAT',
                    'taxable_amount': 0.0,
                    'tax_amount': 0.0,
                },
            )
            # VAT category rate (IBT-119) must be ABSENT for E/O — ibr-119-ae.
            if cat not in self._TCA_NO_RATE_CATEGORIES:
                g['vat_category_rate'] = rate
            g['taxable_amount'] += line.price_subtotal
            if cat not in self._TCA_ZERO_VAT_CATEGORIES:
                g['tax_amount'] += line.price_total - line.price_subtotal
        return list(groups.values())

    def _tca_json_totals(self):
        """Layer 5 — totals (real API leaf names)."""
        self.ensure_one()
        return {
            'sum_of_invoice_line_net_amount': self.amount_untaxed,
            'invoice_total_amount_without_vat': self.amount_untaxed,
            'invoice_total_vat_amount': self.amount_tax,
            'invoice_total_amount_with_vat': self.amount_total,
            'amount_due_for_payment': self.amount_total,
        }

    def _tca_build_json_detail(self):
        """Build the full PINT AE `detail` tree for the inline-JSON submission
        mode (ASP JSON schema §9). Returns a plain dict ready to json-encode."""
        self.ensure_one()
        is_credit_note = self.tca_invoice_type_code in ('381', '81', '361')
        is_selfbill = self.tca_is_self_billing
        seller, buyer = self._tca_json_seller_buyer()

        # Transaction-type code — the 8-char BTAE-02 binary string (reuse the
        # builder's logic so the export bit is auto-set from buyer country).
        builder = self.env['account.edi.xml.ubl_pint_ae']
        transaction_type_code = builder._get_profile_execution_id(self)

        detail = {
            'issue_date': self.invoice_date.isoformat() if self.invoice_date else '',
            'invoice_type_code': self.tca_invoice_type_code or '',
            'transaction_type_code': transaction_type_code,
            'invoice_currency_code': self.currency_id.name or 'AED',
            'process_control': {
                'profile_id': PINT_AE_SELFBILLING_PROFILE_ID if is_selfbill else PINT_AE_PROFILE_ID,
                'customization_id': (
                    PINT_AE_SELFBILLING_CUSTOMIZATION_ID
                    if is_selfbill
                    else PINT_AE_CUSTOMIZATION_ID
                ),
            },
            'seller': self._tca_json_party(seller, self.tca_seller_participant_id, is_buyer=False),
            'buyer': self._tca_json_party(buyer, self.tca_buyer_participant_id, is_buyer=True),
            'lines': self._tca_json_lines(),
            'vat_breakdowns': self._tca_json_vat_breakdown(),
            'totals': self._tca_json_totals(),
        }

        # ── Layer 1 conditional header fields ───────────────────────────────
        # due_date: required when payable > 0, except credit notes / deemed.
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
        # note (IBT-022) — free text; mandatory when billing_frequency is OTH.
        if self.narration:
            # narration is HTML on account.move; strip to plain text.
            note_text = re.sub(r'<[^>]+>', ' ', self.narration or '').strip()
            if note_text:
                detail['note'] = note_text

        # FTZ beneficiary id (BTAE-01) lives on the buyer party. The API's
        # canonical key is `beneficiary_identifier` (confirmed against the GET
        # record) — an earlier `fz_beneficiary_id` was silently ignored as an
        # unknown field, leaving BTAE-01 empty and tripping ibr-007-ae.
        if self.tca_flag_free_trade_zone and self.tca_buyer_beneficiary_id:
            detail['buyer']['beneficiary_identifier'] = self.tca_buyer_beneficiary_id
        # Disclosed-agent principal (BTAE-14). The real API key is
        # `principal_identifier` at the DETAIL ROOT (per the field-reference
        # doc + TCA's own error text). The stale §9 docx's `principle_id` on
        # sending_party is silently dropped → "Missing principal identifier".
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
        # Preceding invoice(s) — mandatory for credit notes unless reason is VD.
        if is_credit_note and self.tca_credit_note_reason != 'VD' and self.reversed_entry_id:
            references['preceding_invoices'] = [
                {
                    'id': self.reversed_entry_id.name or '',
                    'issue_date': (
                        self.reversed_entry_id.invoice_date.isoformat()
                        if self.reversed_entry_id.invoice_date
                        else ''
                    ),
                }
            ]
        if references:
            detail['references'] = references

        # NOTE: the invoicing period (IBG-14) is NOT a root-level object — it
        # nests under `delivery` (detail.delivery.invoicing_period). Built after
        # the delivery block below so it merges into the same container.

        # delivery — mandatory when ecommerce or export. Prefer the explicit
        # tca_delivery_* fields (ecommerce path); fall back to the shipping
        # partner (the export validator's source) so both use cases resolve.
        if self.tca_flag_ecommerce or self.tca_is_export:
            ship = self.partner_shipping_id or self.partner_id
            # Canonical delivery keys (field-reference doc: detail.delivery.*):
            # address_line_1 / city / country_subdivision / country_code,
            # actual_delivery_date, party_identifier — NOT line1/subdivision/
            # country/actual_date/party_id (those are silently dropped, and
            # country_code then reads as missing → "This field is required").
            # Emirate code (ibr-128-ae): map Odoo state code → PINT AE code.
            # Prefer the delivery-state override, else the ship partner's emirate.
            if self.tca_delivery_state_id:
                sub = UAE_STATE_CODE_TO_EMIRATE.get(
                    self.tca_delivery_state_id.code, self.tca_delivery_state_id.code
                )
            else:
                sub = ship._tca_emirate() if ship else ''
            delivery = {
                'address': {
                    'address_line_1': self.tca_delivery_street or ship.street or '',
                    'city': self.tca_delivery_city or ship.city or '',
                    'country_subdivision': sub or '',
                    'country_code': (
                        ship.country_id.code
                        if ship.country_id
                        else (buyer.country_id.code if buyer.country_id else '')
                    )
                    or '',
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
        # TEMP PROBE: the exact JSON key TCA reads is unknown — three internal
        # docs disagree and every single-shape attempt has failed ibr-138-ae
        # (root `invoice_period{start_date,end_date}`, root flat
        # `invoice_period_*`, and delivery.invoicing_period with prefixed
        # subkeys — all confirmed dead via the live outgoing dump). Period keys
        # are tolerated as unknowns (submissions 201'd, not 400'd), so emit the
        # period under EVERY remaining plausible key at once. Whichever TCA's
        # mapper reads makes InvoicePeriod appear → ibr-138-ae clears. Once it
        # passes, GET the record and NARROW to the single winning key.
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
            # (1) root nested, short subkeys (matches process_control/references style)
            detail['invoicing_period'] = dict(obj)
            # (2) root flat, "invoicing" prefix (mirrors line-level line_period_start_date)
            if s:
                detail['invoicing_period_start_date'] = s
            if e:
                detail['invoicing_period_end_date'] = e
            # (3) under delivery, short subkeys (field-ref doc nesting, API-style subkeys)
            detail.setdefault('delivery', {})['invoicing_period'] = dict(obj)

        # payment_instructions — required for all doc types except credit
        # notes / deemed supply. IBT-081 payment means type code.
        if not is_credit_note and not self.tca_flag_deemed_supply and self.tca_payment_means_code:
            detail['payment_instructions'] = [
                {'payment_means_type_code': self.tca_payment_means_code}
            ]

        # Strip empty strings / None / empty containers so TCA does not render
        # empty UBL elements (schematron ibr-079). Numeric 0 / 0.0 is KEPT —
        # e.g. a zero VAT line amount in AED (BTAE-08) must stay present.
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
        Submit this invoice/credit note to TCA via the inline-JSON endpoint.
        Atomic: raises UserError on any failure so the caller can roll back
        super()._post() — UAE FTA compliance requires TCA to ACCEPT the
        document before it is recorded in the books.

        Single call: POST /api/v1/invoices/ with the PINT AE `detail` tree
        (no XML build, no S3 upload). Validation is synchronous —
          201 → validated + queued for Peppol dispatch → mark submitted.
          400 → per-field content errors → TcaValidationError → UserError
                with the field list (nothing posted).
        The backend builds + schematron-validates the UBL server-side.
        """
        self.ensure_one()
        api_svc = self.env['tca.api.service']
        company = self.company_id

        # 1. Build the PINT AE detail tree from this move.
        detail = self._tca_build_json_detail()

        # 2. Unique submission id for THIS attempt (UAE compliance — a given
        #    invoice_number is accepted by the network only once).
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
            self.tca_move_state = 'error'
            self.tca_submission_error = '\n'.join(exc.tca_field_errors) or str(exc)
            raise UserError(
                _(
                    'TCA rejected this invoice — fix these and confirm again:\n\n%s',
                    '\n'.join(f'• {e}' for e in exc.tca_field_errors) or str(exc),
                )
            ) from exc

        # 4. Duplicate (defensive — unique submission_id should prevent it).
        if result.get('tca_duplicate'):
            self.write(
                {
                    'tca_move_state': 'submitted',
                    'tca_submission_error': False,
                    'tca_last_submission_id': submission_id,
                }
            )
            self._message_log(
                body=_(
                    'TCA: document already registered (duplicate on submission "%s"). '
                    'Status will sync via cron.',
                    submission_id,
                )
            )
            return True

        # 5. 201 — validated + queued. Store the TCA id, mark submitted.
        tca_id = result.get('id', '')
        self.write(
            {
                'tca_invoice_uuid': tca_id,
                'tca_move_state': 'submitted',
                'tca_submission_error': False,
                'tca_last_submission_id': submission_id,
            }
        )
        self._message_log(
            body=_(
                'Submitted to TCA Peppol network (validated on submission). '
                'TCA invoice_number: %(sid)s — TCA ID: %(tid)s',
                sid=submission_id,
                tid=tca_id,
            )
        )
        return True

    def _post(self, soft=True):
        """
        EXTENDS account.move.
        Two-phase PINT AE flow at Confirm:
          Phase 1 (pre-post): _tca_validate_mandatory_fields — fast Python checks
                              on partner / invoice fields. Fails → no ledger entry.
          Phase 2 (post-post): _tca_validate_xml_pipeline — renders the XML and
                              runs the full PINT AE constraints, same as the
                              Send & Print wizard. Fails → UserError rolls back
                              the super()._post().

        After Confirm the document is POSTED in the ledger but NOT yet sent to
        TCA — neither invoices nor credit notes auto-submit. The user clicks
        Send & Print and ticks "Submit via TCA Peppol" to do the network
        submission. This keeps the UX identical for the two document classes.

        Scope: sale documents on TCA-active company whose buyer uses ubl_pint_ae,
        plus self-bills issued on a self-billing journal.

        Known limitation — sequence gaps on Phase 2 failure:
          super()._post() assigns the document name from the journal's sequence
          via PostgreSQL's nextval(), which is NOT transactional — the sequence
          advance survives a rollback. When Phase 2 raises UserError, the move
          is rolled back to draft but the consumed sequence number is gone.
          The next successful Confirm picks up the FOLLOWING number, leaving
          a permanent gap. Acceptable in audits with an explanation; a proper
          no-gap counter table is out of scope. TODO: row-locked counter for
          TCA-active journals.
        """
        # ── Phase 1: pre-post fast checks ─────────────────────────────────────
        # Scope: any document we ISSUE through TCA. That's:
        #   · sale documents (out_invoice / out_refund) AND
        #   · self-bills (in_invoice / in_refund on a self-billing journal —
        #     buyer issues on the supplier's behalf).
        # Plain vendor bills are filtered out — they're received, not issued.
        pint_moves = self.env['account.move']
        for move in self:
            partner = move.partner_id.commercial_partner_id
            is_issued_by_us = move.is_sale_document() or move.tca_is_self_billing
            # Outbound requires the partner to have the PINT AE format set
            # (so we can route to them). Self-bills don't — the buyer issues
            # on TCA on the supplier's behalf regardless of the supplier's
            # Peppol presence.
            partner_eligible = (
                move.tca_is_self_billing or partner.invoice_edi_format == 'ubl_pint_ae'
            )
            if (
                move.company_id.tca_is_active
                and move.tca_create_einvoice
                and is_issued_by_us
                and partner_eligible
            ):
                errors = move._tca_validate_mandatory_fields()
                if errors:
                    raise UserError(
                        _(
                            'Cannot confirm this invoice — the following issues must be fixed first:\n\n%s',
                            '\n'.join(f'• {v}' for v in errors),
                        )
                    )
                pint_moves |= move

        # ── Standard Odoo posting (assigns sequence + ledger entries) ────────
        result = super()._post(soft=soft)

        # ── Phase 2: full XML validation on the just-posted invoice ──────────
        # Raising here rolls back super()._post() — invoice returns to draft,
        # no ledger entries persist.
        for move in pint_moves:
            xml_errors = move._tca_validate_xml_pipeline()
            if xml_errors:
                raise UserError(
                    _(
                        'Cannot confirm this invoice — PINT AE validation failed:\n\n%s\n\n'
                        'Fix these issues, then try Confirm again.',
                        '\n'.join(f'• {v}' for v in xml_errors),
                    )
                )

        # No Phase 3. Credit notes (like invoices) are posted here and then
        # submitted via the Send & Print wizard — `_tca_is_send_eligible`
        # and `_compute_display_send_button` both surface the Send button
        # on posted credit notes once their `tca_move_state` is not_sent.
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # INBOUND XML ROUTING — register PINT AE in the import-format dispatch table
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _get_import_file_type(self, file_data):
        """
        EXTENDS account_edi_ubl_cii.
        Route inbound PINT AE XML to the account.edi.xml.ubl_pint_ae builder.

        In Odoo 19 inbound UBL/CII files are routed to a builder by
        _get_import_file_type, which returns the builder model name and reads
        the XML root CustomizationID. We must match PINT AE BEFORE delegating
        to super(), because PINT AE's CustomizationID
        (urn:peppol:pint:billing-1@ae-1 / urn:peppol:pint:selfbilling-1@ae-1)
        does NOT start with urn:cen.eu:en16931:2017 and would otherwise fall
        through unmatched.
        """
        if (tree := file_data.get('xml_tree')) is not None:
            customization_id = tree.findtext('{*}CustomizationID')
            if customization_id in PINT_AE_CUSTOMIZATION_IDS:
                return 'account.edi.xml.ubl_pint_ae'
        return super()._get_import_file_type(file_data)

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
        tca_status = payload.get('status')  # int or None
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
                body=_(
                    'TCA Peppol: Invoice %s — status changed to %s. Detail: %s',
                    self.name,
                    new_state.upper(),
                    error_detail,
                )
            )
        else:
            self.tca_submission_error = False
            self._message_log(
                body=_(
                    'TCA Peppol: Invoice %s — status updated from %s → %s.',
                    self.name,
                    old_state.upper(),
                    new_state.upper(),
                )
            )

        _logger.info(
            'TCA: invoice %s (id=%s) state %s → %s', self.name, self.id, old_state, new_state
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
            pending_invoices = self.env['account.move'].search(
                [
                    ('company_id', '=', company.id),
                    ('tca_move_state', 'in', ['submitted', 'processing']),
                    ('tca_invoice_uuid', '!=', False),
                ],
                limit=100,
            )

            if not pending_invoices:
                continue

            # ── Step 1: Fetch IDs TCA still considers in-flight ───────────────
            still_processing_ids = set()
            try:
                result = api_svc.list_processing_outbound(company, limit=200)
                tca_list = result.get('results', result) if isinstance(result, dict) else result
                still_processing_ids = {
                    str(item.get('id', '')) for item in tca_list if item.get('id')
                }
            except Exception as exc:
                _logger.warning(
                    'TCA cron: list_processing_outbound failed for company %s (%s), '
                    'falling back to per-invoice poll',
                    company.id,
                    exc,
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
        except TcaTransientError as exc:
            # Transient — leave state unchanged, cron will retry next run.
            # `TcaTransientError` covers URLError / 5xx / timeouts / S3 hiccups —
            # raised explicitly by tca_api so we no longer substring-match
            # 'timeout' / '503' / 'cannot reach' on the exception text.
            _logger.warning(
                'TCA cron: transient error polling invoice %s (uuid=%s), will retry: %s',
                invoice.name,
                invoice.tca_invoice_uuid,
                exc,
            )
        except Exception as exc:  # noqa: BLE001 — permanent failure path
            # Permanent — mark as error so user investigates.
            _logger.error(
                'TCA cron: permanent error polling invoice %s (uuid=%s): %s',
                invoice.name,
                invoice.tca_invoice_uuid,
                exc,
            )
            invoice.write(
                {
                    'tca_move_state': 'error',
                    'tca_submission_error': str(exc),
                }
            )
            invoice._message_log(body=_('TCA cron: status poll failed — %s', exc))

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
          G-6: Cursor-based tracking via tca_last_inbound_sync ir.config_parameter
               — only fetches invoices created after the last successful run
               (using created_after query param if supported, otherwise UUID
               deduplication for the full list).

        Deduplication: tca_invoice_uuid — already-imported invoices are skipped.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        active_companies = self.env['res.company'].search([('tca_is_active', '=', True)])
        api_svc = self.env['tca.api.service']

        for company in active_companies:
            cursor_key = f'tca.{company.id}.last_inbound_sync'
            last_sync = ICP.get_param(cursor_key, '')

            try:
                self._tca_pull_inbound_for_company(api_svc, company, ICP, cursor_key, last_sync)
            except Exception as exc:
                _logger.error(
                    'TCA cron: failed to pull inbound invoices for company %s: %s', company.id, exc
                )

    @api.model
    def _tca_pull_inbound_for_company(self, api_svc, company, ICP, cursor_key, last_sync):
        """
        Pull and import all new inbound invoices for one company.
        Paginates through all result pages (G-4).
        Updates the cursor param after each successful import batch (G-6).
        """
        # Fetch page 1 — pass created_after cursor if TCA supports it
        result = api_svc.list_inbound_invoices(company, limit=50)

        latest_created_at = last_sync  # track newest timestamp seen this run

        page_invoices = result.get('results', result) if isinstance(result, dict) else result
        next_url = result.get('next') if isinstance(result, dict) else None

        while True:
            for tca_invoice in page_invoices:
                tca_id = tca_invoice.get('id')
                xml_location_path = tca_invoice.get('document_location_path') or tca_invoice.get(
                    'invoice_xml_location_path'
                )
                created_at = str(tca_invoice.get('created_at') or '')

                if not tca_id:
                    continue

                # G-6: skip if older than cursor (already imported in a prior run)
                if last_sync and created_at and created_at < last_sync:
                    continue

                # Deduplication — belt-and-suspenders after cursor check
                existing = self.env['account.move'].search(
                    [
                        ('tca_invoice_uuid', '=', tca_id),
                        ('company_id', '=', company.id),
                    ],
                    limit=1,
                )
                if existing:
                    continue

                if not xml_location_path:
                    # List endpoint omits xml path — fetch detail (same as webhook).
                    # Backend returns it on single GET via `invoice_xml_location_path`.
                    try:
                        detail = api_svc.get_invoice_status(company, tca_id)
                        xml_location_path = detail.get('document_location_path') or detail.get(
                            'invoice_xml_location_path'
                        )
                    except Exception as exc:
                        _logger.warning(
                            'TCA cron: failed to fetch detail for inbound id=%s: %s', tca_id, exc
                        )
                        continue
                    if not xml_location_path:
                        _logger.warning(
                            'TCA cron: inbound invoice id=%s has no XML path even after detail fetch, skipping',
                            tca_id,
                        )
                        continue

                # G-2: commit between imports — isolate failures.
                # After commit, the ORM cache must be invalidated: records held
                # in-memory may be stale relative to other concurrent transactions
                # that ran while we were doing HTTP work for this iteration.
                move = self._tca_import_inbound_invoice(company, tca_id, xml_location_path, api_svc)
                self.env.cr.commit()
                self.env.invalidate_all()

                if move and created_at > latest_created_at:
                    latest_created_at = created_at

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

        # G-6: advance cursor to latest invoice seen this run
        if latest_created_at and latest_created_at > last_sync:
            ICP.set_param(cursor_key, latest_created_at)
            _logger.info(
                'TCA cron: updated last_inbound_sync for company %s to %s',
                company.id,
                latest_created_at,
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
                tca_id,
                xml_location_path,
                exc,
            )
            # No stub created — cron will retry on next run (cursor doesn't advance).
            # Log to company partner chatter so admins are aware.
            company.partner_id._message_log(
                body=_(
                    'TCA Peppol: failed to download inbound invoice XML (ID: %s). '
                    'Error: %s. Will retry on next cron run.',
                    tca_id,
                    exc,
                )
            )
            return None

        # Find a purchase journal for this company
        journal = self.env['account.journal'].search(
            [
                ('type', '=', 'purchase'),
                ('company_id', '=', company.id),
            ],
            limit=1,
        )
        if not journal:
            _logger.error('TCA: no purchase journal found for company %s', company.id)
            return None

        # Create attachment
        filename = f'tca_inbound_{tca_id}.xml'
        try:
            attachment = self.env['ir.attachment'].create(
                {
                    'name': filename,
                    'datas': b64encode(xml_bytes),
                    'res_model': 'account.journal',
                    'res_id': journal.id,
                    'type': 'binary',
                    'mimetype': 'application/xml',
                }
            )
        except Exception as exc:
            _logger.error('TCA: attachment creation failed for id %s: %s', tca_id, exc)
            return None

        # Use Odoo's standard UBL import pipeline.
        # _create_document_from_attachment routes via _get_import_file_type
        # (Odoo 19; replaces the removed _get_ubl_cii_builder_from_xml_tree)
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
            move = self.env['account.move'].create(
                {
                    'move_type': 'in_invoice',
                    'journal_id': journal.id,
                    'company_id': company.id,
                    'tca_invoice_uuid': tca_id,
                    'tca_move_state': 'inbound_received',
                    'tca_is_inbound': True,
                    'tca_inbound_status': 'pending',
                    'ref': f'TCA-{tca_id}',
                }
            )
            attachment.write({'res_model': 'account.move', 'res_id': move.id})
            move._message_log(
                body=_(
                    'TCA Peppol: UBL parse failed for inbound invoice (ID: %s). '
                    'The raw XML is attached. Please fill in the details manually.',
                    tca_id,
                )
            )
            _logger.warning(
                'TCA: created stub vendor bill for id %s after UBL parse failure', tca_id
            )
            return move

        if move:
            move.sudo().write(
                {
                    'tca_invoice_uuid': tca_id,
                    'tca_move_state': 'inbound_received',
                    'tca_is_inbound': True,
                    'tca_inbound_status': 'pending',
                }
            )
            move._message_log(body=_('Invoice imported from TCA Peppol network (ID: %s).', tca_id))
        else:
            _logger.warning(
                'TCA: _create_document_from_attachment returned empty for id %s', tca_id
            )
            move = self.env['account.move'].create(
                {
                    'move_type': 'in_invoice',
                    'journal_id': journal.id,
                    'company_id': company.id,
                    'tca_invoice_uuid': tca_id,
                    'tca_move_state': 'inbound_received',
                    'tca_is_inbound': True,
                    'tca_inbound_status': 'pending',
                    'ref': f'TCA-{tca_id}',
                }
            )
            attachment.write({'res_model': 'account.move', 'res_id': move.id})
            move._message_log(
                body=_(
                    'TCA Peppol: import returned empty for inbound invoice (ID: %s). '
                    'The raw XML is attached. Please fill in the details manually.',
                    tca_id,
                )
            )

        return move

    # ──────────────────────────────────────────────────────────────────────────
    # MANUAL RESEND
    # ──────────────────────────────────────────────────────────────────────────

    def action_tca_resend(self):
        """
        Resend a failed/rejected invoice to TCA.
        If tca_invoice_uuid exists, uses PUT /resubmit/ endpoint (re-uploads XML
        and retries the existing TCA record). Otherwise opens the Send & Print
        wizard for a full 3-step submission.
        """
        self.ensure_one()
        if self.tca_move_state not in ('error', 'rejected'):
            raise UserError(
                _(
                    'Invoice %s cannot be resent — current TCA state is "%s".',
                    self.name,
                    self.tca_move_state,
                )
            )

        # If we have a TCA ID, use the resubmit endpoint (avoids duplicate 409)
        if self.tca_invoice_uuid:
            return self._tca_resubmit_existing()

        # No TCA ID — go through full wizard flow.
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

    def _tca_resubmit_existing(self):
        """
        Resubmit an invoice that already has a tca_invoice_uuid.
        Uses PUT /api/v1/invoices/{id}/resubmit/ — re-uploads XML and retries.
        """
        self.ensure_one()
        api_svc = self.env['tca.api.service']
        company = self.company_id

        # Generate fresh XML
        builder = self.env['account.edi.xml.ubl_pint_ae']
        xml_content, errors = builder._export_invoice(self)
        if errors:
            raise UserError(_('PINT AE XML generation failed:\n%s', '\n'.join(errors)))
        xml_bytes = xml_content if isinstance(xml_content, bytes) else xml_content.encode()

        try:
            # Upload new XML to S3
            self.tca_move_state = 'uploading'
            upload_response = api_svc.get_document_upload_url(
                company, filename=f'{self.name.replace("/", "_")}_pint_ae.xml'
            )
            upload_url = upload_response.get('upload_url')
            source_file_path = (
                upload_response.get('path')
                or upload_response.get('s3_uri')
                or upload_response.get('s3_path')
                or upload_response.get('file_key')
            )
            if not upload_url or not source_file_path:
                raise UserError(_('TCA did not return a valid upload URL.'))

            api_svc.upload_to_s3(upload_url, xml_bytes)

            # Call resubmit endpoint (return value unused — endpoint is
            # side-effecting; success is signalled by absence of exception).
            api_svc.resubmit_invoice(
                company=company,
                tca_id=self.tca_invoice_uuid,
                name=self.name,
                source_file_path=source_file_path,
            )

            self.write(
                {
                    'tca_move_state': 'submitted',
                    'tca_submission_error': False,
                }
            )
            self._message_log(
                body=_(
                    'Invoice resubmitted to TCA via /resubmit/ endpoint. ID: %s',
                    self.tca_invoice_uuid,
                )
            )
            _logger.info('TCA: invoice %s resubmitted. ID=%s', self.name, self.tca_invoice_uuid)

        except Exception as exc:
            self.write(
                {
                    'tca_move_state': 'error',
                    'tca_submission_error': str(exc),
                }
            )
            self._message_log(body=_('TCA resubmission failed: %s', exc))
            raise UserError(_('TCA resubmission failed: %s', exc)) from exc

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Invoice Resubmitted'),
                'message': _('Invoice %s has been resubmitted to TCA.', self.name),
                'type': 'success',
                'sticky': False,
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
            raise UserError(
                _('This action is only available for invoices received via TCA Peppol.')
            )
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
            raise UserError(
                _('This action is only available for invoices received via TCA Peppol.')
            )
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

        self._message_log(body=_('Inbound invoice rejected.\nReason: %s', reason))

        # TODO: When TCA adds an Invoice Response endpoint, send RE (Rejected)
        # response back to the seller via:
        #   api_svc.send_invoice_response(company, tca_id, response_code='RE',
        #                                  reason=reason)

        _logger.info('TCA: inbound invoice %s rejected. Reason: %s', self.name, reason)
