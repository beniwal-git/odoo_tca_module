# Part of TCA. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models
from odoo.addons.account_tca_peppol.constants import (
    LEGACY_PLACEHOLDER_PARTICIPANT,
    RE_EMAIL,
    RE_PHONE,
    RE_UAE_PARTICIPANT,
    RE_UAE_TIN,
    RE_UAE_TRN,
    UAE_EMIRATES,
)
from odoo.exceptions import ValidationError

# UAE-specific legal entity identifier type codes (schemeAgencyID values)
UAE_LEGAL_ID_TYPES = [
    ('TL', 'Trade License (Commercial)'),
    ('EID', 'Emirates ID'),
    ('PAS', 'Passport'),
    ('CD', 'Cabinet Decision'),
]


class ResPartner(models.Model):
    """
    Extends res.partner to:
    1. Add 'ubl_pint_ae' to the invoice_edi_format selection field
    2. Register AE → ubl_pint_ae in the country format mapping
    3. Add UAE-specific Peppol fields (legal entity type, trade license authority)
    """
    _inherit = 'res.partner'

    # ── UAE-specific Peppol fields ────────────────────────────────────────────

    tca_legal_id_type = fields.Selection(
        selection=UAE_LEGAL_ID_TYPES,
        string='Legal ID Type (UAE)',
        help=(
            'Type of legal registration identifier (BTAE-15 for supplier, BTAE-16 for buyer). '
            'Required when EAS is 0235 and legal registration ID is provided. '
            'TL=Trade License, EID=Emirates ID, PAS=Passport, CD=Cabinet Decision.'
        ),
    )
    tca_legal_authority = fields.Char(
        string='Issuing Authority (UAE)',
        help=(
            'Name of the authority that issued the legal registration document (BTAE-12/11). '
            'Mandatory when Legal ID Type is "Trade License" (BTAE-15/16 = TL). '
            'Example: "Department of Economic Development - Abu Dhabi".'
        ),
    )
    tca_trade_license = fields.Char(
        string='Trade License / Registration ID',
        help=(
            'Legal registration identifier (IBT-030 supplier / IBT-047 buyer). '
            'For UAE companies this is the Trade License number, Emirates ID number, '
            'Passport number, or Cabinet Decision reference, depending on Legal ID Type.'
        ),
    )
    tca_emirate = fields.Selection(
        selection=[(e, e) for e in UAE_EMIRATES],
        string='Emirate',
        help=(
            'UAE emirate code for postal address (ibr-128-ae). '
            'Used in CountrySubentity when country is AE. '
            'AUH=Abu Dhabi, DXB=Dubai, SHJ=Sharjah, UAQ=Umm Al Quwain, '
            'FUJ=Fujairah, AJM=Ajman, RAK=Ras Al Khaimah.'
        ),
    )
    tca_passport_country_id = fields.Many2one(
        'res.country',
        string='Passport Issuing Country (BTAE-18/19)',
        help=(
            'BTAE-18 (Seller) / BTAE-19 (Buyer): ISO 3166-1 alpha-2 country code of the '
            'authority that issued the passport.\n'
            'Required when Legal ID Type is "Passport" (PAS).'
        ),
    )
    tca_legal_form = fields.Char(
        string='Legal Form (IBT-033)',
        help=(
            'IBT-033: Additional legal information about the seller/buyer, '
            'e.g. "Merchant", "LLC", "Free Zone Company". '
            'Rendered as CompanyLegalForm in the PINT AE XML.'
        ),
    )

    # ── invoice_edi_format: add ubl_pint_ae to the selection ─────────────────
    # Odoo 19: the partner's EDI-format field is `invoice_edi_format`
    # (renamed from `ubl_cii_format`, which existed up to Odoo 17). We extend
    # its selection here via _inherit + selection_add.

    invoice_edi_format = fields.Selection(
        selection_add=[('ubl_pint_ae', 'PINT AE (UAE Peppol)')],
        ondelete={'ubl_pint_ae': 'set null'},
    )

    # ── Country → format mapping ──────────────────────────────────────────────

    @api.model
    def _get_ubl_cii_formats_info(self):
        """
        EXTENDS account_edi_ubl_cii.
        Register the PINT AE format so UAE (AE) partners auto-select it and it
        is recognised as a Peppol format.

        Odoo 19: the country→format mapping is derived from this info dict
        (see res.partner._get_ubl_cii_formats_by_country) — the old
        `_get_ubl_cii_formats` dict-mutation hook no longer applies.
        """
        formats_info = super()._get_ubl_cii_formats_info()
        formats_info['ubl_pint_ae'] = {
            'countries': ['AE'],
            'on_peppol': True,
            'sequence': 200,
        }
        return formats_info

    # ── invoice_edi_format auto-suggestion ────────────────────────────────────
    # Odoo 19's `invoice_edi_format` field computes from
    # `_get_suggested_invoice_edi_format` — a base `account` hook that returns
    # False; `account_edi_ubl_cii` never overrides it (its country-mapping logic
    # is in the differently-named `_get_suggested_ubl_cii_edi_format`, used only
    # by the export path). Without an override here `invoice_edi_format` stays
    # empty for every UAE partner, so the TCA send-eligibility check and the
    # `_post()` compliance gate — both keyed on `invoice_edi_format ==
    # 'ubl_pint_ae'` — never fire. Suggest PINT AE for any AE-country partner,
    # matching how l10n_it_edi / l10n_pl_edi override this same hook.

    def _get_suggested_invoice_edi_format(self):
        # OVERRIDE account — auto-select PINT AE for UAE partners.
        res = super()._get_suggested_invoice_edi_format()
        if not res and self.commercial_partner_id._deduce_country_code() == 'AE':
            return 'ubl_pint_ae'
        return res

    # ── EDI builder dispatch ──────────────────────────────────────────────────

    @api.model
    def _get_edi_builder(self, invoice_edi_format):
        """
        EXTENDS account_edi_ubl_cii.
        Route the ubl_pint_ae format to the PINT AE builder model.
        """
        if invoice_edi_format == 'ubl_pint_ae':
            return self.env['account.edi.xml.ubl_pint_ae']
        return super()._get_edi_builder(invoice_edi_format)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _tca_is_uae_party(self):
        """True if this partner's country is the UAE (AE).

        Centralises the `partner.country_id and partner.country_id.code == 'AE'`
        predicate that was repeated in 10+ places across the addon. Callers
        decide whether they want to pass a partner or its commercial form —
        pass `partner.commercial_partner_id._tca_is_uae_party()` when the
        check should be at the commercial-partner level.

        Empty-recordset safe: returns False when called on an empty partner
        (e.g. a draft invoice with `partner_id` not yet set). Matches the
        boolean semantics of the original inline predicate.
        """
        if not self:
            return False
        self.ensure_one()
        return bool(self.country_id and self.country_id.code == 'AE')

    def _tca_emirate(self):
        """The UAE Emirate code for this partner.

        Falls back through: `tca_emirate` field → state code → empty string.
        Used as the `cbc:CountrySubentity` value for AE addresses and the
        ibr-128-ae validation source.

        Empty-recordset safe: returns '' when called on an empty partner.
        """
        if not self:
            return ''
        self.ensure_one()
        return self.tca_emirate or (self.state_id and self.state_id.code) or ''

    def _tca_get_tin(self):
        """
        Return the 10-digit UAE TIN for this partner.

        Per FTA, the TIN is the first 10 digits of the TRN. Users keep
        storing the full 15-character TRN in `partner.vat`; this helper
        derives the TIN for PINT AE IBT-032 emission (PartyTaxScheme/
        CompanyID), which schematron ibr-148-ae requires to match
        ^1[0-9]{9}$. Falls back to peppol_endpoint when vat is empty.

        Returns '' when neither source is set.
        """
        self.ensure_one()
        raw = (self.vat or self.peppol_endpoint or '').strip()
        if not raw:
            return ''
        # Already a 10-digit TIN? Use as-is.
        if len(raw) == 10:
            return raw
        # 15-char TRN: first 10 chars. (TRN regex allows alphanumeric in
        # later positions but real UAE TRNs are all-digit; slice is safe.)
        return raw[:10]

    # ── Peppol endpoint validation override ──────────────────────────────────
    # Format constraints (TRN / TIN / participant / email / phone regexes) live
    # in `account_tca_peppol.constants` — imported at top of file. The legacy
    # `1XXXXXXXXX` placeholder is `LEGACY_PLACEHOLDER_PARTICIPANT`.

    def _build_error_peppol_endpoint(self, eas, endpoint):
        """
        EXTENDS account_edi_ubl_cii.
        For UAE EAS 0235, the Peppol endpoint must be:
          - exactly 10 digits starting with "1" (UAE Peppol Participant ID), or
          - '1XXXXXXXXX' placeholder for unknown/anonymous buyers
        The 15-digit TRN goes in the Tax ID / PartyTaxScheme, not here.
        """
        if eas == '0235':
            if not endpoint:
                return _('The UAE Peppol endpoint is required for EAS 0235.')
            if endpoint == LEGACY_PLACEHOLDER_PARTICIPANT:
                return None
            if not RE_UAE_PARTICIPANT.match(endpoint):
                return _(
                    'The UAE Peppol endpoint must be exactly 10 digits starting with "1" '
                    '(UAE Peppol Participant ID). The 15-digit TRN belongs in the "Tax ID" '
                    'field instead. Current: "%s".', endpoint,
                )
            return None
        return super()._build_error_peppol_endpoint(eas, endpoint)

    @api.depends('peppol_eas')
    def _compute_peppol_endpoint(self):
        """
        EXTENDS account_edi_ubl_cii.
        Base implementation auto-fills `peppol_endpoint` from another field
        (e.g. `vat` for UAE EAS=0235) when the compute trigger fires. For UAE
        we treat the Peppol endpoint as a user-set identifier (Participant ID,
        10 digits) — distinct from the TRN. Auto-filling from vat would
        produce 15-digit values that fail our `RE_UAE_PARTICIPANT` regex.

        Strategy: only delegate to the parent for records that currently have
        NO endpoint set. Records the user has already filled keep their value
        untouched (no self-assignment, no spurious dirty-marker that would
        trigger downstream recomputes on every multi-record write).
        """
        # Records the user has already set: preserve untouched.
        # Records still empty: let the parent fill if it can (caller may have
        # set peppol_eas to a non-UAE EAS where auto-fill is appropriate).
        to_fill = self.filtered(lambda p: not p.peppol_endpoint)
        if to_fill:
            super(ResPartner, to_fill)._compute_peppol_endpoint()

    # ──────────────────────────────────────────────────────────────────────────
    # UAE PARTNER FORMAT VALIDATION — runs on save (create/write).
    #
    # FORMAT only: validates the *shape* of fields the user has filled. It
    # never requires a field to be present. PINT AE *completeness* (every
    # mandatory field set) is enforced where it actually matters — at invoice
    # send time, by the XML builder's `_export_invoice_constraints`.
    #
    # A save-blocking completeness `@api.constrains` was wrong on two counts:
    #   1. It prevented progressive configuration — you couldn't save a
    #      half-filled company.
    #   2. It mis-fired during `res.company.create`: the company's
    #      Invoicing-tab fields are *related* fields that propagate to the
    #      company partner only AFTER this constraint has already run, so the
    #      constraint saw them empty and raised even when the user had filled
    #      them. (That is the "still required after I filled it" bug.)
    # ──────────────────────────────────────────────────────────────────────────

    @api.constrains('is_company', 'country_id', 'vat', 'email', 'phone')
    def _check_tca_partner_formats(self):
        """
        Format validation for UAE business partners — runs on save.

        Scope: UAE company partners, and only when TCA e-invoicing is in use
        (a company's own partner gates on its `tca_is_active`; any other
        partner on whether any company has TCA active). TCA never activated →
        skipped entirely.

        Checks only the FORMAT of filled fields (TRN pattern, email, phone).
        An empty field always passes — so this never blocks creating or
        progressively configuring a company. Completeness is a send-time
        concern, handled by
        `account.edi.xml.ubl_pint_ae._export_invoice_constraints`.
        """
        Company = self.env['res.company'].sudo()
        any_tca_company = bool(Company.search_count([('tca_is_active', '=', True)]))
        for partner in self:
            if not partner.is_company:
                continue
            if not partner._tca_is_uae_party():
                continue
            own_company = Company.search([('partner_id', '=', partner.id)], limit=1)
            if own_company:
                if not own_company.tca_is_active:
                    continue
            elif not any_tca_company:
                continue

            errors = []

            # ── Tax ID (TRN) format ──────────────────────────────────────────
            # Accept the 15-char UAE TRN or the 10-digit TIN, both starting '1'.
            if partner.vat:
                v = partner.vat.strip()
                if not (RE_UAE_TRN.match(v) or RE_UAE_TIN.match(v)):
                    errors.append(_(
                        '"Tax ID" must be either the 15-character UAE TRN or the '
                        '10-digit TIN, both starting with "1". Current: "%s".', v
                    ))

            # ── Email format ─────────────────────────────────────────────────
            if partner.email and not RE_EMAIL.match(partner.email.strip()):
                errors.append(_(
                    '"Email" must be a valid email address (e.g. name@example.com). '
                    'Current: "%s".', partner.email
                ))

            # ── Phone format ─────────────────────────────────────────────────
            phone_val = (partner.phone or '').strip()
            if phone_val:
                if not RE_PHONE.match(phone_val):
                    errors.append(_(
                        '"Phone" may only contain digits, spaces, dashes, '
                        'parentheses, dots and a leading +. Current: "%s".',
                        phone_val
                    ))
                elif sum(c.isdigit() for c in phone_val) < 7:
                    errors.append(_(
                        '"Phone" must contain at least 7 digits. Current: "%s".',
                        phone_val
                    ))

            if errors:
                raise ValidationError(_(
                    'Please fix the following before saving "%(name)s":\n\n%(list)s',
                    name=partner.display_name or _('this contact'),
                    list='\n'.join(f'• {e}' for e in errors),
                ))
