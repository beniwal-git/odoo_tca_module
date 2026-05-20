"""Read-only DB inspection — what PINT AE identity data is on each company."""
print("=" * 70)
print("COMPANIES")
print("=" * 70)
for c in env['res.company'].search([]):
    p = c.partner_id
    print(f"\nCompany: {c.name!r} (id={c.id})  tca_is_active={c.tca_is_active}")
    print(f"  partner id={p.id}  country={p.country_id.code!r}")
    print(f"  vat={p.vat!r}   -> _tca_get_tin()={p._tca_get_tin()!r}")
    print(f"  peppol_eas={p.peppol_eas!r}  peppol_endpoint={p.peppol_endpoint!r}")
    print(f"  tca_legal_id_type={p.tca_legal_id_type!r}")
    print(f"  tca_trade_license={p.tca_trade_license!r}")
    print(f"  tca_legal_authority={p.tca_legal_authority!r}")
    print(f"  tca_emirate={p.tca_emirate!r}")

print("\n" + "=" * 70)
print("RECENT OUT INVOICES")
print("=" * 70)
for inv in env['account.move'].search(
        [('move_type', '=', 'out_invoice')], order='id desc', limit=6):
    print(f"  {inv.name} state={inv.state} tca={inv.tca_move_state} "
          f"company={inv.company_id.name!r} buyer={inv.partner_id.name!r} "
          f"edi_format={inv.partner_id.commercial_partner_id.invoice_edi_format!r}")
