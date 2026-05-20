# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
TCA Send-flow integration — Odoo 19.

Odoo 18/19 rewrote the Send & Print stack:
  - `account.move.send` is now an abstract *engine* model (mixed into the
    per-invoice `account.move.send.wizard` / batch `account.move.send.batch.wizard`).
  - Per-EDI checkboxes were replaced by the JSON `extra_edis` registry,
    populated via `_get_all_extra_edis()`. The wizard renders one checkbox
    per registered key automatically — no custom view is needed.

This module re-adds the "Submit via TCA Peppol" option against that new API
(replacing the removed Odoo-17 `checkbox_send_tca` field + form view):

  - `_get_all_extra_edis` registers the `tca_peppol` extra EDI. Its
    `is_applicable` callback delegates to `account.move._tca_is_send_eligible()`
    — the single source of truth for outbound eligibility (TCA active, posted,
    outbound, PINT AE partner, resubmittable state).
  - The Odoo 19 Send wizard renders one checkbox per key in the *default* set
    (`_get_default_extra_edis`), each pre-checked — there is no "shown but
    unchecked" state. The base `_get_default_extra_edis` already includes a key
    whenever its `is_applicable` callback returns True, so the `tca_peppol`
    checkbox is shown (and pre-checked) for every send-eligible PINT AE invoice
    with no extra override needed.
  - `_call_web_service_after_invoice_pdf_render` runs the existing 3-step
    TCA submission (`account.move._tca_submit_outbound`) after the PDF + UBL
    XML are generated.

Compliance: a TCA submission failure must surface as a hard error, never pass
silently. On failure the move's `tca_move_state` is rolled back to `error`,
`tca_submission_error` is recorded, and `invoice_data['error']` is set so the
Send & Print wizard reports it to the user (and, for batch sends, blocks the
mail/attachment step for that invoice).

Note on credit notes: credit-note submission is *atomic with Confirm* via
`account.move._post()` Phase 3 — it does not depend on this wizard. By the
time the Send dialog runs, a credit note has already left `not_sent`, so
`_tca_is_send_eligible()` returns False for it and the hook below skips it.
This wizard path therefore drives regular outbound invoices only.
"""

import logging

from odoo import _, api, models

_logger = logging.getLogger(__name__)

# Registry key for the TCA Peppol extra EDI in the Send & Print `extra_edis`
# JSON map. Referenced by `invoice_data['extra_edis']` in the engine hook.
TCA_EDI_KEY = 'tca_peppol'


class AccountMoveSend(models.AbstractModel):
    _inherit = 'account.move.send'

    # ──────────────────────────────────────────────────────────────────────────
    # EXTRA-EDI REGISTRY
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _is_tca_peppol_applicable(self, move):
        """
        `is_applicable` callback for the `tca_peppol` extra EDI.

        Delegates to `account.move._tca_is_send_eligible()` so the Send wizard
        and the `_post()` compliance gate share one definition of "can this
        document still be submitted to TCA?".
        """
        return move._tca_is_send_eligible()

    @api.model
    def _get_all_extra_edis(self):
        """
        EXTENDS account.move.send.

        Register the TCA Peppol outbound submission as an extra EDI so the
        Odoo 19 Send & Print wizard renders a "Submit via TCA Peppol" checkbox.
        """
        res = super()._get_all_extra_edis()
        res[TCA_EDI_KEY] = {
            'label': _("Submit via TCA Peppol (UAE)"),
            'is_applicable': self._is_tca_peppol_applicable,
            'help': _(
                "Send this invoice to the UAE Peppol network through the TCA "
                "Access Point as a PINT AE e-invoice."
            ),
        }
        return res

    # The `tca_peppol` checkbox is shown + pre-checked whenever the invoice is
    # send-eligible: the base `account.move.send._get_default_extra_edis` adds
    # every key whose `is_applicable` callback returns True, and ours delegates
    # to `_tca_is_send_eligible()`. No `_get_default_extra_edis` override needed.

    # ──────────────────────────────────────────────────────────────────────────
    # WEB-SERVICE HOOK — OUTBOUND SUBMISSION
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _call_web_service_after_invoice_pdf_render(self, invoices_data):
        """
        EXTENDS account.move.send.

        After the PDF + UBL XML are generated, submit every invoice whose
        selected `extra_edis` includes `tca_peppol` to the TCA Peppol network.

        Submission runs through `account.move._tca_submit_outbound()` — the
        existing 3-step flow (build PINT AE XML → presigned S3 PUT → register
        with TCA). That method raises `UserError` on any failure; this hook
        catches it, rolls the TCA state machine back to `error`, records
        `tca_submission_error`, and writes `invoice_data['error']` so the
        failure is reported (and not silently swallowed) — honouring the
        module's compliance-first rule.
        """
        super()._call_web_service_after_invoice_pdf_render(invoices_data)

        for invoice, invoice_data in invoices_data.items():
            if TCA_EDI_KEY not in invoice_data.get('extra_edis', set()):
                continue

            # Re-check eligibility at submission time: in a batch send another
            # invoice's failure (or a webhook) may have advanced this move's
            # tca_move_state since the wizard computed the default EDIs.
            if not invoice._tca_is_send_eligible():
                _logger.info(
                    "TCA: skipping %s — no longer send-eligible (state=%s).",
                    invoice.name, invoice.tca_move_state,
                )
                continue

            try:
                invoice._tca_submit_outbound()
            except Exception as exc:  # noqa: BLE001 — surface every failure
                _logger.exception(
                    "TCA: outbound submission failed for %s via Send & Print.",
                    invoice.name,
                )
                # _tca_submit_outbound() leaves tca_move_state at 'uploading'
                # if it fails mid-flight; roll it back to 'error' so the
                # invoice stays resubmittable and the dashboard shows the fault.
                invoice.write({
                    'tca_move_state': 'error',
                    'tca_submission_error': str(exc),
                })
                invoice._message_log(body=_(
                    "TCA Peppol submission failed via Send & Print: %s", exc,
                ))
                invoice_data['error'] = {
                    'error_title': _(
                        "Could not submit invoice %s to the TCA Peppol network:",
                        invoice.name,
                    ),
                    'errors': [str(exc)],
                }

            if self._can_commit():
                self.env.cr.commit()
