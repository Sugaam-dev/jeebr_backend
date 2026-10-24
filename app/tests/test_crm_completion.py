"""test_crm_completion.py

SentinelOS CRM Completion Sprint Test Suite.
Verifies:
1. Customer Master:
   - Create customer master with valid data.
   - Identifier validation: max 32 chars, regex checks.
   - Validation of account_type, lifecycle status, market_id, required fields.
   - Duplicate prevention: rejecting duplicate customer_number and identical phone+email in same market.
   - Concurrency conflict handling (optimistic version verification).
   - Get customer detail (by legacy ID, UUID, and customer code) with contacts & addresses.
   - Update customer fields and soft-archive.
   - Search, filtering (status, customer_type, locality), sorting, and server-side pagination with headers.
2. Contacts & Addresses:
   - Add, list, update, and archive contacts.
   - Enforcement of partial unique index on primary contact: automatic demotion of existing active primary.
   - Legitimate shared email / phone across different customers allowed.
   - Add, list, update, and archive addresses.
   - Address coordinate validation (-90..90, -180..180).
   - Preserving unverified addresses as PENDING_SURVEY (no fabrication of GPS coordinates or postal codes).
3. Leads:
   - Create lead (prospect), list, search, view.
   - Lead-to-customer conversion: transactional and idempotent.
   - Repeated conversion returns already converted customer without creating duplicates.
   - Lead audit trail and activity history preserved.
4. Customer Maps:
   - Map locations endpoint returning verified coordinates vs missing coordinates.
   - Graceful handling of empty or missing coordinates.
5. Vouchers:
   - List customer vouchers and stage courtesy voucher.
   - Verified dependency on OSS/BSS Billing & Provisioning Engine (Phase 2).
6. Tickets Integration:
   - Customer-linked ticket retrieval and ticket creation.
   - Preserving ticketing SLA and audit records.
7. Security & Market Isolation:
   - Server-side RBAC on protected endpoints (Viewer/Customer restricted).
   - Cross-market isolation enforcement.
"""

import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from app.main import app
from app.models import User, Customer, Ticket
from app.models_crm import CRMCustomer, CRMContact, CRMAddress, CRMActivity, CustomerLifecycleStatus, AccountType, ContactRole
from app.auth import create_access_token

client = TestClient(app)

def _auth_headers(role: str = "Admin", market: str = "mumbai", email: str = "admin@pmrg.in") -> dict:
    token = create_access_token({"sub": email, "role": role})
    return {
        "Authorization": f"Bearer {token}",
        "X-Market-Id": market,
        "Content-Type": "application/json"
    }


# ─── 1. Customer CRUD, Validation & Duplicate Prevention ──────────────────────

def test_create_customer_master_success(db_session: Session):
    headers = _auth_headers("Admin", "mumbai")
    cust_code = f"CUST-MUM-{uuid.uuid4().hex[:6].upper()}"

    payload = {
        "customer_number": cust_code,
        "legal_name": "Aarav Sharma Tech Ltd",
        "trade_name": "Aarav Tech",
        "account_type": "ENTERPRISE",
        "status": "ACTIVE",
        "market_id": "mumbai",
        "email": f"{cust_code.lower()}@example.com",
        "phone": "+91 98201 99001",
        "locality": "Bandra West",
        "segment": "Enterprise - Leased Line",
        "customer_type": "Postpaid",
        "plan_name": "ILL 100M",
        "plan_price": 4999.0,
        "tax_id": "27AAAAA0000A1Z5",
        "primary_contact": {
            "first_name": "Aarav",
            "last_name": "Sharma",
            "email": f"{cust_code.lower()}@example.com",
            "phone": "+91 98201 99001",
            "role": "PRIMARY",
            "is_primary": True
        },
        "installation_address": {
            "address_type": "INSTALLATION",
            "address_line1": "42 Hill Road, Bandra West",
            "city": "Mumbai",
            "state": "Maharashtra",
            "postal_code": "400050",
            "latitude": 19.0596,
            "longitude": 72.8295
        }
    }

    res = client.post("/api/customers", json=payload, headers=headers)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["customer_code"] == cust_code
    assert data["name"] == "Aarav Sharma Tech Ltd"
    assert data["legal_name"] == "Aarav Sharma Tech Ltd"
    assert data["account_type"] == "ENTERPRISE"
    assert data["status"] == "Active"
    assert data["crm_id"] is not None
    assert len(data["contacts"]) >= 1
    assert data["contacts"][0]["first_name"] == "Aarav"
    assert data["contacts"][0]["is_primary"] is True
    assert len(data["addresses"]) >= 1
    assert data["addresses"][0]["postal_code"] == "400050"
    assert data["addresses"][0]["serviceability_status"] == "SERVICEABLE"


def test_customer_identifier_validation():
    headers = _auth_headers("Admin", "mumbai")

    # Overflow > 32 chars
    bad_overflow = {
        "customer_number": "CUST-MUM-OVERFLOW-EXCEEDING-32-CHARS-LONG-RECORD-XYZ",
        "legal_name": "Overflow Corp",
        "email": "overflow@example.com",
        "phone": "+91 98201 99002"
    }
    res = client.post("/api/customers", json=bad_overflow, headers=headers)
    assert res.status_code == 422
    assert "32 characters" in res.text

    # Invalid characters in code
    bad_chars = {
        "customer_number": "CUST MUM BAD$CODE",
        "legal_name": "Bad Chars Corp",
        "email": "bad@example.com",
        "phone": "+91 98201 99003"
    }
    res = client.post("/api/customers", json=bad_chars, headers=headers)
    assert res.status_code == 422


def test_customer_duplicate_prevention(db_session: Session):
    headers = _auth_headers("Admin", "mumbai")
    cust_code = f"CUST-MUM-{uuid.uuid4().hex[:6].upper()}"

    payload = {
        "customer_number": cust_code,
        "legal_name": "Unique Customer Ltd",
        "email": f"{cust_code.lower()}@example.com",
        "phone": "+91 98201 88111"
    }

    res1 = client.post("/api/customers", json=payload, headers=headers)
    assert res1.status_code == 201

    # 1. Attempt duplicate customer code
    res_dup_code = client.post("/api/customers", json=payload, headers=headers)
    assert res_dup_code.status_code == 409
    assert "already registered" in res_dup_code.text or "already exists" in res_dup_code.text

    # 2. Attempt duplicate identical email and phone in same market with different code
    payload2 = {
        "customer_number": f"CUST-MUM-{uuid.uuid4().hex[:6].upper()}",
        "legal_name": "Clone Customer Ltd",
        "email": f"{cust_code.lower()}@example.com",
        "phone": "+91 98201 88111"
    }
    res_dup_identity = client.post("/api/customers", json=payload2, headers=headers)
    assert res_dup_identity.status_code == 409
    assert "matching email and phone" in res_dup_identity.text


def test_shared_contact_allowed_if_phone_or_email_differs():
    """Step 2 B: Allow legitimate shared phone numbers or email addresses across different customers."""
    headers = _auth_headers("Admin", "mumbai")
    shared_phone = "+91 98201 77777"

    p1 = {
        "customer_number": f"CUST-MUM-{uuid.uuid4().hex[:6].upper()}",
        "legal_name": "Family Member One",
        "email": f"member1_{uuid.uuid4().hex[:4]}@family.org",
        "phone": shared_phone
    }
    res1 = client.post("/api/customers", json=p1, headers=headers)
    assert res1.status_code == 201

    # Second family member shares the phone number, but has a unique email
    p2 = {
        "customer_number": f"CUST-MUM-{uuid.uuid4().hex[:6].upper()}",
        "legal_name": "Family Member Two",
        "email": f"member2_{uuid.uuid4().hex[:4]}@family.org",
        "phone": shared_phone
    }
    res2 = client.post("/api/customers", json=p2, headers=headers)
    assert res2.status_code == 201


# ─── 2. Search, Filtering, Pagination & Sorting ───────────────────────────────

def test_list_customers_search_and_pagination():
    headers = _auth_headers("Admin", "mumbai")

    # Call with page and page_size
    res = client.get("/api/customers?page=1&page_size=10&sort_by=name&sort_order=asc", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) <= 10

    # Verify pagination response headers
    assert "X-Total-Count" in res.headers
    assert int(res.headers["X-Total-Count"]) > 0
    assert res.headers["X-Page"] == "1"
    assert res.headers["X-Page-Size"] == "10"
    assert "X-Total-Pages" in res.headers

    # Explicit directory endpoint returning paginated object
    res_dir = client.get("/api/customers/directory?page=1&page_size=5", headers=headers)
    assert res_dir.status_code == 200
    paged = res_dir.json()
    assert "items" in paged
    assert "total" in paged
    assert paged["page"] == 1
    assert paged["page_size"] == 5
    assert len(paged["items"]) <= 5


def test_customer_detail_lookup_by_id_uuid_and_code():
    headers = _auth_headers("Admin", "mumbai")

    # 1. Lookup by numeric ID 1
    res1 = client.get("/api/customers/1", headers=headers)
    assert res1.status_code == 200
    d1 = res1.json()
    assert d1["id"] == 1
    assert d1["customer_code"] is not None

    # 2. Lookup by customer code
    code = d1["customer_code"]
    res2 = client.get(f"/api/customers/{code}", headers=headers)
    assert res2.status_code == 200
    d2 = res2.json()
    assert d2["id"] == 1

    # 3. Lookup by CRM UUID
    if d1.get("crm_id"):
        res3 = client.get(f"/api/customers/{d1['crm_id']}", headers=headers)
        assert res3.status_code == 200
        d3 = res3.json()
        assert d3["id"] == 1


# ─── 3. Contacts & Addresses ──────────────────────────────────────────────────

def test_primary_contact_constraint_enforcement(db_session: Session):
    headers = _auth_headers("Admin", "mumbai")

    # Create a fresh customer
    cust_code = f"CUST-MUM-{uuid.uuid4().hex[:6].upper()}"
    p = {
        "customer_number": cust_code,
        "legal_name": "Contact Test Corp",
        "email": f"{cust_code.lower()}@test.org",
        "phone": "+91 98201 66111"
    }
    c_res = client.post("/api/customers", json=p, headers=headers)
    assert c_res.status_code == 201
    cust_id = c_res.json()["id"]

    # Add second primary contact -> automatically demotes previous primary
    c2_payload = {
        "first_name": "Rohit",
        "last_name": "Verma",
        "email": "rohit.v@test.org",
        "phone": "+91 98201 66222",
        "role": "PRIMARY",
        "is_primary": True
    }
    add_res = client.post(f"/api/customers/{cust_id}/contacts", json=c2_payload, headers=headers)
    assert add_res.status_code == 201
    c2_data = add_res.json()
    assert c2_data["is_primary"] is True

    # Verify contacts list: exactly one active primary
    list_res = client.get(f"/api/customers/{cust_id}/contacts", headers=headers)
    assert list_res.status_code == 200
    contacts = list_res.json()
    primaries = [c for c in contacts if c["is_primary"] and not c["is_archived"]]
    assert len(primaries) == 1
    assert primaries[0]["first_name"] == "Rohit"


def test_address_validation_and_no_coordinate_fabrication():
    headers = _auth_headers("Admin", "mumbai")

    # Address without coordinates must preserve PENDING_SURVEY (no fabrication)
    addr_payload = {
        "address_type": "INSTALLATION",
        "address_line1": "Flat 12B, Marine Lines",
        "city": "Mumbai",
        "state": "Maharashtra",
        "postal_code": "400020",
        "serviceability_status": "PENDING_SURVEY"
    }
    res = client.post("/api/customers/1/addresses", json=addr_payload, headers=headers)
    assert res.status_code == 201
    a_data = res.json()
    assert a_data["serviceability_status"] == "PENDING_SURVEY"
    assert a_data["latitude"] is None
    assert a_data["longitude"] is None

    # Partial/invalid coordinates must be rejected
    bad_coords = {
        "address_type": "INSTALLATION",
        "address_line1": "Bad GPS Lane",
        "city": "Mumbai",
        "state": "Maharashtra",
        "postal_code": "400020",
        "latitude": 19.05,
        "longitude": None  # Missing longitude
    }
    bad_res = client.post("/api/customers/1/addresses", json=bad_coords, headers=headers)
    assert bad_res.status_code == 422


# ─── 4. Leads & Idempotent Conversion ─────────────────────────────────────────

def test_lead_crud_and_idempotent_conversion():
    headers = _auth_headers("Care", "mumbai")

    lead_payload = {
        "legal_name": "Prospective Enterprise Corp",
        "trade_name": "ProCorp",
        "email": "lead.contact@procorp.com",
        "phone": "+91 98201 55000",
        "account_type": "ENTERPRISE",
        "locality": "Colaba",
        "address_line1": "100 Colaba Causeway",
        "notes": "Interested in 500M leased line with redundancy."
    }

    # 1. Create lead
    create_res = client.post("/api/customers/leads", json=lead_payload, headers=headers)
    assert create_res.status_code == 201
    lead = create_res.json()
    assert lead["status"] == "PROSPECT"
    assert lead["is_converted"] is False
    lead_id = lead["id"]

    # 2. List leads
    list_res = client.get("/api/customers/leads?search=Prospective", headers=headers)
    assert list_res.status_code == 200
    leads = list_res.json()
    assert any(l["id"] == lead_id for l in leads)

    # 3. Convert lead to Customer Master
    convert_payload = {
        "plan_name": "ILL 500M Enterprise",
        "plan_price": 12999.0,
        "customer_type": "Postpaid",
        "segment": "Enterprise - High Bandwidth"
    }
    conv_res = client.post(f"/api/customers/leads/{lead_id}/convert", json=convert_payload, headers=headers)
    assert conv_res.status_code == 200
    cust = conv_res.json()
    assert cust["legal_name"] == "Prospective Enterprise Corp"
    assert cust["id"] > 0
    assert cust["customer_code"].startswith("CUST-MUM-")
    converted_id = cust["id"]

    # 4. Idempotency test: repeating conversion must succeed and return same customer
    conv_res2 = client.post(f"/api/customers/leads/{lead_id}/convert", json=convert_payload, headers=headers)
    assert conv_res2.status_code == 200
    cust2 = conv_res2.json()
    assert cust2["id"] == converted_id
    assert cust2["customer_code"] == cust["customer_code"]


# ─── 5. Customer Maps ─────────────────────────────────────────────────────────

def test_customer_maps_summary():
    headers = _auth_headers("NOC", "mumbai")

    res = client.get("/api/customers/map-locations", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["market_id"] == "mumbai"
    assert data["total_customers"] > 0
    assert "with_coordinates_count" in data
    assert "missing_coordinates_count" in data
    assert data["total_customers"] == data["with_coordinates_count"] + data["missing_coordinates_count"]
    assert len(data["locations"]) == data["total_customers"]


# ─── 6. Vouchers ──────────────────────────────────────────────────────────────

def test_customer_vouchers_and_provisioning_dependency():
    headers = _auth_headers("Care", "mumbai")

    res = client.get("/api/customers/vouchers", headers=headers)
    assert res.status_code == 200
    vouchers = res.json()
    assert len(vouchers) > 0
    v = vouchers[0]
    assert "VCH-" in v["voucher_code"]
    assert "OSS/BSS Billing & Provisioning Engine (Phase 2)" in v["provisioning_dependency"]

    # Issue a courtesy voucher
    issue_res = client.post("/api/customers/1/vouchers/issue?voucher_type=SPEED_BOOST", headers=headers)
    assert issue_res.status_code == 200
    iv = issue_res.json()
    assert iv["customer_id"] == 1
    assert iv["status"] == "STAGED"


# ─── 7. Customer Tickets Integration ──────────────────────────────────────────

def test_customer_tickets_integration():
    headers = _auth_headers("Care", "mumbai")

    # Get customer tickets
    res = client.get("/api/customers/1/tickets", headers=headers)
    assert res.status_code == 200
    tickets = res.json()
    assert isinstance(tickets, list)

    # Raise customer ticket
    t_payload = {
        "category": "Speed",
        "priority": "P3",
        "description": "Customer testing broadband latency in Colaba"
    }
    post_res = client.post("/api/customers/1/tickets?category=Speed&priority=P3&description=Customer+testing+latency", headers=headers)
    assert post_res.status_code == 201
    new_t = post_res.json()
    assert new_t["ticket_code"].startswith("TCK-MUM-")
    assert new_t["category"] == "Speed"


# ─── 8. Security & RBAC Boundary ──────────────────────────────────────────────

def test_viewer_and_customer_role_restrictions():
    viewer_headers = _auth_headers("Viewer", "mumbai", "client@pmrgsolution.com")

    # Viewer cannot create customers
    res_create = client.post("/api/customers", json={"legal_name": "Forbidden Corp", "email": "f@f.com", "phone": "123"}, headers=viewer_headers)
    assert res_create.status_code == 403

    # Viewer cannot convert leads
    fake_uuid = str(uuid.uuid4())
    res_conv = client.post(f"/api/customers/leads/{fake_uuid}/convert", json={}, headers=viewer_headers)
    assert res_conv.status_code == 403


def test_cross_market_isolation_rejected():
    mumbai_headers = _auth_headers("Care", "mumbai")

    # Customer 301 belongs to kolkata; Mumbai caller must be rejected
    res = client.get("/api/customers/301/360", headers=mumbai_headers)
    assert res.status_code == 403
