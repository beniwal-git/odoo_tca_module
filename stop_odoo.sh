#!/bin/bash
pkill -f "odoo-bin" 2>/dev/null && echo "Odoo stopped" || echo "No Odoo process found"
