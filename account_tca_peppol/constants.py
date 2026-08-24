# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
Shared constants for the TCA Peppol PINT AE integration.

Centralizes literals that used to be duplicated as class-level attributes
across account_move.py, account_edi_xml_pint_ae.py, and res_partner.py.
Pure data — no logic, no I/O.
"""

import re

# ── PINT AE document identity ────────────────────────────────────────────────
PINT_AE_CUSTOMIZATION_ID = 'urn:peppol:pint:billing-1@ae-1'
PINT_AE_PROFILE_ID = 'urn:peppol:bis:billing'

# ── Peppol EAS ────────────────────────────────────────────────────────────────
UAE_EAS = '0235'

# ── UAE Emirates codes (ibr-128-ae) ──────────────────────────────────────────
UAE_EMIRATES = ('AUH', 'DXB', 'SHJ', 'UAQ', 'FUJ', 'AJM', 'RAK')

# Odoo's built-in UAE state codes (res.country.state) differ from the PINT AE
# emirate codes above. Map them so an AE address emits a recognised emirate
# code (ibr-128-ae) instead of the raw Odoo code (e.g. 'DU' → 'DXB'). Codes
# already equal to an emirate code pass through unchanged.
UAE_STATE_CODE_TO_EMIRATE = {
    'AZ': 'AUH',  # Abu Dhabi
    'DU': 'DXB',  # Dubai
    'SH': 'SHJ',  # Sharjah
    'AJ': 'AJM',  # Ajman
    'UQ': 'UAQ',  # Umm al-Quwain
    'RK': 'RAK',  # Ras al-Khaimah
    'FU': 'FUJ',  # Fujairah
}

# ── PINT AE predefined endpoints (BIS Section 1.5.3, eas=0235) ──────────────
# Used when the document does not need to reach a real Peppol receiver: the
# participant ID is overridden to one of these so TCA reports to C5 (FTA
# platform) only.
PREDEFINED_DEEMED = '9900000097'            # Deemed Supply (BTAE-02 pos 2 = 1)
PREDEFINED_NOT_SUBJECT = '9900000098'       # Buyer not subject to UAE e-invoicing
PREDEFINED_EXPORT_NO_PEPPOL = '9900000099'  # Export, receiver not in Peppol (BTAE-02 pos 8 = 1)
# `1XXXXXXXXX` was the legacy placeholder used before BIS 1.5.3 was published.
# Treat it as equivalent to "anonymous / not-in-Peppol buyer" for backward compat.
LEGACY_PLACEHOLDER_PARTICIPANT = '1XXXXXXXXX'
ANON_BUYER_PIDS = frozenset((
    PREDEFINED_DEEMED,
    PREDEFINED_NOT_SUBJECT,
    PREDEFINED_EXPORT_NO_PEPPOL,
    LEGACY_PLACEHOLDER_PARTICIPANT,
))

# ── Format regexes ────────────────────────────────────────────────────────────
RE_UAE_TRN = re.compile(r'^1[a-zA-Z0-9]{14}$')
RE_UAE_TIN = re.compile(r'^1[0-9]{9}$')
RE_UAE_PARTICIPANT = re.compile(r'^1[0-9]{9}$')
RE_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
# Phone: allow digits + common separators (space, dash, parens, plus, dot).
# At least 7 digits total. Loose pattern — strict E.164 validation would
# need an external module.
RE_PHONE = re.compile(r'^[\d\s\-\(\)\+\.]+$')
