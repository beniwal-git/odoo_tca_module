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

import copy
import logging
from uuid import uuid4

from odoo import _, fields, models
from odoo.addons.account_edi_ubl_cii.models.account_edi_common import FloatFmt
from odoo.addons.account_tca_peppol.constants import (
    PINT_AE_CUSTOMIZATION_ID,
    PINT_AE_PROFILE_ID,
    PINT_AE_SELFBILLING_CUSTOMIZATION_ID,
    PINT_AE_SELFBILLING_PROFILE_ID,
)

_logger = logging.getLogger(__name__)

# UAE VAT categories used by the mandate — only these six are permitted.
UAE_VAT_CATEGORIES = {
    'S':  5.0,    # Standard Rate 5%
    'E':  0.0,    # Exempt from tax
    'O':  None,   # Services outside scope / Not subject to VAT
    'AE': 5.0,    # VAT Reverse Charge (VAT accounted by buyer)
    'Z':  0.0,    # Zero Rated
    'N':  5.0,    # Standard Rate Additional VAT (extra base not in document totals)
}


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
    # DOCUMENT TEMPLATE — extend the UBL 2.1 templates with PINT AE nodes
    # ──────────────────────────────────────────────────────────────────────────
    # dict_to_xml strictly validates the rendered node tree against the
    # document template: a child tag absent from the template raises
    # ValueError. PINT AE emits UBL elements that Odoo's stock templates omit,
    # so we extend a deep copy of the template with those nodes — placed in
    # UBL 2.1 schema order, since template key order drives XML element order.

    @staticmethod
    def _tca_tmpl_insert_after(node, after_key, new_key, new_val):
        """Return a copy of dict `node` with `new_key` inserted right after
        `after_key` (appended if `after_key` is absent / `new_key` present)."""
        if new_key in node:
            return node
        rebuilt = {}
        for key, value in node.items():
            rebuilt[key] = value
            if key == after_key:
                rebuilt[new_key] = new_val
        if new_key not in rebuilt:
            rebuilt[new_key] = new_val
        return rebuilt

    def _get_document_template(self, vals):
        # OVERRIDE account.edi.xml.ubl_20 — see section note above.
        template = copy.deepcopy(super()._get_document_template(vals))
        line_key = ('cac:CreditNoteLine' if vals['document_type'] == 'credit_note'
                    else 'cac:InvoiceLine')

        # Root: BTAE-21 StatementDocumentReference (after DespatchDocumentReference).
        template = self._tca_tmpl_insert_after(
            template, 'cac:DespatchDocumentReference',
            'cac:StatementDocumentReference',
            copy.deepcopy(template.get('cac:DespatchDocumentReference') or {'cbc:ID': {}}),
        )

        # Party legal entity: IBT-033 CompanyLegalForm (after CompanyID).
        for party_key in ('cac:AccountingSupplierParty',
                          'cac:AccountingCustomerParty',
                          'cac:SellerSupplierParty'):
            party = (template.get(party_key) or {}).get('cac:Party')
            if party and 'cac:PartyLegalEntity' in party:
                party['cac:PartyLegalEntity'] = self._tca_tmpl_insert_after(
                    party['cac:PartyLegalEntity'],
                    'cbc:CompanyID', 'cbc:CompanyLegalForm', {})

        # Invoice / credit-note line.
        line_tmpl = template.get(line_key) or {}
        item_tmpl = line_tmpl.get('cac:Item')
        if item_tmpl is not None:
            # BTAE-09/13: CommodityClassification NatureCode + CommodityCode.
            item_tmpl['cac:CommodityClassification'] = {
                'cbc:NatureCode': {},
                'cbc:CommodityCode': {},
                'cbc:ItemClassificationCode': {},
            }
            # BTAE-24: ItemInstance / LotIdentification.
            item_tmpl['cac:ItemInstance'] = {
                'cac:LotIdentification': {'cbc:LotNumberID': {}},
            }
        # BTAE-08: ItemPriceExtension carries a per-line TaxTotal.
        if 'cac:ItemPriceExtension' in line_tmpl:
            line_tmpl['cac:ItemPriceExtension'] = {
                'cbc:Amount': {},
                'cac:TaxTotal': {'cbc:TaxAmount': {}},
            }

        # IBT-200: TaxIncludedIndicator on the document TaxTotal.
        if 'cac:TaxTotal' in template:
            template['cac:TaxTotal'] = self._tca_tmpl_insert_after(
                template['cac:TaxTotal'], 'cbc:RoundingAmount',
                'cbc:TaxIncludedIndicator', {})

        # BTAE-22: DeliveryTerms on cac:Delivery.
        if 'cac:Delivery' in template:
            template['cac:Delivery'] = self._tca_tmpl_insert_after(
                template['cac:Delivery'], 'cac:DeliveryParty',
                'cac:DeliveryTerms', {'cbc:ID': {}})
        return template

    # ──────────────────────────────────────────────────────────────────────────
    # CUSTOMIZATION / PROFILE IDs
    # ──────────────────────────────────────────────────────────────────────────

    def _get_customization_id(self, process_type='billing'):
        # OVERRIDE account.edi.xml.ubl_bis3 — return the PINT AE CIUS identifier
        # (UAE annex) instead of the EN16931 / Peppol-BIS3 one. Used both for
        # the CustomizationID node and by account_peppol's endpoint validator
        # (account_peppol/models/res_partner.py).
        if process_type == 'selfbilling':
            return PINT_AE_SELFBILLING_CUSTOMIZATION_ID
        return PINT_AE_CUSTOMIZATION_ID

    def _tca_process_type(self, invoice):
        """'selfbilling' or 'billing' for this invoice — driven by the TCA
        self-billing flag, not Odoo's journal-level ``is_self_billing``."""
        return 'selfbilling' if invoice.tca_is_self_billing else 'billing'

    def _ubl_add_customization_id_node(self, vals):
        # EXTENDS account.edi.xml.ubl_bis3 — swap in the PINT AE CustomizationID.
        super()._ubl_add_customization_id_node(vals)
        process_type = self._tca_process_type(vals['invoice'])
        vals['document_node']['cbc:CustomizationID']['_text'] = \
            self._get_customization_id(process_type)

    def _ubl_add_profile_id_node(self, vals):
        # EXTENDS account.edi.xml.ubl_bis3 — PINT AE uses urn:peppol:bis:billing
        # (or :selfbilling), not the BIS3 poacc profile id.
        super()._ubl_add_profile_id_node(vals)
        process_type = self._tca_process_type(vals['invoice'])
        vals['document_node']['cbc:ProfileID']['_text'] = (
            PINT_AE_SELFBILLING_PROFILE_ID if process_type == 'selfbilling'
            else PINT_AE_PROFILE_ID
        )

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

        # F2-9 / F1-3: auto-detect export — if buyer country is not AE, the
        # Exports bit (position 8, index 7) must be 1 per PINT AE spec.
        # Overrides any user-set value for that bit (it's factual, not a choice).
        buyer = invoice.partner_id.commercial_partner_id
        if buyer.country_id and buyer.country_id.code != 'AE':
            flags = flags[:7] + '1'

        return flags

    # ──────────────────────────────────────────────────────────────────────────
    # HEADER NODES — BTAE-02/03/04/05/06/07, IBT-003/007/010/011/019/168, IBG-03
    # ──────────────────────────────────────────────────────────────────────────

    def _add_invoice_header_nodes(self, document_node, vals):
        # EXTENDS account.edi.xml.ubl_bis3 — inject UAE root-level fields.
        # dict_to_xml orders nodes via the UBL 2.1 Invoice/CreditNote templates,
        # so we only set values here (except StatementDocumentReference — see
        # below).
        super()._add_invoice_header_nodes(document_node, vals)
        invoice = vals['invoice']

        # BTAE-02: ProfileExecutionID — 8-flag transaction-type string.
        document_node['cbc:ProfileExecutionID'] = {
            '_text': self._get_profile_execution_id(invoice),
        }

        # BTAE-07: per-document UUID (UUID4).
        document_node['cbc:UUID'] = {'_text': str(uuid4())}

        # IBT-168: IssueTime.
        if invoice.invoice_date:
            document_node['cbc:IssueTime'] = {
                '_text': fields.Datetime.now().strftime('%H:%M:%S'),
            }

        # IBT-007: TaxPointDate.
        if invoice.tca_tax_point_date:
            document_node['cbc:TaxPointDate'] = {'_text': invoice.tca_tax_point_date}

        # IBT-019: buyer accounting reference.
        if invoice.tca_buyer_accounting_ref:
            document_node['cbc:AccountingCost'] = {
                '_text': invoice.tca_buyer_accounting_ref,
            }

        # BTAE-03: credit-note reason (DiscrepancyResponse/ResponseCode).
        if vals['document_type'] == 'credit_note' and invoice.tca_credit_note_reason:
            document_node['cac:DiscrepancyResponse'] = {
                'cbc:ResponseCode': {'_text': invoice.tca_credit_note_reason},
            }

        # BTAE-05: contract reference + value (ContractDocumentReference).
        if invoice.tca_contract_reference or invoice.tca_contract_value:
            document_node['cac:ContractDocumentReference'] = {
                'cbc:ID': {'_text': invoice.tca_contract_reference or invoice.name},
                'cbc:DocumentDescription': {
                    '_text': invoice.tca_contract_value or None,
                },
            }

        # IBT-011: project reference.
        if invoice.tca_project_reference:
            document_node['cac:ProjectReference'] = {
                'cbc:ID': {'_text': invoice.tca_project_reference},
            }

        # BTAE-21: export declaration number (StatementDocumentReference).
        # NOT a key in the UBL 2.1 Invoice template — dict_to_xml appends it
        # after the templated keys. Flagged for live-test review (B2 risk R-B2-1).
        if invoice.tca_export_declaration_number:
            document_node['cac:StatementDocumentReference'] = {
                'cbc:ID': {'_text': invoice.tca_export_declaration_number},
            }

    def _ubl_add_invoice_type_code_node(self, vals):
        # EXTENDS account.edi.xml.ubl_bis3 — IBT-003: emit the UNCL1001 code
        # from tca_uncl1001_code (380 standard / 480 out-of-scope).
        super()._ubl_add_invoice_type_code_node(vals)
        if vals['document_type'] != 'invoice':
            return
        code = vals['invoice'].tca_uncl1001_code or '380'
        vals['document_node']['cbc:InvoiceTypeCode']['_text'] = (
            int(code) if code.isdigit() else code
        )

    def _ubl_add_credit_note_type_code_node(self, vals):
        # EXTENDS account.edi.xml.ubl_bis3 — IBT-003: 381 standard / 81 OOS.
        super()._ubl_add_credit_note_type_code_node(vals)
        if vals['document_type'] != 'credit_note':
            return
        code = vals['invoice'].tca_uncl1001_code or '381'
        vals['document_node']['cbc:CreditNoteTypeCode']['_text'] = (
            int(code) if code.isdigit() else code
        )

    def _ubl_add_buyer_reference_node(self, vals):
        # EXTENDS account.edi.xml.ubl_bis3 — IBT-010 from the TCA field.
        super()._ubl_add_buyer_reference_node(vals)
        invoice = vals.get('invoice')
        if invoice and invoice.tca_buyer_reference:
            vals['document_node']['cbc:BuyerReference']['_text'] = invoice.tca_buyer_reference

    def _ubl_add_invoice_period_nodes(self, vals):
        # EXTENDS account.edi.xml.ubl — BTAE-06 billing frequency +
        # IBT-073/074 invoice-period dates.
        super()._ubl_add_invoice_period_nodes(vals)
        invoice = vals.get('invoice')
        if not invoice:
            return
        period = {}
        if invoice.tca_invoice_period_start:
            period['cbc:StartDate'] = {'_text': invoice.tca_invoice_period_start}
        if invoice.tca_invoice_period_end:
            period['cbc:EndDate'] = {'_text': invoice.tca_invoice_period_end}
        if invoice.tca_billing_frequency:
            period['cbc:DescriptionCode'] = {'_text': invoice.tca_billing_frequency}
        if period:
            vals['document_node']['cac:InvoicePeriod'] = period

    def _ubl_add_billing_reference_nodes(self, vals):
        # EXTENDS account.edi.xml.ubl_bis3 — IBG-03: preceding invoice
        # reference for credit notes, from the Odoo reversal link.
        super()._ubl_add_billing_reference_nodes(vals)
        invoice = vals.get('invoice')
        if invoice and invoice.reversed_entry_id:
            vals['document_node']['cac:BillingReference'].append({
                'cac:InvoiceDocumentReference': {
                    'cbc:ID': {'_text': invoice.reversed_entry_id.name},
                    'cbc:IssueDate': {
                        '_text': invoice.reversed_entry_id.invoice_date,
                    },
                },
            })

    def _add_invoice_exchange_rate_nodes(self, document_node, vals):
        # OVERRIDE account.edi.xml.ubl_20 (no-op parent) — BTAE-04: when the
        # invoice currency is not AED, emit TaxExchangeRate/CalculationRate
        # (max 6 dp per ibr-002-ae).
        super()._add_invoice_exchange_rate_nodes(document_node, vals)
        invoice = vals['invoice']
        aed = self.env.ref('base.AED', raise_if_not_found=False) or invoice.company_id.currency_id
        if invoice.currency_id and invoice.currency_id != aed:
            rate = self.env['res.currency']._get_conversion_rate(
                invoice.currency_id, aed, invoice.company_id,
                invoice.invoice_date or fields.Date.today(),
            )
            document_node['cac:TaxExchangeRate'] = {
                'cbc:SourceCurrencyCode': {'_text': invoice.currency_id.name},
                'cbc:TargetCurrencyCode': {'_text': aed.name},
                'cbc:CalculationRate': {'_text': round(rate, 6)},
            }

    # ──────────────────────────────────────────────────────────────────────────
    # F2-2: VAT CATEGORY / EXEMPTION — UAE-specific overrides
    # ──────────────────────────────────────────────────────────────────────────

    def _get_tax_category_code(self, customer, supplier, tax):
        # EXTENDS account.edi.common — prefer the UAE VAT category
        # (S / E / O / AE / Z / N) set on the tax over Odoo's EU-centric default.
        if tax and tax.tca_tax_category:
            return tax.tca_tax_category
        return super()._get_tax_category_code(customer, supplier, tax)

    def _get_tax_exemption_reason(self, customer, supplier, tax):
        # EXTENDS account.edi.common — IBT-120/121 from the UAE-specific
        # exemption fields when present.
        result = super()._get_tax_exemption_reason(customer, supplier, tax)
        if tax:
            if tax.tca_exemption_reason_code:
                result['tax_exemption_reason_code'] = tax.tca_exemption_reason_code
            if tax.tca_exemption_reason:
                result['tax_exemption_reason'] = tax.tca_exemption_reason
        return result

    def _ubl_default_tax_category_grouping_key(self, base_line, tax_data, vals, currency):
        # EXTENDS account.edi.xml.ubl_bis3 — aligned-ibrp-o-05: an
        # "Out of scope" (O) VAT category MUST NOT carry a rate (Percent).
        key = super()._ubl_default_tax_category_grouping_key(base_line, tax_data, vals, currency)
        if key and key.get('tax_category_code') == 'O':
            key['percent'] = None
        return key

    def _ubl_get_tax_total_node(self, vals, tax_total):
        # EXTENDS account.edi.xml.ubl — IBT-200: PINT AE B2B pricing is always
        # VAT-exclusive, so the document TaxTotal carries TaxIncludedIndicator
        # = false.
        node = super()._ubl_get_tax_total_node(vals, tax_total)
        node['cbc:TaxIncludedIndicator'] = {'_text': 'false'}
        return node

    # ──────────────────────────────────────────────────────────────────────────
    # DELIVERY, PAYMENT MEANS, SELLER SUPPLIER PARTY
    # ──────────────────────────────────────────────────────────────────────────

    def _add_invoice_delivery_nodes(self, document_node, vals):
        # EXTENDS account.edi.xml.ubl_bis3 — IBT-072 ActualDeliveryDate,
        # BTAE-22 DeliveryTerms (incoterms), BTAE-23 DeliveryParty TRN.
        super()._add_invoice_delivery_nodes(document_node, vals)
        invoice = vals['invoice']
        delivery = document_node.get('cac:Delivery')
        if not isinstance(delivery, dict):
            return
        if invoice.tca_delivery_date:
            delivery['cbc:ActualDeliveryDate'] = {'_text': invoice.tca_delivery_date}
        if invoice.tca_incoterms:
            delivery['cac:DeliveryTerms'] = {
                'cbc:ID': {'_text': invoice.tca_incoterms, 'schemeID': 'Incoterms'},
            }
        if invoice.tca_delivery_party_trn:
            party = delivery.setdefault('cac:DeliveryParty', {})
            ids = party.setdefault('cac:PartyIdentification', [])
            if isinstance(ids, list):
                ids.append({'cbc:ID': {'_text': invoice.tca_delivery_party_trn}})

    def _add_invoice_payment_means_nodes(self, document_node, vals):
        # EXTENDS account.edi.xml.ubl_bis3 — ibr-191-ae: a PINT AE credit note
        # (type code 381/81/261) or a Deemed-Supply invoice MUST NOT carry a
        # PaymentMeans element. Odoo 19's account.edi.xml.ubl_21._get_invoice_node
        # adds PaymentMeans to credit notes too (UBL 2.1 permits it), so it must
        # be actively suppressed here for both cases — an empty list renders no
        # node.
        invoice = vals['invoice']
        flags = (invoice.tca_transaction_type_flags or '00000000').ljust(8, '0')
        is_credit_note = invoice.move_type in ('out_refund', 'in_refund')
        if is_credit_note or flags[1] == '1':
            document_node['cac:PaymentMeans'] = []
            return
        super()._add_invoice_payment_means_nodes(document_node, vals)

    def _add_invoice_seller_supplier_party_nodes(self, document_node, vals):
        # EXTENDS account.edi.xml.ubl_20 — BTAE-14: disclosed-agent principal
        # TRN as SellerSupplierParty/Party/PartyIdentification.
        super()._add_invoice_seller_supplier_party_nodes(document_node, vals)
        principal = vals['invoice'].tca_principal_id
        if principal:
            document_node['cac:SellerSupplierParty'] = {
                'cac:Party': {
                    'cac:PartyIdentification': [
                        {'cbc:ID': {'_text': principal}},
                    ],
                },
            }

    # ──────────────────────────────────────────────────────────────────────────
    # PARTY VALS — UAE-specific overrides
    # ──────────────────────────────────────────────────────────────────────────

    def _tca_is_buyer_party(self, vals, partner):
        """True when `partner` is the invoice's customer (buyer) — used to
        decide whether invoice-level buyer overrides apply to this party."""
        invoice = vals.get('invoice')
        return bool(invoice) and partner.commercial_partner_id == invoice.commercial_partner_id

    def _tca_resolve_legal_id(self, vals, partner):
        """Resolve (trade_license, legal_id_type, legal_authority, passport_code)
        for a party. Invoice-level buyer overrides win over the partner record;
        the supplier always uses the partner record."""
        commercial = partner.commercial_partner_id
        invoice = vals.get('invoice')
        is_buyer = self._tca_is_buyer_party(vals, partner)

        if is_buyer and invoice.tca_buyer_trade_license:
            trade_license = invoice.tca_buyer_trade_license
        else:
            trade_license = (
                commercial.tca_trade_license or commercial.company_registry
                or commercial.vat or ''
            )
        if is_buyer and invoice.tca_buyer_legal_id_type:
            legal_id_type = invoice.tca_buyer_legal_id_type
        else:
            legal_id_type = commercial.tca_legal_id_type
        if is_buyer and invoice.tca_buyer_legal_authority:
            legal_authority = invoice.tca_buyer_legal_authority
        else:
            legal_authority = commercial.tca_legal_authority
        if is_buyer and invoice.tca_buyer_passport_country_id:
            passport_code = invoice.tca_buyer_passport_country_id.code
        else:
            passport_code = (
                commercial.tca_passport_country_id.code
                if commercial.tca_passport_country_id else ''
            )
        return trade_license, legal_id_type, legal_authority, passport_code

    def _ubl_get_partner_address_node(self, vals, partner):
        # EXTENDS account.edi.xml.ubl_bis3 — ibr-128-ae: CountrySubentity must
        # be a UAE emirate code (AUH/DXB/SHJ/UAQ/FUJ/AJM/RAK) for AE addresses.
        node = super()._ubl_get_partner_address_node(vals, partner)
        if partner._tca_is_uae_party():
            emirate = ''
            if self._tca_is_buyer_party(vals, partner):
                emirate = vals['invoice'].tca_buyer_emirate or ''
            if not emirate:
                emirate = partner._tca_emirate()
            node['cbc:CountrySubentity'] = {'_text': emirate}
        return node

    def _ubl_add_party_tax_scheme_nodes(self, vals):
        # EXTENDS account.edi.xml.ubl_bis3 — for a UAE party emit exactly one
        # PartyTaxScheme: CompanyID = full 15-char TRN, TaxScheme/ID = 'VAT'
        # (ibr-132-ae / ibr-133-ae / ibr-179-ae). TRN from vat, else
        # peppol_endpoint.
        #
        # Gate on country code (AE), NOT on peppol_eas == '0235' — Odoo 19
        # auto-computes peppol_eas='0235' for every UAE partner, but a partner
        # may have peppol_eas='0235' set on a non-AE record (legacy / data
        # entry slip). Aligning with _ubl_add_party_legal_entity_nodes and
        # _ubl_get_partner_address_node — single source of truth: country.
        super()._ubl_add_party_tax_scheme_nodes(vals)
        commercial = vals['party_vals']['partner'].commercial_partner_id
        if commercial._tca_is_uae_party():
            trn = commercial.vat or commercial.peppol_endpoint or ''
            if trn:
                vals['party_node']['cac:PartyTaxScheme'] = [{
                    'cbc:CompanyID': {'_text': trn},
                    'cac:TaxScheme': {'cbc:ID': {'_text': 'VAT'}},
                }]

    def _ubl_add_party_legal_entity_nodes(self, vals):
        # EXTENDS account.edi.xml.ubl_bis3 — UAE PartyLegalEntity: CompanyID =
        # legal registration ID, with schemeAgencyID = legal-ID type
        # (BTAE-15/16) and schemeAgencyName = issuing authority when TL
        # (BTAE-11/12) / passport country when PAS (BTAE-18/19). IBT-033
        # CompanyLegalForm.
        super()._ubl_add_party_legal_entity_nodes(vals)
        partner = vals['party_vals']['partner']
        commercial = partner.commercial_partner_id
        if not (commercial._tca_is_uae_party()):
            return

        trade_license, legal_id_type, legal_authority, passport_code = \
            self._tca_resolve_legal_id(vals, partner)

        nodes = vals['party_node']['cac:PartyLegalEntity']
        if nodes:
            legal_node = nodes[-1]
        else:
            legal_node = {'cbc:RegistrationName': {'_text': commercial.name}}
            nodes.append(legal_node)

        if trade_license:
            company_id = {'_text': trade_license}
            if legal_id_type:
                company_id['schemeAgencyID'] = legal_id_type
                if legal_id_type == 'TL' and legal_authority:
                    company_id['schemeAgencyName'] = legal_authority
                elif legal_id_type == 'PAS' and passport_code:
                    company_id['schemeAgencyName'] = passport_code
            legal_node['cbc:CompanyID'] = company_id

        if commercial.tca_legal_form:
            legal_node['cbc:CompanyLegalForm'] = {'_text': commercial.tca_legal_form}

    # ──────────────────────────────────────────────────────────────────────────
    # INVOICE LINE VALS — BTAE-08, BTAE-09, BTAE-10, BTAE-13, IBT-158
    # ──────────────────────────────────────────────────────────────────────────

    def _tca_line_record(self, vals):
        """The account.move.line behind the current line node, or False."""
        record = vals['line_vals']['base_line'].get('record')
        if record and record._name == 'account.move.line':
            return record
        return False

    def _ubl_add_line_item_identification_nodes(self, vals):
        # EXTENDS account.edi.xml.ubl — IBT-155/156/157 item identifiers from
        # the TCA per-line fields (override the product-derived defaults).
        super()._ubl_add_line_item_identification_nodes(vals)
        line = self._tca_line_record(vals)
        if not line:
            return
        item_node = vals['item_node']
        if line.tca_seller_item_id:
            item_node['cac:SellersItemIdentification'] = {
                'cbc:ID': {'_text': line.tca_seller_item_id},
            }
        if line.tca_buyer_item_id:
            item_node['cac:BuyersItemIdentification'] = {
                'cbc:ID': {'_text': line.tca_buyer_item_id},
            }
        if line.tca_standard_item_id:
            item_node['cac:StandardItemIdentification'] = {
                'cbc:ID': {
                    '_text': line.tca_standard_item_id,
                    'schemeID': line.tca_standard_item_scheme or '0160',
                },
            }

    def _ubl_add_line_item_commodity_classification_nodes(self, vals):
        # EXTENDS account.edi.xml.ubl — BTAE-13 CommodityCode (G/S/B),
        # BTAE-09 NatureCode (reverse-charge description), IBT-158
        # ItemClassificationCode (HS), BTAE-17 ItemClassificationCode (SAC).
        super()._ubl_add_line_item_commodity_classification_nodes(vals)
        line = self._tca_line_record(vals)
        if not line:
            return
        nodes = vals['item_node']['cac:CommodityClassification']
        primary = {'cbc:CommodityCode': {'_text': line.tca_effective_commodity_type}}
        if line.tca_rc_description:
            primary['cbc:NatureCode'] = {'_text': line.tca_rc_description}
        nodes.append(primary)
        if line.tca_hs_code:
            nodes.append({
                'cbc:ItemClassificationCode': {
                    '_text': line.tca_hs_code,
                    'listID': 'HS',
                    'listVersionID': '1.0',
                },
            })
        sac = line.tca_service_accounting_code or ''
        if sac:
            nodes.append({
                'cbc:ItemClassificationCode': {'_text': sac, 'listID': 'SAC'},
            })

    def _ubl_add_line_price_node(self, vals, in_foreign_currency=True):
        # EXTENDS account.edi.xml.ubl — ibr-126-ae: Price/BaseQuantity is
        # mandatory and a Price-level AllowanceCharge must carry the gross
        # unit price (BaseAmount) and the per-unit discount (Amount).
        #
        # bis3 sets `cbc:PriceAmount` from `raw_gross_price_unit_currency` —
        # which is the GROSS unit price (pre-discount). Earlier this override
        # mis-read PriceAmount as the *net* and back-derived `gross = net /
        # (1 − d/100)`, producing `gross / (1 − d/100)` (over-grossed) and a
        # wrong Amount. With discount=0 the formula collapsed to identity, so
        # the bug only fired on lines with a real discount.
        #
        # Read the gross from base_line directly and compute the per-unit
        # discount as `gross × (discount/100)`.
        super()._ubl_add_line_price_node(vals, in_foreign_currency=in_foreign_currency)
        price_node = vals['line_node'].get('cac:Price')
        if not price_node:
            return
        price_node['cbc:BaseQuantity'] = {'_text': 1}

        base_line = vals['line_vals']['base_line']
        suffix = '_currency' if in_foreign_currency else ''
        currency = base_line['currency_id'] if in_foreign_currency else vals['company_currency']
        dp = currency.decimal_places
        gross = base_line['tax_details'][f'raw_gross_price_unit{suffix}']
        discount = base_line.get('discount') or 0.0
        per_unit_discount = gross * (discount / 100.0)
        price_node['cac:AllowanceCharge'] = {
            'cbc:ChargeIndicator': {'_text': 'false'},
            'cbc:Amount': {
                '_text': FloatFmt(per_unit_discount, min_dp=dp),
                'currencyID': currency.name,
            },
            'cbc:BaseAmount': {
                '_text': FloatFmt(gross, min_dp=dp),
                'currencyID': currency.name,
            },
        }

    def _get_invoice_line_node(self, vals):
        # EXTENDS account.edi.xml.ubl_20 — append UAE per-line nodes:
        # BTAE-08 per-line VAT, BTAE-10 ItemPriceExtension, line-level
        # TaxTotal, IBT-127 note, IBT-132 order line ref, IBG-26 line period,
        # BTAE-24 lot number.
        line_node = super()._get_invoice_line_node(vals)
        line = self._tca_line_record(vals)
        if not line:
            return line_node
        base_line = vals['line_vals']['base_line']
        currency = base_line['currency_id']
        dp = currency.decimal_places

        # BTAE-08: per-line VAT — sum of the line's VAT tax amounts. Excise
        # and recycling-contribution taxes ride on the same line but are
        # reported as AllowanceCharges, not VAT; exclude them so BTAE-08
        # doesn't over-report on UAE excise products (tobacco, energy /
        # sugary drinks).
        line_vat = sum(
            td.get('tax_amount_currency', 0.0)
            for td in base_line['tax_details'].get('taxes_data', [])
            if not self._ubl_is_excise_tax(td)
            and not self._ubl_is_recycling_contribution_tax(td)
        )
        # Net line amount = LineExtensionAmount already on the node.
        line_net = float((line_node.get('cbc:LineExtensionAmount') or {}).get('_text') or 0.0)

        # BTAE-10 + BTAE-08: ItemPriceExtension (amount payable + per-line VAT).
        line_node['cac:ItemPriceExtension'] = {
            'cbc:Amount': {
                '_text': FloatFmt(line_net + line_vat, min_dp=dp),
                'currencyID': currency.name,
            },
            'cac:TaxTotal': {
                'cbc:TaxAmount': {
                    '_text': FloatFmt(line_vat, min_dp=dp),
                    'currencyID': currency.name,
                },
            },
        }
        # Line-level TaxTotal — BIS3 drops it; PINT AE (BTAE-08) needs it.
        line_node['cac:TaxTotal'] = [{
            'cbc:TaxAmount': {
                '_text': FloatFmt(line_vat, min_dp=dp),
                'currencyID': currency.name,
            },
        }]
        # IBT-127: line note. Append (not replace) so any upstream-set note
        # survives — bis3's `_ubl_add_line_note_nodes` initialises cbc:Note
        # as [], but a future cross-cutting localization could add to it.
        if line.tca_line_note:
            line_node.setdefault('cbc:Note', []).append({'_text': line.tca_line_note})
        # IBT-132: order line reference.
        if line.tca_order_line_ref:
            line_node['cac:OrderLineReference'] = {
                'cbc:LineID': {'_text': line.tca_order_line_ref},
            }
        # IBG-26: line invoice period.
        if line.tca_line_period_start or line.tca_line_period_end:
            period = {}
            if line.tca_line_period_start:
                period['cbc:StartDate'] = {'_text': line.tca_line_period_start}
            if line.tca_line_period_end:
                period['cbc:EndDate'] = {'_text': line.tca_line_period_end}
            line_node.setdefault('cac:InvoicePeriod', []).append(period)
        # BTAE-24: lot number (exports).
        lot = line.tca_lot_number or ''
        if lot and line_node.get('cac:Item'):
            line_node['cac:Item']['cac:ItemInstance'] = {
                'cac:LotIdentification': {'cbc:LotNumberID': {'_text': lot}},
            }
        return line_node

    # ──────────────────────────────────────────────────────────────────────────
    # CONSTRAINTS — UAE-specific validation before export
    # ──────────────────────────────────────────────────────────────────────────

    def _export_invoice_constraints(self, invoice, vals):
        """
        EXTENDS account.edi.xml.ubl_bis3.
        Adds UAE-specific pre-export validation.

        Odoo 19: the old payment_means_vals_list padding hack is gone — the
        rewritten bis3 constraints read the node tree (vals['document_node'])
        directly, so no `vals['vals']` shim is needed.

        CQ11: PINT AE rule set lives on `account.move._tca_collect_validation_errors`
        (shared with Phase-1 `_tca_validate_mandatory_fields`). This method just
        merges that dict into the bis3 constraints.
        """
        constraints = super()._export_invoice_constraints(invoice, vals)
        constraints.update(invoice._tca_collect_validation_errors())
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

    def _import_fill_invoice(self, invoice, tree, qty_factor):
        """
        EXTENDS account.edi.xml.ubl_20 (renamed from _import_fill_invoice_form
        in Odoo 19).
        After the base UBL importer fills standard fields, read PINT AE-specific
        elements and populate TCA fields on the invoice (document + lines).
        """
        logs = super()._import_fill_invoice(invoice, tree, qty_factor)

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
                # Self-billing applies only to the in-scope codes (380/381)
                if is_selfbilling and val in ('380', '381'):
                    invoice.tca_invoice_type_code = f'{val}_sb'
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

        logs += self._tca_import_fill_lines(invoice, tree, qty_factor)
        return logs

    def _tca_import_fill_lines(self, invoice, tree, qty_factor):
        """Read PINT AE per-line elements into the matching move lines. Odoo 19
        dropped the per-line import hook (_import_fill_invoice_line_form_batched),
        so pair the XML lines with the product lines positionally — _import_lines
        preserves document order."""
        logs = []
        line_tag = ('CreditNoteLine'
                    if invoice.move_type in ('out_refund', 'in_refund')
                    else 'InvoiceLine')
        line_trees = tree.findall('./{*}' + line_tag)
        product_lines = invoice._tca_product_lines()

        # strict=False: line counts may diverge if upstream importer turns a
        # line into an AllowanceCharge — tracked as a robustness concern in
        # REVIEW2 (positional pairing); silent truncate matches prior behaviour.
        for line_tree, line in zip(line_trees, product_lines, strict=False):
            # ── BTAE-13: CommodityCode → commodity type (G/S/B) ──────────
            node = line_tree.find('.//{*}CommodityClassification/{*}CommodityCode')
            if node is not None and node.text and node.text.strip() in ('G', 'S', 'B'):
                line.tca_commodity_type = node.text.strip()

            # ── BTAE-09: NatureCode → RC description ─────────────────────
            node = line_tree.find('.//{*}CommodityClassification/{*}NatureCode')
            if node is not None and node.text:
                line.tca_rc_description = node.text.strip()

            # ── IBT-158 / BTAE-17: ItemClassificationCode (HS + SAC) ─────
            for cls_node in line_tree.findall('.//{*}CommodityClassification/{*}ItemClassificationCode'):
                list_id = cls_node.attrib.get('listID', '')
                if cls_node.text:
                    if list_id == 'HS':
                        line.tca_hs_code = cls_node.text.strip()
                    elif list_id == 'SAC':
                        line.tca_service_accounting_code = cls_node.text.strip()

            # ── IBT-155: SellersItemIdentification (store, not just lookup)
            node = line_tree.find('.//{*}Item/{*}SellersItemIdentification/{*}ID')
            if node is not None and node.text:
                line.tca_seller_item_id = node.text.strip()

            # ── IBT-156: BuyersItemIdentification ─────────────────────────
            node = line_tree.find('.//{*}Item/{*}BuyersItemIdentification/{*}ID')
            if node is not None and node.text:
                line.tca_buyer_item_id = node.text.strip()

            # ── IBT-157: StandardItemIdentification ───────────────────────
            node = line_tree.find('.//{*}Item/{*}StandardItemIdentification/{*}ID')
            if node is not None and node.text:
                line.tca_standard_item_id = node.text.strip()
                scheme = node.attrib.get('schemeID', '')
                if scheme:
                    line.tca_standard_item_scheme = scheme

            # ── IBT-132: OrderLineReference ───────────────────────────────
            node = line_tree.find('./{*}OrderLineReference/{*}LineID')
            if node is not None and node.text:
                line.tca_order_line_ref = node.text.strip()

            # ── IBT-127: Line Note ────────────────────────────────────────
            node = line_tree.find('./{*}Note')
            if node is not None and node.text:
                line.tca_line_note = node.text.strip()

            # ── BTAE-24: LotNumber ────────────────────────────────────────
            node = line_tree.find('.//{*}ItemInstance/{*}LotIdentification/{*}LotNumberID')
            if node is not None and node.text:
                line.tca_lot_number = node.text.strip()

            # ── IBT-134/135: Line InvoicePeriod ──────────────────────────
            period = line_tree.find('./{*}InvoicePeriod')
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
        return tax.tca_tax_category == 'AE'
