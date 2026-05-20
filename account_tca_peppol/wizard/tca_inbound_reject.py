# Part of TCA. See LICENSE file for full copyright and licensing details.

from odoo import _, fields, models
from odoo.exceptions import UserError


class TcaInboundRejectWizard(models.TransientModel):
    _name = 'tca.inbound.reject.wizard'
    _description = 'Reject Inbound TCA Invoice'

    move_id = fields.Many2one(
        'account.move',
        required=True,
        readonly=True,
    )
    reason = fields.Text(
        string='Rejection Reason',
        required=True,
        help='Explain why this invoice is being rejected. This will be logged in the chatter.',
    )

    def action_reject(self):
        self.ensure_one()
        if not self.reason or not self.reason.strip():
            raise UserError(_('Please provide a reason for rejecting this invoice.'))
        self.move_id._tca_apply_rejection(self.reason.strip())
        return {'type': 'ir.actions.act_window_close'}
