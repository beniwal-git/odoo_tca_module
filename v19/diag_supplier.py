"""Diagnose ibr-181-ae / ibr-148-ae — dump the actual AccountingSupplierParty
the builder emits for a recent out_invoice. Read-only (no commit)."""
import re

seller = env['res.company'].browse(1)
sp = seller.partner_id
print("SELLER company:", repr(seller.name))
print("  partner id:", sp.id, " country:", repr(sp.country_id.code), repr(sp.country_id.name))
print("  vat:", repr(sp.vat), " _tca_get_tin():", repr(sp._tca_get_tin()))
print("  peppol_eas:", repr(sp.peppol_eas), " peppol_endpoint:", repr(sp.peppol_endpoint))
print("  tca_legal_id_type:", repr(sp.tca_legal_id_type))
print("  tca_trade_license:", repr(sp.tca_trade_license))
print("  tca_emirate:", repr(sp.tca_emirate))
print("  commercial_partner_id == self:", sp.commercial_partner_id == sp)

inv = env['account.move'].search([('move_type', '=', 'out_invoice')], order='id desc', limit=1)
print("\nINVOICE:", repr(inv.name), "id=", inv.id, "state=", inv.state,
      "company=", repr(inv.company_id.name))

builder = env['account.edi.xml.ubl_pint_ae']
try:
    xml, errors = builder._export_invoice(inv)
    print("builder constraint errors:", errors or "(none)")
    x = xml.decode()
    for tag in ('AccountingSupplierParty',):
        m = re.search(rf'<cac:{tag}>.*?</cac:{tag}>', x, re.S)
        print(f"\n=== <cac:{tag}> ===")
        if m:
            blk = m.group(0)
            # pretty-ish: break on tag boundaries
            print(re.sub(r'><', '>\n<', blk))
        else:
            print("(not found)")
except Exception:
    import traceback
    traceback.print_exc()

env.cr.rollback()
