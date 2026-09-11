import random
from datetime import datetime, timedelta
from app.database import SessionLocal, engine, Base
from app.models import (
    User, Node, Customer, UsageRecord, Ticket, Invoice, Recommendation, AuditLog, Resource
)
from app.auth import hash_password

def seed_database():
    print("Ensuring database tables in Supabase / PostgreSQL...")
    Base.metadata.create_all(bind=engine)

    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            conn.execute(text("TRUNCATE TABLE audit_logs, recommendations, invoices, tickets, usage_records, customers, resources, nodes, users CASCADE;"))
            conn.commit()
    except Exception as e:
        print(f"Note on truncate: {e}")

    db = SessionLocal()
    try:
        print("1. Seeding Universal Demo Users & Client Viewer Account (PMRG Solution)...")
        users_data = [
            ("executive@pmrg.in", "admin123", "Rajesh Singhania", "Executive"),
            ("noc@pmrg.in", "admin123", "Vikram Rathore", "NOC"),
            ("care@pmrg.in", "admin123", "Pooja Sharma", "Care"),
            ("revenue@pmrg.in", "admin123", "Anand Kulkarni", "Revenue"),
            ("admin@pmrg.in", "admin123", "PMRG AI Administrator", "Admin"),
            ("client@pmrgsolution.com", "Client@pmrg123", "Client Stakeholder", "Viewer"),
        ]

        users = []
        for email, pwd, name, role in users_data:
            existing = db.query(User).filter(User.email == email.lower().strip()).first()
            if existing:
                existing.hashed_password = hash_password(pwd)
                existing.full_name = name
                existing.role = role
                existing.is_active = True
                u = existing
            else:
                u = User(
                    email=email.lower().strip(),
                    hashed_password=hash_password(pwd),
                    full_name=name,
                    role=role,
                    is_active=True,
                    created_at=datetime.utcnow() - timedelta(days=180)
                )
                db.add(u)
            users.append(u)
        db.commit()

        # Seed Mumbai Market
        print("\n--- Seeding Market: Mumbai ---")
        seed_market_dataset(db, market_id="mumbai", users=users)

        # Seed Kolkata Market
        print("\n--- Seeding Market: Kolkata ---")
        seed_market_dataset(db, market_id="kolkata", users=users)

        print("\n[SUCCESS] Both Mumbai and Kolkata market datasets successfully seeded into the database!")

    finally:
        db.close()


def seed_market_dataset(db, market_id: str, users: list):
    is_mumbai = (market_id == "mumbai")

    if is_mumbai:
        nodes_config = [
            ("OLT-BND-01", "Bandra Central OLT-1", "Bandra West", "OLT", 92.5, 3.8, 48.2, -29.8, 14, 38.0, "Critical"),
            ("OLT-AND-03", "Andheri MIDC Hub", "Andheri East", "OLT", 88.0, 4.2, 54.0, -28.1, 11, 44.5, "Degraded"),
            ("FDH-MAL-02", "Malad Link Rd FDH", "Malad West", "FDH", 81.0, 2.5, 35.0, -27.6, 7, 58.0, "Degraded"),
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
        first_names = [
            "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Ayaan", "Krishna", "Ishaan",
            "Shaurya", "Atharva", "Kabir", "Rudra", "Aryan", "Ananya", "Diya", "Gauri", "Myra", "Sara",
            "Aadhya", "Pari", "Saanvi", "Avani", "Isha", "Kavya", "Tara", "Riya", "Meera", "Zara",
            "Vikram", "Sunil", "Ramesh", "Deepak", "Sanjay", "Ketan", "Nilesh", "Prakash", "Manish", "Rahul"
        ]
        last_names = [
            "Sharma", "Verma", "Patel", "Mehta", "Shah", "Joshi", "Deshmukh", "Kulkarni", "Patil", "Sawant",
            "Shinde", "Pawar", "Bhosale", "Naik", "Shetty", "Kamath", "Pai", "Iyer", "Menon", "Nair",
            "Gupta", "Agarwal", "Jain", "Bansal", "Kapoor", "Malhotra", "Chopra", "Khanna", "Singhania", "Trivedi"
        ]
        corp_names = [
            "Apex Infotech Solutions", "Zenith Media Works", "Nexus FinTech Labs", "Bharat Digital Logistics",
            "Mumbai Traders Corp", "Quantify Analytics India", "Matrix Coworking BKC", "Blue Horizon Studios",
            "Reliance Retail Partner", "Kothari Diamond Exports", "Orbit Health Systems", "Vanguard Law Associates"
        ]
    else:
        # Kolkata Market
        nodes_config = [
            ("OLT-SLK-01", "Salt Lake Sector V Hub", "Salt Lake Sector V", "OLT", 91.5, 3.6, 46.0, -29.6, 13, 39.0, "Critical"),
            ("OLT-PST-01", "Park Street Central OLT", "Park Street", "OLT", 87.0, 4.0, 52.0, -28.3, 10, 46.0, "Degraded"),
            ("FDH-NWT-01", "New Town Action Area 1 FDH", "New Town", "FDH", 80.0, 2.4, 34.0, -27.4, 6, 60.0, "Degraded"),
            ("OLT-BLY-01", "Ballygunge Circular OLT", "Ballygunge", "OLT", 60.0, 0.1, 11.5, -20.1, 0, 98.0, "Healthy"),
            ("FDH-HOW-01", "Howrah Station Rail Hub", "Howrah", "FDH", 69.0, 0.4, 16.0, -21.5, 1, 93.0, "Healthy"),
            ("OLT-JAD-01", "Jadavpur University OLT", "Jadavpur", "OLT", 64.0, 0.2, 13.0, -20.5, 0, 97.0, "Healthy"),
            ("FDH-BEH-01", "Behala Chowrasta Hub", "Behala", "FDH", 73.0, 0.7, 21.0, -23.0, 2, 88.0, "Healthy"),
            ("OLT-DUM-01", "Dum Dum Airport Core OLT", "Dum Dum", "OLT", 70.0, 0.5, 18.0, -22.0, 1, 92.0, "Healthy"),
            ("FDH-ALI-01", "Alipore Command Hub", "Alipore", "FDH", 51.0, 0.1, 10.5, -19.2, 0, 99.0, "Healthy"),
            ("OLT-GAR-01", "Gariahat Retail Hub OLT", "Gariahat", "OLT", 66.0, 0.3, 15.0, -21.0, 1, 95.0, "Healthy"),
            ("FDH-RAJ-01", "Rajarhat Expressway FDH", "Rajarhat", "FDH", 58.0, 0.2, 12.0, -20.0, 0, 98.0, "Healthy"),
            ("OLT-SHY-01", "Shyambazar Five-Point OLT", "Shyambazar", "OLT", 72.0, 0.6, 20.0, -22.8, 2, 89.0, "Healthy"),
        ]
        first_names = [
            "Aritra", "Sourav", "Debolina", "Subhashish", "Poulomi", "Indranil", "Sagnik", "Ananya", "Riddhi", "Soham",
            "Abhishek", "Prosenjit", "Swarup", "Moumita", "Shreya", "Tanushree", "Kaushik", "Sayani", "Anupam", "Barnali",
            "Dipankar", "Buddhadeb", "Sandip", "Soma", "Pradip", "Madhumita", "Debashis", "Rituparna", "Subrata", "Parambrata"
        ]
        last_names = [
            "Banerjee", "Mukherjee", "Chatterjee", "Ghosh", "Sen", "Roy", "Das", "Dutta", "Bhattacharya", "Chakraborty",
            "Ganguly", "Bose", "Mitra", "Sarkar", "Majumdar", "Basu", "Pal", "Dey", "Kundu", "Nath", "Mondal", "Pramanik"
        ]
        corp_names = [
            "Bengal Silicon FinTech Labs", "Sector V IT Park Solutions", "Park Street Media Works", "Howrah Digital Logistics",
            "New Town Analytics Corp", "Calcutta Coworking Salt Lake", "Trident Bengal Maritime", "Kolkata Jute & Tea Exports",
            "Apollo Gleneagles Health Systems", "Heritage Law Partners Alipore", "Blue Ribbon Digital Kolkata", "Eastern Rail Logistics"
        ]

    # 1. Seed Nodes
    print(f"Seeding {len(nodes_config)} nodes for {market_id}...")
    nodes = []
    for code, name, area, ntype, util, loss, lat, opt, alarms, health, status in nodes_config:
        node = Node(
            market_id=market_id,
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

    # 1b. Seed Regional & Internal Team Resources
    print(f"Seeding regional field resources & internal team for {market_id}...")
    if is_mumbai:
        resources_config = [
            ("Rohan Mhatre", "rohan.m@pmrg.in", "+91 98201 10011", "FIELD", "Bandra West"),
            ("Suraj Ghadge", "suraj.g@pmrg.in", "+91 98201 10012", "FIELD", "Andheri East"),
            ("Nitin Shinde", "nitin.s@pmrg.in", "+91 98201 10013", "FIELD", "Malad West"),
            ("Deepak Sawant", "deepak.s@pmrg.in", "+91 98201 10014", "FIELD", "BKC"),
            ("Kiran More", "kiran.m@pmrg.in", "+91 98201 10015", "FIELD", "Powai"),
            ("Ajay Pawar", "ajay.p@pmrg.in", "+91 98201 10016", "FIELD", "Lower Parel"),
            ("Prashant Jadhav", "prashant.j@pmrg.in", "+91 98201 10017", "FIELD", "Dadar"),
            ("Santosh Kadam", "santosh.k@pmrg.in", "+91 98201 10018", "FIELD", "Thane West"),
            ("Sachin Rane", "sachin.r@pmrg.in", "+91 98201 10019", "FIELD", "Worli"),
            ("Manish Tambe", "manish.t@pmrg.in", "+91 98201 10020", "FIELD", "Borivali"),
            ("Vinod Gaikwad", "vinod.g@pmrg.in", "+91 98201 10021", "FIELD", "Juhu"),
            ("Ganesh Surve", "ganesh.s@pmrg.in", "+91 98201 10022", "FIELD", "Ghatkopar"),
            ("Amitav Roy (Lead NOC Tech)", "amitav.noc@pmrg.in", "+91 98201 20001", "INTERNAL", "Internal Operations"),
            ("Priyanka Sen (Core BSS Systems)", "priyanka.bss@pmrg.in", "+91 98201 20002", "INTERNAL", "Internal Operations"),
            ("Tanmay Varma (Platform Ops)", "tanmay.infra@pmrg.in", "+91 98201 20003", "INTERNAL", "Internal Operations"),
        ]
    else:
        resources_config = [
            ("Subir Das", "subir.d@pmrg.in", "+91 98301 10011", "FIELD", "Salt Lake Sector V"),
            ("Anirban Ghosh", "anirban.g@pmrg.in", "+91 98301 10012", "FIELD", "Park Street"),
            ("Debojyoti Paul", "debojyoti.p@pmrg.in", "+91 98301 10013", "FIELD", "New Town"),
            ("Sujit Mukherjee", "sujit.m@pmrg.in", "+91 98301 10014", "FIELD", "Ballygunge"),
            ("Biplab Mondal", "biplab.m@pmrg.in", "+91 98301 10015", "FIELD", "Howrah"),
            ("Sanjay Chakraborty", "sanjay.c@pmrg.in", "+91 98301 10016", "FIELD", "Jadavpur"),
            ("Prasenjit Sen", "prasenjit.s@pmrg.in", "+91 98301 10017", "FIELD", "Behala"),
            ("Tapas Sarkar", "tapas.s@pmrg.in", "+91 98301 10018", "FIELD", "Dum Dum"),
            ("Sandip Majumdar", "sandip.m@pmrg.in", "+91 98301 10019", "FIELD", "Alipore"),
            ("Abhijit Dutta", "abhijit.d@pmrg.in", "+91 98301 10020", "FIELD", "Gariahat"),
            ("Prosun Roy", "prosun.r@pmrg.in", "+91 98301 10021", "FIELD", "Rajarhat"),
            ("Soumitra Bose", "soumitra.b@pmrg.in", "+91 98301 10022", "FIELD", "Shyambazar"),
            ("Ritwick Banerjee (Eastern NOC Lead)", "ritwick.noc@pmrg.in", "+91 98301 20001", "INTERNAL", "Internal Operations"),
            ("Sharmila Mitra (Core Systems Specialist)", "sharmila.bss@pmrg.in", "+91 98301 20002", "INTERNAL", "Internal Operations"),
            ("Indranil Guha (Switch Infrastructure)", "indranil.infra@pmrg.in", "+91 98301 20003", "INTERNAL", "Internal Operations"),
        ]

    resources = []
    res_by_region = {}
    internal_resources = []
    for r_name, r_email, r_phone, r_type, r_region in resources_config:
        res = Resource(
            market_id=market_id,
            name=r_name,
            email=r_email,
            phone=r_phone,
            resource_type=r_type,
            region=r_region,
            status="Available",
            active_tickets_count=0,
            max_capacity=10 if r_type == "INTERNAL" else 8,
            created_at=datetime.utcnow() - timedelta(days=90)
        )
        db.add(res)
        resources.append(res)
        if r_type == "INTERNAL":
            internal_resources.append(res)
        else:
            res_by_region[r_region.lower()] = res
    db.commit()

    prepaid_plans = [
        ("Hero Unlimited 1.5GB/Day (28d)", 299.0, 28, 1.5, 345.0, "Prepaid - Daily Unlimited"),
        ("Super 5G 2GB/Day + Hotstar (28d)", 349.0, 28, 2.0, 395.0, "Prepaid - 5G High Speed"),
        ("Voice & Value 1GB/Day (28d)", 239.0, 28, 1.0, 265.0, "Prepaid - Value Voice"),
        ("True 5G Unlimited 2GB/Day (56d)", 579.0, 56, 2.0, 335.0, "Prepaid - 5G High Speed"),
        ("Super Saver 1.5GB/Day (84d)", 719.0, 84, 1.5, 295.0, "Prepaid - Long-term Bundle"),
        ("Cricket & OTT 2GB/Day + Prime (84d)", 859.0, 84, 2.0, 340.0, "Prepaid - Long-term Bundle"),
        ("Annual 5G All-Access 2.5GB/Day (365d)", 2999.0, 365, 2.5, 260.0, "Prepaid - Long-term Bundle"),
    ]

    postpaid_plans = [
        ("Postpaid Individual Infinity 75GB", 499.0, 30, 75.0, 535.0, "Postpaid - Individual Infinity"),
        ("Postpaid Family Plus (3 SIMs) 150GB", 999.0, 30, 150.0, 1180.0, "Postpaid - Family Plus"),
        ("Postpaid Enterprise Corporate ILL 500M", 12000.0, 30, 1000.0, 12000.0, "Postpaid - Enterprise ILL"),
        ("Postpaid Premium Dedicated 1Gbps", 22000.0, 30, 2500.0, 22000.0, "Postpaid - Enterprise ILL"),
    ]

    # 2. Seed Customers (300 subscribers per market)
    print(f"Seeding 300 subscribers for {market_id}...")
    customers = []
    node_map = {n.area: n for n in nodes}
    areas = [n.area for n in nodes]

    cust_prefix = "MUM" if is_mumbai else "KOL"

    for i in range(1, 301):
        is_prepaid = (i <= 210)
        area = random.choice(areas)
        node = node_map[area]
        is_node_degraded = (node.status in ['Degraded', 'Critical'])

        if is_prepaid:
            fn = random.choice(first_names)
            ln = random.choice(last_names)
            c_name = f"{fn} {ln}"
            c_email = f"{fn.lower()}.{ln.lower()}{i%500}@{market_id}isp.in"
            plan, plan_price, validity, daily_quota, _, segment = random.choice(prepaid_plans)
            quota_monthly = daily_quota * 30.0
            customer_type = "Prepaid"
            payment_method = random.choice(["UPI (PhonePe)", "UPI (Google Pay)", "UPI (Paytm)", "Instant UPI QR"])

            booster_spend = random.choice([0.0, 19.0, 29.0, 61.0]) if random.random() < 0.40 else 0.0
            actual_arpu = round((plan_price / (validity / 30.0)) + booster_spend, 1)
            revenue_30d = actual_arpu

            days_left = random.randint(-5, validity)
            validity_status = "Active"
            if days_left < 0:
                validity_status = "Expired"
            elif days_left <= 3:
                validity_status = "Expiring Soon"
            elif days_left <= 7:
                validity_status = "Grace Period"
        else:
            # Postpaid
            is_corp = (random.random() < 0.15)
            if is_corp:
                c_name = f"{random.choice(corp_names)} #{i%25 + 1}"
                c_email = f"billing.{market_id}@{c_name.split()[0].lower()}corp.in"
                plan, plan_price, validity, quota_monthly, _, segment = random.choice(postpaid_plans[2:])
                actual_arpu = plan_price
                revenue_30d = plan_price
            else:
                fn = random.choice(first_names)
                ln = random.choice(last_names)
                c_name = f"{fn} {ln}"
                c_email = f"{fn.lower()}.{ln.lower()}{i%500}@{market_id}isp.in"
                plan, plan_price, validity, quota_monthly, _, segment = random.choice(postpaid_plans[:2])
                actual_arpu = round(plan_price * (1.0 + random.uniform(0.05, 0.25)), 1)
                revenue_30d = actual_arpu

            customer_type = "Postpaid"
            payment_method = random.choice(["UPI Autopay", "Net Banking (HDFC/ICICI)", "Corporate ACH Debit", "Credit Card"])
            days_left = random.randint(3, 28)
            validity_status = "Active"

        tenure = random.randint(1, 48)
        signup_dt = datetime.utcnow() - timedelta(days=tenure * 30 + random.randint(1, 28))
        last_recharge_dt = datetime.utcnow() - timedelta(days=random.randint(1, 25))

        if is_node_degraded and random.random() < 0.45:
            c_status = "At-Risk"
            nps = random.randint(1, 5)
            stage = "Complaint"
        elif days_left <= 3:
            c_status = "At-Risk" if random.random() < 0.35 else "Active"
            nps = random.randint(4, 7)
            stage = "Renewal"
        else:
            c_status = "Active"
            nps = random.randint(7, 10)
            stage = random.choice(["Use", "Use", "Use", "Renewal"])

        cust = Customer(
            market_id=market_id,
            customer_code=f"SUB-{cust_prefix}-{100000 + i}",
            name=c_name,
            email=c_email,
            phone=f"+91 {9800000000 + (1 if is_mumbai else 2)*1000000 + i}",
            locality=area,
            segment=segment,
            customer_type=customer_type,
            plan_name=plan,
            plan_price=plan_price,
            revenue_30d=revenue_30d,
            actual_arpu=actual_arpu,
            arpu=actual_arpu,
            recharge_validity_days=validity,
            days_to_expiry=days_left,
            validity_status=validity_status,
            daily_data_quota_gb=daily_quota if is_prepaid else round(quota_monthly / 30.0, 1),
            daily_data_used_gb=round(random.uniform(0.4, 2.8), 2),
            last_recharge_date=last_recharge_dt,
            last_recharge_amount=plan_price,
            payment_method=payment_method,
            tenure_months=tenure,
            signup_date=signup_dt,
            status=c_status,
            node_id=node.id,
            current_stage=stage,
            nps_score=nps
        )
        db.add(cust)
        customers.append((cust, quota_monthly, is_node_degraded))
    db.commit()

    # 3. Seed Usage Records
    print(f"Seeding usage telemetry for {market_id}...")
    for cust, quota_m, node_deg in customers:
        if cust.status == "At-Risk":
            trend = "Declining"
            trend_pct = -round(random.uniform(25.0, 55.0), 1)
            used_gb = round(quota_m * random.uniform(0.3, 0.65), 1)
        else:
            trend = random.choice(["Stable", "Growing"])
            trend_pct = round(random.uniform(2.0, 20.0), 1) if trend == "Growing" else round(random.uniform(-5.0, 5.0), 1)
            used_gb = round(quota_m * random.uniform(0.75, 1.05), 1)

        ur = UsageRecord(
            market_id=market_id,
            customer_id=cust.id,
            monthly_gb=used_gb,
            quota_gb=quota_m,
            usage_trend=trend,
            trend_pct=trend_pct,
            ott_streaming_flag=random.choice([True, True, False]),
            gaming_flag=random.choice([False, False, True]),
            last_active_at=datetime.utcnow() - timedelta(minutes=random.randint(5, 60))
        )
        db.add(ur)
    db.commit()

    # 4. Seed Support Tickets
    print(f"Seeding service tickets & automated dispatch rules for {market_id}...")
    tickets = []
    for idx, (cust, _, node_deg) in enumerate(customers):
        if cust.status == "At-Risk" or node_deg or random.random() < 0.10:
            category = random.choice(["Outage", "Speed", "Speed", "Hardware", "Billing"])
            # Map into P1, P2, P3, P4
            if category == "Outage" and node_deg:
                priority = "P1"
            elif category == "Speed" and node_deg:
                priority = "P2"
            elif category in ["Speed", "Hardware"]:
                priority = "P3"
            else:
                priority = "P4"

            repeat = (random.random() < 0.35)
            field_res = res_by_region.get(cust.locality.lower()) or (resources[0] if resources else None)

            # Balanced Workload: cap active tickets per engineer at 3, mark remaining as Resolved
            res_active_count = field_res.active_tickets_count if field_res else 0
            is_resolved = (res_active_count >= 3)

            if is_resolved:
                t_status = "Resolved"
                appr_status = "APPROVED"
                assigned_res_id = field_res.id if field_res else None
                assigned_dt = datetime.utcnow() - timedelta(hours=random.randint(12, 48))
                resolved_dt = assigned_dt + timedelta(hours=random.randint(1, 4))
                appr_by_id = users[1].id if len(users) > 1 else None
                appr_dt = assigned_dt
                appr_notes = "Resolved and line calibrated"
                sla_dt = assigned_dt + timedelta(hours=random.randint(4, 12))
                ai_action = f"Resolved by {field_res.name if field_res else 'field team'}"
            elif priority in ["P3", "P4"]:
                # For POC demonstration: Seed a clean batch of P3/P4 incidents in Open / Pending Dispatch
                # so the toggle can be switched ON to watch them immediately auto-dispatch!
                is_unassigned_demo = (len(tickets) % 4 == 0)
                if is_unassigned_demo:
                    t_status = "Open"
                    appr_status = "NOT_REQUIRED"
                    assigned_res_id = None
                    assigned_dt = None
                    resolved_dt = None
                    appr_by_id = None
                    appr_dt = None
                    appr_notes = None
                    sla_dt = datetime.utcnow() + timedelta(hours=8, minutes=random.randint(5, 30))
                    ai_action = f"Pending Dispatch ({priority} Auto-Dispatch Paused)"
                else:
                    t_status = "Assigned"
                    appr_status = "NOT_REQUIRED"
                    assigned_res_id = field_res.id if field_res else None
                    assigned_dt = datetime.utcnow() - timedelta(hours=random.randint(1, 12))
                    resolved_dt = None
                    appr_by_id = None
                    appr_dt = None
                    appr_notes = None
                    sla_offsets = [1.5, 3.5, 6.0]
                    offset_hrs = sla_offsets[res_active_count % len(sla_offsets)]
                    sla_dt = datetime.utcnow() + timedelta(hours=offset_hrs, minutes=random.randint(5, 30))
                    if field_res:
                        field_res.active_tickets_count += 1
                    ai_action = f"Auto-assigned ({priority} Zero-Touch) to {field_res.name if field_res else 'field engineer'}"[:100]
            else:
                # P1/P2: Half pending approval, half approved & auto-assigned
                is_pending = (random.random() < 0.5)
                resolved_dt = None
                if is_pending:
                    t_status = "Pending Approval"
                    appr_status = "PENDING_APPROVAL"
                    assigned_res_id = None
                    assigned_dt = None
                    appr_by_id = None
                    appr_dt = None
                    appr_notes = None
                    sla_dt = datetime.utcnow() + timedelta(hours=random.randint(2, 4))
                    ai_action = f"High-Impact Incident ({priority}): Managerial approval required before automated field dispatch"
                else:
                    t_status = "Assigned"
                    appr_status = "APPROVED"
                    assigned_res_id = field_res.id if field_res else None
                    assigned_dt = datetime.utcnow() - timedelta(hours=random.randint(1, 6))
                    appr_by_id = users[1].id if len(users) > 1 else None
                    appr_dt = assigned_dt
                    appr_notes = f"Approved by {users[1].full_name if len(users) > 1 else 'NOC'} for immediate regional dispatch"
                    sla_dt = datetime.utcnow() + timedelta(hours=random.randint(1, 3), minutes=random.randint(10, 45))
                    if field_res:
                        field_res.active_tickets_count += 1
                    ai_action = f"Approved by {users[1].full_name if len(users) > 1 else 'NOC'} & auto-dispatched to {field_res.name if field_res else 'field engineer'}"

            t = Ticket(
                market_id=market_id,
                ticket_code=f"TCK-{cust_prefix}-{1000 + len(tickets) + 1}",
                source="CUSTOMER",
                region=cust.locality,
                customer_id=cust.id,
                node_id=cust.node_id,
                category=category,
                priority=priority,
                status=t_status,
                created_at=datetime.utcnow() - timedelta(hours=random.randint(1, 48)),
                resolved_at=resolved_dt,
                repeat_flag=repeat,
                description=f"Subscriber in {cust.locality} reports intermittent {category.lower()} on node {node_map[cust.locality].node_code}.",
                ai_triage_action=ai_action,
                sla_deadline=sla_dt,
                assigned_resource_id=assigned_res_id,
                assigned_at=assigned_dt,
                approval_status=appr_status,
                approved_by_id=appr_by_id,
                approved_at=appr_dt,
                approval_notes=appr_notes
            )
            db.add(t)
            tickets.append(t)

    # Seed Internal Team Tickets
    internal_ticket_templates = [
        ("Core Network", "P2", "Upstream DWDM link flapping between aggregation switch and core router"),
        ("Optical Backbone", "P1", "OTDR telemetry indicates micro-bend attenuation (>4.2 dB) on primary feeder trunk"),
        ("BSS Mediation", "P3", "Billing mediation webhook retry buffer approaching 80% watermark"),
        ("RADIUS Gateway", "P2", "Subscriber AAA authentication latency elevated above 450ms"),
        ("Server Infrastructure", "P3", "Log rotation cron job failed on syslog ingestor pool node 2")
    ]
    for idx_int, (cat, prio, desc) in enumerate(internal_ticket_templates):
        int_res = internal_resources[idx_int % len(internal_resources)] if internal_resources else None
        if int_res and int_res.active_tickets_count < 3:
            int_res.active_tickets_count += 1
            t_int_status = "Assigned"
            t_int_resolved = None
            sla_int = datetime.utcnow() + timedelta(hours=2 + idx_int * 2)
        else:
            t_int_status = "Resolved"
            t_int_resolved = datetime.utcnow() - timedelta(hours=random.randint(6, 24))
            sla_int = datetime.utcnow() - timedelta(hours=2)

        t_int = Ticket(
            market_id=market_id,
            ticket_code=f"TCK-{cust_prefix}-INT-{len(tickets) + 1}",
            source="INTERNAL",
            region="Internal Operations",
            customer_id=None,
            node_id=nodes[0].id if nodes else None,
            category=cat,
            priority=prio,
            status=t_int_status,
            created_at=datetime.utcnow() - timedelta(hours=random.randint(2, 24)),
            resolved_at=t_int_resolved,
            repeat_flag=False,
            description=desc,
            ai_triage_action=f"Auto-assigned to Internal NOC Team: {int_res.name if int_res else 'Core NOC'}",
            sla_deadline=sla_int,
            assigned_resource_id=int_res.id if int_res else None,
            assigned_at=datetime.utcnow() - timedelta(hours=random.randint(1, 12)),
            approval_status="NOT_REQUIRED"
        )
        db.add(t_int)
        tickets.append(t_int)

    db.commit()

    # 5. Seed Invoices & Revenue Anomalies
    print(f"Seeding invoices and billing anomalies for {market_id}...")
    invoices = []
    for idx, (cust, _, _) in enumerate(customers):
        has_anomaly = (random.random() < 0.08)
        anomaly_type = None
        leakage_amt = 0.0
        billed = cust.plan_price
        expected = cust.plan_price

        if has_anomaly:
            if cust.customer_type == "Prepaid":
                a_choice = random.choice(["Expired Validity OTT Leakage", "Zero-Rated APN Leakage", "Recharge Webhook Drop"])
                anomaly_type = a_choice
                if a_choice == "Expired Validity OTT Leakage":
                    leakage_amt = 299.0
                elif a_choice == "Zero-Rated APN Leakage":
                    leakage_amt = 450.0
                else:
                    leakage_amt = 719.0
            else:
                a_choice = random.choice(["Plan Mismatch", "Duplicate Credit", "Unbilled Usage", "Dunning Failure"])
                anomaly_type = a_choice
                if a_choice == "Plan Mismatch":
                    billed = 499.0
                    expected = 999.0
                    leakage_amt = 500.0
                elif a_choice == "Duplicate Credit":
                    leakage_amt = 400.0
                elif a_choice == "Unbilled Usage":
                    expected = cust.plan_price + 500.0
                    leakage_amt = 500.0
                else:
                    leakage_amt = cust.plan_price

        inv = Invoice(
            market_id=market_id,
            invoice_code=f"INV-{cust_prefix}-{100000 + idx}",
            customer_id=cust.id,
            plan_name=cust.plan_name,
            transaction_type="Pack Recharge" if cust.customer_type == "Prepaid" else "Monthly Postpaid Bill",
            payment_method=cust.payment_method,
            billed_amount=billed,
            expected_amount=expected,
            due_date=datetime.utcnow() - timedelta(days=random.randint(2, 25)),
            paid_date=datetime.utcnow() - timedelta(days=random.randint(1, 10)) if not has_anomaly else None,
            status="Paid" if not has_anomaly else "Unpaid",
            waiver_amount=400.0 if anomaly_type == "Duplicate Credit" else 0.0,
            renewal_date=datetime.utcnow() + timedelta(days=max(1, cust.days_to_expiry)),
            anomaly_flag=has_anomaly,
            anomaly_type=anomaly_type,
            leakage_amount=leakage_amt,
            created_at=datetime.utcnow() - timedelta(days=random.randint(5, 30))
        )
        db.add(inv)
        invoices.append(inv)
    db.commit()

    # 6. Seed Recommendations across 5 modules
    print(f"Seeding AI recommendations for {market_id}...")
    crit_node = next((n for n in nodes if n.status in ["Critical", "Degraded"]), nodes[0])
    at_risk_c = next((c for c, _, _ in customers if c.status == "At-Risk"), customers[0][0])
    renewal_c = next((c for c, _, _ in customers if c.current_stage == "Renewal"), customers[0][0])
    open_t = next((t for t in tickets if t.status in ["Open", "Assigned", "Pending Approval"]), tickets[0] if tickets else None)
    anom_inv = next((i for i in invoices if i.anomaly_flag), invoices[0])

    # Rec 1: Assurance
    r1 = Recommendation(
        market_id=market_id,
        source_module="Predictive Service Assurance",
        target_entity_type="Node",
        target_entity_id=crit_node.id,
        target_entity_label=f"{crit_node.node_name} ({crit_node.area})",
        title=f"Proactive Field Dispatch - {crit_node.node_name}",
        description=f"AI detected optical attenuation ({crit_node.optical_power_dbm} dBm). Recommend field technician dispatch for splice calibration.",
        recommended_action=f"Dispatch field technician to {crit_node.node_name} for optical line calibration and OTDR trace test.",
        confidence_score=0.96,
        status="PENDING",
        created_at=datetime.utcnow() - timedelta(hours=2)
    )
    db.add(r1)

    # Rec 2: Churn
    r2 = Recommendation(
        market_id=market_id,
        source_module="Churn Prediction & Retention AI",
        target_entity_type="Customer",
        target_entity_id=at_risk_c.id,
        target_entity_label=f"{at_risk_c.name} ({at_risk_c.customer_code})",
        title=f"Targeted Retention Save Offer - {at_risk_c.name}",
        description=f"Subscriber churn risk scored high due to repeated incident complaints and bandwidth drops.",
        recommended_action="Deliver Instant 5G Data Booster Top-up & 1-Click WhatsApp Renewal Cashback Link.",
        confidence_score=0.92,
        status="PENDING",
        created_at=datetime.utcnow() - timedelta(hours=3)
    )
    db.add(r2)

    # Rec 3: Journeys
    r3 = Recommendation(
        market_id=market_id,
        source_module="Intelligent Customer Journeys",
        target_entity_type="Customer",
        target_entity_id=renewal_c.id,
        target_entity_label=f"{renewal_c.name} ({renewal_c.customer_code})",
        title=f"Next-Best-Action - Stage: Renewal ({renewal_c.name})",
        description=f"Pack expiring in {renewal_c.days_to_expiry} days. Automated recharge incentive proposed.",
        recommended_action=f"Deliver 1-Click WhatsApp UPI Recharge Link with 5GB Bonus Voucher ({renewal_c.plan_name}).",
        confidence_score=0.93,
        status="PENDING",
        created_at=datetime.utcnow() - timedelta(hours=4)
    )
    db.add(r3)

    # Rec 4: Orchestration
    r4 = Recommendation(
        market_id=market_id,
        source_module="AI-driven OSS/BSS Orchestration",
        target_entity_type="Ticket",
        target_entity_id=open_t.id,
        target_entity_label=f"Ticket {open_t.ticket_code} ({open_t.category})",
        title=f"Automated Profile Re-provisioning - {open_t.ticket_code}",
        description="AI diagnosed BRAS profile desync and packet loss. Recommending remote TR-069 QoS bandwidth sync.",
        recommended_action="Execute remote ONT reset and BRAS QoS bandwidth profile re-synchronization.",
        confidence_score=0.94,
        status="PENDING",
        created_at=datetime.utcnow() - timedelta(hours=5)
    )
    db.add(r4)

    # Rec 5: Revenue
    r5 = Recommendation(
        market_id=market_id,
        source_module="Revenue Assurance & Leakage Analytics",
        target_entity_type="Invoice",
        target_entity_id=anom_inv.id,
        target_entity_label=f"Transaction {anom_inv.invoice_code} ({anom_inv.anomaly_type})",
        title=f"Billing Remediation - INR {anom_inv.leakage_amount:.0f} Leakage",
        description=f"Detected {anom_inv.anomaly_type}. Recommending ledger reconciliation and profile alignment.",
        recommended_action=f"Synchronize billing catalog and issue corrective ledger entry for INR {anom_inv.leakage_amount:.0f}.",
        confidence_score=0.98,
        status="PENDING",
        created_at=datetime.utcnow() - timedelta(hours=6)
    )
    db.add(r5)

    # Rec 6: Automatic Ticketing & Regional Dispatch (sync with PENDING_APPROVAL tickets)
    pending_tickets = [t for t in tickets if t.approval_status == "PENDING_APPROVAL"]
    for pt in pending_tickets[:3]:
        r_tick = Recommendation(
            market_id=market_id,
            source_module="Automatic Ticketing & Regional Dispatch",
            target_entity_type="Ticket",
            target_entity_id=pt.id,
            target_entity_label=f"Ticket {pt.ticket_code} ({pt.priority} - {pt.category})",
            title=f"Dispatch Approval Required: {pt.ticket_code} ({pt.priority})",
            description=f"High-impact {pt.priority} incident logged in {pt.region}. Requires supervisory sign-off before regional field dispatch.",
            recommended_action=f"Approve automated field dispatch to optimal regional resource in {pt.region}.",
            confidence_score=0.96,
            status="PENDING",
            created_at=datetime.utcnow() - timedelta(hours=3)
        )
        db.add(r_tick)

    # 7. Seed Audit Logs
    noc_user = next(u for u in users if u.role == "NOC")
    rev_user = next(u for u in users if u.role == "Revenue")
    care_user = next(u for u in users if u.role == "Care")

    city_label = "Mumbai" if is_mumbai else "Kolkata"
    audits_data = [
        ("AI-driven OSS/BSS Orchestration", "Execute remote ONT reset and BRAS QoS sync.", noc_user, 0.94, timedelta(hours=8)),
        ("Revenue Assurance & Leakage Analytics", f"Adjusted billing mismatch for {city_label} enterprise account.", rev_user, 0.98, timedelta(hours=14)),
        ("Churn Prediction & Retention AI", "Approved 15% retention concession and validity extension.", care_user, 0.91, timedelta(days=1)),
        ("Intelligent Customer Journeys", "Sent WhatsApp 1-Click KYC & Installation Slot Scheduler.", care_user, 0.93, timedelta(days=2)),
        ("Predictive Service Assurance", f"Dispatched Field Splicing Technician to {crit_node.node_name}.", noc_user, 0.96, timedelta(days=3))
    ]

    for module, action, usr, conf, time_offset in audits_data:
        audit = AuditLog(
            market_id=market_id,
            recommendation_id=None,
            source_module=module,
            action_taken=action,
            decision="APPROVED",
            user_id=usr.id,
            user_name=usr.full_name,
            user_role=usr.role,
            confidence_score=conf,
            original_signals={"market": market_id, "status": "Audited"},
            execution_result={"status": "Executed", "recorded_in_ledger": True},
            timestamp=datetime.utcnow() - time_offset
        )
        db.add(audit)

    db.commit()
    print(f"[COMPLETED] Seeded {market_id} dataset.")

if __name__ == "__main__":
    seed_database()
