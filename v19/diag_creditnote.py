"""Diagnose ibr-191-ae on a credit note — dump root element, type code,
ProfileExecutionID and whether PaymentMeans is present. Read-only."""
import re

cn = env['account.move'].search([('move_type', '=', 'out_refund')], order='id desc', limit=1)
if not cn:
    print("NO out_refund (credit note) found in the DB.")
else:
    print("CREDIT NOTE:", repr(cn.name), "id=", cn.id, "state=", cn.state)
    print("  move_type:", cn.move_type)
    print("  tca_invoice_type_code:", repr(cn.tca_invoice_type_code))
    print("  tca_uncl1001_code:", repr(cn.tca_uncl1001_code))
    print("  tca_transaction_type_flags:", repr(cn.tca_transaction_type_flags))
    print("  reversed_entry_id:", repr(cn.reversed_entry_id.name))
    print("  tca_is_self_billing:", cn.tca_is_self_billing)

    xml, errors = env['account.edi.xml.ubl_pint_ae']._export_invoice(cn)
    x = xml.decode()
    print("\n  builder constraint errors:", errors or "(none)")
    print("  ROOT element:", x[:160].split('>')[0] + '>')
    for tag in ('InvoiceTypeCode', 'CreditNoteTypeCode', 'ProfileExecutionID'):
        m = re.search(rf'<cbc:{tag}>([^<]*)</cbc:{tag}>', x)
        print(f"  cbc:{tag}:", repr(m.group(1)) if m else "(ABSENT)")
    m = re.search(r'<cac:PaymentMeans>.*?</cac:PaymentMeans>', x, re.S)
    print("  <cac:PaymentMeans>:", "PRESENT →\n" + re.sub(r'><', '>\n<', m.group(0))
          if m else "(absent)")

env.cr.rollback()
