"""Shared constants for `account_tca_peppol` — single source of truth for
values previously re-declared across models, wizards, and tests.

Spec reference: PINT AE = ``urn:peppol:pint:billing-1@ae-1``, UBL 2.1, aligned
to UAE Cabinet Decision No. 106 of 2025 (FTA e-invoicing mandate).
"""

import re

# ──────────────────────────────────────────────────────────────────────────
# PINT AE identifiers
# ──────────────────────────────────────────────────────────────────────────

PINT_AE_CUSTOMIZATION_ID = 'urn:peppol:pint:billing-1@ae-1'
PINT_AE_SELFBILLING_CUSTOMIZATION_ID = 'urn:peppol:pint:selfbilling-1@ae-1'
PINT_AE_PROFILE_ID = 'urn:peppol:bis:billing'
PINT_AE_SELFBILLING_PROFILE_ID = 'urn:peppol:bis:selfbilling'

#: All PINT AE CustomizationIDs (billing + selfbilling) — for inbound routing.
PINT_AE_CUSTOMIZATION_IDS = frozenset(
    [
        PINT_AE_CUSTOMIZATION_ID,
        PINT_AE_SELFBILLING_CUSTOMIZATION_ID,
    ]
)

#: UAE Peppol Electronic Address Scheme code (FTA-assigned).
UAE_EAS = '0235'

#: UAE Emirate codes for CountrySubentity validation (ibr-128-ae).
#: AUH=Abu Dhabi, DXB=Dubai, SHJ=Sharjah, UAQ=Umm Al Quwain,
#: FUJ=Fujairah, AJM=Ajman, RAK=Ras Al Khaimah.
UAE_EMIRATES = ('AUH', 'DXB', 'SHJ', 'UAQ', 'FUJ', 'AJM', 'RAK')

#: Odoo's built-in UAE state codes (res.country.state, ISO-3166-2-ish) differ
#: from the PINT AE emirate codes above. Map them so an AE address emits a
#: recognised emirate code (ibr-128-ae) instead of the raw Odoo code (e.g.
#: 'DU' → 'DXB'). Codes already equal to an emirate code pass through unchanged.
UAE_STATE_CODE_TO_EMIRATE = {
    'AZ': 'AUH',  # Abu Dhabi
    'DU': 'DXB',  # Dubai
    'SH': 'SHJ',  # Sharjah
    'AJ': 'AJM',  # Ajman
    'UQ': 'UAQ',  # Umm al-Quwain
    'RK': 'RAK',  # Ras al-Khaimah
    'FU': 'FUJ',  # Fujairah
}

# ──────────────────────────────────────────────────────────────────────────
# PINT AE predefined participant IDs (BIS 1.5.3, eas=0235)
#
# Used when the document does not need to reach a real Peppol receiver: the
# participant ID is overridden to one of these so TCA reports to C5 (FTA
# platform) only.
# ──────────────────────────────────────────────────────────────────────────

PREDEFINED_DEEMED = '9900000097'  # Deemed Supply (BTAE-02 pos 2 = 1)
PREDEFINED_NOT_SUBJECT = '9900000098'  # Buyer not subject to UAE e-invoicing
PREDEFINED_EXPORT_NO_PEPPOL = '9900000099'  # Export, receiver not in Peppol (BTAE-02 pos 8 = 1)

#: Legacy placeholder used before BIS 1.5.3 was published. Treated as
#: equivalent to "anonymous / not-in-Peppol buyer" for backward compat.
LEGACY_PLACEHOLDER_PARTICIPANT = '1XXXXXXXXX'

#: All participant IDs that mean "no real Peppol receiver" — used to detect
#: anonymous buyers (`tca_buyer_participant_id` membership check).
ANON_BUYER_PIDS = frozenset(
    (
        PREDEFINED_DEEMED,
        PREDEFINED_NOT_SUBJECT,
        PREDEFINED_EXPORT_NO_PEPPOL,
        LEGACY_PLACEHOLDER_PARTICIPANT,
    )
)

# ──────────────────────────────────────────────────────────────────────────
# Validation regexes
#
# UAE FTA: the TIN is the first 10 digits of the 15-character TRN. The XML
# builder derives the 10-digit TIN for IBT-032 emission at write time.
# ──────────────────────────────────────────────────────────────────────────

#: 15-character UAE TRN — leading '1' + 14 alphanumeric (IBT-031 source).
RE_UAE_TRN = re.compile(r'^1[a-zA-Z0-9]{14}$')

#: 10-digit UAE TIN — leading '1' + 9 digits (IBT-032 supplier PartyTaxScheme/CompanyID).
RE_UAE_TIN = re.compile(r'^1[0-9]{9}$')

#: UAE Peppol Participant ID (EndpointID @schemeID=0235) — same shape as TIN.
RE_UAE_PARTICIPANT = re.compile(r'^1[0-9]{9}$')

#: Lightweight email-shape check (Odoo doesn't enforce a format by default).
RE_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

#: Phone: digits + common separators. At least 7 digits enforced separately.
RE_PHONE = re.compile(r'^[\d\s\-\(\)\+\.]+$')
