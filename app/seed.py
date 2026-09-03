import random
from datetime import datetime, timedelta
from app.database import SessionLocal, engine, Base
from app.models import (
    User, Node, Customer, UsageRecord, Ticket, Invoice, Recommendation, AuditLog
)
from app.auth import hash_password

def seed_database():
    print("Recreating database tables...")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        print("1. Seeding Demo Users...")
        users_data = [
            ("executive@pmrg.in", "admin123", "Rajesh Singhania", "Executive"),
            ("noc@pmrg.in", "admin123", "Vikram Rathore", "NOC"),
            ("care@pmrg.in", "admin123", "Pooja Sharma", "Care"),
            ("revenue@pmrg.in", "admin123", "Anand Kulkarni", "Revenue"),
            ("admin@pmrg.in", "admin123", "PMRG AI Administrator", "Admin"),
        ]

        users = []
        for email, pwd, name, role in users_data:
            u = User(
                email=email,
                hashed_password=hash_password(pwd),
                full_name=name,
                role=role,
                is_active=True,
                created_at=datetime.utcnow() - timedelta(days=180)
            )
            db.add(u)
            users.append(u)
        db.commit()

        print("2. Seeding Mumbai Network Nodes...")
        nodes_config = [
            # High Degraded Node 1
            ("OLT-BND-01", "Bandra Central OLT-1", "Bandra West", "OLT", 92.5, 3.8, 48.2, -29.8, 14, 38.0, "Critical"),
            # Degraded Node 2
            ("OLT-AND-03", "Andheri MIDC Hub", "Andheri East", "OLT", 88.0, 4.2, 54.0, -28.1, 11, 44.5, "Degraded"),
            # Degraded Node 3
            ("FDH-MAL-02", "Malad Link Rd FDH", "Malad West", "FDH", 81.0, 2.5, 35.0, -27.6, 7, 58.0, "Degraded"),
            # Healthy Nodes
            ("OLT-BKC-01", "BKC Financial Core OLT", "BKC", "OLT", 62.0, 0.1, 12.0, -20.2, 0, 98.0, "Healthy"),
            ("OLT-POW-01", "Hiranandani Tech Hub OLT", "Powai", "OLT", 68.5, 0.3, 15.4, -21.0, 1, 94.0, "Healthy"),
            ("OLT-LP-01", "Lower Parel Commercial OLT", "Lower Parel", "OLT", 71.0, 0.4, 18.0, -22.1, 2, 91.5, "Healthy"),
            ("FDH-DAD-01", "Dadar TT Circle Hub", "Dadar", "FDH", 59.0, 0.2, 14.5, -20.8, 0, 97.0, "Healthy"),
            ("OLT-THA-01", "Thane Majiwada OLT", "Thane West", "OLT", 74.0, 0.8, 22.0, -23.4, 3, 86.0, "Healthy"),
            ("FDH-WOR-01", "Worli Sea Face Hub", "Worli", "FDH", 52.0, 0.1, 11.0, -19.5, 0, 99.0, "Healthy"),
            ("OLT-BOR-01", "Borivali West Hub", "Borivali", "OLT", 65.0, 0.5, 19.0, -22.0, 1, 93.0, "Healthy"),
            ("FDH-JUH-01", "Juhu Scheme Hub", "Juhu", "FDH", 55.0, 0.2, 13.0, -20.0, 0, 98.5, "Healthy"),
            ("OLT-GHT-01", "Ghatkopar R-City Hub", "Ghatkopar", "OLT", 70.0, 0.6, 21.0, -23.0, 2, 90.0, "Healthy"),
        ]

        nodes = []
        for code, name, area, ntype, util, loss, lat, opt, alarms, health, status in nodes_config:
            node = Node(
                node_code=code,
                node_name=name,
                area=area,
                node_type=ntype,
                utilization_pct=util,
                packet_loss_pct=loss,
                latency_ms=lat,
                optical_power_dbm=opt,
                alarm_count=alarms,
                health_score=health,
                status=status,
                last_telemetry_at=datetime.utcnow() - timedelta(minutes=random.randint(2, 20))
            )
            db.add(node)
            nodes.append(node)
        db.commit()

        print("3. Seeding Customers (~1,000 realistic Mumbai ISP subscribers)...")
        first_names = [
            "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Ayaan", "Krishna", "Ishaan",
            "Shaurya", "Atharva", "Kabir", "Rudra", "Aryan", "Ananya", "Diya", "Gauri", "Myra", "Sara",
            "Aadhya", "Pari", "Saanvi", "Avani", "Isha", "Kavya", "Tara", "Riya", "Meera", "Zara",
            "Vikram", "Sunil", "Ramesh", "Deepak", "Sanjay", "Ketan", "Nilesh", "Prakash", "Manish", "Rahul",
            "Amit", "Pooja", "Neha", "Sneha", "Anjali", "Swati", "Kiran", "Divya", "Tanvi", "Sonali"
        ]
        last_names = [
            "Sharma", "Verma", "Patel", "Mehta", "Shah", "Joshi", "Deshmukh", "Kulkarni", "Patil", "Sawant",
            "Shinde", "Pawar", "Bhosale", "Naik", "Shetty", "Kamath", "Pai", "Iyer", "Menon", "Nair",
            "Gupta", "Agarwal", "Jain", "Bansal", "Kapoor", "Malhotra", "Chopra", "Khanna", "Singhania", "Trivedi"
        ]
        corp_names = [
            "Apex Infotech Solutions", "Zenith Media Works", "Nexus FinTech Labs", "Bharat Digital Logistics",
            "Mumbai Traders Corp", "Quantify Analytics India", "Matrix Coworking BKC", "Blue Horizon Studios",
            "Reliance Retail Partner", "Kothari Diamond Exports", "Orbit Health Systems", "Vanguard Law Associates",
            "Trident Maritime Corp", "Elevate E-Commerce", "Sahara Hospitality Pvt Ltd", "Prime Capital Advisory"
        ]

        prepaid_plans = [
            # plan_name, plan_price, validity_days, daily_quota_gb, actual_arpu, segment
            ("Hero Unlimited 1.5GB/Day (28d)", 299.0, 28, 1.5, 345.0, "Prepaid - Daily Unlimited"),
            ("Super 5G 2GB/Day + Hotstar (28d)", 349.0, 28, 2.0, 395.0, "Prepaid - 5G High Speed"),
            ("Voice & Value 1GB/Day (28d)", 239.0, 28, 1.0, 265.0, "Prepaid - Value Voice"),
            ("True 5G Unlimited 2GB/Day (56d)", 579.0, 56, 2.0, 335.0, "Prepaid - 5G High Speed"),
            ("Super Saver 1.5GB/Day (84d)", 719.0, 84, 1.5, 295.0, "Prepaid - Long-term Bundle"),
            ("Cricket & OTT 2GB/Day + Prime (84d)", 859.0, 84, 2.0, 340.0, "Prepaid - Long-term Bundle"),
            ("Annual 5G All-Access 2.5GB/Day (365d)", 2999.0, 365, 2.5, 260.0, "Prepaid - Long-term Bundle"),
        ]

        postpaid_plans = [
            # plan_name, plan_price, billing_cycle_days, monthly_quota_gb, actual_arpu, segment
            ("Postpaid Individual Infinity 75GB", 499.0, 30, 75.0, 535.0, "Postpaid - Individual Infinity"),
            ("Postpaid Family Plus (3 SIMs) 150GB", 999.0, 30, 150.0, 1180.0, "Postpaid - Family Plus"),
            ("Postpaid Enterprise Corporate ILL 500M", 12000.0, 30, 1000.0, 12000.0, "Postpaid - Enterprise ILL"),
            ("Postpaid Premium Dedicated 1Gbps", 22000.0, 30, 2500.0, 22000.0, "Postpaid - Enterprise ILL"),
        ]

        customers = []
        node_map = {n.area: n for n in nodes}

        # Create 1000 customers: 700 Prepaid (70%), 300 Postpaid (30%)
        for i in range(1, 1001):
            is_prepaid = (i <= 700)
            area = random.choice([n.area for n in nodes])
            node = node_map[area]
            is_node_degraded = (node.status in ['Degraded', 'Critical'])

            # Seed specific reference customers for customer-level ARPU verification
            if i == 1:
                c_name = "Myra Pawar"
                c_email = "myra.pawar@gmail.com"
                plan = "Annual 5G All-Access 2.5GB/Day (365d)"
                plan_price = 2999.0
                validity = 365
                daily_quota = 2.5
                revenue_30d = 260.0
                actual_arpu = 260.0
                segment = "Prepaid - Long-term Bundle"
                quota_monthly = daily_quota * 30.0
                customer_type = "Prepaid"
                payment_method = "UPI (PhonePe)"
                booster_spend = 14.0
            elif i == 2:
                c_name = "Meera Bansal"
                c_email = "meera.bansal@gmail.com"
                plan = "True 5G Unlimited 2GB/Day (56d)"
                plan_price = 579.0
                validity = 56
                daily_quota = 2.0
                revenue_30d = 335.0
                actual_arpu = 335.0
                segment = "Prepaid - 5G High Speed"
                quota_monthly = daily_quota * 30.0
                customer_type = "Prepaid"
                payment_method = "UPI (Google Pay)"
                booster_spend = 25.0
            elif i == 3:
                c_name = "Reyansh Pawar"
                c_email = "reyansh.pawar@gmail.com"
                plan = "Super Saver 1.5GB/Day (84d)"
                plan_price = 719.0
                validity = 84
                daily_quota = 1.5
                revenue_30d = 295.0
                actual_arpu = 295.0
                segment = "Prepaid - Long-term Bundle"
                quota_monthly = daily_quota * 30.0
                customer_type = "Prepaid"
                payment_method = "UPI (Paytm)"
                booster_spend = 38.0
            elif is_prepaid:
                fn = random.choice(first_names)
                ln = random.choice(last_names)
                c_name = f"{fn} {ln}"
                c_email = f"{fn.lower()}.{ln.lower()}{i%500}@gmail.com"
                plan, plan_price, validity, daily_quota, _, segment = random.choice(prepaid_plans)
                quota_monthly = daily_quota * 30.0
                customer_type = "Prepaid"
                payment_method = random.choice(["UPI (PhonePe)", "UPI (Google Pay)", "UPI (Paytm)", "MyJio / Airtel Thanks UPI"])
                # Customer ARPU (30D): Normalized 30-day base pack recognized revenue + booster add-on spend
                base_30d = round((plan_price / validity) * 30.0, 1)
                booster_spend = random.choice([0.0, 0.0, 19.0, 29.0, 61.0, 149.0])
                revenue_30d = round(base_30d + booster_spend, 1)
                actual_arpu = revenue_30d
            else:
                is_corp = (i % 5 == 0)
                if is_corp:
                    c_name = random.choice(corp_names) + f" #{i%100}"
                    c_email = f"billing.mumbai@{c_name.split()[0].lower()}corp.in"
                    plan, plan_price, validity, quota_monthly, _, segment = random.choice(postpaid_plans[2:])
                    revenue_30d = plan_price
                    booster_spend = 0.0
                else:
                    fn = random.choice(first_names)
                    ln = random.choice(last_names)
                    c_name = f"{fn} {ln}"
                    c_email = f"{fn.lower()}.{ln.lower()}{i%500}@gmail.com"
                    plan, plan_price, validity, quota_monthly, _, segment = random.choice(postpaid_plans[:2])
                    if plan_price == 499.0:
                        revenue_30d = random.choice([499.0, 535.0, 560.0])
                    else:
                        revenue_30d = random.choice([999.0, 1180.0, 1240.0])
                    booster_spend = round(revenue_30d - plan_price, 1)
                daily_quota = round(quota_monthly / 30.0, 1)
                customer_type = "Postpaid"
                payment_method = random.choice(["Autopay (NACH / e-Mandate)", "Corporate Net Banking", "Credit Card Autopay"])
                actual_arpu = revenue_30d

            tenure = random.randint(1, 48)
            signup_date = datetime.utcnow() - timedelta(days=tenure * 30 + random.randint(1, 28))

            # Correlate churn risk & status with degraded nodes & validity expiry
            if is_node_degraded and random.random() < 0.45:
                status = "At-Risk"
                stage = random.choice(["Complaint", "Renewal", "Win-back"])
                nps = random.randint(2, 5)
                days_to_expiry = random.randint(-6, 2) if is_prepaid else random.randint(2, 10)
                validity_status = "Grace Period (Overdue)" if days_to_expiry < 0 else "Expiring Soon"
                daily_data_used = round(daily_quota * random.uniform(0.95, 1.25), 2)
            elif random.random() < 0.08:
                status = "At-Risk"
                stage = "Complaint" if random.random() < 0.5 else "Renewal"
                nps = random.randint(3, 6)
                days_to_expiry = random.randint(-5, 2) if is_prepaid else random.randint(2, 12)
                validity_status = "Grace Period (Overdue)" if days_to_expiry < 0 else "Expiring Soon"
                daily_data_used = round(daily_quota * random.uniform(0.9, 1.2), 2)
            else:
                status = "Active"
                stage = random.choice(["Use", "Use", "Use", "Renewal", "Acquisition", "Install"])
                nps = random.randint(7, 10)
                days_to_expiry = random.randint(4, max(5, validity - 3)) if is_prepaid else random.randint(5, 28)
                validity_status = "Active" if is_prepaid else "Billed Active"
                daily_data_used = round(daily_quota * random.uniform(0.40, 0.85), 2)

            last_recharge_date = datetime.utcnow() - timedelta(days=max(1, validity - max(0, days_to_expiry)))
            last_recharge_amount = plan_price

            c = Customer(
                customer_code=f"JBR-MUM-{10000+i}",
                name=c_name,
                email=c_email,
                phone=f"+91 98{random.randint(10000000, 99999999)}",
                locality=area,
                segment=segment,
                customer_type=customer_type,
                plan_name=plan,
                plan_price=plan_price,
                revenue_30d=revenue_30d,
                actual_arpu=actual_arpu,
                arpu=actual_arpu,
                recharge_validity_days=validity,
                days_to_expiry=days_to_expiry,
                validity_status=validity_status,
                daily_data_quota_gb=daily_quota,
                daily_data_used_gb=daily_data_used,
                last_recharge_date=last_recharge_date,
                last_recharge_amount=last_recharge_amount,
                payment_method=payment_method,
                tenure_months=tenure,
                signup_date=signup_date,
                status=status,
                node_id=node.id,
                current_stage=stage,
                nps_score=nps,
                created_at=signup_date
            )
            db.add(c)
            customers.append((c, quota_monthly, is_node_degraded, booster_spend))

        db.commit()

        print("4. Seeding Usage, Tickets, and Billing Records...")
        tickets = []
        invoices = []
        recs_to_create = []

        for idx, (cust, quota, is_degraded, booster_spend) in enumerate(customers, 1):
            # Usage
            if cust.status == 'At-Risk' or is_degraded:
                usage_trend = random.choice(['Declining', 'Declining', 'Stable'])
                trend_pct = -random.uniform(20.0, 58.0) if usage_trend == 'Declining' else 0.0
                used_gb = max(20.0, quota * random.uniform(0.15, 0.45))
            else:
                usage_trend = random.choice(['Stable', 'Growing', 'Growing'])
                trend_pct = random.uniform(5.0, 32.0) if usage_trend == 'Growing' else 0.0
                used_gb = quota * random.uniform(0.60, 0.95)

            u_rec = UsageRecord(
                customer_id=cust.id,
                monthly_gb=round(used_gb, 1),
                quota_gb=quota,
                usage_trend=usage_trend,
                trend_pct=round(trend_pct, 1),
                ott_streaming_flag=True,
                gaming_flag=random.random() < 0.4,
                last_active_at=datetime.utcnow() - timedelta(hours=random.randint(1, 48))
            )
            db.add(u_rec)

            # Tickets (Higher volume on degraded nodes)
            ticket_prob = 0.65 if is_degraded else 0.15
            if random.random() < ticket_prob:
                num_tickets = random.randint(2, 4) if is_degraded else 1
                for t_i in range(num_tickets):
                    if cust.customer_type == 'Prepaid':
                        cat = random.choice(['Outage', 'Speed']) if is_degraded else random.choice(['Speed', 'Validity', 'Billing', 'Hardware'])
                    else:
                        cat = random.choice(['Outage', 'Speed']) if is_degraded else random.choice(['Speed', 'Billing', 'Hardware', 'Install'])
                    t_status = random.choice(['Open', 'In-Progress']) if t_i == 0 else 'Resolved'
                    repeat = (num_tickets > 1 and t_i > 0)
                    desc = f"Subscriber in {cust.locality} ({cust.customer_type}) reports {cat.lower()} degradation. Packet drops observed on port."
                    
                    t = Ticket(
                        ticket_code=f"TCK-{20260000 + len(tickets) + 1}",
                        customer_id=cust.id,
                        node_id=cust.node_id,
                        category=cat,
                        priority="Critical" if (cust.segment.startswith('Postpaid - Enterprise') or repeat) else "High" if is_degraded else "Medium",
                        status=t_status,
                        created_at=datetime.utcnow() - timedelta(days=random.randint(1, 14), hours=random.randint(1, 23)),
                        resolved_at=datetime.utcnow() - timedelta(hours=random.randint(2, 24)) if t_status == 'Resolved' else None,
                        repeat_flag=repeat,
                        description=desc,
                        ai_triage_action="Automated Diagnostics + Field Dispatch" if cat == 'Outage' else "QoS Profile Re-sync",
                        sla_deadline=datetime.utcnow() + timedelta(hours=random.randint(2, 8))
                    )
                    db.add(t)
                    tickets.append(t)

            # Invoices / Recharge records
            # Intentional anomalies for Revenue Assurance:
            # First 25 customers: Prepaid revenue leakages
            # Customers 701-725: Postpaid revenue leakages
            is_prepaid_anomaly = (idx <= 25)
            is_postpaid_anomaly = (701 <= idx <= 725)
            has_anomaly = is_prepaid_anomaly or is_postpaid_anomaly

            anomaly_type = None
            leakage_amt = 0.0
            billed_amt = cust.plan_price
            expected_amt = cust.plan_price
            waiver = 0.0
            inv_status = 'Paid'

            if is_prepaid_anomaly:
                a_choice = idx % 3
                if a_choice == 0:
                    anomaly_type = 'Expired Validity OTT Leakage'
                    billed_amt = 0.0
                    expected_amt = 299.0
                    leakage_amt = 299.0  # Premium OTT streaming unbilled past pack expiry
                elif a_choice == 1:
                    anomaly_type = 'Zero-Rated APN Leakage'
                    billed_amt = 0.0
                    expected_amt = 450.0
                    leakage_amt = 450.0  # Streaming data routed via zero-rated portal APN
                else:
                    anomaly_type = 'Recharge Webhook Drop'
                    billed_amt = 719.0
                    expected_amt = 1438.0
                    leakage_amt = 719.0  # Bank debited, gateway webhook dropped, double credit
            elif is_postpaid_anomaly:
                a_choice = idx % 4
                if a_choice == 0:
                    anomaly_type = 'Plan Mismatch'
                    billed_amt = 499.0
                    expected_amt = 999.0
                    leakage_amt = 500.0
                elif a_choice == 1:
                    anomaly_type = 'Duplicate Credit'
                    waiver = 400.0
                    leakage_amt = 400.0
                elif a_choice == 2:
                    anomaly_type = 'Unbilled Usage'
                    expected_amt = cust.plan_price + 500.0
                    leakage_amt = 500.0
                else:
                    anomaly_type = 'Dunning Failure'
                    inv_status = 'Unpaid'
                    leakage_amt = cust.plan_price
            elif cust.status == 'At-Risk' and cust.customer_type == 'Postpaid' and random.random() < 0.4:
                inv_status = 'Late'

            tx_code_prefix = "RCG" if cust.customer_type == 'Prepaid' else "INV"
            tx_type = "Base Unlimited Pack Recharge" if cust.customer_type == 'Prepaid' else "Monthly Postpaid Bill"

            inv = Invoice(
                invoice_code=f"{tx_code_prefix}-2026-{100000 + idx}",
                customer_id=cust.id,
                plan_name=cust.plan_name,
                transaction_type=tx_type,
                payment_method=cust.payment_method,
                billed_amount=billed_amt,
                expected_amount=expected_amt,
                due_date=datetime.utcnow() - timedelta(days=random.randint(5, 35)),
                paid_date=datetime.utcnow() - timedelta(days=random.randint(1, 10)) if inv_status == 'Paid' else None,
                status=inv_status,
                waiver_amount=waiver,
                waiver_reason="SLA adjustment" if waiver > 0 else None,
                renewal_date=datetime.utcnow() + timedelta(days=max(1, cust.days_to_expiry)),
                anomaly_flag=has_anomaly,
                anomaly_type=anomaly_type,
                leakage_amount=leakage_amt,
                created_at=datetime.utcnow() - timedelta(days=25)
            )
            db.add(inv)
            invoices.append(inv)

            # If prepaid customer has booster spend, add the corresponding recent booster recharge invoice
            if cust.customer_type == 'Prepaid' and booster_spend > 0:
                booster_amt = booster_spend
                booster_name = "6GB 5G High-Speed Booster" if booster_amt >= 60.0 else "2GB Daily Top-up" if booster_amt <= 30.0 else "OTT Entertainment Add-on"
                b_inv = Invoice(
                    invoice_code=f"RCG-2026-B{100000 + idx}",
                    customer_id=cust.id,
                    plan_name=booster_name,
                    transaction_type="Data Booster Add-on",
                    payment_method=cust.payment_method,
                    billed_amount=booster_amt,
                    expected_amount=booster_amt,
                    due_date=datetime.utcnow() - timedelta(days=random.randint(2, 15)),
                    paid_date=datetime.utcnow() - timedelta(days=random.randint(2, 15)),
                    status="Paid",
                    waiver_amount=0.0,
                    renewal_date=datetime.utcnow() + timedelta(days=max(1, cust.days_to_expiry)),
                    anomaly_flag=False,
                    created_at=datetime.utcnow() - timedelta(days=random.randint(2, 15))
                )
                db.add(b_inv)
                invoices.append(b_inv)

        db.commit()

        print("5. Seeding Pre-Generated AI Recommendations & Audit Logs across ALL 5 Modules...")
        admin_user = next(u for u in users if u.role == "Admin")
        noc_user = next(u for u in users if u.role == "NOC")
        care_user = next(u for u in users if u.role == "Care")
        rev_user = next(u for u in users if u.role == "Revenue")

        # Recommendation 1: Service Assurance (Pending)
        deg_node = next(n for n in nodes if n.status == 'Critical')
        r1 = Recommendation(
            source_module="Predictive Service Assurance",
            target_entity_type="Node",
            target_entity_id=deg_node.id,
            target_entity_label=f"{deg_node.node_name} ({deg_node.area})",
            title=f"Proactive Field Dispatch - {deg_node.node_name}",
            description="AI detected optical power attenuation (-29.8 dBm) affecting 112 downstream subscribers in Bandra West. Recommending emergency line splicing calibration.",
            recommended_action="Dispatch Field Team to Bandra Central Hub for optical line calibration and OTDR trace test.",
            confidence_score=0.96,
            status="PENDING",
            action_payload={
                "node_code": deg_node.node_code,
                "area": deg_node.area,
                "health_score": deg_node.health_score,
                "signals": [
                    {"signal": "Optical Power", "value": "-29.8 dBm", "impact": "+35 pts"},
                    {"signal": "Backhaul Utilization", "value": "92.5%", "impact": "+26 pts"},
                    {"signal": "Active Alarms", "value": "14 alarms", "impact": "+20 pts"}
                ]
            },
            created_at=datetime.utcnow() - timedelta(hours=2)
        )
        db.add(r1)

        # Recommendation 2: Churn Retention (Pending) - Focused on Prepaid Subscriber
        at_risk_c = next(c for c, *_ in customers if c.status == 'At-Risk' and c.customer_type == 'Prepaid')
        r2 = Recommendation(
            source_module="Churn Prediction & Retention AI",
            target_entity_type="Customer",
            target_entity_id=at_risk_c.id,
            target_entity_label=f"{at_risk_c.name} ({at_risk_c.customer_code})",
            title=f"Prepaid Retention Save Offer - {at_risk_c.name}",
            description=f"Prepaid subscriber scored at 88.5% churn risk. Validity expired {abs(at_risk_c.days_to_expiry)} days ago + daily 1.5GB quota exhausted early + 2 repeat buffering complaints. Recommending 3-day validity extension + emergency 5G booster.",
            recommended_action="Apply 3-Day Validity Extension + 5GB 5G High-Speed Booster Voucher & 20% UPI Renewal Concession.",
            confidence_score=0.94,
            status="PENDING",
            action_payload={
                "customer_code": at_risk_c.customer_code,
                "customer_type": at_risk_c.customer_type,
                "plan_price": at_risk_c.plan_price,
                "actual_arpu": at_risk_c.actual_arpu,
                "arpu": at_risk_c.arpu,
                "locality": at_risk_c.locality,
                "signals": [
                    {"factor": "Recharge Lag / Validity Expired", "weight": "+35 pts", "detail": f"Validity expired {abs(at_risk_c.days_to_expiry)} days ago; pending renewal"},
                    {"factor": "Daily Quota Exhaustion", "weight": "+25 pts", "detail": "Exhausted 100% of daily 1.5GB cap by 2 PM on 14 of last 20 days"},
                    {"factor": "Buffering Complaints", "weight": "+22 pts", "detail": "2 tickets logged regarding video buffering during prime time"}
                ]
            },
            created_at=datetime.utcnow() - timedelta(hours=3)
        )
        db.add(r2)

        # Recommendation 3: Intelligent Customer Journeys (Pending) - Focused on Prepaid Renewal NBA
        renewal_c = next(c for c, *_ in customers if c.current_stage == 'Renewal' and c.customer_type == 'Prepaid')
        r3 = Recommendation(
            source_module="Intelligent Customer Journeys",
            target_entity_type="Customer",
            target_entity_id=renewal_c.id,
            target_entity_label=f"{renewal_c.name} ({renewal_c.customer_code})",
            title=f"Next-Best-Action - Stage: Renewal ({renewal_c.name})",
            description=f"Prepaid {renewal_c.plan_name} pack expiring in {renewal_c.days_to_expiry} days. AI recommended automated 1-click WhatsApp UPI recharge link with 5GB bonus voucher.",
            recommended_action=f"Deliver 1-Click WhatsApp UPI Recharge Link with 5GB Bonus Voucher ({renewal_c.plan_name}).",
            confidence_score=0.93,
            status="PENDING",
            action_payload={
                "customer_code": renewal_c.customer_code,
                "customer_type": renewal_c.customer_type,
                "stage": "Renewal",
                "locality": renewal_c.locality,
                "channel": "WhatsApp Interactive Message",
                "signals": [
                    {"signal": "Pack Expiry Countdown", "value": f"Expires in {renewal_c.days_to_expiry} days", "weight": "+35 pts"},
                    {"signal": "Recharge Channel", "value": "UPI PhonePe", "weight": "+25 pts"},
                    {"signal": "NPS Sentiment", "value": f"{renewal_c.nps_score}/10 NPS", "weight": "+20 pts"}
                ]
            },
            created_at=datetime.utcnow() - timedelta(hours=3, minutes=30)
        )
        db.add(r3)

        # Recommendation 4: AI-driven OSS/BSS Orchestration (Pending)
        open_ticket = next(t for t in tickets if t.status == 'Open')
        r4 = Recommendation(
            source_module="AI-driven OSS/BSS Orchestration",
            target_entity_type="Ticket",
            target_entity_id=open_ticket.id,
            target_entity_label=f"Ticket {open_ticket.ticket_code} ({open_ticket.category})",
            title=f"Automated Profile Re-provisioning - {open_ticket.ticket_code}",
            description=f"AI diagnosed BRAS profile desync and packet loss. Recommending remote TR-069 QoS bandwidth sync.",
            recommended_action="Execute remote ONT reset and BRAS QoS bandwidth profile re-synchronization.",
            confidence_score=0.94,
            status="PENDING",
            action_payload={
                "ticket_code": open_ticket.ticket_code,
                "category": open_ticket.category,
                "priority": open_ticket.priority,
                "workflow_type": "Automated TR-069 Reboot & BRAS QoS Sync",
                "signals": [
                    {"signal": "BRAS QoS Sync", "value": "Out-of-sync profile", "weight": "+35 pts"},
                    {"signal": "Packet Loss", "value": "4.2% drop rate", "weight": "+25 pts"}
                ]
            },
            created_at=datetime.utcnow() - timedelta(hours=4)
        )
        db.add(r4)

        # Recommendation 5: Revenue Assurance (Pending) - Prepaid OTT Policy Leakage
        anomaly_inv = next(inv for inv in invoices if inv.anomaly_flag and inv.anomaly_type == 'Expired Validity OTT Leakage')
        r5 = Recommendation(
            source_module="Revenue Assurance & Leakage Analytics",
            target_entity_type="Invoice",
            target_entity_id=anomaly_inv.id,
            target_entity_label=f"Transaction {anomaly_inv.invoice_code} (Expired Validity OTT Leakage)",
            title=f"Prepaid Policy Re-sync - INR {anomaly_inv.leakage_amount:.0f} OTT Leakage",
            description=f"Subscriber actively streaming premium OTT content 6 days after pack validity expired due to PCRF policy push lag. Estimated unbilled leakage INR {anomaly_inv.leakage_amount:.0f}.",
            recommended_action=f"Execute PCRF policy revocation to terminate zero-balance OTT tunnel and deliver automated 1-click WhatsApp renewal prompt.",
            confidence_score=0.98,
            status="PENDING",
            action_payload={
                "invoice_code": anomaly_inv.invoice_code,
                "leakage_amount": anomaly_inv.leakage_amount,
                "anomaly_type": anomaly_inv.anomaly_type,
                "signals": [{"anomaly": "Expired Validity OTT Tunnel Active", "leakage": anomaly_inv.leakage_amount}]
            },
            created_at=datetime.utcnow() - timedelta(hours=5)
        )
        db.add(r5)

        # Historical Executed Recommendations and Audit Logs
        # Audit Log 1: Orchestration Executed
        audit1 = AuditLog(
            recommendation_id=None,
            source_module="AI-driven OSS/BSS Orchestration",
            action_taken="Execute remote ONT reset and BRAS QoS bandwidth profile re-synchronization.",
            decision="APPROVED",
            user_id=noc_user.id,
            user_name=noc_user.full_name,
            user_role=noc_user.role,
            confidence_score=0.94,
            original_signals={"target": "Ticket TCK-20260001", "cause": "BRAS QoS sync loss"},
            execution_result={"status": "Executed", "message": "ONT QoS re-synced in 4.2 seconds via TR-069"},
            timestamp=datetime.utcnow() - timedelta(hours=10)
        )
        db.add(audit1)

        # Audit Log 2: Revenue Action Executed
        audit2 = AuditLog(
            recommendation_id=None,
            source_module="Revenue Assurance & Leakage Analytics",
            action_taken="Revoke duplicate downtime credit voucher of INR 400.",
            decision="APPROVED",
            user_id=rev_user.id,
            user_name=rev_user.full_name,
            user_role=rev_user.role,
            confidence_score=0.99,
            original_signals={"anomaly": "Duplicate Credit", "amount": 400.0},
            execution_result={"status": "Credit revoked in billing ledger", "invoice_adjusted": True},
            timestamp=datetime.utcnow() - timedelta(hours=18)
        )
        db.add(audit2)

        # Audit Log 3: Customer Retention Executed
        audit3 = AuditLog(
            recommendation_id=None,
            source_module="Churn Prediction & Retention AI",
            action_taken="Dispatch Priority Relationship Manager with 15% annual renewal concession.",
            decision="APPROVED",
            user_id=care_user.id,
            user_name=care_user.full_name,
            user_role=care_user.role,
            confidence_score=0.91,
            original_signals={"churn_risk": 82.0, "reason": "Tenure renewal + complaint history"},
            execution_result={"status": "Offer accepted by customer; renewal secured for 12 months"},
            timestamp=datetime.utcnow() - timedelta(days=1)
        )
        db.add(audit3)

        # Audit Log 4: Journey Next-Best-Action Executed
        audit4 = AuditLog(
            recommendation_id=None,
            source_module="Intelligent Customer Journeys",
            action_taken="Send WhatsApp 1-Click Digital KYC & Fiber Installation Slot Scheduler.",
            decision="APPROVED",
            user_id=care_user.id,
            user_name=care_user.full_name,
            user_role=care_user.role,
            confidence_score=0.93,
            original_signals={"stage": "Acquisition", "channel": "WhatsApp Interactive"},
            execution_result={"status": "Message sent via WhatsApp API", "slot_scheduled": "Tomorrow 10:00 AM"},
            timestamp=datetime.utcnow() - timedelta(days=2)
        )
        db.add(audit4)

        # Audit Log 5: Predictive Assurance Field Dispatch Executed
        audit5 = AuditLog(
            recommendation_id=None,
            source_module="Predictive Service Assurance",
            action_taken="Dispatch Field Splicing Technician to Andheri MIDC Hub.",
            decision="APPROVED",
            user_id=noc_user.id,
            user_name=noc_user.full_name,
            user_role=noc_user.role,
            confidence_score=0.96,
            original_signals={"node": "OLT-AND-03", "optical_power": "-28.1 dBm"},
            execution_result={"status": "Field technician assigned #FDO-2026-772", "calibration": "Completed"},
            timestamp=datetime.utcnow() - timedelta(days=3)
        )
        db.add(audit5)

        db.commit()
        print("Database seeded successfully with ~1,000 customers, nodes, tickets, invoices, recommendations (ALL 5 modules), and audit logs!")
    finally:
        db.close()

if __name__ == "__main__":
    seed_database()
