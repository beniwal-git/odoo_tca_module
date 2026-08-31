#!/bin/bash
/Users/devashishbeniwal/Documents/TCA/Odoo/odoo-17.0/.venv/bin/python \
  /Users/devashishbeniwal/Documents/TCA/Odoo/odoo-17.0/odoo-bin \
  --addons-path="/Users/devashishbeniwal/Documents/TCA/Odoo/odoo-17.0/addons,/Users/devashishbeniwal/Documents/TCA/Services/odoo_tca_module" \
  -d odoo17_tca_demo \
  -u account_tca_peppol \
  --http-port=8069
