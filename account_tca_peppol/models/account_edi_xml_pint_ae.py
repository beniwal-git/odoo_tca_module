# -*- coding: utf-8 -*-
# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
PINT AE (UAE Peppol CIUS) UBL 2.1 XML builder.

Specification: urn:peppol:pint:billing-1@ae-1
Profile ID:    urn:peppol:bis:billing
Based on:      PINT (Peppol International Invoice model) aligned to UAE e-invoicing mandate
               Cabinet Decision No. 106 of 2025

UAE-specific additions over PEPPOL BIS3:
  BTAE-01  Buyer internal identification number  (BuyerCustomerParty/PartyIdentification/ID)
  BTAE-02  Invoice transaction type code          (ProfileExecutionID — 8-digit binary flags)
  BTAE-03  Credit note reason code                (DiscrepancyResponse/ResponseCode — mandatory on CN)
  BTAE-04  Currency exchange rate                  (TaxExchangeRate/CalculationRate, max 6dp)
  BTAE-05  Contract value                          (ContractDocumentReference/DocumentDescription)
  BTAE-06  Supply period description code          (InvoicePeriod/DescriptionCode)
  BTAE-07  Invoice UUID                            (cbc:UUID — UUID4 per document)
  BTAE-08  Per-line VAT amount                     (InvoiceLine/ItemPriceExtension/TaxTotal/TaxAmount)
  BTAE-09  Type of goods/services (RC)             (CommodityClassification/NatureCode — mandatory when AE)
  BTAE-10  Per-line amount payable                 (InvoiceLine/ItemPriceExtension/Amount)
  BTAE-11  Buyer trade license authority name      (CompanyID/@schemeAgencyName when BTAE-16=TL)
  BTAE-12  Seller trade license authority name     (CompanyID/@schemeAgencyName when BTAE-15=TL)
  BTAE-13  Commodity type code  G/S                (CommodityClassification/CommodityCode)
  BTAE-14  Principal TRN (Disclosed Agent)         (field stored; XML binding TBD)
  BTAE-15  Seller legal registration ID type       (CompanyID/@schemeAgencyID supplier)
  BTAE-16  Buyer legal registration ID type        (CompanyID/@schemeAgencyID buyer)
  BTAE-18  Seller passport issuing country         (CompanyID/@schemeAgencyName when BTAE-15=PAS)
  BTAE-19  Buyer passport issuing country          (CompanyID/@schemeAgencyName when BTAE-16=PAS)
  BTAE-20  Tax total in AED                        (second TaxTotal with currencyID=AED)
  IBT-003  Out of scope type codes                 (480 invoice / 81 credit note via tca_invoice_type_code)
  IBT-200  Tax included indicator                  (TaxTotal/TaxIncludedIndicator = false)
"""

import logging
import uuid as _uuid_mod

from odoo import models, fields, api, _

from .. import constants

_logger = logging.getLogger(__name__)


# ── PINT AE identifiers ────────────────────────────────────────────────────────
PINT_AE_CUSTOMIZATION_ID = constants.PINT_AE_CUSTOMIZATION_ID
# Self-billing variants are declared but not wired up — see
# docs/PORTING_17_vs_19.md P2.1 (self-billing intentionally out of scope
# for 17.0; Odoo 17/18 doesn't have core self-billing support to build on).
PINT_AE_SELFBILLING_CUSTOMIZATION_ID = 'urn:peppol:pint:selfbilling-1@ae-1'
PINT_AE_PROFILE_ID = constants.PINT_AE_PROFILE_ID
PINT_AE_SELFBILLING_PROFILE_ID = 'urn:peppol:bis:selfbilling'
UAE_EAS = constants.UAE_EAS

# UAE VAT categories used by the mandate (UNCL5305, restricted to the six
# codes valid under the UAE mandate — no EU-only G/K carryover).
UAE_VAT_CATEGORIES = {
    'S': 5.0,    # Standard Rate 5%
    'E': 0.0,    # Exempt
    'O': None,   # Out of scope / Not subject to VAT
    'AE': 5.0,   # Reverse Charge (VAT accounted by buyer)
    'Z': 0.0,    # Zero Rated
    'N': 5.0,    # Standard Rate Additional VAT (reported, not added to payable — see ibr-108-ae)
}


class AccountEdiXmlUBL21Extended(models.AbstractModel):
    """
    Extends the base UBL 2.1 builder so that any caller looking up
    `_get_customization_ids()` on `account.edi.xml.ubl_21` (e.g. account_peppol's
    endpoint validator) sees the `ubl_pint_ae` key. Without this, a KeyError
    fires when account_peppol checks an AE partner endpoint.
    """
    _inherit = 'account.edi.xml.ubl_21'

    @api.model
    def _get_customization_ids(self):
        ids = super()._get_customization_ids()
        ids['ubl_pint_ae'] = PINT_AE_CUSTOMIZATION_ID
        return ids


class AccountEdiXmlUBLPintAe(models.AbstractModel):
    """
    PINT AE XML builder — inherits the full UBL BIS3 pipeline and
    overrides/extends only the UAE-specific elements.
    """
    _name = 'account.edi.xml.ubl_pint_ae'
    _inherit = 'account.edi.xml.ubl_bis3'
    _description = 'UAE PINT AE (Peppol International Invoice — UAE Annex)'

    # ──────────────────────────────────────────────────────────────────────────
    # FILENAME & SCHEMATRON
    # ──────────────────────────────────────────────────────────────────────────

    def _export_invoice_filename(self, invoice):
        return f"{invoice.name.replace('/', '_')}_pint_ae.xml"

    def _export_invoice_ecosio_schematrons(self):
        return {}  # TCA runs its own schematron; no ecosio integration needed

    # ──────────────────────────────────────────────────────────────────────────
    # CUSTOMIZATION / PROFILE IDs
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _get_customization_ids(self):
        ids = super()._get_customization_ids()
        ids['ubl_pint_ae'] = PINT_AE_CUSTOMIZATION_ID
        return ids

    # ──────────────────────────────────────────────────────────────────────────
    # BTAE-02: ProfileExecutionID
    # ──────────────────────────────────────────────────────────────────────────

    def _get_profile_execution_id(self, invoice):
        """
        BTAE-02: UAE ProfileExecutionID — 8-digit binary flag string.

        Positions: [FTZ][DeemedSupply][MarginScheme][SummaryInv][ContinuousSupply]
                   [DisclosedAgent][Ecommerce][Exports]

        All standard use cases (UC1 Standard, UC2 Reverse Charge, UC3 Zero-Rated):
          '00000000'
        UC4 Deemed Supply: '01000000'
        UC6 Summary Invoice: '00010000'
        UC7 Continuous Supply: '00001000'
        UC8 Free Trade Zone: '10000000'
        UC9 E-commerce: '00000010'
        UC10 Exports: '00000001'
        UC11 Margin Scheme: '00100000'
        UC5/UC13 Disclosed Agent: '00000100'

        Reads from tca_transaction_type_flags field. Defaults to '00000000'.
        """
        flags = (invoice.tca_transaction_type_flags or '00000000').strip()
        if len(flags) != 8 or not all(c in '01' for c in flags):
            _logger.warning(
                'PINT AE: invalid BTAE-02 flags "%s" on invoice %s — using 00000000',
                flags, invoice.name
            )
            flags = '00000000'

        # Export (position 8) is a manual user flag (tca_flag_export),
        # already encoded in tca_transaction_type_flags — no auto-detection
        # from buyer country here. A foreign buyer alone does not make a
        # supply an "export" (it may be out-of-scope / not-subject-to-VAT
        # instead); auto-forcing this bit previously blocked those cases.
        return flags

    # ──────────────────────────────────────────────────────────────────────────
    # F2-2: VAT CATEGORY / EXEMPTION — UAE-specific overrides
    # ──────────────────────────────────────────────────────────────────────────

    def _get_tax_unece_codes(self, invoice, tax):
        """
        EXTENDS account.edi.common.
        If the tax has UAE-specific fields set (tca_tax_category, tca_exemption_reason_code,
        tca_exemption_reason), use them instead of Odoo's EU-centric defaults.

        IBT-118: TaxCategory/ID          ← tca_tax_category
        IBT-121: TaxExemptionReasonCode  ← tca_exemption_reason_code
        IBT-120: TaxExemptionReason      ← tca_exemption_reason
        """
        result = super()._get_tax_unece_codes(invoice, tax)

        if not tax:
            return result

        # Override category code if UAE category is explicitly set
        if tax.tca_tax_category:
            result['tax_category_code'] = tax.tca_tax_category

        # Override exemption codes if UAE-specific values are provided
        if tax.tca_exemption_reason_code:
            result['tax_exemption_reason_code'] = tax.tca_exemption_reason_code
        if tax.tca_exemption_reason:
            result['tax_exemption_reason'] = tax.tca_exemption_reason

        return result

    # ──────────────────────────────────────────────────────────────────────────
    # PARTY VALS — UAE-specific overrides
    # ──────────────────────────────────────────────────────────────────────────

    def _get_partner_party_legal_entity_vals_list(self, partner):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        Adds UAE-specific schemeAgencyID (BTAE-15/16) and schemeAgencyName:
          - BTAE-12/11: Issuing authority name when type = TL
          - BTAE-18/19: Passport issuing country code when type = PAS

        For the buyer party, prefer invoice-level overrides (set via env context
        by `_export_invoice_vals`) so users can fill values per invoice without
        editing the partner record.
        """
        vals_list = super()._get_partner_party_legal_entity_vals_list(partner)

        ctx = self.env.context
        is_buyer = ctx.get('tca_buyer_partner_id') == partner.id

        # Resolve each value: invoice override (buyer only) → partner field
        if is_buyer and ctx.get('tca_buyer_trade_license_override'):
            trade_license = ctx['tca_buyer_trade_license_override']
        else:
            trade_license = (
                partner.tca_trade_license
                or partner.company_registry
                or partner.vat
                or ''
            )

        if is_buyer and ctx.get('tca_buyer_legal_id_type_override'):
            legal_id_type = ctx['tca_buyer_legal_id_type_override']
        else:
            legal_id_type = partner.tca_legal_id_type

        if is_buyer and ctx.get('tca_buyer_legal_authority_override'):
            legal_authority = ctx['tca_buyer_legal_authority_override']
        else:
            legal_authority = partner.tca_legal_authority

        if is_buyer and ctx.get('tca_buyer_passport_country_code_override'):
            passport_country_code = ctx['tca_buyer_passport_country_code_override']
        else:
            passport_country_code = (
                partner.tca_passport_country_id.code
                if partner.tca_passport_country_id else ''
            )

        for vals in vals_list:
            if trade_license:
                vals['company_id'] = trade_license
                if legal_id_type:
                    attrs = {'schemeAgencyID': legal_id_type}
                    if legal_id_type == 'TL' and legal_authority:
                        # BTAE-12/11: trade license issuing authority
                        attrs['schemeAgencyName'] = legal_authority
                    elif legal_id_type == 'PAS' and passport_country_code:
                        # BTAE-18/19: passport issuing country code
                        attrs['schemeAgencyName'] = passport_country_code
                    vals['company_id_attrs'] = attrs
            # IBT-033: CompanyLegalForm (still partner-level only)
            if partner.tca_legal_form:
                vals['company_legal_form'] = partner.tca_legal_form

        return vals_list

    def _get_partner_party_tax_scheme_vals_list(self, partner, role):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        For UAE partners (peppol_eas='0235'), emit exactly ONE PartyTaxScheme
        entry with CompanyID = 10-digit TIN and TaxScheme/ID = 'VAT'.

        Schematron rules at play:
          - ibr-148-ae (line 2356, PINT-jurisdiction-aligned-rules.xslt):
            Supplier PartyTaxScheme/CompanyID must match ^1[0-9]{9}$.
          - ibr-133-ae (line 2379): every TaxScheme/ID in the document must
            be 'VAT' whenever the supplier emits PartyTaxScheme/CompanyID.
          - ibr-134-ae: rejects parent UBL's NOT_EU_VAT fallback for UAE
            (partner.vat starts with a digit); requires 'VAT'.
          - ibr-104 / ibr-179-ae: count(PartyTaxScheme/CompanyID) <= 1.

        Out-of-Scope (480 / 81) is NOT signaled by '!VAT' on PartyTaxScheme.
        The OOS classification lives in InvoiceTypeCode + line/document
        ClassifiedTaxCategory.ID='O' (with Percent stripped). The supplier's
        TaxScheme/ID stays 'VAT' on OOS docs to satisfy ibr-133-ae.

        Source of TIN: `vat` preferred, fallback to `peppol_endpoint` (which
        happens to share the 10-digit format).
        """
        if partner.peppol_eas == UAE_EAS:
            # PartyTaxScheme/CompanyID for an AE-country supplier carries the
            # 15-char TRN. Schematron rules:
            #   - ibr-132-ae (priority 1002, matches Party[country=AE]/.../
            #     PartyTaxScheme/CompanyID): demands ^1[a-zA-Z0-9]{14}$.
            #   - ibr-148-ae (priority 1001, matches AccountingSupplierParty/
            #     Party/PartyTaxScheme/CompanyID): demands ^1[0-9]{9}$.
            # XSLT priority resolves to ibr-132-ae for an AE supplier, so we
            # MUST emit the full TRN, not the sliced TIN. (The 10-digit TIN
            # rule is effectively for non-AE suppliers, which PINT AE
            # doesn't really cover.)
            trn = partner.vat or partner.peppol_endpoint or ''
            if trn:
                return [{
                    'company_id': trn,
                    'tax_scheme_vals': {'id': 'VAT'},
                }]
        # Non-UAE partner — defer to parent
        return super()._get_partner_party_tax_scheme_vals_list(partner, role)

    def _get_partner_address_vals(self, partner):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        UAE mandate (ibr-128-ae): when country = AE, CountrySubentity must be
        one of AUH / DXB / SHJ / UAQ / FUJ / AJM / RAK.

        For the buyer party, prefer the invoice-level `tca_buyer_emirate`
        override (set via env context by `_export_invoice_vals`) so users can
        fill it per invoice without editing the partner record.
        """
        vals = super()._get_partner_address_vals(partner)
        if partner.country_id and partner.country_id.code == 'AE':
            emirate = ''
            ctx = self.env.context
            if ctx.get('tca_buyer_partner_id') == partner.id and ctx.get('tca_buyer_emirate_override'):
                emirate = ctx['tca_buyer_emirate_override']
            if not emirate:
                emirate = partner.tca_emirate or ''
            if not emirate and partner.state_id:
                emirate = partner.state_id.code or ''
            vals['country_subentity'] = emirate
        return vals

    # ──────────────────────────────────────────────────────────────────────────
    # TAX TOTAL VALS — IBT-200 + BTAE-20 (AED total for foreign currency)
    # ──────────────────────────────────────────────────────────────────────────

    def _get_invoice_tax_totals_vals_list(self, invoice, taxes_vals):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        1. IBT-200: Adds TaxIncludedIndicator = false (mandatory in PINT AE).
        2. BTAE-20: When invoice currency != AED, adds a second minimal TaxTotal
           in AED (accounting currency) containing only cbc:TaxAmount.
        """
        vals_list = super()._get_invoice_tax_totals_vals_list(invoice, taxes_vals)

        # BTAE-20 requires AED specifically, not just company currency
        aed = self.env.ref('base.AED', raise_if_not_found=False) or invoice.company_id.currency_id

        for vals in vals_list:
            # IBT-200: UAE always VAT-exclusive pricing for B2B
            vals['tax_included_indicator'] = 'false'

            # ibr-119-ae: each VAT breakdown (TaxSubtotal) shall have a
            # VAT category rate UNLESS the invoice is "not subject to VAT".
            # For OOS subtotals (category 'O'), drop Percent at both
            # subtotal and TaxCategory levels.
            subtotals = vals.get('tax_subtotal_vals', []) or []
            for sub in subtotals:
                cat = sub.get('tax_category_vals') or {}
                if cat.get('id') == 'O':
                    sub.pop('percent', None)
                    cat.pop('percent', None)
                elif cat.get('id') == 'N':
                    # ibr-108-ae: category N (Standard Rate Additional VAT)
                    # reports the additional taxable base but must NOT add
                    # to the document's payable VAT — force TaxAmount to 0.
                    sub['tax_amount'] = 0.0

            # ibr-co-14: TaxTotal/TaxAmount must equal the sum of its
            # TaxSubtotal amounts — re-derive it since a category-N
            # subtotal may have just been zeroed above.
            if subtotals:
                vals['tax_amount'] = sum((s.get('tax_amount') or 0.0) for s in subtotals)

        # BTAE-20: second TaxTotal in AED when invoice is in foreign currency
        if invoice.currency_id and invoice.currency_id != aed:
            # Convert tax amount to AED
            tax_amount_company = taxes_vals.get('tax_amount', 0.0)
            # If company currency is already AED, use directly; otherwise convert
            if invoice.company_id.currency_id == aed:
                aed_tax_amount = round(tax_amount_company, 2)
            else:
                aed_tax_amount = round(invoice.company_id.currency_id._convert(
                    tax_amount_company, aed, invoice.company_id,
                    invoice.invoice_date or fields.Date.today(),
                ), 2)
            vals_list.append({
                'currency': aed,
                'currency_dp': 2,
                'tax_amount': aed_tax_amount,
                'btae_20_aed_total': True,
            })

        return vals_list

    # ──────────────────────────────────────────────────────────────────────────
    # INVOICE LINE VALS — BTAE-08, BTAE-09, BTAE-10, BTAE-13, IBT-158
    # ──────────────────────────────────────────────────────────────────────────

    def _get_invoice_line_item_vals(self, line, taxes_vals):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        Adds item identifiers (IBT-155/156/157), restores TaxExemptionReasonCode/Reason
        at line level, and adds PerUnitAmount for margin/e-commerce.
        """
        vals = super()._get_invoice_line_item_vals(line, taxes_vals)

        # ── IBT-155: Seller item identifier ───────────────────────────────────
        if line.tca_seller_item_id:
            vals['sellers_item_identification_vals'] = {'id': line.tca_seller_item_id}

        # ── IBT-156: Buyer item identifier ────────────────────────────────────
        if line.tca_buyer_item_id:
            vals['buyers_item_identification_vals'] = {'id': line.tca_buyer_item_id}

        # ── IBT-157: Standard item identifier (GTIN etc.) ────────────────────
        if line.tca_standard_item_id:
            vals['standard_item_identification_vals'] = {
                'id': line.tca_standard_item_id,
                'id_attrs': {'schemeID': line.tca_standard_item_scheme or '0160'},
            }

        # BIS3 strips tax_exemption_reason_code/reason from ClassifiedTaxCategory.
        # PINT AE needs them back for exempt (E) lines (ibr-167-ae).
        # Also add PerUnitAmount for margin scheme (N) and e-commerce.
        per_unit_amount = line.tca_per_unit_amount if hasattr(line, 'tca_per_unit_amount') else 0
        currency = line.currency_id or line.company_currency_id

        for ctc in vals.get('classified_tax_category_vals', []):
            tax_category_code = ctc.get('id', '')

            # Restore exemption reason for E category from the line's actual taxes
            if tax_category_code == 'E':
                for tax in line.tax_ids:
                    unece = self._get_tax_unece_codes(line.move_id, tax)
                    if unece.get('tax_category_code') == 'E':
                        if unece.get('tax_exemption_reason_code'):
                            ctc['tax_exemption_reason_code'] = unece['tax_exemption_reason_code']
                        if unece.get('tax_exemption_reason'):
                            ctc['tax_exemption_reason'] = unece['tax_exemption_reason']
                        break

            # aligned-ibrp-o-05: line ClassifiedTaxCategory with ID='O'
            # (Not subject to VAT) MUST NOT carry an Invoiced item VAT rate
            # (IBT-152, the Percent). Drop the percent key so the parent
            # template suppresses it.
            if tax_category_code == 'O':
                ctc.pop('percent', None)

            # PerUnitAmount only for margin (N) and e-commerce lines that have it set
            if per_unit_amount and tax_category_code in ('N', 'S'):
                ctc['per_unit_amount'] = per_unit_amount
                ctc['per_unit_amount_currency'] = currency

        return vals

    def _get_invoice_line_price_vals(self, line):
        """
        EXTENDS account.edi.xml.ubl_20.
        ibr-126-ae: BaseQuantity and GrossPrice (AllowanceCharge/BaseAmount) are mandatory.
        """
        vals = super()._get_invoice_line_price_vals(line)
        vals['base_quantity'] = 1
        # AllowanceCharge inside Price: ChargeIndicator=false, Amount=discount, BaseAmount=gross
        net = vals.get('price_amount', 0.0)
        gross = net  # default when no discount
        discount_amount = 0.0
        if line.discount and line.discount != 100.0:
            gross = net / (1.0 - line.discount / 100.0)
            discount_amount = gross - net
        vals['allowance_charge_vals'] = {
            'charge_indicator': 'false',
            'amount': round(discount_amount, 10),
            'base_amount': round(gross, 10),
            'currency': line.currency_id,
            'currency_dp': self._get_currency_decimal_places(line.currency_id),
        }
        return vals

    def _get_invoice_line_vals(self, line, line_id, taxes_vals):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        Adds UAE-specific per-line elements:
          - BTAE-10: ItemPriceExtension/Amount (net line amount payable)
          - BTAE-08: ItemPriceExtension/TaxTotal/TaxAmount (per-line VAT)
          - BTAE-09: CommodityClassification/NatureCode (RC goods/services type code)
          - BTAE-13: CommodityClassification/CommodityCode (G/S/B)
          - IBT-158: CommodityClassification/ItemClassificationCode (HS code)
          - BTAE-17: AdditionalItemIdentification with schemeID="SAC"
          - BTAE-24: ItemInstance/LotIdentification/LotNumberID
          - Line-level TaxTotal (restored from BIS3 removal)
        """
        vals = super()._get_invoice_line_vals(line, line_id, taxes_vals)

        currency = line.currency_id or line.company_currency_id
        dp = 2

        # ── IBT-127: Line note ────────────────────────────────────────────────
        if line.tca_line_note:
            vals['note'] = line.tca_line_note

        # ── IBT-133: Buyer accounting reference per line ──────────────────────
        # (AccountingCost at line level — parent renders it from vals)

        # ── IBT-132: Order line reference ─────────────────────────────────────
        if line.tca_order_line_ref:
            vals['order_line_ref'] = line.tca_order_line_ref

        # ── IBG-26: Line invoice period ───────────────────────────────────────
        if line.tca_line_period_start or line.tca_line_period_end:
            vals['invoice_period_vals_list'] = [{
                'start_date': line.tca_line_period_start,
                'end_date': line.tca_line_period_end,
            }]

        # ── BTAE-10: net line amount ──────────────────────────────────────────
        line_net_amount = vals.get('line_extension_amount', 0.0)

        # ── BTAE-08: per-line VAT amount ──────────────────────────────────────
        line_vat_amount = sum(
            detail.get('tax_amount_currency', 0.0)
            for detail in taxes_vals.get('tax_details', {}).values()
        )

        vals['item_price_extension_vals'] = {
            'amount': round(line_net_amount + line_vat_amount, dp),
            'currency': currency,
            'currency_dp': dp,
            'tax_total_vals': {
                'tax_amount': round(line_vat_amount, dp),
                'currency': currency,
                'currency_dp': dp,
            },
        }

        # ── Restore line-level TaxTotal (BIS3 removes it, PINT AE needs it) ──
        vals['tax_total_vals'] = [{
            'currency': currency,
            'currency_dp': dp,
            'tax_amount': round(line_vat_amount, dp),
        }]

        # ── Commodity classification (BTAE-13 + IBT-158 + BTAE-09) ───────────
        commodity_code = line.tca_effective_commodity_type
        hs_code = line.tca_hs_code or ''
        rc_description = line.tca_rc_description or ''

        item_vals = vals.get('item_vals', {})
        pint_classifications = [{'commodity_code': commodity_code}]

        # BTAE-09: NatureCode for reverse charge (goes inside CommodityClassification)
        if rc_description:
            pint_classifications[0]['nature_code'] = rc_description

        if hs_code:
            # If we already have a classification entry with nature_code, add HS to same entry
            if rc_description:
                pint_classifications[0]['item_classification_code'] = hs_code
                pint_classifications[0]['item_classification_code_attrs'] = {
                    'listID': 'HS',
                    'listVersionID': '1.0',
                }
            else:
                pint_classifications.append({
                    'item_classification_code': hs_code,
                    'item_classification_code_attrs': {
                        'listID': 'HS',
                        'listVersionID': '1.0',
                    },
                })
        item_vals['pint_ae_commodity_classifications'] = pint_classifications

        # ── BTAE-17: Service accounting code (CommodityClassification/ItemClassificationCode[@listID='SAC'])
        sac = getattr(line, 'tca_service_accounting_code', None) or ''
        if sac:
            pint_classifications.append({
                'item_classification_code': sac,
                'item_classification_code_attrs': {
                    'listID': 'SAC',
                },
            })

        # ── BTAE-24: Lot number (exports) ─────────────────────────────────────
        lot_number = getattr(line, 'tca_lot_number', None) or ''
        if lot_number:
            item_vals['btae_24_lot_number'] = lot_number

        vals['item_vals'] = item_vals
        return vals

    # ──────────────────────────────────────────────────────────────────────────
    # INVOICE PERIOD — BTAE-06 billing frequency
    # ──────────────────────────────────────────────────────────────────────────

    def _get_invoice_period_vals_list(self, invoice):
        """
        EXTENDS account.edi.xml.ubl_20.
        Adds BTAE-06 billing frequency code and IBT-073/074 period dates.
        """
        vals_list = super()._get_invoice_period_vals_list(invoice)
        freq = invoice.tca_billing_frequency or ''
        start = invoice.tca_invoice_period_start
        end = invoice.tca_invoice_period_end
        if freq or start or end:
            if vals_list:
                entry = vals_list[0]
            else:
                entry = {}
                vals_list = [entry]
            if freq:
                entry['description_code'] = freq
            if start:
                entry['start_date'] = start
            if end:
                entry['end_date'] = end
        return vals_list

    # ──────────────────────────────────────────────────────────────────────────
    # ibr-191-ae: suppress PaymentMeans for credit notes; keep it (with a
    # user-selected or fallback code) for Deemed Supply. The schematron rule
    # technically exempts PaymentMeans from being required on both, but in
    # practice TCA's server still wants the element present for anything
    # other than a credit note — so only credit notes suppress it entirely.
    # ──────────────────────────────────────────────────────────────────────────

    def _get_invoice_payment_means_vals_list(self, invoice):
        """EXTENDS account.edi.xml.ubl_bis3.
        ibr-191-ae: credit notes (381/81/261) MUST NOT carry a
        <cac:PaymentMeans> element — returning [] suppresses the entire group
        in the rendered XML (the QWeb t-foreach loop simply iterates 0×).
        Otherwise, emit ONE <cac:PaymentMeans> per tca_payment_means_ids line
        — PaymentMeans is 0..n per PINT AE (IBG-16), so an invoice can
        declare several (e.g. part cash, part credit card). Deemed-Supply
        invoices with no line at all fall back to '1' (Instrument not
        defined), since TCA's server still expects the element present even
        though the schematron doesn't strictly require it for that use case.

        Bank details (code 30, credit transfer) are attached from the
        invoice's own partner_bank_id, same as upstream. Card details (codes
        54/55) map to cac:CardAccount (IBG-18); mandate details (code 49)
        map to cac:PaymentMandate (IBG-19). Both are capped at ONE per
        document (ibr-066-ae / ibr-067-ae respectively) — only the first
        line with the relevant details filled gets the block (a second such
        line is blocked at confirm by
        account_move._tca_validate_mandatory_fields, so this is just a
        defensive fallback, not the enforcement point). None of these detail
        fields are spec-mandatory even when their code is selected — only
        PaymentMeansCode itself is (ibr-049-ae).
        """
        is_credit_note = invoice.move_type in ('out_refund', 'in_refund')
        if is_credit_note:
            return []

        lines = invoice.tca_payment_means_ids
        if not lines:
            flags = (invoice.tca_transaction_type_flags or '00000000').ljust(8, '0')
            deemed_supply = flags[1] == '1'
            return [{'payment_means_code': '1'}] if deemed_supply else []

        base_vals = (super()._get_invoice_payment_means_vals_list(invoice) or [{}])[0]
        card_used = False
        mandate_used = False
        vals_list = []
        for index, line in enumerate(lines):
            entry = {'payment_means_code': line.tca_payment_means_code}
            if index == 0:
                for key in ('payment_due_date', 'instruction_id', 'payment_id_vals'):
                    if base_vals.get(key):
                        entry[key] = base_vals[key]
            if line.tca_payment_means_code == '30' and invoice.partner_bank_id:
                entry['payee_financial_account_vals'] = self._get_financial_account_vals(
                    invoice.partner_bank_id
                )
            if (
                line.tca_payment_means_code in ('54', '55')
                and not card_used
                and (line.tca_card_pan or line.tca_card_holder_name)
            ):
                entry['card_account_vals'] = {
                    'primary_account_number_id': line.tca_card_pan or '',
                    'network_id': 'NA',
                    'holder_name': line.tca_card_holder_name or '',
                }
                card_used = True
            if (
                line.tca_payment_means_code == '49'
                and not mandate_used
                and (line.tca_mandate_id or line.tca_payer_account_id)
            ):
                mandate_vals = {}
                if line.tca_mandate_id:
                    mandate_vals['id'] = line.tca_mandate_id
                if line.tca_payer_account_id:
                    mandate_vals['payer_financial_account_vals'] = {
                        'id': line.tca_payer_account_id
                    }
                entry['payment_mandate_vals'] = mandate_vals
                mandate_used = True
            vals_list.append(entry)
        return vals_list

    # ──────────────────────────────────────────────────────────────────────────
    # DELIVERY VALS — BTAE-22 Incoterms
    # ──────────────────────────────────────────────────────────────────────────

    def _get_delivery_vals_list(self, invoice):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        Adds ActualDeliveryDate (IBT-072), DeliveryTerms/Incoterms (BTAE-22),
        and DeliveryParty TRN (BTAE-23).
        """
        vals_list = super()._get_delivery_vals_list(invoice)
        incoterms = invoice.tca_incoterms or ''
        delivery_date = invoice.tca_delivery_date
        delivery_trn = invoice.tca_delivery_party_trn or ''
        if incoterms or delivery_date or delivery_trn:
            if vals_list:
                entry = vals_list[0]
            else:
                entry = {}
                vals_list = [entry]
            if delivery_date:
                entry['actual_delivery_date'] = delivery_date
            if incoterms:
                entry['delivery_terms_vals'] = {
                    'id': incoterms,
                    'id_attrs': {'schemeID': 'Incoterms'},
                }
            if delivery_trn:
                entry['delivery_party_vals'] = {
                    'party_identification_vals': [{'id': delivery_trn}],
                }
        return vals_list

    # ──────────────────────────────────────────────────────────────────────────
    # ADDITIONAL DOCUMENT REFERENCES — export BTAE-20 as AdditionalDocumentReference
    # ──────────────────────────────────────────────────────────────────────────

    def _get_pricing_exchange_rate_vals_list(self, invoice):
        """
        EXTENDS account.edi.xml.ubl_20.
        Adds PricingExchangeRate when invoice currency != AED (for exports).
        Uses the parent's foreach mechanism — no template xpath needed.
        """
        vals_list = super()._get_pricing_exchange_rate_vals_list(invoice)
        aed = self.env.ref('base.AED', raise_if_not_found=False) or invoice.company_id.currency_id
        if invoice.currency_id and invoice.currency_id != aed and not vals_list:
            rate = self.env['res.currency']._get_conversion_rate(
                invoice.currency_id, aed, invoice.company_id,
                invoice.invoice_date or fields.Date.today(),
            )
            vals_list.append({
                'source_currency_code': invoice.currency_id.name,
                'target_currency_code': aed.name,
                'calculation_rate': round(rate, 6),
            })
        return vals_list

    def _get_additional_document_reference_list(self, invoice):
        """
        EXTENDS account.edi.xml.ubl_20.
        For exports with foreign currency, adds BTAE-20 as AdditionalDocumentReference
        with DocumentTypeCode 'aedtotal-incl-vat'.
        """
        vals_list = super()._get_additional_document_reference_list(invoice)
        aed = self.env.ref('base.AED', raise_if_not_found=False) or invoice.company_id.currency_id
        if invoice.currency_id and invoice.currency_id != aed:
            # Convert total inclusive of VAT to AED
            aed_total = round(invoice.currency_id._convert(
                abs(invoice.amount_total), aed, invoice.company_id,
                invoice.invoice_date or fields.Date.today(),
            ), 2)
            vals_list.append({
                'id': 'aedtotal-incl-vat',
                'document_type_code': 'aedtotal-incl-vat',
                'document_description': str(aed_total),
            })
        return vals_list

    # ──────────────────────────────────────────────────────────────────────────
    # MAIN EXPORT — root-level PINT AE fields
    # ──────────────────────────────────────────────────────────────────────────

    def _export_invoice_vals(self, invoice):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        Overrides customization_id/profile_id and injects all UAE-specific
        root-level fields:
          - CustomizationID → PINT AE value (or self-billing variant)
          - ProfileID        → urn:peppol:bis:billing (or selfbilling)
          - ProfileExecutionID (BTAE-02) — 8-digit flags from tca_transaction_type_flags
          - UUID (BTAE-07)
          - IssueTime (IBT-168)
          - TaxExchangeRate/CalculationRate (BTAE-04) when currency != AED
          - PricingExchangeRate (for exports with foreign currency)
          - BuyerCustomerParty/PartyIdentification (BTAE-01)
          - SellerSupplierParty/PartyIdentification (BTAE-14) for disclosed agent
          - DiscrepancyResponse/ResponseCode (BTAE-03) on credit notes
          - ContractDocumentReference/DocumentDescription (BTAE-05)
          - StatementDocumentReference (BTAE-21) for exports
          - Invoice type code 480/81 from tca_invoice_type_code
          - InvoicePeriod/Description (BTAE-06) via _get_invoice_period_vals_list
        """
        # Stash buyer overrides on env context so per-party builder hooks can
        # apply invoice-level field values to the buyer only (not supplier).
        buyer_partner = invoice.partner_id.commercial_partner_id
        passport_country = invoice.tca_buyer_passport_country_id
        self = self.with_context(
            tca_buyer_partner_id=buyer_partner.id,
            tca_buyer_emirate_override=invoice.tca_buyer_emirate or '',
            tca_buyer_legal_id_type_override=invoice.tca_buyer_legal_id_type or '',
            tca_buyer_trade_license_override=invoice.tca_buyer_trade_license or '',
            tca_buyer_legal_authority_override=invoice.tca_buyer_legal_authority or '',
            tca_buyer_passport_country_code_override=(passport_country.code if passport_country else ''),
        )
        vals = super()._export_invoice_vals(invoice)

        # ── Override CustomizationID & ProfileID ─────────────────────────────
        if invoice.tca_is_self_billing:
            vals['vals']['customization_id'] = PINT_AE_SELFBILLING_CUSTOMIZATION_ID
            vals['vals']['profile_id'] = PINT_AE_SELFBILLING_PROFILE_ID
        else:
            vals['vals']['customization_id'] = PINT_AE_CUSTOMIZATION_ID
            vals['vals']['profile_id'] = PINT_AE_PROFILE_ID

        # ── BTAE-02: ProfileExecutionID ───────────────────────────────────────
        vals['vals']['profile_execution_id'] = self._get_profile_execution_id(invoice)

        # ── BTAE-07: UUID ──────────────────────────────────────────────────────
        vals['vals']['uuid'] = str(_uuid_mod.uuid4())

        # ── IssueTime (IBT-168) ───────────────────────────────────────────────
        if invoice.invoice_date:
            vals['vals']['issue_time'] = fields.Datetime.now().strftime('%H:%M:%S')

        # ── IBT-007: TaxPointDate ─────────────────────────────────────────────
        if invoice.tca_tax_point_date:
            vals['vals']['tax_point_date'] = invoice.tca_tax_point_date

        # ── IBT-019: Buyer accounting reference ───────────────────────────────
        if invoice.tca_buyer_accounting_ref:
            vals['vals']['accounting_cost'] = invoice.tca_buyer_accounting_ref

        # ── IBT-010: Buyer reference ──────────────────────────────────────────
        if invoice.tca_buyer_reference:
            vals['vals']['buyer_reference'] = invoice.tca_buyer_reference

        # ── BTAE-04: Currency exchange rate ────────────────────────────────────
        aed = self.env.ref('base.AED', raise_if_not_found=False) or invoice.company_id.currency_id
        if invoice.currency_id and invoice.currency_id != aed:
            rate = self.env['res.currency']._get_conversion_rate(
                invoice.currency_id,
                aed,
                invoice.company_id,
                invoice.invoice_date or fields.Date.today(),
            )
            # PINT AE rule ibr-002-ae: max 6 decimal places
            vals['vals']['tax_exchange_rate'] = round(rate, 6)
            vals['vals']['tax_exchange_rate_currency_code'] = invoice.currency_id.name
            vals['vals']['tax_exchange_rate_base_currency_code'] = aed.name

        # ── BTAE-01: Buyer internal identification (BuyerCustomerParty) ───────
        buyer = invoice.partner_id.commercial_partner_id
        if buyer.ref:
            vals['vals']['buyer_customer_party_id'] = buyer.ref

        # ── BTAE-14: Principal TRN (SellerSupplierParty) for disclosed agent ──
        if invoice.tca_principal_id:
            vals['vals']['seller_supplier_party_id'] = invoice.tca_principal_id

        # ── BTAE-03: Credit note reason code (DiscrepancyResponse) ────────────
        if (invoice.tca_uncl1001_code or '') in ('381', '81'):
            vals['vals']['btae_03_reason'] = invoice.tca_credit_note_reason or ''

        # ── IBG-03: Preceding invoice reference for credit notes ──────────────
        # PINT AE requires <cac:BillingReference> to point at the original
        # invoice (except for Volume Discount credit notes). Populated from
        # the Odoo reversal link (reversed_entry_id) set by the Credit Note
        # wizard. Parent bis3 only populates this for Netherlands suppliers —
        # we extend it for UAE.
        if invoice.reversed_entry_id:
            vals['vals']['billing_reference_vals'] = {
                'id': invoice.reversed_entry_id.name,
                'issue_date': invoice.reversed_entry_id.invoice_date,
            }

        # ── BTAE-05: Contract value (ContractDocumentReference/DocumentDescription)
        if invoice.tca_contract_value or invoice.tca_contract_reference:
            vals['vals']['btae_05_contract_value'] = invoice.tca_contract_value or ''
            vals['vals']['contract_reference'] = invoice.tca_contract_reference or ''

        # ── IBT-011: Project reference ────────────────────────────────────────
        if invoice.tca_project_reference:
            vals['vals']['project_reference'] = invoice.tca_project_reference

        # ── BTAE-21: Export declaration number (StatementDocumentReference) ────
        if invoice.tca_export_declaration_number:
            vals['vals']['btae_21_export_declaration'] = invoice.tca_export_declaration_number

        # ── IBT-003: Invoice type code — UNCL1001 string emitted in the XML ──
        # Self-billing variants (380_sb, 381_sb) emit the bare 380/381 (parent default);
        # OOS codes (480, 81) are explicitly overridden here.
        uncl_code = invoice.tca_uncl1001_code or ''
        if uncl_code in ('480', '81'):
            vals['vals']['document_type_code'] = int(uncl_code)

        # ── Override template references to use PINT AE templates ─────────────
        vals['PartyType_template'] = 'account_tca_peppol.pint_ae_PartyType'
        vals['InvoiceType_template'] = 'account_tca_peppol.pint_ae_InvoiceType'
        vals['CreditNoteType_template'] = 'account_tca_peppol.pint_ae_CreditNoteType'
        vals['InvoiceLineType_template'] = 'account_tca_peppol.pint_ae_InvoiceLineType'
        vals['CreditNoteLineType_template'] = 'account_tca_peppol.pint_ae_CreditNoteLineType'
        vals['TaxTotalType_template'] = 'account_tca_peppol.pint_ae_TaxTotalType'
        vals['TaxCategoryType_template'] = 'account_tca_peppol.pint_ae_TaxCategoryType'
        vals['DeliveryType_template'] = 'account_tca_peppol.pint_ae_DeliveryType'

        return vals

    # ──────────────────────────────────────────────────────────────────────────
    # CONSTRAINTS — UAE-specific validation before export
    # ──────────────────────────────────────────────────────────────────────────

    def _export_invoice_constraints(self, invoice, vals):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        Adds UAE-specific pre-export validation.

        Also pads payment_means_vals_list around the super() call: our
        _get_invoice_payment_means_vals_list returns [] for credit notes
        (per ibr-191-ae), but bis3's parent constraint check blindly indexes
        payment_means_vals_list[0]. Pad an inert entry so the indexing
        succeeds, then restore the empty list so the XML template still
        emits no PaymentMeans element.
        """
        is_credit_note = invoice.move_type in ('out_refund', 'in_refund')
        # Sentinel object to distinguish "key absent" from "key present with
        # value None" — defensive restore even if the key shape changes upstream.
        _PMM_MISSING = object()
        _pmm_original = vals['vals'].get('payment_means_vals_list', _PMM_MISSING)
        _pmm_pad = False
        if is_credit_note and not _pmm_original:
            vals['vals']['payment_means_vals_list'] = [{'payment_means_code': 0}]
            _pmm_pad = True
        try:
            constraints = super()._export_invoice_constraints(invoice, vals)
        finally:
            if _pmm_pad:
                # Restore exactly what was there (vs. blindly resetting to [])
                # so repeated callers see consistent state.
                if _pmm_original is _PMM_MISSING:
                    vals['vals'].pop('payment_means_vals_list', None)
                else:
                    vals['vals']['payment_means_vals_list'] = _pmm_original

        supplier = invoice.company_id.partner_id.commercial_partner_id
        customer = invoice.partner_id.commercial_partner_id

        # ── Emirate code for AE addresses ─────────────────────────────────────
        # Emirate check — only for seller (our company). Buyer emirate is optional.
        if supplier.country_id and supplier.country_id.code == 'AE':
            emirate = supplier.tca_emirate or (supplier.state_id and supplier.state_id.code) or ''
            valid_emirates = ['AUH', 'DXB', 'SHJ', 'UAQ', 'FUJ', 'AJM', 'RAK']
            if emirate not in valid_emirates:
                constraints['pint_ae_emirate_supplier'] = _(
                    'Your company: "Emirate" must be set to one of: '
                    'AUH (Abu Dhabi), DXB (Dubai), SHJ (Sharjah), UAQ (Umm Al Quwain), '
                    'FUJ (Fujairah), AJM (Ajman), RAK (Ras Al Khaimah). '
                    'Open the company partner record and set the "Emirate" field.'
                )

        # ── Supplier legal registration ID ──────────────────────────────────
        if supplier.peppol_eas == UAE_EAS and not (
            supplier.tca_trade_license or supplier.company_registry or supplier.vat
        ):
            constraints['pint_ae_supplier_legal_id'] = _(
                'Your company is missing a "Trade License / Registration ID". '
                'Open the company partner record and set the Trade License number, '
                'Emirates ID, or VAT number.'
            )

        # ── Supplier legal ID type ───────────────────────────────────────────
        if (
            supplier.peppol_eas == UAE_EAS
            and supplier.country_id
            and supplier.country_id.code == 'AE'
            and (supplier.tca_trade_license or supplier.company_registry)
            and not supplier.tca_legal_id_type
        ):
            constraints['pint_ae_supplier_legal_id_type'] = _(
                'Your company has a Trade License / Registration ID but the '
                '"Legal ID Type" is not set. Open the company partner record and '
                'set it to TL (Trade License), EID (Emirates ID), PAS (Passport), or CD (Cabinet Decision).'
            )

        # ── Supplier authority name (when Trade License) ─────────────────────
        if supplier.tca_legal_id_type == 'TL' and not supplier.tca_legal_authority:
            constraints['pint_ae_supplier_authority'] = _(
                'Your company\'s Legal ID Type is "Trade License" but the '
                '"Issuing Authority" is not set. Open the company partner record and '
                'enter the authority name (e.g. "Department of Economic Development - Dubai").'
            )

        # ── Passport country (seller) ────────────────────────────────────────
        if supplier.tca_legal_id_type == 'PAS' and not supplier.tca_passport_country_id:
            constraints['pint_ae_supplier_passport_country'] = _(
                'Your company\'s Legal ID Type is "Passport" but the '
                '"Passport Issuing Country" is not set. Open the company partner record '
                'and select the country that issued the passport.'
            )

        # Buyer passport country (BTAE-19) — conditional, not checked on seller side.
        # TCA backend validates buyer legal ID completeness.

        # ── Credit note reason (381/81) ──────────────────────────────────────────
        # type_code holds the bare UNCL1001 string (380/381/480/81); self-billing
        # variants are stripped here so the downstream checks don't need _sb logic.
        type_code = invoice.tca_uncl1001_code or ''
        is_credit_note_type = type_code in ('381', '81')
        is_out_of_scope = type_code in ('480', '81')

        if is_credit_note_type and not invoice.tca_credit_note_reason:
            constraints['pint_ae_btae_03'] = _(
                '"Credit Note Reason" is required for all UAE credit notes. '
                'Select a reason in the "Invoice & Buyer" section.'
            )

        # ── Preceding invoice reference (credit notes) ───────────────────────
        if (
            is_credit_note_type
            and invoice.tca_credit_note_reason
            and invoice.tca_credit_note_reason != 'VD'
            and not invoice.reversed_entry_id
        ):
            constraints['pint_ae_btae_ibg03'] = _(
                'This credit note must be linked to the original invoice it corrects. '
                'Use the "Add Credit Note" button from the original invoice, '
                'or set the "Reversal Of" field. '
                '(Not required only for Volume Discount "VD" credit notes.)'
            )

        # ── Transaction type flags format ────────────────────────────────────
        flags = (invoice.tca_transaction_type_flags or '00000000').strip()
        if len(flags) != 8 or not all(c in '01' for c in flags):
            constraints['pint_ae_btae_02'] = _(
                '"Transaction Type Flags" must be exactly 8 digits of 0 or 1. '
                'Example: "00000000" for standard, "00000001" for export. '
                'Current value: "%s". Fix it in the "Transaction Type" section.',
                flags
            )

        # ── Principal TRN (disclosed agent) ──────────────────────────────────
        disclosed_agent_flag = flags[5:6] == '1' if len(flags) == 8 else False
        if disclosed_agent_flag and not invoice.tca_principal_id:
            constraints['pint_ae_btae_14'] = _(
                'The "Disclosed Agent" flag is set in Transaction Type Flags, '
                'but "Principal TRN" is empty. '
                'Enter the Principal\'s Tax Registration Number in the "Transaction Type" section.'
            )

        # ── VAT category validation per invoice type ─────────────────────────
        # 480 (Out of Scope invoice): only E, O, Z allowed
        # 81 (Out of Scope CN): only E, O allowed
        # 380/381: all categories allowed
        if is_out_of_scope:
            allowed = {'E', 'O', 'Z'} if type_code == '480' else {'E', 'O'}
            allowed_str = ', '.join(sorted(allowed))
            for line in invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
                for tax in line.tax_ids:
                    cat = getattr(tax, 'tca_tax_category', None) or ''
                    if not cat:
                        constraints['pint_ae_oos_vat_missing'] = _(
                            'Invoice type %s requires all taxes to have a UAE VAT category. '
                            'Tax "%s" on line "%s" has no category set. '
                            'Go to Accounting → Taxes and set the "UAE VAT Category" field.',
                            type_code, tax.name, line.name or str(line.id),
                        )
                        break
                    if cat not in allowed:
                        constraints['pint_ae_oos_vat'] = _(
                            'Invoice type %s only allows VAT categories: %s. '
                            'Line "%s" uses tax "%s" with category "%s". '
                            'Change the tax or the invoice type.',
                            type_code, allowed_str,
                            line.name or str(line.id), tax.name, cat,
                        )
                        break
                if 'pint_ae_oos_vat' in constraints or 'pint_ae_oos_vat_missing' in constraints:
                    break

        # ── Flag compatibility with type 480 ─────────────────────────────────
        # 480 cannot combine with Deemed Supply (pos 2), Margin Scheme (pos 3),
        # or Summary Invoice (pos 4)
        if type_code == '480' and len(flags) == 8:
            incompatible = []
            if flags[1] == '1':
                incompatible.append('Deemed Supply')
            if flags[2] == '1':
                incompatible.append('Margin Scheme')
            if flags[3] == '1':
                incompatible.append('Summary Invoice')
            if incompatible:
                constraints['pint_ae_480_flags'] = _(
                    'Invoice type 480 (Out of Scope) cannot be combined with: %s. '
                    'Either change the invoice type to 380 or unset those flags.',
                    ', '.join(incompatible),
                )

        # ── BTAE-09: RC description required for reverse charge lines ─────────
        for line in invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
            has_rc_tax = any(
                t.tca_tax_category == 'AE'
                for t in line.tax_ids
                if hasattr(t, 'tca_tax_category')
            )
            if has_rc_tax and not line.tca_rc_description:
                constraints[f'pint_ae_btae_09_{line.id}'] = _(
                    'Line "%s" uses Reverse Charge VAT. '
                    'The "Goods/Services Type" field is required for reverse charge lines. '
                    'Describe the nature of the supply (e.g. "IT consultancy services").',
                    line.name or str(line.id)
                )
                break  # Report once, not for every RC line

        # ── ibr-126-ae: Price BaseQuantity and GrossPrice required ────────────
        for line in invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
            if not line.quantity or line.quantity == 0:
                constraints[f'pint_ae_price_qty_{line.id}'] = _(
                    'Line "%s": "Quantity" cannot be zero.',
                    line.name or str(line.id)
                )
                break

        # ── ibr-185/186-ae: Commodity type dependent fields ────────────────────
        # HS code (IBT-158) is NOT client-side mandatory here for Goods/Both —
        # it's only enforced for Reverse-Charge lines (see ibr-184-ae check in
        # account_move.py). Service accounting code stays mandatory for S/B.
        for line in invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
            ct = line.tca_effective_commodity_type
            if ct == 'S' and not getattr(line, 'tca_service_accounting_code', None):
                constraints[f'pint_ae_sac_{line.id}'] = _(
                    '[ibr-185-ae] Line "%s": Item type is Services (S) — '
                    'Service accounting code (BTAE-17) is mandatory.',
                    line.name or str(line.id)
                )
                break
            if ct == 'B' and not getattr(line, 'tca_service_accounting_code', None):
                constraints[f'pint_ae_both_{line.id}'] = _(
                    '[ibr-186-ae] Line "%s": Item type is Both (B) — '
                    'service accounting code (BTAE-17) is mandatory.',
                    line.name or str(line.id)
                )
                break

        # ── ibr-138-ae: Summary invoice → InvoicePeriod required ──────────────
        if len(flags) == 8 and flags[3] == '1':
            if not invoice.tca_invoice_period_start or not invoice.tca_invoice_period_end:
                constraints['pint_ae_summary_period'] = _(
                    '[ibr-138-ae] Summary Invoice flag is set — '
                    'Invoice Period Start and End dates are required. '
                    'Set them in the "Transaction Type" section.'
                )

        # ── Continuous supply → InvoicePeriod + Contract required ─────────────
        if len(flags) == 8 and flags[4] == '1':
            if not invoice.tca_invoice_period_start or not invoice.tca_invoice_period_end:
                constraints['pint_ae_continuous_period'] = _(
                    'Continuous Supply flag is set — '
                    'Invoice Period Start and End dates are required. '
                    'Set them in the "Transaction Type" section.'
                )
            if not invoice.tca_contract_reference:
                constraints['pint_ae_continuous_contract'] = _(
                    'Continuous Supply flag is set — '
                    '"Contract Reference" is required. '
                    'Set it in the "Transaction Type" section.'
                )

        # ── ibr-160-ae: OTH frequency → invoice note required ─────────────────
        if invoice.tca_billing_frequency == 'OTH' and not invoice.narration:
            constraints['pint_ae_oth_note'] = _(
                '[ibr-160-ae] Billing frequency is "Others" (OTH) — '
                'an Invoice Note (IBT-022) must be provided to describe the frequency.'
            )

        # ── ibr-190-ae: Standard rated (S) → rate must be 5.00 ────────────────
        for line in invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
            for tax in line.tax_ids:
                if hasattr(tax, 'tca_tax_category') and tax.tca_tax_category == 'S':
                    if tax.amount != 5.0:
                        constraints['pint_ae_s_rate'] = _(
                            '[ibr-190-ae] Standard rated (S) VAT must be exactly 5.00%%. '
                            'Tax "%s" has rate %.2f%%.',
                            tax.name, tax.amount
                        )
                    break
            if 'pint_ae_s_rate' in constraints:
                break

        # ══════════════════════════════════════════════════════════════════════
        # STANDARD PINT AE MANDATORY FIELDS
        # Error messages use Odoo UI field names so users know exactly where to fix.
        # ══════════════════════════════════════════════════════════════════════

        # ── Document level ───────────────────────────────────────────────────

        # Note: IBT-001 (invoice number) is NOT checked here because it's '/' until
        # _post() assigns the sequence. This check runs in the wizard (pre-send) instead.

        if not invoice.invoice_date:
            constraints['pint_ae_bt2'] = _(
                '"Invoice Date" is required. Set it in the invoice header.'
            )

        if not invoice.currency_id:
            constraints['pint_ae_bt5'] = _(
                '"Currency" is required. Set it in the invoice header.'
            )

        if not invoice.invoice_date_due:
            constraints['pint_ae_bt9'] = _(
                '"Due Date" is required. Set a due date or payment terms on the invoice.'
            )

        if not invoice.invoice_payment_term_id and not invoice.invoice_date_due:
            constraints['pint_ae_bt81'] = _(
                '"Payment Terms" or "Due Date" is required. '
                'Set either one in the invoice header.'
            )

        # ── Seller (your company) ────────────────────────────────────────────

        if not supplier.name:
            constraints['pint_ae_ibt027'] = _(
                'Your company name is missing. '
                'Go to Settings → Companies and set the company name.'
            )

        if not supplier.peppol_eas or not supplier.peppol_endpoint:
            constraints['pint_ae_ibt034'] = _(
                'Your company\'s Peppol EAS and Endpoint are missing. '
                'Go to the company partner record → "Invoicing" tab → '
                'set "Peppol EAS" to 0235 and "Peppol Endpoint" to your '
                '10-digit Peppol Participant ID or 15-digit TRN.'
            )

        if not supplier.vat and supplier.peppol_eas == UAE_EAS:
            constraints['pint_ae_ibt031'] = _(
                'Your company\'s "Tax ID" (TRN) is missing. '
                'Go to Settings → Companies and set the Tax ID field.'
            )

        if not supplier.street:
            constraints['pint_ae_ibt035'] = _(
                'Your company\'s "Street" address is missing. '
                'Go to Settings → Companies and fill in the street address.'
            )

        if not supplier.city:
            constraints['pint_ae_ibt037'] = _(
                'Your company\'s "City" is missing. '
                'Go to Settings → Companies and set the city.'
            )

        if not supplier.country_id:
            constraints['pint_ae_ibt040'] = _(
                'Your company\'s "Country" is missing. '
                'Go to Settings → Companies and set the country.'
            )

        # ── Buyer (customer) ─────────────────────────────────────────────────

        if not customer.name:
            constraints['pint_ae_ibt044'] = _(
                'The customer name is missing. Set the name on the customer record.'
            )

        # ── Buyer Participant ID — mandatory, must be 10 digits for UAE ──
        buyer_pid = invoice.tca_buyer_participant_id or ''
        if not buyer_pid:
            constraints['pint_ae_buyer_pid'] = _(
                '"Buyer Participant ID" is required. '
                'Enter the buyer\'s 10-digit Peppol Participant ID, or set the '
                'customer\'s Country to UAE and Peppol Endpoint — it will auto-populate.'
            )
        elif buyer_pid != '1XXXXXXXXX':
            # Not the legacy placeholder — validate strict 10-digit format.
            # PINT AE predefined endpoints (9900000097/98/99) are 10 digits, pass naturally.
            if not buyer_pid.isdigit() or len(buyer_pid) != 10:
                constraints['pint_ae_buyer_pid_format'] = _(
                    '"Buyer Participant ID" must be exactly 10 digits. '
                    'The 15-digit TRN belongs in the "Tax ID" field, not here. '
                    'Current value: "%s".',
                    buyer_pid
                )

        # Buyer street (IBT-050) and city (IBT-052) are optional — not checked.
        # TCA backend / SMP lookup provides missing buyer address info.

        if not customer.country_id:
            constraints['pint_ae_ibt055'] = _(
                'Customer "%s" is missing a country. '
                'Open the customer record and set the "Country" field.',
                customer.name
            )

        # ── Invoice lines ────────────────────────────────────────────────────

        product_lines = invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product')

        if not product_lines:
            constraints['pint_ae_no_lines'] = _(
                'The invoice has no lines. Add at least one product or service line.'
            )

        for line in product_lines:
            line_label = line.name or (line.product_id and line.product_id.name) or f'Line {line.sequence}'

            if not line.quantity:
                constraints[f'pint_ae_ibt129_{line.id}'] = _(
                    'Line "%s": "Quantity" is required and cannot be zero.',
                    line_label
                )
                break

            if not line.product_uom_id:
                constraints[f'pint_ae_ibt130_{line.id}'] = _(
                    'Line "%s": "Unit of Measure" is required. Select a UoM for this line.',
                    line_label
                )
                break

            if not line.name and not (line.product_id and line.product_id.name):
                constraints[f'pint_ae_ibt153_{line.id}'] = _(
                    'Line "%s": A "Description" or product name is required.',
                    line_label
                )
                break

            if not line.tax_ids:
                constraints[f'pint_ae_ibt151_{line.id}'] = _(
                    'Line "%s": At least one "Tax" must be applied. '
                    'Select a VAT rate (e.g. 5%% Standard, 0%% Zero-Rated, or Exempt).',
                    line_label
                )
                break

            # Defensive guard: tca_effective_commodity_type is always set by
            # its compute (falls back to 'S'); this branch is effectively
            # unreachable in practice but retained for clarity / future-proofing.
            if not line.tca_effective_commodity_type:
                constraints[f'pint_ae_btae13_{line.id}'] = _(
                    'Line "%s": "Commodity Type" is required. '
                    'Set it to Goods (G) or Services (S) in the line details.',
                    line_label
                )
                break

        return constraints

    # ──────────────────────────────────────────────────────────────────────────
    # IMPORT — parse inbound PINT AE XML and populate TCA fields
    # ──────────────────────────────────────────────────────────────────────────

    # Valid selection keys for fields that must match exactly
    _CREDIT_NOTE_REASON_KEYS = {
        'DL8.61.1.A', 'DL8.61.1.B', 'DL8.61.1.C',
        'DL8.61.1.D', 'DL8.61.1.E', 'VD',
    }
    _BILLING_FREQ_KEYS = {
        'DLY', 'WKY', 'Q15', 'MTH', 'Q45', 'Q60', 'QTR', 'YRL', 'HYR', 'OTH',
    }
    _INVOICE_TYPE_KEYS = {'380', '381', '480', '81'}

    def _import_fill_invoice_form(self, invoice, tree, qty_factor):
        """
        EXTENDS account.edi.xml.ubl_20.
        After the base UBL importer fills standard fields, read PINT AE-specific
        elements and populate TCA fields on the invoice.
        """
        logs = super()._import_fill_invoice_form(invoice, tree, qty_factor)

        # ── BTAE-02: ProfileExecutionID → transaction type flags ──────────
        node = tree.find('./{*}ProfileExecutionID')
        if node is not None and node.text and len(node.text.strip()) == 8:
            invoice.tca_transaction_type_flags = node.text.strip()

        # ── BTAE-07: UUID — NOT set here. For inbound invoices,
        # _tca_import_inbound_invoice sets tca_invoice_uuid from the TCA
        # platform ID (needed for status polling). The XML UUID (BTAE-07)
        # is preserved in the attached XML file.

        # ── IBT-003: InvoiceTypeCode / CreditNoteTypeCode → type code ────
        # Detect self-billing variant from CustomizationID/ProfileID — the
        # XML still carries 380/381 in the type code element but the profile
        # tells us it's self-billed (UC4/UC5).
        is_selfbilling = False
        for tag in ('CustomizationID', 'ProfileID'):
            n = tree.find(f'./{{*}}{tag}')
            if n is not None and n.text and 'selfbilling' in n.text.lower():
                is_selfbilling = True
                break

        node = tree.find('./{*}InvoiceTypeCode')
        if node is None:
            node = tree.find('./{*}CreditNoteTypeCode')
        if node is not None and node.text:
            val = node.text.strip()
            if val in self._INVOICE_TYPE_KEYS:
                # Self-billing applies only to the in-scope codes (380/381).
                # tca_invoice_type_code's selection stores self-billing as
                # bare '389'/'261', not a '_sb' suffix — see its help text.
                selfbilling_map = {'380': '389', '381': '261'}
                if is_selfbilling and val in selfbilling_map:
                    invoice.tca_invoice_type_code = selfbilling_map[val]
                else:
                    invoice.tca_invoice_type_code = val
            else:
                _logger.warning(
                    'PINT AE import: unrecognised invoice type code "%s" on %s',
                    val, invoice.ref or invoice.name,
                )

        # ── BTAE-03: DiscrepancyResponse/ResponseCode → credit note reason
        node = tree.find('./{*}DiscrepancyResponse/{*}ResponseCode')
        if node is not None and node.text:
            val = node.text.strip()
            if val in self._CREDIT_NOTE_REASON_KEYS:
                invoice.tca_credit_note_reason = val
            else:
                logs.append(_('PINT AE: unrecognised credit note reason code "%s".', val))

        # ── IBT-010: BuyerReference ───────────────────────────────────────
        node = tree.find('./{*}BuyerReference')
        if node is not None and node.text:
            invoice.tca_buyer_reference = node.text.strip()

        # ── IBT-019: AccountingCost → buyer accounting ref ────────────────
        node = tree.find('./{*}AccountingCost')
        if node is not None and node.text:
            invoice.tca_buyer_accounting_ref = node.text.strip()

        # ── IBT-007: TaxPointDate ─────────────────────────────────────────
        node = tree.find('./{*}TaxPointDate')
        if node is not None and node.text:
            invoice.tca_tax_point_date = node.text.strip()

        # ── IBT-012 / BTAE-05: ContractDocumentReference ─────────────────
        contract_node = tree.find('./{*}ContractDocumentReference')
        if contract_node is not None:
            cid = contract_node.find('./{*}ID')
            if cid is not None and cid.text:
                invoice.tca_contract_reference = cid.text.strip()
            cdesc = contract_node.find('./{*}DocumentDescription')
            if cdesc is not None and cdesc.text:
                invoice.tca_contract_value = cdesc.text.strip()

        # ── IBT-011: ProjectReference ─────────────────────────────────────
        node = tree.find('./{*}ProjectReference/{*}ID')
        if node is not None and node.text:
            invoice.tca_project_reference = node.text.strip()

        # ── BTAE-06 / IBT-073 / IBT-074: InvoicePeriod ───────────────────
        period_node = tree.find('./{*}InvoicePeriod')
        if period_node is not None:
            # BTAE-06: billing frequency — may be in DescriptionCode or Description
            desc_code = period_node.find('./{*}DescriptionCode')
            if desc_code is None:
                desc_code = period_node.find('./{*}Description')
            if desc_code is not None and desc_code.text:
                val = desc_code.text.strip()
                if val in self._BILLING_FREQ_KEYS:
                    invoice.tca_billing_frequency = val
            start = period_node.find('./{*}StartDate')
            if start is not None and start.text:
                invoice.tca_invoice_period_start = start.text.strip()
            end = period_node.find('./{*}EndDate')
            if end is not None and end.text:
                invoice.tca_invoice_period_end = end.text.strip()

        # ── BTAE-21: StatementDocumentReference → export declaration ──────
        node = tree.find('./{*}StatementDocumentReference/{*}ID')
        if node is not None and node.text:
            invoice.tca_export_declaration_number = node.text.strip()

        # ── BTAE-22: Delivery/DeliveryTerms → incoterms ──────────────────
        node = tree.find('.//{*}Delivery/{*}DeliveryTerms/{*}ID')
        if node is not None and node.text:
            invoice.tca_incoterms = node.text.strip()

        # ── IBT-072: Delivery/ActualDeliveryDate → tca_delivery_date ─────
        node = tree.find('.//{*}Delivery/{*}ActualDeliveryDate')
        if node is not None and node.text:
            invoice.tca_delivery_date = node.text.strip()

        # ── IBT-049: Buyer EndpointID → tca_buyer_participant_id ──────────
        node = tree.find('.//{*}AccountingCustomerParty//{*}EndpointID')
        if node is not None and node.text:
            invoice.tca_buyer_participant_id = node.text.strip()

        # ── BTAE-14: Principal TRN (SellerSupplierParty) ──────────────────
        node = tree.find('.//{*}SellerSupplierParty//{*}PartyIdentification/{*}ID')
        if node is not None and node.text:
            invoice.tca_principal_id = node.text.strip()

        return logs

    def _import_fill_invoice_line_form_batched(self, trees, invoice_lines, qty_factor):
        """
        EXTENDS account.edi.xml.ubl_20.
        After the base importer fills standard line fields, read PINT AE-specific
        per-line elements and populate TCA fields.
        """
        logs = super()._import_fill_invoice_line_form_batched(trees, invoice_lines, qty_factor)

        for tree, line in zip(trees, invoice_lines):
            # ── BTAE-13: CommodityCode → commodity type (G/S/B) ──────────
            node = tree.find('.//{*}CommodityClassification/{*}CommodityCode')
            if node is not None and node.text and node.text.strip() in ('G', 'S', 'B'):
                line.tca_commodity_type = node.text.strip()

            # ── BTAE-09: NatureCode → RC description ─────────────────────
            node = tree.find('.//{*}CommodityClassification/{*}NatureCode')
            if node is not None and node.text:
                line.tca_rc_description = node.text.strip()

            # ── IBT-158 / BTAE-17: ItemClassificationCode (HS + SAC) ─────
            for cls_node in tree.findall('.//{*}CommodityClassification/{*}ItemClassificationCode'):
                list_id = cls_node.attrib.get('listID', '')
                if cls_node.text:
                    if list_id == 'HS':
                        line.tca_hs_code = cls_node.text.strip()
                    elif list_id == 'SAC':
                        line.tca_service_accounting_code = cls_node.text.strip()

            # ── IBT-155: SellersItemIdentification (store, not just lookup)
            node = tree.find('.//{*}Item/{*}SellersItemIdentification/{*}ID')
            if node is not None and node.text:
                line.tca_seller_item_id = node.text.strip()

            # ── IBT-156: BuyersItemIdentification ─────────────────────────
            node = tree.find('.//{*}Item/{*}BuyersItemIdentification/{*}ID')
            if node is not None and node.text:
                line.tca_buyer_item_id = node.text.strip()

            # ── IBT-157: StandardItemIdentification ───────────────────────
            node = tree.find('.//{*}Item/{*}StandardItemIdentification/{*}ID')
            if node is not None and node.text:
                line.tca_standard_item_id = node.text.strip()
                scheme = node.attrib.get('schemeID', '')
                if scheme:
                    line.tca_standard_item_scheme = scheme

            # ── IBT-132: OrderLineReference ───────────────────────────────
            node = tree.find('./{*}OrderLineReference/{*}LineID')
            if node is not None and node.text:
                line.tca_order_line_ref = node.text.strip()

            # ── IBT-127: Line Note ────────────────────────────────────────
            node = tree.find('./{*}Note')
            if node is not None and node.text:
                line.tca_line_note = node.text.strip()

            # ── BTAE-24: LotNumber ────────────────────────────────────────
            node = tree.find('.//{*}ItemInstance/{*}LotIdentification/{*}LotNumberID')
            if node is not None and node.text:
                line.tca_lot_number = node.text.strip()

            # ── IBT-134/135: Line InvoicePeriod ──────────────────────────
            period = tree.find('./{*}InvoicePeriod')
            if period is not None:
                start = period.find('./{*}StartDate')
                if start is not None and start.text:
                    line.tca_line_period_start = start.text.strip()
                end = period.find('./{*}EndDate')
                if end is not None and end.text:
                    line.tca_line_period_end = end.text.strip()

        return logs

    # ──────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    def _is_reverse_charge_tax(self, tax):
        """Return True if the tax is a UAE reverse-charge (AE category) tax."""
        if hasattr(tax, 'tca_tax_category'):
            return tax.tca_tax_category == 'AE'
        return 'reverse' in (tax.name or '').lower()
