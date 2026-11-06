"""
app/services/hierarchy_service.py

SentinelOS Enterprise Corporate Hierarchy & Multi-Site Account Management Service.
Designed for large-scale enterprise telecom customers, holding companies,
subsidiaries, branches, franchises, and regional offices.

Core Features:
1. Multi-tier Parent-Child CRM Account linking with cycle-detection.
2. Unified Corporate Billing Account hierarchy with configurable invoicing preferences:
   - CONSOLIDATED: Parent receives a single aggregated master tax invoice covering all sites.
   - DECENTRALIZED: Each branch receives its own independent tax invoice for local accounting.
3. Hierarchical subtree rollup analytics (total sites, total active subscriptions, aggregated MRR, total balance).
4. Aggregated Consolidated Multi-Site Billing Engine with double-entry General Ledger balancing.
"""

import uuid
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models_crm import CRMCustomer, HierarchyType, CRMActivity, ActivityType
from app.models_finance import (
    BillingAccount, BillingSubscription, SubscriptionStatus, InvoicingPreference,
    InvoiceLineItem, ChartOfAccounts
)
from app.models import Invoice, Customer
from app.services.sequence_service import generate_sequential_number, SequenceType
from app.services.tax_service import calculate_gst_breakdown, get_supplier_tax_profile
from app.services.billing_service import post_journal_entry


def link_customer_hierarchy(
    db: Session,
    child_customer_id: uuid.UUID,
    parent_customer_id: uuid.UUID,
    hierarchy_type: str = HierarchyType.BRANCH_LOCATION,
    invoicing_preference: str = InvoicingPreference.CONSOLIDATED,
    user_id: Optional[int] = None
) -> CRMCustomer:
    """
    Links a child corporate account to a parent holding company or regional HQ.
    Enforces loop-prevention and synchronizes CRM and Billing Account hierarchies.
    """
    if child_customer_id == parent_customer_id:
        raise ValueError("A customer cannot be set as its own parent.")

    child = db.query(CRMCustomer).filter(CRMCustomer.id == child_customer_id).first()
    if not child:
        raise ValueError(f"Child customer with ID {child_customer_id} not found.")

    parent = db.query(CRMCustomer).filter(CRMCustomer.id == parent_customer_id).first()
    if not parent:
        raise ValueError(f"Parent customer with ID {parent_customer_id} not found.")

    # Prevent circular reference
    curr = parent
    while curr:
        if curr.parent_customer_id == child_customer_id:
            raise ValueError(f"Circular hierarchy detected: {parent.legal_name} is already a descendant of {child.legal_name}.")
        if not curr.parent_customer_id:
            break
        curr = db.query(CRMCustomer).filter(CRMCustomer.id == curr.parent_customer_id).first()

    # If parent was STANDALONE, promote to HOLDING_COMPANY
    if parent.hierarchy_type == HierarchyType.STANDALONE:
        parent.hierarchy_type = HierarchyType.HOLDING_COMPANY

    child.parent_customer_id = parent_customer_id
    child.hierarchy_type = hierarchy_type

    # Link corresponding billing accounts
    child_ba = db.query(BillingAccount).filter(BillingAccount.crm_customer_id == child_customer_id).first()
    parent_ba = db.query(BillingAccount).filter(BillingAccount.crm_customer_id == parent_customer_id).first()

    if child_ba and parent_ba:
        child_ba.parent_billing_account_id = parent_ba.id
        child_ba.invoicing_preference = invoicing_preference

    # Log CRM Activities
    act_child = CRMActivity(
        crm_customer_id=child.id,
        activity_type=ActivityType.NOTE,
        summary="Linked to Corporate Parent",
        details=f"Account linked under {parent.legal_name} ({parent.customer_number}) as {hierarchy_type}.",
        performed_by_id=user_id
    )
    act_parent = CRMActivity(
        crm_customer_id=parent.id,
        activity_type=ActivityType.NOTE,
        summary="Branch/Subsidiary Added",
        details=f"Added child location {child.legal_name} ({child.customer_number}) as {hierarchy_type}.",
        performed_by_id=user_id
    )
    db.add(act_child)
    db.add(act_parent)

    db.commit()
    db.refresh(child)
    return child


def get_corporate_hierarchy_tree(db: Session, root_customer_id: uuid.UUID) -> Dict[str, Any]:
    """
    Recursively builds the full corporate hierarchy tree starting from a root parent.
    Computes rolled-up MRR, active subscription count, and total outstanding balance.
    """
    root = db.query(CRMCustomer).filter(CRMCustomer.id == root_customer_id).first()
    if not root:
        raise ValueError(f"Customer with ID {root_customer_id} not found.")

    def _build_node(cust: CRMCustomer) -> Dict[str, Any]:
        ba = db.query(BillingAccount).filter(BillingAccount.crm_customer_id == cust.id).first()
        subs = []
        node_mrr = Decimal('0.00')
        node_balance = Decimal('0.00')

        if ba:
            node_balance = Decimal(str(ba.balance))
            active_subs = db.query(BillingSubscription).filter(
                BillingSubscription.billing_account_id == ba.id,
                BillingSubscription.status == SubscriptionStatus.ACTIVE
            ).all()
            for s in active_subs:
                price = Decimal(str(s.unit_price))
                node_mrr += price
                subs.append({
                    "id": str(s.id),
                    "code": s.subscription_code,
                    "plan_name": s.plan_name,
                    "unit_price": float(price),
                    "status": s.status,
                    "billing_frequency": s.billing_frequency,
                })

        children_nodes = []
        subtree_mrr = node_mrr
        subtree_subs_count = len(subs)
        subtree_nodes_count = 1
        subtree_balance = node_balance

        direct_children = db.query(CRMCustomer).filter(CRMCustomer.parent_customer_id == cust.id).all()
        for child in direct_children:
            child_tree = _build_node(child)
            children_nodes.append(child_tree)
            subtree_mrr += Decimal(str(child_tree["rollup_metrics"]["total_mrr"]))
            subtree_subs_count += child_tree["rollup_metrics"]["active_subscriptions_count"]
            subtree_nodes_count += child_tree["rollup_metrics"]["total_nodes_count"]
            subtree_balance += Decimal(str(child_tree["rollup_metrics"]["total_balance"]))

        return {
            "customer_id": str(cust.id),
            "customer_number": cust.customer_number,
            "legal_name": cust.legal_name,
            "trade_name": cust.trade_name,
            "hierarchy_type": cust.hierarchy_type,
            "status": cust.status,
            "market_id": cust.market_id,
            "billing_account": {
                "id": str(ba.id) if ba else None,
                "account_number": ba.account_number if ba else None,
                "balance": float(node_balance),
                "invoicing_preference": ba.invoicing_preference if ba else InvoicingPreference.CONSOLIDATED,
            } if ba else None,
            "active_subscriptions": subs,
            "rollup_metrics": {
                "node_mrr": float(node_mrr),
                "total_mrr": float(subtree_mrr),
                "active_subscriptions_count": subtree_subs_count,
                "total_nodes_count": subtree_nodes_count,
                "total_balance": float(subtree_balance),
            },
            "children": children_nodes
        }

    return _build_node(root)


def generate_parent_consolidated_invoice(
    db: Session,
    parent_billing_account_id: uuid.UUID,
    billing_period_start: datetime,
    billing_period_end: datetime,
    due_date: Optional[datetime] = None
) -> Invoice:
    """
    Generates a consolidated master tax invoice for a parent corporate holding account.
    Aggregates billable subscription charges across:
    1. The parent holding account itself.
    2. All child branch/subsidiary accounts whose invoicing_preference is CONSOLIDATED.

    Each line item clearly identifies the branch location and service description.
    Enforces double-entry General Ledger balancing.
    """
    parent_ba = db.query(BillingAccount).filter(BillingAccount.id == parent_billing_account_id).first()
    if not parent_ba:
        raise ValueError(f"Parent BillingAccount {parent_billing_account_id} not found.")

    parent_crm = parent_ba.crm_customer
    parent_legacy_cust = parent_ba.customer
    if not parent_legacy_cust:
        parent_legacy_cust = db.query(Customer).filter(Customer.id == parent_ba.customer_id).first()

    market_id = parent_ba.market_id or "mumbai"
    supplier = get_supplier_tax_profile(market_id)

    # Gather parent subscriptions
    billable_accounts = [parent_ba]

    # Gather all child accounts linked to this parent that have CONSOLIDATED preference
    child_accounts = db.query(BillingAccount).filter(
        BillingAccount.parent_billing_account_id == parent_ba.id,
        BillingAccount.invoicing_preference == InvoicingPreference.CONSOLIDATED
    ).all()
    billable_accounts.extend(child_accounts)

    line_items_to_create: List[Dict[str, Any]] = []
    total_taxable = Decimal('0.00')

    for ba in billable_accounts:
        cust_label = "Headquarters"
        if ba.id != parent_ba.id:
            child_crm = ba.crm_customer
            cust_label = child_crm.legal_name if child_crm else f"Site-{ba.account_number}"

        active_subs = db.query(BillingSubscription).filter(
            BillingSubscription.billing_account_id == ba.id,
            BillingSubscription.status == SubscriptionStatus.ACTIVE
        ).all()

        for sub in active_subs:
            item_price = Decimal(str(sub.unit_price))
            total_taxable += item_price
            desc = f"[{cust_label}] {sub.plan_name} ({billing_period_start.strftime('%d-%b-%Y')} to {billing_period_end.strftime('%d-%b-%Y')})"
            line_items_to_create.append({
                "subscription_id": sub.id,
                "description": desc,
                "quantity": Decimal('1.00'),
                "unit_price": item_price,
                "amount": item_price,
            })

    if not line_items_to_create:
        raise ValueError("No active subscriptions found for parent or consolidated child branches.")

    # Determine Place of Supply and GST breakdown
    cust_state = None
    if parent_crm and parent_crm.addresses:
        for addr in parent_crm.addresses:
            if addr.address_type in ("BILLING", "REGISTERED") and addr.state:
                cust_state = addr.state
                break

    tax_calc = calculate_gst_breakdown(
        taxable_amount=total_taxable,
        market_id=market_id,
        customer_state=cust_state,
        sac_code="998422"
    )

    total_invoice_amount = total_taxable + tax_calc.tax_amount
    due_dt = due_date or (datetime.utcnow() + timedelta(days=parent_ba.payment_term_days))

    invoice_code = generate_sequential_number(db, SequenceType.INVOICE, market_id)

    new_invoice = Invoice(
        market_id=market_id,
        invoice_code=invoice_code,
        customer_id=parent_legacy_cust.id if parent_legacy_cust else 1,
        billing_account_id=parent_ba.id,
        plan_name=f"Corporate Consolidated Service ({len(line_items_to_create)} Sites/Links)",
        transaction_type="Corporate Consolidated Invoice",
        payment_method="NET_BANKING",
        billed_amount=float(total_invoice_amount),
        expected_amount=float(total_invoice_amount),
        due_date=due_dt,
        status="Issued",
        currency="INR",
        subtotal=total_taxable,
        tax_amount=tax_calc.tax_amount,
        discount_amount=Decimal('0.00'),
        total_amount=total_invoice_amount,
        paid_amount=Decimal('0.00'),
        outstanding_amount=total_invoice_amount,
        billing_period_start=billing_period_start,
        billing_period_end=billing_period_end,
        tax_treatment=tax_calc.tax_treatment,
        cgst_amount=tax_calc.cgst_amount,
        sgst_amount=tax_calc.sgst_amount,
        igst_amount=tax_calc.igst_amount,
        place_of_supply=tax_calc.place_of_supply,
        supplier_gstin=supplier.gstin,
        customer_gstin=parent_crm.tax_id if parent_crm else None,
        notes=f"Aggregated corporate invoice covering {len(billable_accounts)} enterprise locations.",
        idempotency_key=f"CONSOL-INV-{parent_ba.id}-{billing_period_start.strftime('%Y%m%d')}"
    )
    db.add(new_invoice)
    db.flush()

    # Create Line Items with exact proportionate GST
    for item in line_items_to_create:
        item_tax = (item["amount"] * Decimal('0.18')).quantize(Decimal('0.01'))
        line_item = InvoiceLineItem(
            invoice_id=new_invoice.id,
            subscription_id=item["subscription_id"],
            description=item["description"],
            quantity=item["quantity"],
            unit_price=item["unit_price"],
            amount=item["amount"],
            tax_rate=Decimal('18.00'),
            tax_amount=item_tax,
            discount_amount=Decimal('0.00'),
            total_amount=item["amount"] + item_tax,
            currency="INR"
        )
        db.add(line_item)

    # Post balanced General Ledger entries
    # Debit: Accounts Receivable (1200) = total_invoice_amount
    # Credit: Broadband Revenue (4100) = total_taxable
    # Credit: GST Output Tax (2200) = tax_calc.tax_amount
    journal_lines = [
        {
            "account_code": ChartOfAccounts.ACCOUNTS_RECEIVABLE,
            "debit": total_invoice_amount,
            "credit": Decimal('0.00'),
            "description": f"Corporate Receivables - Invoice {invoice_code}"
        },
        {
            "account_code": ChartOfAccounts.BROADBAND_REVENUE,
            "debit": Decimal('0.00'),
            "credit": total_taxable,
            "description": f"Broadband Revenue - Invoice {invoice_code}"
        },
        {
            "account_code": ChartOfAccounts.TAX_PAYABLE_GST,
            "debit": Decimal('0.00'),
            "credit": tax_calc.tax_amount,
            "description": f"GST Output Tax Payable ({tax_calc.tax_treatment}) - Invoice {invoice_code}"
        }
    ]

    post_journal_entry(
        db=db,
        market_id=market_id,
        reference_type="INVOICE",
        reference_id=str(new_invoice.id),
        description=f"Corporate Consolidated Billing Invoice {invoice_code} for {parent_crm.legal_name if parent_crm else parent_ba.account_number}",
        lines=journal_lines
    )

    db.commit()
    db.refresh(new_invoice)
    return new_invoice
