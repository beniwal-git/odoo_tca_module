# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
E5: OAuth2 token management — fetch, cache, proactive refresh
E6: inline-JSON outbound submission (POST /invoices/ with a `detail` tree),
    status polling, inbound listing, XML download
"""

import json
import time
from io import BytesIO
from unittest.mock import MagicMock, patch, call

from odoo.tests import tagged

from ..services.tca_api import TcaAuthError, TcaTransientError, TcaValidationError
from .common import TcaTestCase

# Module path for urlopen — must match where it is imported in tca_api.py
_URLOPEN = 'odoo.addons.account_tca_peppol.services.tca_api.urlopen'


def _mock_http_response(body_dict, status=200):
    """Build a mock context-manager response for urlopen."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(body_dict).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _token_response(org_name='Test Org', org_id='org-uuid-001', expires_in=600):
    return {
        'access_token': 'test_access_token_xyz',
        'refresh_token': 'test_refresh_token_abc',
        'token_type': 'Bearer',
        'expires_in': expires_in,
        'organization': {'id': org_id, 'name': org_name},
        'client_name': 'Odoo Integration',
    }


@tagged('post_install', '-at_install')
class TestTcaApiTokenManagement(TcaTestCase):
    """E5 — OAuth2 token fetch, cache hit, proactive refresh."""

    def setUp(self):
        super().setUp()
        self.api = self.env['tca.api.service']
        # Clear any cached tokens
        for key in ('access_token', 'refresh_token', 'access_token_expires_at', 'org_name', 'org_id'):
            self.company._set_tca_param(key, '')

    def test_fetch_new_token_stores_tokens(self):
        """_fetch_new_token posts to /api/v1/oauth/token/ and persists tokens."""
        with patch(_URLOPEN, return_value=_mock_http_response(_token_response())) as mock_open:
            token = self.api._fetch_new_token(self.company)

        self.assertEqual(token, 'test_access_token_xyz')
        # The request should have been a POST to the token endpoint
        mock_open.assert_called_once()
        req_arg = mock_open.call_args[0][0]
        self.assertIn('/api/v1/oauth/token/', req_arg.full_url)
        self.assertEqual(req_arg.get_method(), 'POST')
        # Tokens must be persisted
        self.assertEqual(
            self.company._get_tca_param('access_token'), 'test_access_token_xyz'
        )
        self.assertEqual(
            self.company._get_tca_param('refresh_token'), 'test_refresh_token_abc'
        )

    def test_fetch_new_token_stores_org_info(self):
        """Token response's organization.name/id must be stored in config params."""
        with patch(_URLOPEN, return_value=_mock_http_response(
            _token_response(org_name='TCA Test Org', org_id='abc-123')
        )):
            self.api._fetch_new_token(self.company)

        self.assertEqual(self.company._get_tca_param('org_name'), 'TCA Test Org')
        self.assertEqual(self.company._get_tca_param('org_id'), 'abc-123')

    def test_fetch_new_token_uses_form_urlencoded(self):
        """Request body must be form-urlencoded (not JSON), with client_id + client_secret."""
        with patch(_URLOPEN, return_value=_mock_http_response(_token_response())):
            self.api._fetch_new_token(self.company)

        # The request body sent to the token endpoint should contain client_id
        req = patch(_URLOPEN).__enter__
        # Re-capture the actual request object
        with patch(_URLOPEN, return_value=_mock_http_response(_token_response())) as m:
            self.api._fetch_new_token(self.company)
        req_obj = m.call_args[0][0]
        # Content-Type header must be form-urlencoded
        ct = req_obj.get_header('Content-type')
        self.assertIn('application/x-www-form-urlencoded', ct)
        # Body must NOT contain grant_type — per API spec only client_id+secret
        body = req_obj.data.decode()
        self.assertIn('client_id=test_client_id', body)
        self.assertIn('client_secret=test_client_secret', body)

    def test_get_valid_token_returns_cached_when_fresh(self):
        """_get_valid_token must NOT call urlopen if cached token is still fresh."""
        # Pre-populate a fresh token
        future_expiry = int(time.time()) + 500   # 500s remaining > 60s buffer
        self.company._set_tca_param('access_token', 'cached_token')
        self.company._set_tca_param('access_token_expires_at', str(future_expiry))

        with patch(_URLOPEN) as mock_open:
            token = self.api._get_valid_token(self.company)

        mock_open.assert_not_called()
        self.assertEqual(token, 'cached_token')

    def test_get_valid_token_refreshes_when_near_expiry(self):
        """_get_valid_token must use refresh endpoint when token expires within the buffer."""
        # Token expires in 30 s — within the 60 s buffer
        near_expiry = int(time.time()) + 30
        self.company._set_tca_param('access_token', 'old_token')
        self.company._set_tca_param('access_token_expires_at', str(near_expiry))
        self.company._set_tca_param('refresh_token', 'valid_refresh_token')

        refresh_resp = _token_response()
        refresh_resp['access_token'] = 'refreshed_access_token'

        with patch(_URLOPEN, return_value=_mock_http_response(refresh_resp)) as mock_open:
            token = self.api._get_valid_token(self.company)

        self.assertEqual(token, 'refreshed_access_token')
        # Must have called the refresh endpoint, not the initial token endpoint
        req_obj = mock_open.call_args[0][0]
        self.assertIn('/api/v1/oauth/token/refresh/', req_obj.full_url)

    def test_refresh_token_uses_json_body(self):
        """_refresh_token must POST JSON body to /api/v1/oauth/token/refresh/."""
        refresh_resp = _token_response()
        with patch(_URLOPEN, return_value=_mock_http_response(refresh_resp)) as mock_open:
            self.api._refresh_token(self.company, 'my_refresh_token')

        req_obj = mock_open.call_args[0][0]
        self.assertIn('/api/v1/oauth/token/refresh/', req_obj.full_url)
        ct = req_obj.get_header('Content-type')
        self.assertIn('application/json', ct)
        body = json.loads(req_obj.data.decode())
        self.assertEqual(body['refresh_token'], 'my_refresh_token')

    def test_refresh_fallback_to_full_reauth(self):
        """If refresh fails, _get_valid_token must fall back to full client_credentials flow."""
        near_expiry = int(time.time()) + 10
        self.company._set_tca_param('access_token', 'old_token')
        self.company._set_tca_param('access_token_expires_at', str(near_expiry))
        self.company._set_tca_param('refresh_token', 'bad_refresh')

        full_auth_resp = _token_response()
        full_auth_resp['access_token'] = 'brand_new_token'

        from urllib.error import HTTPError
        import io
        # First call (refresh) raises 401; second call (full reauth) succeeds
        def side_effect(*args, **kwargs):
            req = args[0]
            if 'refresh' in req.full_url:
                raise HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'{}'))
            return _mock_http_response(full_auth_resp)

        with patch(_URLOPEN, side_effect=side_effect):
            token = self.api._get_valid_token(self.company)

        self.assertEqual(token, 'brand_new_token')

    def test_get_org_info_returns_stored_data(self):
        """get_org_info must return org name/id from config params without an extra HTTP call."""
        # _fetch_new_token is called once inside get_org_info to populate org params
        with patch(_URLOPEN, return_value=_mock_http_response(
            _token_response(org_name='My UAE Org', org_id='org-9999')
        )):
            org = self.api.get_org_info(self.company)

        self.assertEqual(org['name'], 'My UAE Org')
        self.assertEqual(org['id'], 'org-9999')

    def test_missing_credentials_raises_user_error(self):
        """_fetch_new_token raises UserError if client_id or secret is blank."""
        from odoo.exceptions import UserError
        self.company.tca_client_id = ''
        with self.assertRaises(UserError):
            self.api._fetch_new_token(self.company)


@tagged('post_install', '-at_install')
class TestTcaApiSendFlow(TcaTestCase):
    """
    E6 — Inline-JSON outbound invoice submission (P2.2 — the XML/S3 3-step
    flow was removed entirely; submit_invoice_json is now the only outbound
    path):
        POST /api/v1/invoices/  { name, invoice_number, detail }
            201 → { id, ... }               validated + queued
            400 → per-field error dict      TcaValidationError

    Also covers: status polling, inbound listing, XML download.
    """

    def setUp(self):
        super().setUp()
        self.api = self.env['tca.api.service']
        future_expiry = int(time.time()) + 500
        self.company.tca_is_active = True
        self.company._set_tca_param('access_token', 'valid_bearer_token')
        self.company._set_tca_param('access_token_expires_at', str(future_expiry))

    # ── POST /api/v1/invoices/ (inline JSON) ─────────────────────────────────

    def test_submit_invoice_json_payload_fields(self):
        """submit_invoice_json must POST { name, invoice_number, issue_date,
        invoice_type_code, detail } — issue_date/invoice_type_code pulled to
        the payload root, everything else stays nested under detail."""
        api_resp = {'id': 'inv-tca-001', 'status': 1}
        detail = {
            'issue_date': '2025-01-01',
            'invoice_type_code': '380',
            'invoice_currency_code': 'AED',
            'lines': [],
        }
        with patch(_URLOPEN, return_value=_mock_http_response(api_resp, status=201)) as mock_open:
            result = self.api.submit_invoice_json(
                company=self.company,
                name='INV/2025/00001-a1b2c3d4',
                invoice_number='INV/2025/00001-a1b2c3d4',
                detail=detail,
            )

        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode())

        self.assertIn('/api/v1/invoices/', req.full_url)
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(body['name'], 'INV/2025/00001-a1b2c3d4')
        self.assertEqual(body['invoice_number'], 'INV/2025/00001-a1b2c3d4')
        # Pulled to root
        self.assertEqual(body['issue_date'], '2025-01-01')
        self.assertEqual(body['invoice_type_code'], '380')
        # Not duplicated inside detail
        self.assertNotIn('issue_date', body['detail'])
        self.assertNotIn('invoice_type_code', body['detail'])
        # Everything else stays nested
        self.assertEqual(body['detail']['invoice_currency_code'], 'AED')
        self.assertEqual(body['detail']['lines'], [])
        self.assertNotIn('source_file_path', body)
        # Caller's dict must not be mutated by the pop()
        self.assertIn('issue_date', detail)
        self.assertIn('invoice_type_code', detail)
        self.assertEqual(result['id'], 'inv-tca-001')

    def test_submit_invoice_json_sends_bearer_token(self):
        """submit_invoice_json must include Authorization: Bearer header."""
        with patch(_URLOPEN, return_value=_mock_http_response({'id': 'x'}, status=201)) as m:
            self.api.submit_invoice_json(self.company, 'X', 'X', {})

        req = m.call_args[0][0]
        self.assertEqual(req.get_header('Authorization'), 'Bearer valid_bearer_token')

    def test_submit_invoice_json_400_raises_validation_error(self):
        """A 400 with a per-field error tree must raise TcaValidationError
        with the flattened field-error list attached."""
        from urllib.error import HTTPError
        import io

        error_body = json.dumps({
            'lines': [{'item_name': ['This field is required.']}],
            'buyer': {'country_code': ['This field is required.']},
        }).encode()

        def side_effect(req, timeout=30):
            raise HTTPError(req.full_url, 400, 'Bad Request', {}, io.BytesIO(error_body))

        with patch(_URLOPEN, side_effect=side_effect):
            with self.assertRaises(TcaValidationError) as cm:
                self.api.submit_invoice_json(self.company, 'X', 'X', {'lines': []})

        self.assertTrue(cm.exception.tca_field_errors)
        self.assertTrue(
            any('item_name' in e for e in cm.exception.tca_field_errors)
        )
        self.assertTrue(
            any('country_code' in e for e in cm.exception.tca_field_errors)
        )

    def test_submit_invoice_json_409_treated_as_duplicate(self):
        """A 409 must be surfaced as {'tca_duplicate': True, ...} rather than raised."""
        from urllib.error import HTTPError
        import io

        def side_effect(req, timeout=30):
            raise HTTPError(
                req.full_url, 409, 'Conflict', {}, io.BytesIO(b'{"id": "inv-existing"}')
            )

        with patch(_URLOPEN, side_effect=side_effect):
            result = self.api.submit_invoice_json(self.company, 'X', 'X', {})

        self.assertTrue(result.get('tca_duplicate'))
        self.assertEqual(result.get('id'), 'inv-existing')

    def test_submit_invoice_json_401_raises_auth_error(self):
        """A 401 must raise TcaAuthError specifically (isinstance-checkable)."""
        from urllib.error import HTTPError
        import io

        def side_effect(req, timeout=30):
            raise HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'{}'))

        with patch(_URLOPEN, side_effect=side_effect):
            with self.assertRaises(TcaAuthError):
                self.api.submit_invoice_json(self.company, 'X', 'X', {})

    def test_submit_invoice_json_5xx_raises_transient_error(self):
        """A 5xx must raise TcaTransientError (retry-safe), not TcaPermanentError."""
        from urllib.error import HTTPError
        import io

        def side_effect(req, timeout=30):
            raise HTTPError(req.full_url, 503, 'Service Unavailable', {}, io.BytesIO(b''))

        with patch(_URLOPEN, side_effect=side_effect):
            with self.assertRaises(TcaTransientError):
                self.api.submit_invoice_json(self.company, 'X', 'X', {})

    def test_submit_invoice_json_network_error_is_transient(self):
        """A URLError (network unreachable) must raise TcaTransientError."""
        from urllib.error import URLError

        with patch(_URLOPEN, side_effect=URLError('Connection refused')):
            with self.assertRaises(TcaTransientError):
                self.api.submit_invoice_json(self.company, 'X', 'X', {})

    # ── Status polling ────────────────────────────────────────────────────────

    def test_get_invoice_status_hits_correct_url(self):
        """get_invoice_status must GET /api/v1/invoices/{id}/ and return the full dict."""
        status_resp = {
            'id': 'inv-abc', 'status': 1,
            'c3_mls_status': 0, 'c5_mls_status': 0,
            'can_resubmit': False,
            'invoice_xml_location_path': None,
        }
        with patch(_URLOPEN, return_value=_mock_http_response(status_resp)) as m:
            result = self.api.get_invoice_status(self.company, 'inv-abc')

        req = m.call_args[0][0]
        self.assertIn('/api/v1/invoices/inv-abc/', req.full_url)
        self.assertEqual(req.get_method(), 'GET')
        self.assertEqual(result['status'], 1)
        self.assertIn('can_resubmit', result)

    # ── Inbound listing ───────────────────────────────────────────────────────

    def test_list_inbound_invoices_uses_direction_2(self):
        """list_inbound_invoices must GET with direction=2 query param."""
        resp = {'count': 1, 'next': None, 'previous': None, 'results': [
            {'id': 'in-001', 'direction': 2, 'invoice_xml_location_path': 's3://in/001.xml'}
        ]}
        with patch(_URLOPEN, return_value=_mock_http_response(resp)) as m:
            result = self.api.list_inbound_invoices(self.company)

        req = m.call_args[0][0]
        self.assertIn('direction=2', req.full_url)
        self.assertEqual(req.get_method(), 'GET')
        self.assertEqual(len(result['results']), 1)

    def test_list_processing_outbound_uses_direction_1_status_1(self):
        """list_processing_outbound must GET with direction=1&status=1."""
        resp = {'count': 0, 'next': None, 'previous': None, 'results': []}
        with patch(_URLOPEN, return_value=_mock_http_response(resp)) as m:
            self.api.list_processing_outbound(self.company)

        req = m.call_args[0][0]
        self.assertIn('direction=1', req.full_url)
        self.assertIn('status=1', req.full_url)

    # ── Inbound XML download ──────────────────────────────────────────────────

    def test_get_document_download_url_posts_s3_uri(self):
        """get_document_download_url must POST /api/v1/documents/download/
        with { s3_uri: <path> } as the JSON body."""
        s3_path = 's3://tca-invoices/org/inv.xml'
        with patch(_URLOPEN, return_value=_mock_http_response({'download_url': 'https://s3/dl'})) as m:
            self.api.get_document_download_url(self.company, s3_path)

        req = m.call_args[0][0]
        self.assertIn('/api/v1/documents/download/', req.full_url)
        self.assertEqual(req.get_method(), 'POST')
        body = json.loads(req.data.decode())
        self.assertEqual(body['s3_uri'], s3_path)

    def test_download_inbound_xml_two_step_flow(self):
        """
        download_inbound_xml must:
          1. Call get_document_download_url to resolve the presigned URL
          2. Fetch raw bytes from S3 (no auth)
        """
        presigned_url = 'https://s3.amazonaws.com/bucket/key?sig=abc'
        xml_bytes = b'<?xml version="1.0"?><CreditNote/>'

        # First call → download URL response; second call → raw XML bytes
        dl_resp = MagicMock()
        dl_resp.status = 200
        dl_resp.read.return_value = xml_bytes
        dl_resp.__enter__ = lambda s: s
        dl_resp.__exit__ = MagicMock(return_value=False)

        call_count = {'n': 0}
        def side_effect(req, timeout=30):
            call_count['n'] += 1
            if call_count['n'] == 1:
                # First: return the download URL JSON
                return _mock_http_response({'download_url': presigned_url})
            # Second: return raw XML bytes from presigned URL
            return dl_resp

        with patch(_URLOPEN, side_effect=side_effect) as m:
            result = self.api.download_inbound_xml(self.company, 's3://tca/inv.xml')

        self.assertEqual(result, xml_bytes)
        self.assertEqual(call_count['n'], 2)
        # Second call must hit the presigned URL directly
        second_req = m.call_args_list[1][0][0]
        self.assertEqual(second_req.full_url, presigned_url)


@tagged('post_install', '-at_install')
class TestTcaStatusMapping(TcaTestCase):
    """
    B4 regression tests: _tca_update_state_from_payload uses integer status codes.
    """

    def test_status_1_maps_to_processing(self):
        invoice = self._make_invoice()
        invoice._tca_update_state_from_payload({'status': 1, 'c3_mls_status': 0, 'c5_mls_status': 0})
        self.assertEqual(invoice.tca_move_state, 'processing')

    def test_status_2_c3_accepted_maps_to_delivered(self):
        invoice = self._make_invoice()
        invoice.tca_move_state = 'processing'
        invoice._tca_update_state_from_payload({'status': 2, 'c3_mls_status': 4, 'c5_mls_status': 0})
        self.assertEqual(invoice.tca_move_state, 'delivered')

    def test_c5_accepted_maps_to_buyer_confirmed(self):
        invoice = self._make_invoice()
        invoice.tca_move_state = 'delivered'
        invoice._tca_update_state_from_payload({'status': 2, 'c3_mls_status': 4, 'c5_mls_status': 4})
        self.assertEqual(invoice.tca_move_state, 'buyer_confirmed')

    def test_status_3_maps_to_rejected(self):
        invoice = self._make_invoice()
        invoice._tca_update_state_from_payload({'status': 3, 'c3_mls_status': 5, 'c5_mls_status': 0})
        self.assertEqual(invoice.tca_move_state, 'rejected')

    def test_status_4_maps_to_error(self):
        invoice = self._make_invoice()
        invoice._tca_update_state_from_payload({'status': 4, 'c3_mls_status': 6, 'c5_mls_status': 0})
        self.assertEqual(invoice.tca_move_state, 'error')

    def test_string_status_does_not_change_state(self):
        """
        Regression guard: if TCA ever accidentally sends a string status,
        the method must log a warning and not corrupt the state.
        """
        invoice = self._make_invoice()
        original_state = invoice.tca_move_state
        # String codes are the old (wrong) values — must be ignored
        invoice._tca_update_state_from_payload({'status': 'PROCESSING'})
        self.assertEqual(invoice.tca_move_state, original_state,
                         'State must not change when status is an unknown string')

    def test_no_change_when_state_is_same(self):
        """No chatter entry should be created if state is already current."""
        invoice = self._make_invoice()
        invoice.tca_move_state = 'processing'
        before_messages = len(invoice.message_ids)
        invoice._tca_update_state_from_payload({'status': 1, 'c3_mls_status': 0, 'c5_mls_status': 0})
        # State is already 'processing' — no new message should be added
        self.assertEqual(len(invoice.message_ids), before_messages)
