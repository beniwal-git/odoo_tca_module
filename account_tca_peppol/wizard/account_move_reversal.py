# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
Override of Odoo's account.move.reversal wizard.

On TCA-active companies, both reversal buttons ("Reverse" and "Reverse and
Create Invoice") redirect the user to the **credit note** record — the one
that will be sent to TCA Peppol as a PINT AE credit-note XML.

Standard Odoo behavior on "Reverse and Create Invoice" redirects to the
replacement invoice draft (a copy of the original), which is confusing in
an e-invoicing context: the user thinks they are looking at the credit note
they will submit, when in fact they are looking at a new tax invoice.
"""

from odoo import _, api, fields, models

# Import the same selection list so wizard and account.move stay in sync.
from odoo.addons.account_tca_peppol.models.account_move import CREDIT_NOTE_REASONS


class AccountMoveReversal(models.TransientModel):
    _inherit = 'account.move.reversal'

    # ── BTAE-03 Credit Note Reason picker on the wizard ───────────────────────
    # Lets the user choose the structured PINT AE reason code BEFORE clicking
    # Reverse, so the new credit note has the code populated and our validator
    # doesn't block the auto-post.
    tca_credit_note_reason = fields.Selection(
        selection=CREDIT_NOTE_REASONS,
        string='Credit Note Reason (BTAE-03)',
        help='Mandatory PINT AE reason code for the credit note. '
             '"VD" (Volume Discount) is the only value that does NOT require a '
             'preceding invoice reference; all other codes link to the original.',
    )
    tca_company_is_active = fields.Boolean(
        compute='_compute_tca_company_is_active',
        store=False,
    )

    @api.depends('company_id')
    def _compute_tca_company_is_active(self):
        for rec in self:
            rec.tca_company_is_active = bool(rec.company_id.tca_is_active)

    def _prepare_default_reversal(self, move):
        """EXTENDS account.move.reversal.
        Pass the wizard's tca_credit_note_reason into the new credit note's
        defaults so the structured BTAE-03 code is set on creation.
        """
        defaults = super()._prepare_default_reversal(move)
        if self.tca_credit_note_reason:
            defaults['tca_credit_note_reason'] = self.tca_credit_note_reason
        return defaults

    def reverse_moves(self, is_modify=False):
        """
        OVERRIDES account.move.reversal for TCA-active companies.

        Standard Odoo on "Reverse and Create Invoice" passes cancel=True to
        _reverse_moves, which immediately auto-posts the credit note and
        auto-reconciles it with the original. That blocks the user from
        editing the credit note (e.g. changing line quantities for a partial
        credit) before submission.

        For UAE e-invoicing we need:
          - Credit notes stay in DRAFT after the wizard closes, so the user
            can edit them (partial credit, line tweaks).
          - Confirm on the draft credit note triggers our atomic post + TCA
            submit flow (account_move._post()).
          - Redirect always lands on the credit note, regardless of which
            wizard button was clicked.

        Non-TCA companies keep the standard Odoo flow untouched.
        """
        self.ensure_one()

        # Non-TCA path: delegate to the standard Odoo wizard.
        if not self.move_ids or not getattr(
            self.move_ids[:1].company_id, 'tca_is_active', False
        ):
            return super().reverse_moves(is_modify=is_modify)

        moves = self.move_ids

        # Build defaults from the wizard (includes tca_credit_note_reason via
        # our _prepare_default_reversal override).
        default_values_list = [
            {'partner_bank_id': False, **self._prepare_default_reversal(move)}
            for move in moves
        ]

        # Create the credit notes. cancel=False keeps them in DRAFT state —
        # no auto-post, no auto-reconciliation with the original.
        new_moves = moves._reverse_moves(default_values_list, cancel=False)
        new_moves._compute_partner_bank_id()
        moves._message_log_batch(
            bodies={
                move.id: _('This entry has been %s', reverse._get_html_link(title=_("reversed")))
                for move, reverse in zip(moves, new_moves, strict=True)
            }
        )

        moves_to_redirect = new_moves

        # is_modify is intentionally IGNORED in the TCA flow. Standard Odoo
        # would here create a draft replacement invoice (a copy of the
        # original for re-issuing corrections). UAE e-invoicing scope:
        # we only want the credit note — if a corrected invoice is needed,
        # it should be a separate, deliberate creation. The
        # "Reverse and Create Invoice" wizard button is also hidden for
        # TCA companies in views/account_move_views.xml, but this branch
        # is removed defensively in case the flow is reached programmatically.

        self.new_move_ids = moves_to_redirect

        # Build the action — redirect lands on the credit note (always).
        action = {
            'name': _('Credit Note'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
        }
        if len(moves_to_redirect) == 1:
            action.update({
                'view_mode': 'form',
                'res_id': moves_to_redirect.id,
                'context': {'default_move_type': moves_to_redirect.move_type},
            })
        else:
            action.update({
                'view_mode': 'list,form',
                'domain': [('id', 'in', moves_to_redirect.ids)],
                'context': {'default_move_type': moves_to_redirect[:1].move_type},
            })
        return action
