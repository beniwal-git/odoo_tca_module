# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
Bridge res.config.settings ↔ res.company for TCA fields.
Odoo's Settings UI reads/writes from res.config.settings;
we proxy the TCA company fields through here.
"""

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Proxy fields — related to the current company
    tca_is_active = fields.Boolean(
        string='TCA Peppol Integration Active',
        related='company_id.tca_is_active',
        readonly=False,
    )
    tca_client_id = fields.Char(
        string='TCA Client ID',
        related='company_id.tca_client_id',
        readonly=False,
    )
    tca_client_secret = fields.Char(
        string='TCA Client Secret',
        related='company_id.tca_client_secret',
        readonly=False,
    )
    tca_base_url = fields.Char(
        string='TCA API Base URL',
        related='company_id.tca_base_url',
        readonly=False,
    )
    tca_webhook_secret = fields.Char(
        string='TCA Webhook Secret',
        related='company_id.tca_webhook_secret',
        readonly=False,
    )
    tca_org_name = fields.Char(
        string='TCA Organisation',
        related='company_id.tca_org_name',
        readonly=True,
    )
    def action_tca_test_connection(self):
        return self.company_id.action_tca_test_connection()

    def action_tca_disconnect(self):
        return self.company_id.action_tca_disconnect()
