# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
TCA Webhook Controller

Receives event notifications from the TCA backend:
  - STATUS_UPDATE: outbound invoice delivery status changed
  - DOCUMENT_RECEIVED: new inbound invoice arrived from Peppol network
  - VALIDATION_FAILED: invoice failed TCA internal validation

Endpoint: POST /tca/webhook/invoice-status?company_id={id}

Security:
  HMAC-SHA256 signature is validated against the company's tca_webhook_secret.
  Signature is in the X-TCA-Signature header as: sha256=<hex_digest>
  Computed over the raw request body.

Multi-company:
  company_id query param identifies which company the webhook belongs to.
  Each company has an independent webhook secret.
"""

import hashlib
import hmac
import json
import logging

from odoo import _, http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Supported webhook event types
EVENT_STATUS_UPDATE = 'STATUS_UPDATE'
EVENT_DOCUMENT_RECEIVED = 'DOCUMENT_RECEIVED'
EVENT_VALIDATION_FAILED = 'VALIDATION_FAILED'


class TcaWebhookController(http.Controller):

    @http.route(
        '/tca/webhook/invoice-status',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
        save_session=False,
    )
    def receive_webhook(self, company_id=None, **kwargs):
        """
        Main webhook entry point.
        Validates HMAC signature, identifies company, and dispatches event.
        Returns HTTP 200 immediately; processing is synchronous but fast.
        """
        # ── 1. Read raw body ─────────────────────────────────────────────────
        raw_body = request.httprequest.get_data()

        # ── 2. Identify company ──────────────────────────────────────────────
        if not company_id:
            _logger.warning('TCA webhook: missing company_id query param')
            return self._response_error('Missing company_id parameter', 400)

        try:
            company_id = int(company_id)
        except (TypeError, ValueError):
            return self._response_error('Invalid company_id', 400)

        # Use sudo to find company (webhook is unauthenticated at the HTTP level)
        company = request.env['res.company'].sudo().browse(company_id)
        if not company.exists() or not company.tca_is_active:
            _logger.warning('TCA webhook: company %s not found or TCA not active', company_id)
            return self._response_error('Company not found or TCA not active', 403)

        # ── 3. Validate HMAC signature ───────────────────────────────────────
        signature_header = request.httprequest.headers.get('X-TCA-Signature', '')
        if not self._validate_signature(raw_body, signature_header, company.tca_webhook_secret):
            _logger.warning(
                'TCA webhook: HMAC signature mismatch for company %s', company_id
            )
            return self._response_error('Invalid signature', 401)

        # ── 4. Parse payload ─────────────────────────────────────────────────
        try:
            payload = json.loads(raw_body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _logger.error('TCA webhook: invalid JSON body: %s', exc)
            return self._response_error('Invalid JSON body', 400)

        event_type = payload.get('event_type') or payload.get('event', '')
        # TCA-266: primary key is 'id'; keep legacy 'uuid' fields as fallback
        tca_id = (
            payload.get('id')
            or (payload.get('invoice') or {}).get('id')
            or payload.get('uuid')
            or (payload.get('invoice') or {}).get('uuid')
            or payload.get('invoice_uuid')
            or ''
        )

        _logger.info(
            'TCA webhook: company=%s event=%s id=%s',
            company_id, event_type, tca_id
        )

        # ── 5. Dispatch ──────────────────────────────────────────────────────
        try:
            if event_type == EVENT_STATUS_UPDATE:
                self._handle_status_update(company, tca_id, payload)
            elif event_type == EVENT_DOCUMENT_RECEIVED:
                self._handle_document_received(company, tca_id, payload)
            elif event_type == EVENT_VALIDATION_FAILED:
                self._handle_validation_failed(company, tca_id, payload)
            else:
                _logger.warning(
                    'TCA webhook: unrecognised event_type "%s" — ignoring', event_type
                )
        except Exception as exc:
            _logger.exception(
                'TCA webhook: error processing event %s for id %s: %s',
                event_type, tca_id, exc
            )
            # Return 200 even on processing error so TCA does not endlessly retry.
            # Errors are logged; admin can investigate via chatter and cron fallback.
            return self._response_ok({'status': 'error', 'message': str(exc)})

        return self._response_ok({'status': 'ok', 'event': event_type, 'id': tca_id})

    # ──────────────────────────────────────────────────────────────────────────
    # EVENT HANDLERS
    # ──────────────────────────────────────────────────────────────────────────

    def _handle_status_update(self, company, tca_id, payload):
        """
        Handle STATUS_UPDATE webhook event.
        Finds the matching outbound invoice by tca_invoice_uuid and updates state.
        """
        if not tca_id:
            _logger.warning('TCA webhook STATUS_UPDATE: missing id in payload')
            return

        invoice_data = payload.get('invoice') or payload
        invoice = request.env['account.move'].sudo().search([
            ('tca_invoice_uuid', '=', tca_id),
            ('company_id', '=', company.id),
        ], limit=1)

        if not invoice:
            _logger.warning(
                'TCA webhook STATUS_UPDATE: no invoice found for id %s (company %s)',
                tca_id, company.id
            )
            return

        invoice._tca_update_state_from_payload(invoice_data)

    def _handle_document_received(self, company, tca_id, payload):
        """
        Handle DOCUMENT_RECEIVED webhook event.
        A new inbound Peppol document has arrived at TCA.
        Import it as a draft vendor bill.

        TCA-266: acknowledge_invoice() removed from API.
        xml_location_path fetched via GET /api/v1/invoices/{id}/ if not in payload.
        """
        if not tca_id:
            _logger.warning('TCA webhook DOCUMENT_RECEIVED: missing id in payload')
            return

        # Check if already imported — idempotent skip (no acknowledge retry needed)
        existing = request.env['account.move'].sudo().search([
            ('tca_invoice_uuid', '=', tca_id),
            ('company_id', '=', company.id),
        ], limit=1)

        if existing:
            _logger.info(
                'TCA webhook: inbound invoice id=%s already imported as move id=%s — skipping',
                tca_id, existing.id
            )
            return

        # Get invoice_xml_location_path — may be in the webhook payload directly,
        # or we need to fetch it via GET /api/v1/invoices/{id}/
        invoice_data = payload.get('invoice') or payload
        xml_location_path = (
            invoice_data.get('document_location_path')
            or invoice_data.get('invoice_xml_location_path')
        )

        if not xml_location_path:
            _logger.info(
                'TCA webhook: invoice_xml_location_path not in payload for id=%s — fetching from API',
                tca_id
            )
            try:
                api_svc = request.env['tca.api.service'].sudo()
                detail = api_svc.get_invoice_status(company, tca_id)
                xml_location_path = detail.get('document_location_path') or detail.get('invoice_xml_location_path')
            except Exception as exc:
                _logger.error(
                    'TCA webhook: failed to fetch invoice detail for id=%s: %s', tca_id, exc
                )
                return

        if not xml_location_path:
            _logger.error(
                'TCA webhook: no invoice_xml_location_path for id=%s — cannot import', tca_id
            )
            return

        _logger.info('TCA webhook: importing inbound invoice id=%s', tca_id)
        api_svc = request.env['tca.api.service'].sudo()
        move = request.env['account.move'].sudo()._tca_import_inbound_invoice(
            company, tca_id, xml_location_path, api_svc
        )
        if move:
            _logger.info(
                'TCA webhook: imported inbound invoice id=%s as move %s', tca_id, move.name
            )
        else:
            _logger.error(
                'TCA webhook: failed to import inbound invoice id=%s', tca_id
            )

    def _handle_validation_failed(self, company, tca_id, payload):
        """
        Handle VALIDATION_FAILED webhook event.
        TCA rejected the document before sending to the Peppol network.
        Update invoice to error state with the validation details.
        """
        if not tca_id:
            return

        invoice = request.env['account.move'].sudo().search([
            ('tca_invoice_uuid', '=', tca_id),
            ('company_id', '=', company.id),
        ], limit=1)

        if not invoice:
            _logger.warning(
                'TCA webhook VALIDATION_FAILED: no invoice for id %s', tca_id
            )
            return

        error_detail = (
            payload.get('error_message')
            or payload.get('detail')
            or payload.get('message')
            or 'TCA validation failed.'
        )
        invoice.sudo().write({
            'tca_move_state': 'error',
            'tca_submission_error': error_detail,
        })
        invoice._message_log(
            body=_('TCA Peppol validation failed for invoice %s:\n%s', invoice.name, error_detail)
        )
        _logger.warning(
            'TCA webhook VALIDATION_FAILED: invoice %s (id=%s): %s',
            invoice.name, tca_id, error_detail
        )

    # ──────────────────────────────────────────────────────────────────────────
    # SIGNATURE VALIDATION
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_signature(raw_body, signature_header, webhook_secret):
        """
        Validate HMAC-SHA256 signature.
        TCA sends: X-TCA-Signature: sha256=<hex_digest>
        We compute HMAC-SHA256(webhook_secret, raw_body) and compare.

        Uses hmac.compare_digest for constant-time comparison (prevents timing attacks).
        If webhook_secret is not configured, signature validation is skipped
        with a warning (allows testing without a secret set).
        """
        if not webhook_secret:
            _logger.critical(
                'TCA webhook: no webhook_secret configured for company — REJECTING. '
                'Set the Webhook Secret in company TCA settings for production use.'
            )
            return False

        if not signature_header:
            return False

        # Strip the 'sha256=' prefix
        if signature_header.startswith('sha256='):
            received_digest = signature_header[7:]
        else:
            received_digest = signature_header

        # Compute expected digest
        secret_bytes = webhook_secret.encode('utf-8') if isinstance(webhook_secret, str) else webhook_secret
        expected_digest = hmac.new(secret_bytes, raw_body, hashlib.sha256).hexdigest()

        return hmac.compare_digest(expected_digest, received_digest)

    # ──────────────────────────────────────────────────────────────────────────
    # RESPONSE HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _response_ok(data):
        return request.make_response(
            json.dumps(data),
            headers=[
                ('Content-Type', 'application/json'),
                ('X-TCA-Webhook-Received', 'true'),
            ],
            status=200,
        )

    @staticmethod
    def _response_error(message, status_code):
        return request.make_response(
            json.dumps({'error': message}),
            headers=[('Content-Type', 'application/json')],
            status=status_code,
        )
