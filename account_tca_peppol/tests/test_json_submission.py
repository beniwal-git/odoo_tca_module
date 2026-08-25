# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
JSON submission builder (_tca_build_json_detail and friends) — the inline-JSON
outbound flow that replaced the XML/S3 3-step submission (P2.2). Pure-Python
dict construction, no HTTP involved — see test_tca_api.py for the API-layer
(submit_invoice_json) tests.
"""

from odoo import fields
from odoo.tests import tagged

from .. import constants
from .common import TcaTestCase


@tagged('post_install', '-at_install')
class TestJsonDetailBuilder(TcaTestCase):
    """_tca_build_json_detail must produce a §9-shaped `detail` tree."""

    def test_root_scalars(self):
        invoice = self._make_invoice()
        detail = invoice._tca_build_json_detail()
        self.assertEqual(detail['invoice_type_code'], '380')
        self.assertEqual(detail['invoice_currency_code'], invoice.currency_id.name)
        self.assertTrue(detail['issue_date'])
        self.assertEqual(len(detail['transaction_type_code']), 8)

    def test_process_control_uses_standard_billing_ids(self):
        """No self-billing swap — always the standard billing profile/customization."""
        invoice = self._make_invoice()
        detail = invoice._tca_build_json_detail()
        self.assertEqual(detail['process_control']['customization_id'], constants.PINT_AE_CUSTOMIZATION_ID)
        self.assertEqual(detail['process_control']['profile_id'], constants.PINT_AE_PROFILE_ID)

    def test_credit_note_type_code(self):
        cn = self._make_invoice(move_type='out_refund')
        detail = cn._tca_build_json_detail()
        self.assertEqual(detail['invoice_type_code'], '381')

    def test_seller_is_company_buyer_is_partner(self):
        """No self-billing swap: seller is always the company, buyer the customer."""
        invoice = self._make_invoice()
        detail = invoice._tca_build_json_detail()
        self.assertEqual(detail['seller']['name'], self.company.partner_id.name)
        self.assertEqual(detail['buyer']['name'], self.partner.name)
        self.assertEqual(detail['seller']['electronic_address_scheme'], constants.UAE_EAS)
        self.assertEqual(detail['seller']['vat_identifier'], self.company.partner_id.vat)
        self.assertEqual(detail['buyer']['vat_identifier'], self.partner.vat)

    def test_lines_shape_and_amounts(self):
        invoice = self._make_invoice(quantity=10.0, price_unit=100.0)
        detail = invoice._tca_build_json_detail()
        self.assertEqual(len(detail['lines']), 1)
        line = detail['lines'][0]
        self.assertEqual(line['line_id'], '1')
        self.assertEqual(line['invoiced_quantity'], 10.0)
        self.assertEqual(line['line_net_amount'], 1000.0)
        self.assertEqual(line['vat_info'][0]['vat_category_code'], 'S')
        self.assertEqual(line['vat_info'][0]['vat_rate'], 5.0)
        self.assertEqual(line['vat_line_amount_in_aed'], 50.0)

    def test_vat_breakdown_groups_by_category_and_rate(self):
        invoice = self._make_invoice(quantity=10.0, price_unit=100.0)
        detail = invoice._tca_build_json_detail()
        self.assertEqual(len(detail['vat_breakdowns']), 1)
        breakdown = detail['vat_breakdowns'][0]
        self.assertEqual(breakdown['vat_category_code'], 'S')
        self.assertEqual(breakdown['taxable_amount'], 1000.0)
        self.assertEqual(breakdown['tax_amount'], 50.0)

    def test_totals(self):
        invoice = self._make_invoice(quantity=10.0, price_unit=100.0)
        detail = invoice._tca_build_json_detail()
        totals = detail['totals']
        self.assertEqual(totals['sum_of_invoice_line_net_amount'], 1000.0)
        self.assertEqual(totals['invoice_total_vat_amount'], 50.0)
        self.assertEqual(totals['invoice_total_amount_with_vat'], 1050.0)

    def test_exempt_category_has_no_rate_and_no_vat_line_amount(self):
        """ibr-119-ae / ibr-163-ae: E category must omit vat_rate and
        vat_line_amount_in_aed entirely, not send them as 0."""
        exempt_tax = self.env['account.tax'].create({
            'name': 'Exempt (test)',
            'amount': 0.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.company.id,
            'tax_group_id': self.tax_5.tax_group_id.id,
            'tca_tax_category': 'E',
            'tca_exemption_reason_code': 'DL8.46.1',
        })
        invoice = self.env['account.move'].with_company(self.company).create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'journal_id': self.journal.id,
            'invoice_date': fields.Date.context_today(self.env['account.move']),
            'invoice_line_ids': [(0, 0, self._line_vals(
                name='Exempt service', tax_ids=[(6, 0, [exempt_tax.id])],
            ))],
        })
        invoice.action_post()
        detail = invoice._tca_build_json_detail()
        line = detail['lines'][0]
        self.assertNotIn('vat_rate', line['vat_info'][0])
        self.assertNotIn('vat_line_amount_in_aed', line)
        self.assertEqual(line['vat_info'][0]['vat_exemption_reason_code'], 'DL8.46.1')

    def test_prune_empty_drops_blanks_keeps_zero(self):
        raw = {
            'a': '',
            'b': None,
            'c': {},
            'd': [],
            'e': 0,
            'f': 0.0,
            'g': False,
            'h': 'value',
            'i': {'nested_empty': '', 'nested_value': 'x'},
        }
        from odoo.addons.account_tca_peppol.models.account_move import AccountMove
        pruned = AccountMove._tca_prune_empty(raw)
        self.assertNotIn('a', pruned)
        self.assertNotIn('b', pruned)
        self.assertNotIn('c', pruned)
        self.assertNotIn('d', pruned)
        self.assertEqual(pruned['e'], 0)
        self.assertEqual(pruned['f'], 0.0)
        self.assertEqual(pruned['g'], False)
        self.assertEqual(pruned['h'], 'value')
        self.assertEqual(pruned['i'], {'nested_value': 'x'})

    def test_credit_note_references_preceding_invoice(self):
        invoice = self._make_invoice()
        reversal = self.env['account.move'].with_company(self.company).create({
            'move_type': 'out_refund',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'journal_id': self.journal.id,
            'invoice_date': fields.Date.context_today(self.env['account.move']),
            'reversed_entry_id': invoice.id,
            'tca_credit_note_reason': 'DL8.61.1.D',
            'invoice_line_ids': [(0, 0, self._line_vals(name='Return'))],
        })
        reversal.action_post()
        detail = reversal._tca_build_json_detail()
        self.assertEqual(detail['references']['credit_note_reason_code'], 'DL8.61.1.D')
        self.assertEqual(detail['references']['preceding_invoices'][0]['id'], invoice.name)

    def test_volume_discount_credit_note_has_no_preceding_reference(self):
        """VD reason is the one case that does NOT require IBG-03."""
        cn = self._make_invoice(move_type='out_refund')  # common.py sets reason 'VD'
        detail = cn._tca_build_json_detail()
        self.assertNotIn('preceding_invoices', detail.get('references', {}))

    def test_payment_instructions_present_for_standard_invoice(self):
        invoice = self._make_invoice()
        detail = invoice._tca_build_json_detail()
        self.assertEqual(
            detail['payment_instructions'][0]['payment_means_type_code'],
            invoice.tca_payment_means_code,
        )

    def test_payment_instructions_absent_for_credit_note(self):
        cn = self._make_invoice(move_type='out_refund')
        detail = cn._tca_build_json_detail()
        self.assertNotIn('payment_instructions', detail)
