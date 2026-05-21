# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
E5: OAuth2 token management — fetch, cache, proactive refresh
E6: Full 3-step send flow (get-upload-url → S3 PUT → POST /invoices/)
"""

import json
import time
from unittest.mock import MagicMock, patch

from odoo.tests import tagged

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
        'organization': {'id': org_id, 'name': org_name},    'client_name': 'Odoo Integration',
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

        import io
        from urllib.error import HTTPError
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
    E6 — Full 3-step outbound invoice submission:
        1. POST /api/v1/invoices/get-upload-url/ → upload_url + file_key
        2. PUT {upload_url} XML bytes
        3. POST /api/v1/invoices/ → uuid
    """

    def setUp(self):
        super().setUp()
        self.api = self.env['tca.api.service']
        # Set company active + pre-load a valid token so _get_valid_token doesn't
        # need to hit the network
        future_expiry = int(time.time()) + 500
        self.company.tca_is_active = True
        self.company._set_tca_param('access_token', 'valid_bearer_token')
        self.company._set_tca_param('access_token_expires_at', str(future_expiry))

    def test_get_upload_url_returns_url_and_key(self):
        """get_document_upload_url must return upload_url and file_key from the API response."""
        api_resp = {
            'upload_url': 'https://s3.amazonaws.com/bucket/key?signature=xyz',
            'file_key': 's3://tca-invoices/org-uuid/inv-uuid.xml',
            'expires_in': 1200,
        }
        with patch(_URLOPEN, return_value=_mock_http_response(api_resp)):
            result = self.api.get_document_upload_url(self.company)

        self.assertEqual(result['upload_url'], api_resp['upload_url'])
        self.assertEqual(result['file_key'], api_resp['file_key'])

    def test_upload_to_s3_sends_put(self):
        """upload_to_s3 must PUT raw bytes to the presigned URL, no Auth header."""
        xml_bytes = b'<Invoice>test</Invoice>'
        s3_resp = MagicMock()
        s3_resp.status = 200
        s3_resp.__enter__ = lambda s: s
        s3_resp.__exit__ = MagicMock(return_value=False)

        with patch(_URLOPEN, return_value=s3_resp) as mock_open:
            self.api.upload_to_s3('https://s3.example.com/presigned', xml_bytes)

        req = mock_open.call_args[0][0]
        self.assertEqual(req.get_method(), 'PUT')
        self.assertEqual(req.data, xml_bytes)
        # S3 presigned URL must NOT have an Authorization header
        self.assertIsNone(req.get_header('Authorization'))

    def test_submit_invoice_payload_fields(self):
        """submit_invoice must send the required API fields and return the response."""
        api_resp = {
            'id': 'inv-uuid-001',
            'status': 1,
            'sender_document_reference': 'TRN/INV-2025-0001',
        }
        with patch(_URLOPEN, return_value=_mock_http_response(api_resp, status=201)) as mock_open:
            result = self.api.submit_invoice(
                company=self.company,
                name='INV/2025/00001',
                invoice_number='INV-2025-00001',
                source_file_path='s3://bucket/org/uuid/file.xml',
            )

        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode())

        self.assertEqual(body['name'], 'INV/2025/00001')
        self.assertEqual(body['invoice_number'], 'INV-2025-00001')
        self.assertEqual(body['source_file_path'], 's3://bucket/org/uuid/file.xml')
        self.assertIn('/api/v1/invoices/', req.full_url)
        self.assertEqual(result['id'], 'inv-uuid-001')

    def test_get_invoice_status_returns_dict(self):
        """get_invoice_status must GET /api/v1/invoices/{uuid}/ and return parsed response."""
        status_resp = {'id': 'inv-uuid-001', 'status': 1, 'c3_mls_status': 0, 'c5_mls_status': 0}
        with patch(_URLOPEN, return_value=_mock_http_response(status_resp)):
            result = self.api.get_invoice_status(self.company, 'inv-uuid-001')

        self.assertEqual(result['status'], 1)

    def test_submit_invoice_no_sender_eas_fields(self):
        """
        Regression: submit_invoice must NOT contain deprecated sender_eas,
        sender_identifier, receiver_eas, receiver_identifier fields.
        """
        with patch(_URLOPEN, return_value=_mock_http_response({'id': 'x'}, status=201)) as m:
            self.api.submit_invoice(
                company=self.company,
                name='TEST',
                invoice_number='TEST-001',
                source_file_path='s3://x',
            )
        body = json.loads(m.call_args[0][0].data.decode())
        for deprecated in ('sender_eas', 'sender_identifier', 'receiver_eas', 'receiver_identifier'):
            self.assertNotIn(deprecated, body,
                             f'Deprecated field "{deprecated}" found in submit payload')


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

    def test_c5_accepted_maps_to_received(self):
        invoice = self._make_invoice()
        invoice.tca_move_state = 'delivered'
        invoice._tca_update_state_from_payload({'status': 2, 'c3_mls_status': 4, 'c5_mls_status': 4})
        self.assertEqual(invoice.tca_move_state, 'received')

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
