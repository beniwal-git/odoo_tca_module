# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
Model-level regression tests for the P0/P2 fixes ported from 19.0:
  - AED currency lock (_compute_currency_id)
  - tca_create_einvoice per-document opt-out
  - tca_is_out_of_scope auto-mirror onto reversing credit notes
  - UAE VAT category N (Standard Rate Additional VAT) TaxAmount handling
"""

from odoo.tests import tagged

from .common import TcaTestCase


@tagged('post_install', '-at_install')
class TestAedCurrencyLock(TcaTestCase):

    def test_currency_forced_to_aed_when_tca_active(self):
        """A draft sale document on a TCA-active company must be forced to AED."""
        self.company.tca_is_active = True
        usd = self.env.ref('base.USD', raise_if_not_found=False)
        if not usd:
            self.skipTest('base.USD not available')
        invoice = self.env['account.move'].with_company(self.company).create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'journal_id': self.journal.id,
            'currency_id': usd.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Line',
                'quantity': 1.0,
                'price_unit': 100.0,
                'tax_ids': [(6, 0, [self.tax_5.id])],
                'account_id': self.revenue_account.id,
                'tca_commodity_type': 'S',
            })],
        })
        aed = self.env.ref('base.AED', raise_if_not_found=False)
        if aed:
            self.assertEqual(invoice.currency_id, aed)

    def test_currency_not_forced_when_tca_inactive(self):
        """Without TCA active, currency is left to the normal Odoo default — no lock."""
        self.company.tca_is_active = False
        usd = self.env.ref('base.USD', raise_if_not_found=False)
        if not usd:
            self.skipTest('base.USD not available')
        invoice = self.env['account.move'].with_company(self.company).create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'journal_id': self.journal.id,
            'currency_id': usd.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Line',
                'quantity': 1.0,
                'price_unit': 100.0,
                'tax_ids': [(6, 0, [self.tax_5.id])],
                'account_id': self.revenue_account.id,
                'tca_commodity_type': 'S',
            })],
        })
        self.assertEqual(invoice.currency_id, usd)

    def test_currency_not_forced_when_create_einvoice_off(self):
        """Ticking off tca_create_einvoice lifts the AED lock for that document."""
        self.company.tca_is_active = True
        usd = self.env.ref('base.USD', raise_if_not_found=False)
        if not usd:
            self.skipTest('base.USD not available')
        invoice = self.env['account.move'].with_company(self.company).create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'journal_id': self.journal.id,
            'currency_id': usd.id,
            'tca_create_einvoice': False,
            'invoice_line_ids': [(0, 0, {
                'name': 'Line',
                'quantity': 1.0,
                'price_unit': 100.0,
                'tax_ids': [(6, 0, [self.tax_5.id])],
                'account_id': self.revenue_account.id,
                'tca_commodity_type': 'S',
            })],
        })
        self.assertEqual(invoice.currency_id, usd)


@tagged('post_install', '-at_install')
class TestCreateEinvoiceToggle(TcaTestCase):

    def test_default_true(self):
        invoice = self._make_invoice()
        self.assertTrue(invoice.tca_create_einvoice)

    def test_send_ineligible_when_toggled_off(self):
        """_tca_is_send_eligible must return False when tca_create_einvoice is off,
        even though every other condition is met."""
        self.company.tca_is_active = True
        invoice = self._make_invoice()
        invoice.tca_create_einvoice = False
        self.assertFalse(invoice._tca_is_send_eligible())

    def test_send_eligible_when_toggled_on(self):
        self.company.tca_is_active = True
        invoice = self._make_invoice()
        self.assertTrue(invoice.tca_create_einvoice)
        self.assertTrue(invoice._tca_is_send_eligible())


@tagged('post_install', '-at_install')
class TestOutOfScopeMirror(TcaTestCase):

    def test_credit_note_mirrors_oos_from_original(self):
        """A credit note reversing an OOS invoice must itself be OOS (480 → 81)."""
        invoice = self._make_invoice()
        invoice.tca_is_out_of_scope = True
        self.assertEqual(invoice.tca_invoice_type_code, '480')

        reversal = self.env['account.move'].with_company(self.company).create({
            'move_type': 'out_refund',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'journal_id': self.journal.id,
            'reversed_entry_id': invoice.id,
            'tca_credit_note_reason': 'VD',
            'invoice_line_ids': [(0, 0, {
                'name': 'Return',
                'quantity': 1.0,
                'price_unit': 100.0,
                'tax_ids': [(6, 0, [self.tax_5.id])],
                'account_id': self.revenue_account.id,
                'tca_commodity_type': 'S',
            })],
        })
        self.assertTrue(reversal.tca_is_out_of_scope)
        self.assertEqual(reversal.tca_invoice_type_code, '81')

    def test_credit_note_mirrors_non_oos_from_original(self):
        """A credit note reversing a standard (non-OOS) invoice stays non-OOS (380 → 381)."""
        invoice = self._make_invoice()
        self.assertFalse(invoice.tca_is_out_of_scope)

        reversal = self.env['account.move'].with_company(self.company).create({
            'move_type': 'out_refund',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'journal_id': self.journal.id,
            'reversed_entry_id': invoice.id,
            'tca_credit_note_reason': 'VD',
            'invoice_line_ids': [(0, 0, {
                'name': 'Return',
                'quantity': 1.0,
                'price_unit': 100.0,
                'tax_ids': [(6, 0, [self.tax_5.id])],
                'account_id': self.revenue_account.id,
                'tca_commodity_type': 'S',
            })],
        })
        self.assertFalse(reversal.tca_is_out_of_scope)
        self.assertEqual(reversal.tca_invoice_type_code, '381')


@tagged('post_install', '-at_install')
class TestVatCategoryN(TcaTestCase):
    """UAE VAT category N (Standard Rate Additional VAT) — ibr-108-ae: the rate
    is present (5%) but TaxAmount must be forced to 0 / not added to payable."""

    def test_n_category_tax_amount_zeroed_in_xml(self):
        from lxml import etree
        n_tax = self.env['account.tax'].create({
            'name': 'Additional VAT (test)',
            'amount': 5.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.company.id,
            'tax_group_id': self.tax_5.tax_group_id.id,
            'tca_tax_category': 'N',
        })
        invoice = self.env['account.move'].with_company(self.company).create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'journal_id': self.journal.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Line',
                'quantity': 1.0,
                'price_unit': 100.0,
                'tax_ids': [(6, 0, [n_tax.id])],
                'account_id': self.revenue_account.id,
                'tca_commodity_type': 'S',
            })],
        })
        invoice.action_post()
        xml_bytes, errors = self._export_xml(invoice)
        self.assertFalse(errors, f'Unexpected export errors: {errors}')
        tree = etree.fromstring(xml_bytes)
        ns = {
            'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
            'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
        }
        # Document-level TaxTotal/TaxAmount must be 0 — the only tax present is N.
        amounts = tree.xpath(
            '//cac:TaxTotal[not(ancestor::cac:InvoiceLine)]/cbc:TaxAmount', namespaces=ns,
        )
        self.assertTrue(amounts)
        total = sum(float(n.text) for n in amounts)
        self.assertAlmostEqual(total, 0.0, places=2)

    def test_n_category_json_vat_amount_zero_but_rate_present(self):
        n_tax = self.env['account.tax'].create({
            'name': 'Additional VAT (test)',
            'amount': 5.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.company.id,
            'tax_group_id': self.tax_5.tax_group_id.id,
            'tca_tax_category': 'N',
        })
        invoice = self.env['account.move'].with_company(self.company).create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'journal_id': self.journal.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Line',
                'quantity': 1.0,
                'price_unit': 100.0,
                'tax_ids': [(6, 0, [n_tax.id])],
                'account_id': self.revenue_account.id,
                'tca_commodity_type': 'S',
            })],
        })
        invoice.action_post()
        detail = invoice._tca_build_json_detail()
        line = detail['lines'][0]
        self.assertEqual(line['vat_info'][0]['vat_category_code'], 'N')
        self.assertEqual(line['vat_info'][0]['vat_rate'], 5.0)  # rate present
        self.assertEqual(line['vat_line_amount_in_aed'], 0.0)   # amount zeroed
