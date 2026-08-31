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
            ("executive@jeebr.in", "admin123", "Rajesh Singhania", "Executive"),
            ("noc@jeebr.in", "admin123", "Vikram Rathore", "NOC"),
            ("care@jeebr.in", "admin123", "Pooja Sharma", "Care"),
            ("revenue@jeebr.in", "admin123", "Anand Kulkarni", "Revenue"),
            ("admin@jeebr.in", "admin123", "PMRG AI Administrator", "Admin"),
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

        plans_retail = [
            ("Fiber Starter 100Mbps", 699.0, 500.0),
            ("Fiber Turbo 200Mbps", 849.0, 750.0),
            ("Fiber Ultra 300Mbps", 999.0, 1000.0),
            ("Fiber Giga 500Mbps", 1499.0, 1500.0),
            ("Fiber Max 1Gbps", 2499.0, 3000.0),
        ]
        plans_corp = [
            ("ILL Enterprise 500Mbps", 12000.0, 10000.0),
            ("ILL Premium 1Gbps Dedicated", 22000.0, 25000.0),
            ("ILL Ultra 2Gbps Symmetric", 40000.0, 50000.0),
        ]

        customers = []
        node_map = {n.area: n for n in nodes}

        # Create 1000 customers
        for i in range(1, 1001):
            is_corp = (i % 18 == 0)
            area = random.choice([n.area for n in nodes])
            node = node_map[area]
            is_node_degraded = (node.status in ['Degraded', 'Critical'])

            if is_corp:
                c_name = random.choice(corp_names) + f" #{i%100}"
                c_email = f"billing.mumbai@{c_name.split()[0].lower()}corp.in"
                segment = "ILL-Corporate"
                plan, arpu, quota = random.choice(plans_corp)
            else:
                fn = random.choice(first_names)
                ln = random.choice(last_names)
                c_name = f"{fn} {ln}"
                c_email = f"{fn.lower()}.{ln.lower()}{i%500}@gmail.com"
                segment = "Home Broadband"
                plan, arpu, quota = random.choice(plans_retail)

            tenure = random.randint(1, 48)
            signup_date = datetime.utcnow() - timedelta(days=tenure * 30 + random.randint(1, 28))

            # Correlate churn risk & status with degraded nodes
            if is_node_degraded and random.random() < 0.45:
                status = "At-Risk"
                stage = random.choice(["Complaint", "Renewal", "Win-back"])
                nps = random.randint(2, 5)
            elif random.random() < 0.08:
                status = "At-Risk"
                stage = "Complaint" if random.random() < 0.5 else "Renewal"
                nps = random.randint(3, 6)
            else:
                status = "Active"
                stage = random.choice(["Use", "Use", "Use", "Renewal", "Acquisition", "Install"])
                nps = random.randint(7, 10)

            c = Customer(
                customer_code=f"JBR-MUM-{10000+i}",
                name=c_name,
                email=c_email,
                phone=f"+91 98{random.randint(10000000, 99999999)}",
                locality=area,
                segment=segment,
                plan_name=plan,
                arpu=arpu,
                tenure_months=tenure,
                signup_date=signup_date,
                status=status,
                node_id=node.id,
                current_stage=stage,
                nps_score=nps,
                created_at=signup_date
            )
            db.add(c)
            customers.append((c, quota, is_node_degraded))

        db.commit()

        print("4. Seeding Usage, Tickets, and Billing Records...")
        tickets = []
        invoices = []
        recs_to_create = []

        for idx, (cust, quota, is_degraded) in enumerate(customers, 1):
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
                    cat = random.choice(['Outage', 'Speed']) if is_degraded else random.choice(['Speed', 'Billing', 'Hardware', 'Install'])
                    t_status = random.choice(['Open', 'In-Progress']) if t_i == 0 else 'Resolved'
                    repeat = (num_tickets > 1 and t_i > 0)
                    desc = f"Subscriber in {cust.locality} reports {cat.lower()} degradation. Packet drops observed on port."
                    
                    t = Ticket(
                        ticket_code=f"TCK-{20260000 + len(tickets) + 1}",
                        customer_id=cust.id,
                        node_id=cust.node_id,
                        category=cat,
                        priority="Critical" if (cust.segment == 'ILL-Corporate' or repeat) else "High" if is_degraded else "Medium",
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

            # Invoices
            # Generate intentional billing anomalies for ~40 customers
            has_anomaly = (idx <= 45)
            anomaly_type = None
            leakage_amt = 0.0
            billed_amt = cust.arpu
            expected_amt = cust.arpu
            waiver = 0.0
            inv_status = 'Paid'

            if has_anomaly:
                a_choice = idx % 4
                if a_choice == 0:
                    anomaly_type = 'Plan Mismatch'
                    billed_amt = 699.0
                    expected_amt = 1499.0
                    leakage_amt = 800.0  # ₹800/mo unbilled differential
                elif a_choice == 1:
                    anomaly_type = 'Duplicate Credit'
                    waiver = 400.0
                    leakage_amt = 400.0  # ₹400 duplicate downtime waiver
                elif a_choice == 2:
                    anomaly_type = 'Unbilled Usage'
                    expected_amt = cust.arpu + 500.0
                    leakage_amt = 500.0  # Turbo boost active without line item
                else:
                    anomaly_type = 'Dunning Failure'
                    inv_status = 'Unpaid'
                    leakage_amt = cust.arpu  # Uncollected overdue balance > 45d
            elif cust.status == 'At-Risk' and random.random() < 0.4:
                inv_status = 'Late'

            inv = Invoice(
                invoice_code=f"INV-2026-{100000 + idx}",
                customer_id=cust.id,
                plan_name=cust.plan_name,
                billed_amount=billed_amt,
                expected_amount=expected_amt,
                due_date=datetime.utcnow() - timedelta(days=random.randint(5, 35)),
                paid_date=datetime.utcnow() - timedelta(days=random.randint(1, 10)) if inv_status == 'Paid' else None,
                status=inv_status,
                waiver_amount=waiver,
                waiver_reason="Downtime SLA adjustment (AI flagged)" if waiver > 0 else None,
                renewal_date=datetime.utcnow() + timedelta(days=random.randint(10, 90)),
                anomaly_flag=has_anomaly,
                anomaly_type=anomaly_type,
                leakage_amount=leakage_amt,
                created_at=datetime.utcnow() - timedelta(days=30)
            )
            db.add(inv)
            invoices.append(inv)

        db.commit()

        print("5. Seeding Pre-Generated AI Recommendations & Audit Logs...")
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

        # Recommendation 2: Churn Retention (Pending)
        at_risk_c = next(c for c, _, _ in customers if c.status == 'At-Risk')
        r2 = Recommendation(
            source_module="Churn Prediction & Retention AI",
            target_entity_type="Customer",
            target_entity_id=at_risk_c.id,
            target_entity_label=f"{at_risk_c.name} ({at_risk_c.customer_code})",
            title=f"VIP Retention Outreach - {at_risk_c.name}",
            description=f"Subscriber churn risk scored at 88.5% due to 3 recent outage complaints + 42% bandwidth drop. Proposing Speed Boost & 20% billing discount.",
            recommended_action="Apply 20% Retention Credit Voucher + Schedule Priority Account Manager Satisfaction Call.",
            confidence_score=0.93,
            status="PENDING",
            action_payload={
                "customer_code": at_risk_c.customer_code,
                "arpu": at_risk_c.arpu,
                "locality": at_risk_c.locality,
                "signals": [
                    {"factor": "Complaint Recency", "weight": "+30 pts", "detail": "3 open complaints in 7 days"},
                    {"factor": "Bandwidth Drop", "weight": "+28 pts", "detail": "Monthly consumption dropped 42%"}
                ]
            },
            created_at=datetime.utcnow() - timedelta(hours=3)
        )
        db.add(r2)

        # Recommendation 3: Revenue Assurance (Pending)
        anomaly_inv = next(inv for inv in invoices if inv.anomaly_flag and inv.anomaly_type == 'Plan Mismatch')
        r3 = Recommendation(
            source_module="Revenue Assurance & Leakage Analytics",
            target_entity_type="Invoice",
            target_entity_id=anomaly_inv.id,
            target_entity_label=f"Invoice {anomaly_inv.invoice_code} (Plan Mismatch)",
            title=f"Billing Rate Adjustment - ₹{anomaly_inv.leakage_amount:.0f} Leakage",
            description=f"Subscriber on 500 Mbps plan billed starter tier rate of ₹{anomaly_inv.billed_amount:.0f}. Estimated revenue leakage ₹{anomaly_inv.leakage_amount:.0f}/mo.",
            recommended_action=f"Issue supplemental invoice for ₹{anomaly_inv.leakage_amount:.0f} and align billing catalog in SAP BRIM.",
            confidence_score=0.98,
            status="PENDING",
            action_payload={
                "invoice_code": anomaly_inv.invoice_code,
                "leakage_amount": anomaly_inv.leakage_amount,
                "anomaly_type": anomaly_inv.anomaly_type,
                "signals": [{"anomaly": "Catalog Plan Rate Mismatch", "leakage": anomaly_inv.leakage_amount}]
            },
            created_at=datetime.utcnow() - timedelta(hours=4)
        )
        db.add(r3)

        # Recommendation 4 & Audit Log (Approved & Executed)
        r4 = Recommendation(
            source_module="AI-driven OSS/BSS Orchestration",
            target_entity_type="Ticket",
            target_entity_id=tickets[0].id if tickets else 1,
            target_entity_label=f"Ticket {tickets[0].ticket_code if tickets else 'TCK-2026-01'} (Speed Drop)",
            title=f"Automated Profile Re-provisioning - {tickets[0].ticket_code if tickets else 'TCK-2026-01'}",
            description="AI diagnosed ONT sync mismatch on BRAS cluster. Recommending TR-069 remote reset.",
            recommended_action="Execute remote ONT reset and BRAS QoS bandwidth profile re-synchronization.",
            confidence_score=0.94,
            status="EXECUTED",
            action_payload={"ticket_code": tickets[0].ticket_code if tickets else "TCK-1", "signals": [{"cause": "BRAS QoS sync loss"}]},
            created_at=datetime.utcnow() - timedelta(hours=12),
            reviewed_by_id=noc_user.id,
            reviewed_at=datetime.utcnow() - timedelta(hours=10),
            review_notes="Approved automated profile push to BRAS."
        )
        db.add(r4)
        db.flush()

        audit1 = AuditLog(
            recommendation_id=r4.id,
            source_module="AI-driven OSS/BSS Orchestration",
            action_taken="Execute remote ONT reset and BRAS QoS bandwidth profile re-synchronization.",
            decision="APPROVED",
            user_id=noc_user.id,
            user_name=noc_user.full_name,
            user_role=noc_user.role,
            confidence_score=0.94,
            original_signals={"target": r4.target_entity_label, "cause": "BRAS QoS sync loss"},
            execution_result={"status": "Executed", "message": "ONT QoS re-synced in 4.2 seconds"},
            timestamp=datetime.utcnow() - timedelta(hours=10)
        )
        db.add(audit1)

        # Audit Log 2: Revenue Action Executed
        audit2 = AuditLog(
            recommendation_id=None,
            source_module="Revenue Assurance & Leakage Analytics",
            action_taken="Revoke duplicate downtime credit voucher of ₹400.",
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

        db.commit()
        print("Database seeded successfully with ~1,000 customers, nodes, tickets, invoices, recommendations, and audit logs!")
    finally:
        db.close()

if __name__ == "__main__":
    seed_database()
