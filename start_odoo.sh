#!/bin/bash
/Users/devashishbeniwal/Documents/TCA/Odoo/odoo/.venv/bin/python \
  /Users/devashishbeniwal/Documents/TCA/Odoo/odoo/odoo-bin \
  --addons-path="/Users/devashishbeniwal/Documents/TCA/Odoo/odoo/addons,/Users/devashishbeniwal/Documents/TCA/Services/odoo_tca_module" \
  -d odoo19_tca \
  -u account_tca_peppol \
  --http-port=8069
