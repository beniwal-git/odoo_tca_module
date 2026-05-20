#!/bin/bash
# Run Odoo 19 against the TCA dev DB on an alternate port (8081).
# No -u: assumes the module is already installed/upgraded. Use start_odoo.sh
# (port 8069 + -u account_tca_peppol) when picking up code changes.
/Users/devashishbeniwal/Documents/TCA/Odoo/odoo/.venv/bin/python \
  /Users/devashishbeniwal/Documents/TCA/Odoo/odoo/odoo-bin \
  --addons-path="/Users/devashishbeniwal/Documents/TCA/Odoo/odoo/addons,/Users/devashishbeniwal/Documents/TCA/Services/odoo_tca_module" \
  -d odoo19_tca \
  --http-port=8081
