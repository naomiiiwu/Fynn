"""Lazada's fee taxonomy, as published by BigSeller.

Transcribed from "Profit Analysis for Lazada"
<https://help.bigseller.com/en_US/detailPage/10/1/2786/content>, which states
every figure is obtained from the Lazada API. Three columns: BigSeller's own
profit bucket, Lazada's Fee Classification, and the exact Terms in Lazada.

Why this is here rather than in a comment: the classification a fee sits under
is the only key that covers fee names nobody has seen yet, and whether a
classification can carry a single accounting decision has to be *derived* from
this table rather than asserted. See services/classification.MIXED_CATEGORIES.

Two things to hold in mind when reading it:

  BigSeller's buckets are a profit-and-loss view, not a chart of accounts. They
  answer "did this order make money", not "which account does this post to", so
  they inform Fynn's decisions without deciding them.

  The final bucket, Buyer Refund Amount, is where the published table collects
  everything left over — withholding tax, seller incentives, balance transfers.
  Its breadth is an artefact of that, not a claim that a withholding tax is a
  buyer refund.
"""

# (BigSeller bucket, Lazada Fee Classification, Term in Lazada)
LAZADA_TAXONOMY: list[tuple[str, str, str]] = [
    ("Order Revenue", "Grand Total", "Grand Total"),

    ("Product Sales", "Orders-Sales", "Item Price Credit"),

    ("Compensation", "Orders-Claims", "Shipping Fee Claims"),
    *[("Compensation", "Refunds-Claims", t) for t in (
        "Lost Claim", "Lost Claim(Lazada)", "Lost Claims - First Mile",
        "Reversal - Lost Claims", "Damaged Claim", "Damaged Claim(Lazada)",
        "Shipping Fee Subsidy Claim", "Shipping Fee Correction", "Wrong Status Claims",
        "CB Shipping Fee Claim", "Customer Shipping Fee Claim",
        "Sponsored Affiliates Claims",
    )],

    *[("Logistics Cost", "Orders-Logistics", t) for t in (
        "Shipping Fee (Paid By Customer)", "Shipping Fee Paid by Seller",
        "Shipping Fee Voucher (by Lazada)", "Auto. Shipping fee subsidy (by Lazada)",
        "Wrong Weight Adjustment", "Shipping Fee Voucher(By Seller)",
        "International Shipping fees", "Domestic Shipping fees",
        "Shipping Fee Subsidy(By Seller)", "Shipping Fee Subsidy (By Seller)",
    )],
    *[("Logistics Cost", "3P Services-Logistics", t) for t in (
        "Shipping Fee (Charged By 3P)", "FBL Handling Fee",
        "Import Duties and Tax (Charged by 3PL)",
        "International Shipping fees (Charged by 3P)",
        "Crossbroder shipping fee refund - correction for overcharge",
        "Domestic Shipping fees (Charged by 3P)", "DPP Service Fee", "Pick Up Fee",
        "Pullout Charge", "Storage Fee", "XB FBL Logistics Mgt Service Fee (Agent)",
        "Inventory Storage Fees", "B2B VAT", "XB - FBL WH Handling Fee",
        "B2B Shipping Fees",
    )],
    *[("Logistics Cost", "Refunds-Logistics", t) for t in (
        "Reversal Shipping Fee Voucher (by Lazada)",
        "Reversal shipping Fee (Paid by Customer)", "Return shipping fees",
        "Reversal automated shipping subsidy", "Seller Shipping Subsidy Charges",
        "Shipping fee - correction for undercharge", "Shipping Fee Cashback",
        "Shipping Fee Voucher Refund to Laz", "Shipping Fee Refund to Customer",
        "Shipping Fee Paid by Seller",
    )],
    ("Logistics Cost", "Orders-Other Credit", "Shipping Fee (Paid By Customer)"),
    ("Logistics Cost", "Orders-Lazada Fees", "Shipping Fee Subsidy(By Seller)"),
    ("Logistics Cost", "Orders-Lazada Fees", "Shipping Fee Subsidy (By Seller)"),

    ("Transaction Fee", "Orders-Lazada Fees", "Payment Fee"),
    ("Transaction Fee", "Orders-Lazada Fees", "Payment fee refund - correction for overcharge"),
    ("Transaction Fee", "Refunds-Lazada Fees", "Payment fee - correction for undercharge"),
    ("Transaction Fee", "Refunds-Lazada Fees", "Payment Fee"),
    ("Transaction Fee", "Refunds-Lazada Fees", "Payment Fee Credit"),

    ("Commission Fee", "Orders-Lazada Fees", "Commission"),
    ("Commission Fee", "Orders-Lazada Fees", "Commission fee refund - correction for overcharge"),
    ("Commission Fee", "Refunds-Lazada Fees", "Reversal Commission"),
    ("Commission Fee", "Refunds-Lazada Fees", "Commission fee - correction for undercharge"),

    ("Service Charge", "Orders-Lazada Fees", "Campaign Fee"),
    ("Service Charge", "Refunds-Lazada Fees", "Reversal Campaign Fee"),
    ("Service Charge", "Refunds-Lazada Fees", "Adjustments Campaign Fee"),

    *[("Discount Promotion", "Orders-Marketing Fees", t) for t in (
        "Promotional Charges Vouchers", "Promotional Charges Flexi-Combo",
        "Lazcoin discount", "Promotional Charges Bundles", "LCP Fee",
        "Seller Funded Marketing Voucher", "Free Shipping Max Fee",
    )],
    *[("Discount Promotion", "Refunds-Marketing Fees", t) for t in (
        "Reversal of Seller Picks Commission", "Reversal of DPP Service Fee",
        "Reversal Promotional Charges Vouchers", "DRTM Offline Refund Adjustment",
        "Adjustments Item Charge", "Reversal Promotional Charges Flexi-Combo",
        "Reversal LCP Fee", "Reversal Lazcoin discount",
    )],
    ("Discount Promotion", "Other Services-Marketing", "Sponsored Affiliates Claims"),
    ("Discount Promotion", "Orders-Lazada Fees", "Free Shipping Max Fee"),
    ("Discount Promotion", "Orders-Sales", "Free Shipping Max Fee"),

    ("Buyer Refund Amount", "Refunds-Item Charges", "Reversal Item Price"),
    ("Buyer Refund Amount", "Refunds-Sales", "Reversal Item Price"),
    ("Buyer Refund Amount", "Refunds-Lazada Fees", "Lazada Bonus - Reversal"),
    ("Buyer Refund Amount", "Refunds-Lazada Fees", "Lazada Bonus - LZD co-fund - Reversal"),
    ("Buyer Refund Amount", "Other Services-Lazada Fees", "Installment fee"),
    ("Buyer Refund Amount", "Orders-Item Charges", "Item Price Credit"),
    *[("Buyer Refund Amount", "Orders-YouPik Fees", t) for t in (
        "Shipping Fee Paid by Seller", "Payment Fee", "Commission",
        "Youpik Marketing Commission Charge",
    )],
    *[("Buyer Refund Amount", "Other Services-Sales", t) for t in (
        "Seller Incentive", "Paylater Interest Settlement (Paid by Seller)",
        "Reimbursements Withholding tax (LEL)", "Reimbursements Withholding tax",
    )],
    *[("Buyer Refund Amount", "Orders-Lazada Fees", t) for t in (
        "Lazada Bonus", "Lazada Bonus - LZD co-fund", "Lazada Bonus - LZD co-fund Extra",
        "Reversal Item Price", "Seller Picks Commission",
    )],
    ("Buyer Refund Amount", "Refunds-Lazada Fees", "Reversal of Seller Picks Commission"),
    ("Buyer Refund Amount", "Refunds-Lazada Fees", "Reversal of DPP Service Fee"),
    ("Buyer Refund Amount", "Refunds-Sales", "DRTM Offline Refund Adjustment"),
    ("Buyer Refund Amount", "Refunds-Sales", "Adjustments Item Charge"),
    ("Buyer Refund Amount", "Refunds-YouPik Fees", "Reversal of Youpik Marketing Commission Charge"),
    ("Buyer Refund Amount", "Refunds-YouPik Fees", "Reversal Commission"),
    *[("Buyer Refund Amount", "Orders-Sales", t) for t in (
        "Import Duties and Tax (Paid by Customer)", "Item Price Subsidy",
        "Adjustments Item Charge", "Offline Refund Adjustment DRTM",
        "Seller balance adjustments - Debit", "Seller Two Transfer From",
        "FromSellerAccountThird", "lazada nine new manual fee",
        "Seller balance adjustments - Credit", "Seller Two Transfer To",
    )],

    ("Other", "Other", "The fee that does not belong to the terms above"),
]


def classifications() -> dict[str, set[str]]:
    """Fee Classification → the BigSeller buckets its terms fall into."""
    out: dict[str, set[str]] = {}
    for bucket, classification, _term in LAZADA_TAXONOMY:
        out.setdefault(classification, set()).add(bucket)
    return out


def terms() -> dict[str, set[str]]:
    """Fee Classification → the terms Lazada files under it."""
    out: dict[str, set[str]] = {}
    for _bucket, classification, term in LAZADA_TAXONOMY:
        out.setdefault(classification, set()).add(term)
    return out


# Classifications whose terms plainly need different accounts even though
# BigSeller files them under one bucket. Derivation from bucket spread alone
# misses these, so they are named — with the reason — rather than inferred.
ACCOUNT_HETEROGENEOUS = {
    # shipping, payment, commission and marketing commission, in one heading
    "Orders-YouPik Fees",
    # a marketing-commission reversal beside a plain commission reversal
    "Refunds-YouPik Fees",
    # seller incentive, interest expense and withholding tax together
    "Other Services-Sales",
    # voucher reversals beside reversals of Seller Picks commission, DPP service
    # fees and item charges
    "Refunds-Marketing Fees",
}
