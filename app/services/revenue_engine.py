from typing import List, Tuple
from sqlalchemy.orm import Session
from app.models import Invoice, Customer, Recommendation
from app.schemas import RevenueLeakageItem, ContributingSignal

def evaluate_invoice_anomaly(inv: Invoice, db: Session) -> Tuple[float, str, float, List[ContributingSignal], str, str]:
    cust = db.query(Customer).filter(Customer.id == inv.customer_id).first()
    
    score = 15.0
    factors: List[ContributingSignal] = []
    desc = ""
    action = ""

    rate_diff = max(0.0, inv.expected_amount - inv.billed_amount)
    if rate_diff > 100.0 or inv.anomaly_type == 'Plan Mismatch':
        mismatch_pts = min(40.0, (rate_diff / max(1.0, inv.expected_amount)) * 60.0)
        score += mismatch_pts
        factors.append(ContributingSignal(
            signal="Catalog Plan Rate Mismatch",
            value=f"Billed ₹{inv.billed_amount:.0f} vs Expected ₹{inv.expected_amount:.0f}",
            weight=f"+{mismatch_pts:.0f} pts",
            detail=f"Subscriber is provisioned for high-tier speed ({inv.plan_name}) but billed base tier tariff.",
            impact_type="negative"
        ))
        desc = f"Subscriber provisioned on {inv.plan_name} (₹{inv.expected_amount:.0f}) but billed ₹{inv.billed_amount:.0f}."
        action = f"Generate supplementary debit invoice for ₹{inv.leakage_amount:.0f} and synchronize SAP BRIM catalog profile."

    if inv.waiver_amount > 200.0 or inv.anomaly_type == 'Duplicate Credit':
        waiver_pts = min(35.0, (inv.waiver_amount / 400.0) * 30.0)
        score += waiver_pts
        factors.append(ContributingSignal(
            signal="Duplicate SLA Credit Waiver",
            value=f"₹{inv.waiver_amount:.0f} credited",
            weight=f"+{waiver_pts:.0f} pts",
            detail="Repeated downtime compensation credit applied multiple times in single billing cycle.",
            impact_type="negative"
        ))
        if not desc:
            desc = f"Multiple downtime compensation credits (₹{inv.waiver_amount:.0f}) posted in single billing cycle."
            action = f"Revoke duplicate credit adjustment of ₹{inv.leakage_amount:.0f} on next billing cycle."

    if inv.anomaly_type == 'Unbilled Usage':
        score += 30.0
        factors.append(ContributingSignal(
            signal="Unbilled Turbo Bandwidth Add-on",
            value=f"₹{inv.leakage_amount:.0f}/mo unbilled",
            weight="+30 pts",
            detail="BRAS RADIUS accounting session confirms active turbo speed tier without recurring subscription billing item.",
            impact_type="negative"
        ))
        if not desc:
            desc = f"Active high-speed port boost without recurring subscription charge."
            action = f"Attach recurring add-on subscription item of ₹{inv.leakage_amount:.0f}/mo to customer billing profile."

    if inv.status == 'Unpaid' or inv.anomaly_type == 'Dunning Failure':
        score += 35.0
        factors.append(ContributingSignal(
            signal="Dunning Workflow Execution Gap",
            value=f"Unpaid balance: ₹{inv.leakage_amount:.0f}",
            weight="+35 pts",
            detail="Account active with unpaid balance for > 45 days without automatic dunning SMS or soft speed-throttle.",
            impact_type="negative"
        ))
        if not desc:
            desc = f"Account active with uncollected balance for > 45 days lacking automated dunning follow-up."
            action = f"Trigger dunning payment link via WhatsApp API and schedule soft QoS throttle in 48 hours."

    final_score = min(99.0, max(10.0, score))
    confidence = min(0.99, max(0.85, 0.82 + (final_score / 300.0)))

    if final_score >= 65.0:
        risk_level = 'Critical'
    elif final_score >= 40.0:
        risk_level = 'High'
    else:
        risk_level = 'Medium'

    if not desc:
        desc = f"Unclassified billing anomaly detected in invoice {inv.invoice_code}."
        action = "Audit billing ledger and reconcile SAP BRIM subscription record."

    return round(final_score, 1), risk_level, round(confidence, 2), factors, desc, action

def detect_revenue_leakages(db: Session) -> List[RevenueLeakageItem]:
    invoices = db.query(Invoice).filter(Invoice.anomaly_flag == True).all()
    leakages = []

    for inv in invoices:
        cust = db.query(Customer).filter(Customer.id == inv.customer_id).first()
        cust_name = cust.name if cust else "Unknown Customer"
        locality = cust.locality if cust else "Mumbai"
        segment = cust.segment if cust else "Home Broadband"

        score, risk_lvl, conf, factors, desc, action = evaluate_invoice_anomaly(inv, db)

        has_pending = db.query(Recommendation).filter(
            Recommendation.source_module == 'Revenue Assurance & Leakage Analytics',
            Recommendation.target_entity_type == 'Invoice',
            Recommendation.target_entity_id == inv.id,
            Recommendation.status == 'PENDING'
        ).first() is not None

        leakages.append(RevenueLeakageItem(
            invoice_id=inv.id,
            invoice_code=inv.invoice_code,
            customer_id=inv.customer_id,
            customer_name=cust_name,
            locality=locality,
            segment=segment,
            plan_name=inv.plan_name,
            billed_amount=inv.billed_amount,
            expected_amount=inv.expected_amount,
            waiver_amount=inv.waiver_amount,
            status=inv.status,
            anomaly_type=inv.anomaly_type or 'Billing Anomaly',
            leakage_amount=inv.leakage_amount,
            leakage_risk_score=score,
            risk_level=risk_lvl,
            confidence_score=conf,
            description=desc,
            recommended_action=action,
            contributing_signals=factors,
            has_pending_recommendation=has_pending
        ))

    leakages.sort(key=lambda x: (x.leakage_risk_score, x.leakage_amount), reverse=True)
    return leakages
