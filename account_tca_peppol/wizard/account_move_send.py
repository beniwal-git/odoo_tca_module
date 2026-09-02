# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
Override of account.move.send wizard to intercept the Peppol send step
and route outbound invoices through TCA instead of the native IAP proxy.

Flow:
  1. User opens "Send & Print" dialog for a posted invoice
  2. Wizard shows checkbox_send_tca when company has TCA active + partner is PINT AE
  3. User confirms → action_send_and_print() is called
  4. _call_web_service_after_invoice_pdf_render() is our override:
     a. Build the PINT AE JSON `detail` tree from the invoice's own fields
     b. POST /api/v1/invoices/  { name, invoice_number, detail } — TCA
        builds the UBL and validates it (incl. the official schematron)
        server-side, synchronously: 201 = accepted, 400 = per-field errors.
     c. Store response 'id' as tca_invoice_uuid; set tca_move_state = 'submitted'
     d. On error: set tca_move_state = 'error' (or 'submitted' + retry-cron
        pickup for a transient network/5xx failure) + log to chatter
"""

import logging

from odoo import _, api, fields, models

from ..services.tca_api import TcaTransientError, TcaValidationError

_logger = logging.getLogger(__name__)


class AccountMoveSend(models.TransientModel):
    _inherit = 'account.move.send'

    # ── New wizard field ───────────────────────────────────────────────────────

    checkbox_send_tca = fields.Boolean(
        string='Send via TCA Peppol',
        compute='_compute_checkbox_send_tca',
        store=True,
        readonly=False,
        help='Submit this invoice to the TCA Peppol Access Point for UAE e-invoicing.',
    )
    enable_tca = fields.Boolean(compute='_compute_enable_tca')
    tca_warning = fields.Char(string='TCA Warning', compute='_compute_tca_warning')

    # ──────────────────────────────────────────────────────────────────────────
    # COMPUTE
    # ──────────────────────────────────────────────────────────────────────────

    @api.depends('move_ids', 'move_ids.tca_create_einvoice', 'enable_ubl_cii_xml')
    def _compute_enable_tca(self):
        """
        Show the TCA Peppol send option when:
          - The company has TCA integration active
          - At least one move still has the per-document "Create E-Invoice"
            toggle on — a move with it off must never be auto-queued for
            TCA submission from this wizard (that's the whole point of the
            toggle; see tca_create_einvoice's help text)
          - The invoices are not already in-flight on TCA

        The buyer's Peppol registration is NOT checked here — TCA handles
        routing to the buyer's AP. The seller just uploads the XML.
        """
        for wizard in self:
            if not wizard.company_id.tca_is_active:
                wizard.enable_tca = False
                continue
            if not any(wizard.move_ids.mapped('tca_create_einvoice')):
                wizard.enable_tca = False
                continue

            wizard.enable_tca = not all(
                m.tca_move_state in ('processing', 'delivered', 'buyer_confirmed')
                for m in wizard.move_ids
            )

    @api.depends('enable_tca', 'move_ids', 'tca_warning')
    def _compute_checkbox_send_tca(self):
        """
        Auto-tick the TCA send checkbox when:
          - TCA option is available (enable_tca = True)
          - No configuration warning
          - Company has 'invoice_is_tca' set (analogous to invoice_is_ubl_cii)
        """
        for wizard in self:
            wizard.checkbox_send_tca = (
                wizard.enable_tca
                and not wizard.tca_warning
                and wizard.company_id.invoice_is_tca
            )

    @api.depends('move_ids')
    def _compute_tca_warning(self):
        for wizard in self:
            invalid = wizard.move_ids.partner_id.commercial_partner_id.filtered(
                lambda p: p.ubl_cii_format == 'ubl_pint_ae'
                and (not p.peppol_eas or not p.peppol_endpoint)
            )
            if invalid:
                names = ', '.join(invalid[:3].mapped('display_name'))
                wizard.tca_warning = _(
                    'Partners missing Peppol EAS/Endpoint: %s. '
                    'Configure Peppol settings on the partner before sending.', names
                )
            else:
                wizard.tca_warning = False

    # ──────────────────────────────────────────────────────────────────────────
    # WIZARD VALUES
    # ──────────────────────────────────────────────────────────────────────────

    def _get_wizard_values(self):
        values = super()._get_wizard_values()
        values['send_tca'] = self.checkbox_send_tca
        return values

    @api.model
    def _get_wizard_vals_restrict_to(self, only_options):
        values = super()._get_wizard_vals_restrict_to(only_options)
        return {'checkbox_send_tca': False, **values}

    # ──────────────────────────────────────────────────────────────────────────
    # ENSURE XML IS GENERATED WHEN SENDING VIA TCA (but hide the separate checkbox)
    # ──────────────────────────────────────────────────────────────────────────

    @api.depends('checkbox_send_tca')
    def _compute_checkbox_ubl_cii_xml(self):
        super()._compute_checkbox_ubl_cii_xml()
        for wizard in self:
            if wizard.checkbox_send_tca and wizard.enable_ubl_cii_xml:
                wizard.checkbox_ubl_cii_xml = True

    def _needs_ubl_cii_placeholder(self):
        # Hide the "PINT AE (UAE Peppol)" XML checkbox when sending via TCA —
        # TCA handles XML generation internally, user doesn't need to see it separately
        return super()._needs_ubl_cii_placeholder() and not self.checkbox_send_tca

    # ──────────────────────────────────────────────────────────────────────────
    # XML POST-PROCESSING — fix XSD order for PDF AdditionalDocumentReference
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _postprocess_invoice_ubl_xml(self, invoice, invoice_data):
        """
        EXTENDS account_edi_ubl_cii wizard.

        Parent inserts the PDF AdditionalDocumentReference at the index of
        AccountingSupplierParty, putting it AFTER our PINT AE ProjectReference
        (which was injected before AccountingSupplierParty via QWeb xpath).
        UBL InvoiceType XSD requires AdditionalDocumentReference < ProjectReference,
        so the parent's anchor choice produces an XSD-invalid sequence for PINT AE.

        Workaround: temporarily inject a sentinel ProjectReference removal so
        parent's anchor lookup hits the correct insertion point, then restore.
        Cleaner: re-walk the tree after super() and reorder if needed.
        """
        super()._postprocess_invoice_ubl_xml(invoice, invoice_data)

        from lxml import etree
        try:
            raw = invoice_data.get('ubl_cii_xml_attachment_values', {}).get('raw')
            if not raw:
                return
            tree = etree.fromstring(raw)
            # Find positions of AdditionalDocumentReference (last) + ProjectReference
            adrs = tree.xpath("./*[local-name()='AdditionalDocumentReference']")
            if not adrs:
                return
            project_refs = tree.xpath("./*[local-name()='ProjectReference']")
            if not project_refs:
                return
            last_adr = adrs[-1]
            first_project = project_refs[0]
            adr_idx = tree.index(last_adr)
            project_idx = tree.index(first_project)
            # If AdditionalDocumentReference is AFTER ProjectReference, swap to fix XSD order
            if adr_idx > project_idx:
                tree.remove(last_adr)
                tree.insert(project_idx, last_adr)
                invoice_data['ubl_cii_xml_attachment_values']['raw'] = etree.tostring(
                    tree, xml_declaration=True, encoding='UTF-8'
                )
        except Exception as exc:
            _logger.warning('TCA: PINT AE postprocess reorder failed: %s', exc)

    # ──────────────────────────────────────────────────────────────────────────
    # SEND ACTION
    # ──────────────────────────────────────────────────────────────────────────

    def action_send_and_print(self, force_synchronous=False, allow_fallback_pdf=False, **kwargs):
        """
        EXTENDS account.move.send.
        Mark invoices as 'uploading' before sending so the user sees
        immediate state feedback.
        """
        self.ensure_one()
        if self.checkbox_send_tca and self.enable_tca:
            # Ensure XML checkbox is ticked
            if self.enable_ubl_cii_xml and not self.checkbox_ubl_cii_xml:
                self.checkbox_ubl_cii_xml = True
            # Mark as uploading (will be updated in _call_web_service_after_invoice_pdf_render)
            for move in self.move_ids:
                if move.tca_move_state in ('not_sent', 'error', 'rejected'):
                    move.sudo().tca_move_state = 'uploading'
        return super().action_send_and_print(
            force_synchronous=force_synchronous,
            allow_fallback_pdf=allow_fallback_pdf,
            **kwargs
        )

    # ──────────────────────────────────────────────────────────────────────────
    # MAIN SEND OVERRIDE — this is where TCA submission happens
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _call_web_service_after_invoice_pdf_render(self, invoices_data):
        """
        OVERRIDES account.move.send (from account_edi_ubl_cii or base).
        Handles TCA submission for invoices flagged with send_tca=True.

        Submission is inline-JSON (POST /api/v1/invoices/ with a `detail`
        tree) — TCA builds the UBL and runs the official PINT AE schematron
        server-side, synchronously. No client-side XML build or S3 upload
        for the submission itself (the UBL XML attachment, if
        checkbox_ubl_cii_xml is also ticked, is still generated by the
        parent call above for record-keeping/PDF-embedding — just not used
        as the wire payload).
        """
        super()._call_web_service_after_invoice_pdf_render(invoices_data)

        from psycopg2 import OperationalError

        api_svc = self.env['tca.api.service']

        for invoice, invoice_data in invoices_data.items():
            if not invoice_data.get('send_tca'):
                continue
            if invoice.tca_move_state in ('processing', 'delivered', 'buyer_confirmed'):
                _logger.info('TCA: skipping already-submitted invoice %s', invoice.name)
                continue
            # send_tca is a single wizard-level checkbox applied uniformly to
            # every move in the batch (account.move.send._process_send_and_print
            # spreads _get_wizard_values() across all moves_data identically) —
            # it does NOT vary per invoice. Re-check the per-document opt-out
            # here so a batch send with the TCA checkbox on can't submit an
            # invoice whose "Create E-Invoice" toggle is off.
            if not invoice.tca_create_einvoice:
                _logger.info(
                    'TCA: skipping %s — "Create E-Invoice" is off for this document.',
                    invoice.name
                )
                continue

            # Resolve company per invoice. Standard `account.move.send`
            # supports multi-company batches; using a single company for the
            # whole batch would route every invoice through the first
            # invoice's TCA credentials and corrupt state on the others.
            company = invoice.company_id

            # ── Idempotency guard — lock invoice row ──────────────────────────
            try:
                with self.env.cr.savepoint(flush=False):
                    self.env.cr.execute(
                        'SELECT id FROM account_move WHERE id = %s FOR UPDATE NOWAIT',
                        [invoice.id]
                    )
            except OperationalError:
                _logger.info('TCA: invoice %s locked by another transaction, skipping', invoice.name)
                continue

            # ── Validate partner Peppol config ────────────────────────────────
            partner = invoice.partner_id.commercial_partner_id
            if not partner.peppol_eas or not partner.peppol_endpoint:
                invoice.tca_move_state = 'error'
                error_msg = _('Partner %s is missing Peppol EAS and/or Endpoint.', partner.name)
                invoice.tca_submission_error = error_msg
                invoice_data['error'] = error_msg
                continue

            # ── Pre-submission PINT AE validation (local rule replica) ───────
            # Only run for PINT AE partners — running PINT-AE-specific vals/constraints
            # on a non-PINT-AE builder (e.g. plain bis3) produces irrelevant keys and
            # may surface false-positive errors.
            if partner.ubl_cii_format == 'ubl_pint_ae':
                builder = partner._get_edi_builder()
                if hasattr(builder, '_export_invoice_constraints'):
                    try:
                        vals = builder._export_invoice_vals(invoice)
                        constraints = builder._export_invoice_constraints(invoice, vals)
                        # Parent returns {key: None} for passed checks — filter them out
                        errors = {k: v for k, v in constraints.items() if v}
                        if errors:
                            error_msg = '\n'.join(errors.values())
                            invoice.tca_move_state = 'error'
                            invoice.tca_submission_error = error_msg
                            invoice_data['error'] = error_msg
                            invoice._message_log(body=_('TCA PINT AE validation failed:\n%s', error_msg))
                            continue
                    except Exception as exc:
                        _logger.warning('TCA: pre-validation failed for %s: %s', invoice.name, exc)

            # ── Build the JSON detail tree and submit ─────────────────────────
            # invoice_number is the record's own name (_tca_build_submission_id
            # on account.move — same helper used by the credit-note atomic-post
            # path, account_move._tca_submit_outbound, so both stay in sync).
            invoice.tca_move_state = 'uploading'
            try:
                detail = invoice._tca_build_json_detail()
            except Exception as exc:
                invoice.tca_move_state = 'error'
                invoice.tca_submission_error = str(exc)
                invoice_data['error'] = str(exc)
                invoice._message_log(body=_('TCA: failed to build submission payload: %s', exc))
                continue

            submission_id = invoice._tca_build_submission_id()
            try:
                result = api_svc.submit_invoice_json(
                    company=company,
                    name=submission_id,
                    invoice_number=submission_id,
                    detail=detail,
                )
            except TcaValidationError as exc:
                error_msg = '\n'.join(exc.tca_field_errors) or str(exc)
                invoice.tca_move_state = 'error'
                invoice.tca_submission_error = error_msg
                invoice_data['error'] = error_msg
                invoice._message_log(body=_('TCA rejected the invoice content:\n%s', error_msg))
                continue
            except TcaTransientError as exc:
                # Transient error — keep as 'submitted' so the retry cron picks it up.
                invoice.tca_move_state = 'submitted'
                invoice.tca_submission_error = str(exc)
                invoice._message_log(body=_('TCA Peppol: transient submission error, will retry: %s', exc))
                if self._can_commit():
                    self._cr.commit()
                continue
            except Exception as exc:
                invoice.tca_move_state = 'error'
                invoice.tca_submission_error = str(exc)
                invoice_data['error'] = str(exc)
                invoice._message_log(body=_('TCA invoice submission error: %s', exc))
                continue

            # ── Handle 409/400-already-exists duplicate as success ───────────
            # A resubmit reuses the same invoice_number, so this is the
            # expected path when the prior attempt actually made it through
            # despite the error that triggered this resend.
            if result.get('tca_duplicate'):
                _logger.info('TCA: invoice %s already exists on TCA, treating as success', invoice.name)
                invoice.write({
                    'tca_move_state': 'submitted',
                    'tca_submission_error': False,
                    'tca_last_submission_id': submission_id,
                })
                invoice._message_log(body=_('TCA: Invoice already registered (duplicate). Status will sync via cron.'))
                if self._can_commit():
                    self._cr.commit()
                continue

            # ── Success: store TCA id, submission id, mark submitted ─────────
            tca_id = result.get('id', '')
            invoice.write({
                'tca_invoice_uuid': tca_id,
                'tca_move_state': 'submitted',
                'tca_submission_error': False,
                'tca_last_submission_id': submission_id,
            })
            invoice._message_log(
                body=_('Invoice submitted to TCA Peppol network (validated on submission). '
                       'TCA invoice_number: %(sid)s — TCA ID: %(tid)s',
                       sid=submission_id, tid=tca_id)
            )
            _logger.info('TCA: invoice %s submitted (sid=%s, tcaid=%s)', invoice.name, submission_id, tca_id)

            # ── Per-invoice commit — don't lose this on later failures ───────
            if self._can_commit():
                self._cr.commit()

    # ──────────────────────────────────────────────────────────────────────────
    # ERROR HOOK
    # ──────────────────────────────────────────────────────────────────────────

    def _hook_if_errors(self, moves_data, from_cron=False, allow_fallback_pdf=False):
        """
        EXTENDS account.move.send.
        Reset tca_move_state to 'error' for moves that failed PDF/XML generation.
        """
        for move, move_data in moves_data.items():
            if move_data.get('send_tca') and move_data.get('blocking_error'):
                move.tca_move_state = 'error'
                move.tca_submission_error = move_data.get('error', 'PDF/XML generation failed.')
        return super()._hook_if_errors(
            moves_data, from_cron=from_cron, allow_fallback_pdf=allow_fallback_pdf
        )
