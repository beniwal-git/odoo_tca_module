# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
Shared test fixtures for account_tca_peppol.

Provides TcaTestCase, a TransactionCase subclass that sets up:
  - A UAE company with TCA credentials (inactive by default so real HTTP is never made)
  - A UAE partner with Peppol EAS 0235, TRN endpoint, and ubl_pint_ae format
  - A standard posted invoice (out_invoice) and a credit note (out_refund)
  - A 5% UAE VAT tax sourced from the loaded chart of accounts

All HTTP calls must be mocked in individual tests — no live network calls are made.
"""

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TcaTestCase(TransactionCase):
    """Base test class for account_tca_peppol unit tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # ── UAE country ───────────────────────────────────────────────────────
        cls.uae = cls.env.ref('base.ae', raise_if_not_found=False)
        if not cls.uae:
            cls.uae = cls.env['res.country'].create({'name': 'United Arab Emirates', 'code': 'AE'})

        # ── Company ───────────────────────────────────────────────────────────
        cls.company = cls.env['res.company'].create({
            'name': 'TCA Test Company LLC',
            'country_id': cls.uae.id,
            'vat': '100230400900003',
        })
        # Grant current user access to the new company so CoA loading works
        cls.env.user.company_ids |= cls.company

        # Load UAE chart of accounts — this installs receivable/payable/tax accounts
        # and sets company properties, preventing account_move_line constraint violations.
        cls.env['account.chart.template'].try_loading('ae', company=cls.company, install_demo=False)

        # Add UAE company fields expected by PINT AE (emirate, TRN, Peppol EAS)
        cls.company.partner_id.write({
            'country_id': cls.uae.id,
            'street': 'Sheikh Zayed Road',
            'city': 'Dubai',
            'peppol_eas': '0235',
            'peppol_endpoint': '1000000001',
            'tca_emirate': 'DXB',
            'tca_legal_id_type': 'TL',
            'tca_trade_license': 'DED-2024-000001',
            'tca_legal_authority': 'Department of Economic Development - Dubai',
        })
        # TCA settings — credentials are fake; tests that need HTTP must mock urlopen
        cls.company.write({
            'tca_client_id': 'test_client_id',
            'tca_client_secret': 'test_client_secret',
            'tca_is_active': False,   # tests opt-in via company.tca_is_active = True
            'tca_base_url': 'https://api.tcapeppol.test',
        })

        # ── Partner (UAE buyer) ───────────────────────────────────────────────
        cls.partner = cls.env['res.partner'].create({
            'name': 'UAE Buyer Co.',
            'is_company': True,
            'country_id': cls.uae.id,
            'street': 'Corniche Road',
            'city': 'Abu Dhabi',
            'vat': '200000000000003',
            'peppol_eas': '0235',
            'peppol_endpoint': '1000000002',
            'invoice_edi_format': 'ubl_pint_ae',
            'tca_emirate': 'AUH',
            'tca_legal_id_type': 'TL',
            'tca_trade_license': 'ADDED-2024-999',
            'tca_legal_authority': 'ADDED',
        })

        # ── Sale journal ──────────────────────────────────────────────────────
        # CoA loading creates journals for the company — reuse the sale journal.
        cls.journal = cls.env['account.journal'].search([
            ('type', '=', 'sale'),
            ('company_id', '=', cls.company.id),
        ], limit=1)
        if not cls.journal:
            cls.journal = cls.env['account.journal'].create({
                'name': 'Customer Invoices (TCA)',
                'type': 'sale',
                'code': 'TCAINV',
                'company_id': cls.company.id,
            })

        # ── UAE 5% VAT ────────────────────────────────────────────────────────
        # CoA loading creates UAE VAT taxes — reuse one rather than creating a
        # bare tax with no repartition-line accounts (which would cause constraint
        # violations on auto-generated tax journal lines).
        cls.tax_5 = cls.env['account.tax'].search([
            ('company_id', '=', cls.company.id),
            ('amount', '=', 5.0),
            ('type_tax_use', '=', 'sale'),
        ], limit=1)
        if not cls.tax_5:
            # Fallback: create a tax with a proper tax group.
            # The CoA should normally provide this; this path is a safety net.
            tax_group = cls.env['account.tax.group'].search([
                ('country_id', '=', cls.uae.id),
            ], limit=1)
            if not tax_group:
                tax_group = cls.env['account.tax.group'].create({
                    'name': 'VAT',
                    'country_id': cls.uae.id,
                })
            cls.tax_5 = cls.env['account.tax'].create({
                'name': 'UAE VAT 5%',
                'amount': 5.0,
                'amount_type': 'percent',
                'type_tax_use': 'sale',
                'company_id': cls.company.id,
                'tax_group_id': tax_group.id,
            })

        # ── Revenue account ───────────────────────────────────────────────────
        # CoA loading creates income accounts — reuse one.
        cls.revenue_account = cls.env['account.account'].with_company(cls.company).search([
            ('account_type', '=', 'income'),
            ('company_ids', 'in', cls.company.id),
        ], limit=1)
        if not cls.revenue_account:
            cls.revenue_account = cls.env['account.account'].create({
                'name': 'Revenue',
                'code': '400000',
                'account_type': 'income',
                'company_ids': [(6, 0, [cls.company.id])],
            })

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_invoice(self, move_type='out_invoice', quantity=10.0, price_unit=100.0,
                      commodity_type='S', currency=None, partner=None):
        """
        Create and post a minimal invoice/credit note for the UAE test partner.
        Pass partner= to override the default UAE buyer (e.g. for export tests).
        Returns a posted account.move.
        """
        vals = {
            'move_type': move_type,
            'partner_id': (partner or self.partner).id,
            'company_id': self.company.id,
            'journal_id': self.journal.id,
            'tca_buyer_reference': 'PO-TEST-001',
            'invoice_line_ids': [(0, 0, {
                'name': 'Consulting Services',
                'quantity': quantity,
                'price_unit': price_unit,
                'tax_ids': [(6, 0, [self.tax_5.id])],
                'account_id': self.revenue_account.id,
                'tca_commodity_type': commodity_type,
            })],
        }
        # Credit notes need reason (BTAE-03) — use VD (Volume Discount, no preceding ref needed)
        if move_type in ('out_refund', 'in_refund'):
            vals['tca_credit_note_reason'] = 'VD'
        if currency:
            vals['currency_id'] = currency.id
        invoice = self.env['account.move'].with_company(self.company).create(vals)
        invoice.action_post()
        return invoice

    def _get_builder(self):
        """Return the PINT AE EDI builder instance."""
        return self.env['account.edi.xml.ubl_pint_ae']

    def _export_xml(self, invoice):
        """
        Generate PINT AE XML for the given invoice.
        Returns (xml_bytes, errors) where errors is a dict (possibly empty).
        """
        builder = self._get_builder()
        return builder._export_invoice(invoice)
