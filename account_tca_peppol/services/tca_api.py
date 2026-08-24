# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
TCA API Service — OAuth2 + inline-JSON invoice submission + invoice lifecycle

Handles all HTTP communication between Odoo and the TCA backend.

OAuth2 flow (client credentials):
  POST /api/v1/oauth/token/
  Content-Type: application/x-www-form-urlencoded
  Body: client_id=...&client_secret=...
  Response: { access_token, refresh_token, expires_in: 600, organization: { id, name } }

  Access token lifetime: 10 minutes (600 seconds)
  Refresh token: rotating — each use issues a new pair

Outbound invoice flow (inline JSON — single call, synchronous validation):
  POST /api/v1/invoices/  { name, invoice_number, detail }
    201 → validated + queued for Peppol dispatch. Body: { id, ... }.
    400 → content validation failed; body is a per-field error dict, raised
          as TcaValidationError with the flattened field-error list.
  TCA builds the UBL XML and runs the official PINT AE schematron
  server-side — there is no client-side XML build or S3 upload for
  submission. (The PINT AE XML builder is still used locally for Tier-1
  Python constraint validation and for inbound-document parsing — just not
  for the outbound wire payload.)

Inbound invoice flow:
  GET  /api/v1/invoices/?direction=2    list received invoices (each has invoice_xml_location_path)
  POST /api/v1/documents/download/      { s3_uri } → presigned download URL
  GET  {presigned_url}                  → raw XML bytes
"""

import json
import logging
import time
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError

from odoo import models, _, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
TOKEN_EXPIRY_BUFFER_SECONDS = 60   # refresh token 60 s before actual expiry
# Defaults — overridable per deployment via ir.config_parameter
# (tca.http_timeout). See _tca_http_timeout below.
DEFAULT_HTTP_TIMEOUT = 30           # seconds for general API calls


# ── Exception hierarchy ──────────────────────────────────────────────────────
# All still UserError subclasses (so an un-caught one surfaces a clean dialog
# to the user), but callers that need to branch on failure kind (e.g. the
# status-poll cron deciding whether to retry) can isinstance() check instead
# of substring-matching the message text.
#
#   TcaError
#   ├── TcaTransientError — network / timeout / 5xx. Safe to retry after
#   │                       backoff. Poll/retry loops should keep going.
#   ├── TcaAuthError      — 401 or token-missing. Caller should re-auth, not retry.
#   └── TcaPermanentError — 4xx other than 401, malformed response, input
#                           validation. Retrying without changing input fails.
#       └── TcaValidationError — synchronous 400 content-validation failure
#                                 from the inline-JSON invoice endpoint.
# ──────────────────────────────────────────────────────────────────────────


class TcaError(UserError):
    """Base for TCA API failures."""


class TcaTransientError(TcaError):
    """Network / timeout / 5xx — safe to retry after backoff."""


class TcaAuthError(TcaError):
    """Authentication failure — re-auth before retrying."""


class TcaPermanentError(TcaError):
    """Request rejected; retrying without changing input will fail identically."""


class TcaValidationError(TcaPermanentError):
    """Synchronous content-validation failure (400) from the inline-JSON invoice
    endpoint. `tca_field_errors` holds the flattened list of per-field messages
    for surfacing in the UI; `tca_field_dict` keeps the raw per-field dict as
    TCA returned it."""

    def __init__(self, message, field_errors=None, field_dict=None):
        super().__init__(message)
        self.tca_field_errors = field_errors or []
        self.tca_field_dict = field_dict or {}


def _tca_flatten_field_errors(err, prefix=''):
    """Flatten a DRF-style nested error structure into ['path: message', ...].
    Handles dicts (field → errors), lists (index → errors), and leaf strings."""
    out = []
    if isinstance(err, dict):
        for key, val in err.items():
            path = f'{prefix}.{key}' if prefix else str(key)
            out.extend(_tca_flatten_field_errors(val, path))
    elif isinstance(err, (list, tuple)):
        for i, item in enumerate(err):
            if isinstance(item, (dict, list, tuple)):
                _p = f'{prefix}[{i}]' if prefix else f'[{i}]'
                out.extend(_tca_flatten_field_errors(item, _p))
            else:
                out.append(f'{prefix}: {item}' if prefix else str(item))
    else:
        out.append(f'{prefix}: {err}' if prefix else str(err))
    return out


class TcaApiService(models.AbstractModel):
    """
    Stateless service model providing all TCA API operations.
    Methods are @api.model — call via self.env['tca.api.service'].method()
    """
    _name = 'tca.api.service'
    _description = 'TCA Peppol API Service'

    # ──────────────────────────────────────────────────────────────────────────
    # CONFIG ACCESSORS — timeouts are tunable per deployment.
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _tca_http_timeout(self):
        """Seconds for general TCA REST calls. Override via
        ir.config_parameter `tca.http_timeout`; falls back to
        DEFAULT_HTTP_TIMEOUT (30 s)."""
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'tca.http_timeout', DEFAULT_HTTP_TIMEOUT,
        )
        try:
            return int(raw)
        except (TypeError, ValueError):
            _logger.warning(
                'TCA: invalid tca.http_timeout %r — using default %ss',
                raw, DEFAULT_HTTP_TIMEOUT,
            )
            return DEFAULT_HTTP_TIMEOUT

    # ──────────────────────────────────────────────────────────────────────────
    # TOKEN MANAGEMENT
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _get_valid_token(self, company):
        """
        Return a valid Bearer token for the given company.
        Proactively refreshes if the access token is expiring within the buffer window.
        Falls back to fetching a completely new token pair if refresh fails.

        Concurrency: uses pg_advisory_xact_lock (Postgres advisory lock scoped
        to the current transaction) to serialize concurrent refreshes on the
        same company. The lock is auto-released at transaction commit/rollback
        — no manual cleanup, no savepoint-scoping pitfalls. After acquiring
        the lock, we re-read the token: a peer that held the lock just before
        us may have already done the refresh, in which case we return the
        fresh token without making a redundant HTTP call.
        """
        now = int(time.time())
        expires_at = int(company._get_tca_param('access_token_expires_at', '0'))
        access_token = company._get_tca_param('access_token', '')

        if access_token and expires_at > now + TOKEN_EXPIRY_BUFFER_SECONDS:
            return access_token  # Still valid

        # Acquire a per-company advisory lock. pg_advisory_xact_lock blocks
        # until the lock is acquired and is released automatically at the end
        # of the transaction. The two-int signature uses a stable hash of the
        # subsystem name plus the company id so locks across companies and
        # across unrelated subsystems do not collide.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext('tca_token'), %s)",
            [company.id],
        )

        # Re-check token freshness — a concurrent worker may have refreshed
        # while we waited for the lock.
        now = int(time.time())
        expires_at = int(company._get_tca_param('access_token_expires_at', '0'))
        access_token = company._get_tca_param('access_token', '')
        if access_token and expires_at > now + TOKEN_EXPIRY_BUFFER_SECONDS:
            return access_token

        refresh_token = company._get_tca_param('refresh_token', '')
        if refresh_token:
            try:
                return self._refresh_token(company, refresh_token)
            except Exception as exc:
                _logger.warning(
                    'TCA token refresh failed for company %s, attempting full re-auth: %s',
                    company.id, exc
                )

        # Full re-auth using client credentials
        return self._fetch_new_token(company)

    @api.model
    def _fetch_new_token(self, company):
        """
        Obtain a brand-new token pair using client_id + client_secret.
        POST /api/v1/oauth/token/  (application/x-www-form-urlencoded)
        Body: client_id=...&client_secret=...
        Stores tokens + org metadata in ir.config_parameter.
        """
        if not company.tca_client_id or not company.tca_client_secret:
            raise TcaAuthError(_(
                'TCA credentials not configured for company "%s". '
                'Go to Settings → Accounting → TCA E-Invoicing.', company.name
            ))

        payload = urlencode({
            'client_id': company.tca_client_id,
            'client_secret': company.tca_client_secret,
        }).encode()

        response = self._http_post(
            company, '/api/v1/oauth/token/', payload,
            content_type='application/x-www-form-urlencoded',
            auth=False,
        )
        return self._store_token_response(company, response)

    @api.model
    def _refresh_token(self, company, refresh_token):
        """
        Obtain a new token pair using the refresh token.
        TCA uses rotating refresh tokens — each use issues a new pair.
        POST /api/v1/oauth/token/refresh/  (application/json)
        Body: { "refresh_token": "..." }
        """
        payload = {'refresh_token': refresh_token}

        response = self._http_post(
            company, '/api/v1/oauth/token/refresh/', payload,
            content_type='application/json',
            auth=False,
        )
        return self._store_token_response(company, response)

    @api.model
    def _store_token_response(self, company, response):
        """
        Parse a token response dict and persist tokens + org info to ir.config_parameter.
        Token response shape:
          { access_token, refresh_token, token_type, expires_in: 600,
            organization: { id, name }, client_name }
        Returns the access_token string.
        """
        access_token = response.get('access_token', '')
        refresh_token = response.get('refresh_token', '')
        expires_in = int(response.get('expires_in', 600))
        expires_at = int(time.time()) + expires_in

        if not access_token:
            raise TcaAuthError(_(
                'TCA did not return an access token. Response: %s', response
            ))

        company._set_tca_param('access_token', access_token)
        company._set_tca_param('refresh_token', refresh_token)
        company._set_tca_param('access_token_expires_at', str(expires_at))

        # Persist org info from the token response (avoids a separate API call)
        org = response.get('organization') or {}
        if org.get('name'):
            company._set_tca_param('org_name', org['name'])
        if org.get('id'):
            company._set_tca_param('org_id', str(org['id']))

        _logger.info('TCA: stored new token for company %s (expires in %s s)', company.id, expires_in)
        return access_token

    # ──────────────────────────────────────────────────────────────────────────
    # INVOICE OPERATIONS
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def submit_invoice_json(self, company, name, invoice_number, detail):
        """
        Inline-JSON submission (ASP JSON schema §9). One call — no S3 upload.
        POST /api/v1/invoices/  body: { name, invoice_number, detail }

        Validation is SYNCHRONOUS: TCA validates `detail` before returning.
          201 → validated + queued for Peppol dispatch. Body: { id, ... }.
          400 → content validation failed; body is a per-field error dict.
                Surfaced by _execute_request as a TcaValidationError carrying
                the parsed field errors (see tca_field_errors).

        Store the returned 'id' as tca_invoice_uuid on the Odoo invoice.
        """
        payload = {
            'name': name,
            'invoice_number': invoice_number,
            'detail': detail,
        }
        return self._http_post(company, '/api/v1/invoices/', payload, expected_status=201)

    @api.model
    def get_invoice_status(self, company, tca_id):
        """
        GET /api/v1/invoices/{id}/ — fetch current status of an invoice.
        Returns the full TCA invoice dict including:
          id, name, invoice_number, direction, status, can_resubmit,
          invoice_xml_location_path, c3_mls_status, c5_mls_status,
          internal_validation_status, internal_validation_error_message,
          created_at, updated_at
        """
        return self._http_get(company, f'/api/v1/invoices/{tca_id}/')

    @api.model
    def list_inbound_invoices(self, company, limit=50):
        """
        GET /api/v1/invoices/?direction=2
        List received (inbound) invoices for the given company.
        Each item includes invoice_xml_location_path (S3 URI of the XML).
        Used by the fallback cron to import missed inbound documents.
        Returns a paginated DRF response: { count, next, previous, results: [...] }
        """
        return self._http_get(
            company,
            f'/api/v1/invoices/?direction=2&page_size={limit}',
        )

    @api.model
    def list_processing_outbound(self, company, limit=50):
        """
        GET /api/v1/invoices/?direction=1&status=1
        List outbound invoices still in Processing state.
        Used for fallback polling when webhooks are missed.
        Returns a paginated DRF response.
        """
        return self._http_get(
            company,
            f'/api/v1/invoices/?direction=1&status=1&page_size={limit}',
        )

    @api.model
    def get_document_download_url(self, company, s3_path):
        """
        POST /api/v1/documents/download/
        Body: { "s3_uri": "s3://bucket/path/to/file.xml" }
        Returns a presigned S3 download URL.
        Response: { download_url: "...", expires_in: 1200 }
        """
        return self._http_post(company, '/api/v1/documents/download/', {
            's3_uri': s3_path,
        })

    @api.model
    def download_inbound_xml(self, company, s3_path):
        """
        Download raw XML bytes for an inbound invoice from TCA S3.
        Two-step: get presigned download URL via /api/v1/documents/download/,
        then fetch the raw bytes from S3 (no auth needed on presigned URL).

        s3_path: the invoice_xml_location_path field from the TCA invoice response.
        Returns raw XML bytes.
        """
        result = self.get_document_download_url(company, s3_path)
        # Response may use 'download_url', 'url', or similar — try common keys
        download_url = (
            result.get('download_url')
            or result.get('url')
            or result.get('presigned_url')
        )
        if not download_url:
            raise TcaPermanentError(_('TCA did not return a download URL. Response: %s', result))

        req = Request(download_url, method='GET')
        try:
            with urlopen(req, timeout=self._tca_http_timeout()) as resp:
                return resp.read()
        except HTTPError as exc:
            raise TcaTransientError(_(
                'S3 download failed (HTTP %s): %s', exc.code, exc.reason
            )) from exc
        except URLError as exc:
            raise TcaTransientError(_('S3 download network error: %s', str(exc.reason))) from exc

    @api.model
    def get_org_info(self, company):
        """
        Return organisation details for the authenticated company.
        The TCA OAuth2 token response includes { organization: { id, name } }
        which is stored in ir.config_parameter by _store_token_response.
        There is no separate /api/v1/organisations/me/ endpoint on the /api/v1/ prefix.
        Calling _fetch_new_token ensures the params are up to date.
        """
        # Ensure a fresh token is obtained so org info is stored from the response
        self._fetch_new_token(company)
        return {
            'name': company._get_tca_param('org_name', ''),
            'id': company._get_tca_param('org_id', ''),
        }

    # ──────────────────────────────────────────────────────────────────────────
    # HTTP HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _http_post(self, company, path, payload, content_type='application/json',
                   auth=True, expected_status=200):
        """Generic authenticated POST. Returns parsed JSON dict."""
        url = (company.tca_base_url or 'https://api.tcapeppol.com').rstrip('/') + path

        if content_type == 'application/json':
            data = json.dumps(payload).encode('utf-8')
        else:
            data = payload  # already encoded (e.g. form-urlencoded)

        req = Request(url, data=data, method='POST')
        req.add_header('Content-Type', content_type)
        req.add_header('Accept', 'application/json')

        if auth:
            token = self._get_valid_token(company)
            req.add_header('Authorization', f'Bearer {token}')

        return self._execute_request(req, expected_status)

    @api.model
    def _http_get(self, company, path):
        """Generic authenticated GET by relative path. Returns parsed JSON dict."""
        url = (company.tca_base_url or 'https://api.tcapeppol.com').rstrip('/') + path

        req = Request(url, method='GET')
        req.add_header('Accept', 'application/json')

        token = self._get_valid_token(company)
        req.add_header('Authorization', f'Bearer {token}')

        return self._execute_request(req, expected_status=200)

    @api.model
    def _http_get_url(self, company, url):
        """
        Authenticated GET by absolute URL — used for DRF pagination next/previous links.
        The url comes from the TCA API response 'next' field and is already fully qualified.
        """
        req = Request(url, method='GET')
        req.add_header('Accept', 'application/json')

        token = self._get_valid_token(company)
        req.add_header('Authorization', f'Bearer {token}')

        return self._execute_request(req, expected_status=200)

    @api.model
    def _execute_request(self, req, expected_status):
        """Execute an urllib Request and return parsed JSON. Raises a TcaError
        subclass on failure (see the hierarchy at the top of this module)."""
        try:
            with urlopen(req, timeout=self._tca_http_timeout()) as resp:
                status = resp.status
                raw = resp.read()
        except HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')
            _logger.error('TCA API HTTP %s error on %s:\n%s', exc.code, req.full_url, body)

            # Try to parse TCA error body
            try:
                err_data = json.loads(body)
                detail = err_data.get('detail') or err_data.get('message') or body
            except Exception:
                detail = body or str(exc)

            if exc.code == 401:
                raise TcaAuthError(_('TCA authentication failed (401). Check API credentials.')) from exc
            if exc.code == 409:
                _logger.info('TCA: 409 duplicate — invoice already exists: %s', detail)
                try:
                    return {**json.loads(body), 'tca_duplicate': True}
                except Exception:
                    return {'tca_duplicate': True, 'detail': detail}
            # TCA returns HTTP 400 (not 409) when invoice_number is already taken
            # for the organization. The response shape is a DRF field-level error:
            #   {"invoice_number": ["An invoice with this number already exists..."]}
            # Match on the STRUCTURED shape — checking the invoice_number key has a
            # message containing 'exists' or 'duplicate'. Robust against TCA wording
            # changes (translations, rewording) — only triggers if TCA explicitly
            # flags the invoice_number field as the conflicting one.
            if exc.code == 400:
                try:
                    err_data = json.loads(body)
                except Exception:
                    err_data = None
                if isinstance(err_data, dict):
                    inv_num_errs = err_data.get('invoice_number')
                    if isinstance(inv_num_errs, list) and any(
                        isinstance(e, str)
                        and ('exists' in e.lower() or 'duplicate' in e.lower())
                        for e in inv_num_errs
                    ):
                        _logger.info(
                            'TCA: 400 duplicate invoice_number on %s: %s',
                            req.full_url, inv_num_errs,
                        )
                        return {**err_data, 'tca_duplicate': True}
                # Inline-JSON content validation: per-field error tree. TCA may
                # return it at the top level ({"lines": [...]}) OR wrapped in a
                # "detail" envelope ({"detail": {"lines": [...]}}). Only a plain
                # string detail (e.g. "permission denied") is NOT a field tree.
                if isinstance(err_data, dict):
                    d = err_data.get('detail')
                    if isinstance(d, (dict, list)):
                        err_tree = d
                    elif d is None:
                        err_tree = err_data
                    else:
                        err_tree = None  # detail is a plain string message
                    if err_tree is not None:
                        field_errors = _tca_flatten_field_errors(err_tree)
                        if field_errors:
                            _logger.error(
                                'TCA: 400 content validation on %s:\n%s',
                                req.full_url, '\n'.join(field_errors),
                            )
                            raise TcaValidationError(
                                _('TCA rejected the invoice content (400):\n%s',
                                  '\n'.join(field_errors)),
                                field_errors=field_errors,
                                field_dict=err_data,
                            ) from exc
            if exc.code == 422:
                raise TcaPermanentError(_('TCA validation error (422): %s', detail)) from exc

            # 5xx → transient (server's fault, retry); 4xx → permanent (caller's fault).
            cls = TcaTransientError if 500 <= exc.code < 600 else TcaPermanentError
            raise cls(_('TCA API error (HTTP %s): %s', exc.code, detail)) from exc

        except URLError as exc:
            raise TcaTransientError(_('Cannot reach TCA API: %s', str(exc.reason))) from exc

        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {'raw': raw.decode('utf-8', errors='replace')}
        else:
            data = {}

        if status != expected_status and status not in (200, 201, 204):
            raise TcaPermanentError(_(
                'Unexpected TCA API response status %s (expected %s).', status, expected_status
            ))

        return data
