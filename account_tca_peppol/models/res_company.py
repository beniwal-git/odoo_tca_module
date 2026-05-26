# Part of TCA. See LICENSE file for full copyright and licensing details.

from odoo import _, fields, models
from odoo.exceptions import UserError


class ResCompany(models.Model):
    """
    Extends res.company with TCA OAuth2 credentials and configuration.
    Each Odoo company maps to exactly one TCA organization.
    Credentials are stored per-company via ir.config_parameter.
    """
    _inherit = 'res.company'

    # ── TCA connection settings ────────────────────────────────────────────────

    tca_base_url = fields.Char(
        string='TCA API Base URL',
        default='https://api.tcapeppol.com',
        help='Base URL for the TCA Peppol API. Change only for sandbox/staging environments.',
    )
    tca_client_id = fields.Char(
        string='TCA Client ID',
        help='OAuth2 client_id from TCA portal: Settings → API Clients → Create Client.',
    )
    tca_client_secret = fields.Char(
        string='TCA Client Secret',
        help='OAuth2 client_secret. Visible only once at creation time in the TCA portal.',
    )
    tca_webhook_secret = fields.Char(
        string='TCA Webhook Secret',
        help=(
            'HMAC-SHA256 secret for validating inbound webhook payloads from TCA. '
            'Register the webhook in TCA portal: Settings → Webhooks → Create Endpoint.'
        ),
    )

    # ── Read-only info fetched from TCA ───────────────────────────────────────

    tca_org_name = fields.Char(
        string='TCA Organisation',
        readonly=True,
        help='Organisation name as registered in TCA. Populated after a successful connection test.',
    )
    tca_is_active = fields.Boolean(
        string='TCA Integration Active',
        default=False,
        help='When enabled, invoices with a PINT AE partner can be sent via TCA Peppol.',
    )
    # ── PINT AE company identity — related to the company partner ─────────────
    # The PINT AE identity fields live on res.partner; these writable related
    # fields surface them on the company form's "Invoicing" tab so a company
    # can be configured for e-invoicing without opening it in Contacts.

    peppol_eas = fields.Selection(
        related='partner_id.peppol_eas', readonly=False,
        string='Peppol EAS',
    )
    peppol_endpoint = fields.Char(
        related='partner_id.peppol_endpoint', readonly=False,
        string='Peppol Endpoint',
    )
    tca_emirate = fields.Selection(
        related='partner_id.tca_emirate', readonly=False,
        string='Emirate',
    )
    tca_legal_id_type = fields.Selection(
        related='partner_id.tca_legal_id_type', readonly=False,
        string='Legal ID Type',
    )
    tca_trade_license = fields.Char(
        related='partner_id.tca_trade_license', readonly=False,
        string='Trade License / Registration ID',
    )
    tca_legal_authority = fields.Char(
        related='partner_id.tca_legal_authority', readonly=False,
        string='Issuing Authority',
    )
    tca_passport_country_id = fields.Many2one(
        related='partner_id.tca_passport_country_id', readonly=False,
        string='Passport Issuing Country',
    )

    # ── Computed helpers ──────────────────────────────────────────────────────

    def _get_tca_config_key(self, key):
        """Return a company-scoped ir.config_parameter key."""
        self.ensure_one()
        return f'tca.{self.id}.{key}'

    def _get_tca_param(self, key, default=None):
        """Read a company-scoped token param."""
        self.ensure_one()
        return self.env['ir.config_parameter'].sudo().get_param(
            self._get_tca_config_key(key), default
        )

    def _set_tca_param(self, key, value):
        """Write a company-scoped token param."""
        self.ensure_one()
        self.env['ir.config_parameter'].sudo().set_param(
            self._get_tca_config_key(key), value or ''
        )

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_tca_test_connection(self):
        """
        Test the TCA connection using the stored credentials.
        Fetches a token and retrieves the organisation name.
        """
        self.ensure_one()
        if not self.tca_client_id or not self.tca_client_secret:
            raise UserError(_(
                'Please enter both the TCA Client ID and Client Secret before testing the connection.'
            ))
        if not self.tca_base_url:
            raise UserError(_(
                'Please enter the TCA API Base URL before testing the connection.\n\n'
                'For sandbox/dev: use the URL provided by TCA.\n'
                'For production: https://api.tcapeppol.com'
            ))
        api_service = self.env['tca.api.service']
        try:
            token = api_service._fetch_new_token(self)
            if not token:
                raise UserError(_(
                    'Connection failed: TCA did not return an access token.\n\n'
                    'Please check:\n'
                    '• Client ID and Client Secret are correct\n'
                    '• The API Base URL is reachable'
                ))
            org_info = api_service.get_org_info(self)
            self.tca_org_name = org_info.get('name', '')
            self.tca_is_active = True
            # Materialise the six canonical PINT AE taxes (S/E/O/AE/Z/N)
            # × (sale + purchase) so users see exactly six entries in the
            # invoice tax picker. Idempotent.
            self.env['account.tax']._tca_ensure_pint_taxes(self)
        except UserError:
            self.tca_is_active = False
            self.tca_org_name = ''
            raise
        except Exception as exc:
            self.tca_is_active = False
            self.tca_org_name = ''
            error_str = str(exc)
            # Make common errors more user-friendly
            if 'nodename nor servname' in error_str or 'Name or service not known' in error_str:
                raise UserError(_(
                    'Connection failed: Cannot reach the TCA server.\n\n'
                    'The API Base URL "%s" could not be resolved.\n'
                    'Please check the URL is correct.',
                    self.tca_base_url
                )) from exc
            if '401' in error_str or 'authentication' in error_str.lower():
                raise UserError(_(
                    'Connection failed: Authentication rejected (401).\n\n'
                    'Please check your Client ID and Client Secret are correct.'
                )) from exc
            if 'timed out' in error_str.lower() or 'timeout' in error_str.lower():
                raise UserError(_(
                    'Connection failed: Request timed out.\n\n'
                    'The TCA server at "%s" did not respond in time.\n'
                    'Please try again or check if the URL is correct.',
                    self.tca_base_url
                )) from exc
            raise UserError(_(
                'Connection failed: %s\n\n'
                'Please check your TCA credentials and API Base URL.',
                error_str
            )) from exc

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('✓ TCA Connection Successful'),
                'message': _('Connected to TCA organisation: %s', self.tca_org_name or 'Unknown'),
                'type': 'success',
                'sticky': True,
            },
        }

    def action_tca_disconnect(self):
        """Clear all stored TCA tokens and mark the integration inactive."""
        self.ensure_one()
        for key in ('access_token', 'access_token_expires_at', 'refresh_token'):
            self._set_tca_param(key, '')
        self.tca_is_active = False
        self.tca_org_name = ''
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('TCA Disconnected'),
                'message': _('TCA integration has been disconnected for this company.'),
                'type': 'warning',
                'sticky': False,
            },
        }
