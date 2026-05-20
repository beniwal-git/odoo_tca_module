"""B1-B3 smoke test — build a UAE invoice, run the PINT AE builder, dump XML.
Run:  odoo-bin shell -d odoo19_tca --addons-path=... < v19/smoke_b1_b3.py
Rolls back — leaves no data."""
import traceback

try:
    uae = env.ref('base.ae')
    # Create the company bare (no country) so the company partner passes the
    # _check_tca_partner_complete constraint; then write every TCA field in one
    # shot so the constraint re-check sees a complete AE partner.
    company = env['res.company'].create({
        'name': 'TCA Smoke Co', 'vat': '100230400900003',
    })
    env.user.company_ids = [(4, company.id)]
    company.partner_id.write({
        'country_id': uae.id, 'street': 'Sheikh Zayed Road', 'city': 'Dubai',
        'peppol_eas': '0235', 'peppol_endpoint': '1002304009',
        'tca_emirate': 'DXB', 'tca_legal_id_type': 'TL',
        'tca_trade_license': 'DED-2024-1', 'tca_legal_authority': 'DED Dubai',
    })
    company.write({'country_id': uae.id})
    env['account.chart.template'].try_loading('ae', company=company, install_demo=False)
    partner = env['res.partner'].create({
        'name': 'UAE Buyer Co', 'is_company': True, 'country_id': uae.id,
        'street': 'Corniche Road', 'city': 'Abu Dhabi', 'vat': '100000000000002',
        'peppol_eas': '0235', 'peppol_endpoint': '1000000002',
        'invoice_edi_format': 'ubl_pint_ae', 'tca_emirate': 'AUH',
        'tca_legal_id_type': 'TL', 'tca_trade_license': 'ADDED-2024-999',
        'tca_legal_authority': 'ADDED', 'ref': 'BUYER-INTERNAL-01',
    })
    journal = env['account.journal'].search(
        [('type', '=', 'sale'), ('company_id', '=', company.id)], limit=1)
    tax = env['account.tax'].search(
        [('company_id', '=', company.id), ('amount', '=', 5.0),
         ('type_tax_use', '=', 'sale')], limit=1)
    acct = env['account.account'].search(
        [('account_type', '=', 'income'), ('company_ids', 'in', company.id)], limit=1)
    print('FIXTURE: journal=%s tax=%s acct=%s' % (journal.name, tax.name, acct.code))

    inv = env['account.move'].with_company(company).create({
        'move_type': 'out_invoice', 'partner_id': partner.id,
        'company_id': company.id, 'journal_id': journal.id,
        'tca_buyer_reference': 'PO-1',
        'invoice_line_ids': [(0, 0, {
            'name': 'Consulting Services', 'quantity': 10.0, 'price_unit': 100.0,
            'tax_ids': [(6, 0, [tax.id])], 'account_id': acct.id,
            'product_uom_id': env.ref('uom.product_uom_unit').id,
            'tca_commodity_type': 'S', 'tca_service_accounting_code': '998311',
        })],
    })
    inv.action_post()
    print('INVOICE posted: %s  total=%s' % (inv.name, inv.amount_total))

    builder = env['account.edi.xml.ubl_pint_ae']
    xml, errors = builder._export_invoice(inv)
    print('=' * 70)
    print('EXPORT ERRORS:', errors or '(none)')
    print('=' * 70)
    print(xml.decode())
except Exception:
    traceback.print_exc()
finally:
    env.cr.rollback()
