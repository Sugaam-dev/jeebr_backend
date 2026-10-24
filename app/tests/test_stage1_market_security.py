import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from app.main import app
from app.database import SessionLocal
from app.models import Customer, User, Resource
from app.auth import create_access_token, hash_password
from app.markets import USER_MARKET_ASSIGNMENTS

client = TestClient(app)

@pytest.fixture(scope="module")
def setup_stage1_fixtures():
    db = SessionLocal()
    try:
        # 1. Fetch Mumbai & Kolkata customers from seeded dataset
        mumbai_cust = db.query(Customer).filter(Customer.market_id == "mumbai").first()
        kolkata_cust = db.query(Customer).filter(Customer.market_id == "kolkata").first()
        assert mumbai_cust is not None, "Mumbai seeded customer must exist"
        assert kolkata_cust is not None, "Kolkata seeded customer must exist"

        # 2. Setup test users for single-market, multi-market, unauthorized, and super admin
        users_to_ensure = [
            ("sec_mumbai_staff@pmrg.in", "NOC", "Mumbai Security Staff"),
            ("sec_kolkata_staff@pmrg.in", "NOC", "Kolkata Security Staff"),
            ("sec_multimarket_staff@pmrg.in", "NOC", "MultiMarket Security Staff"),
            ("sec_unauthorized_user@pmrg.in", "Viewer", "Unassigned Security User"),
            ("sec_unassigned_care@pmrg.in", "Care", "Unassigned Care User"),
            ("sec_unassigned_noc@pmrg.in", "NOC", "Unassigned NOC User"),
            ("sec_super_admin@pmrg.in", "SUPER_ADMIN", "Super Admin Security Test"),
            ("sec_imposter@pmrg.in", "Customer", "Customer Imposter"),
            ("sec_owner_a@pmrg.in", "Field Engineer", "Resource Owner A"),
            ("sec_hijacker_b@pmrg.in", "Field Engineer", "Resource Hijacker B"),
            ("sec_legacy_user@pmrg.in", "Field Engineer", "Legacy Resource User"),
            ("sec_ambiguous_user@pmrg.in", "Field Engineer", "Ambiguous Resource User"),
        ]

        created_users = {}
        for email, role, full_name in users_to_ensure:
            u = db.query(User).filter(User.email == email).first()
            if not u:
                u = User(
                    email=email,
                    hashed_password=hash_password("SecPass123!"),
                    full_name=full_name,
                    role=role,
                    is_active=True
                )
                db.add(u)
                db.commit()
                db.refresh(u)
            created_users[email] = u

        # Ensure a User exists with mumbai_cust.email for the legitimate customer owner
        cust_user = db.query(User).filter(User.email == mumbai_cust.email).first()
        if not cust_user:
            cust_user = User(
                email=mumbai_cust.email,
                hashed_password=hash_password("SecPass123!"),
                full_name=mumbai_cust.name,
                role="Customer",
                is_active=True
            )
            db.add(cust_user)
            db.commit()

        # SEC-S1-04: Link sec_kolkata_staff ONLY in Resource table to verify DB-level server assignment
        # Do NOT add sec_kolkata_staff to USER_MARKET_ASSIGNMENTS!
        res_kol = db.query(Resource).filter(Resource.user_id == created_users["sec_kolkata_staff@pmrg.in"].id).first()
        if not res_kol:
            res_kol = Resource(
                market_id="kolkata",
                name="Kolkata Security Staff",
                email="sec_kolkata_staff@pmrg.in",
                phone="+91 98301 99999",
                resource_type="INTERNAL",
                region="Salt Lake Sector V",
                status="Available",
                user_id=created_users["sec_kolkata_staff@pmrg.in"].id
            )
            db.add(res_kol)
            db.commit()

        # SEC-S1-02: Setup conflicting resource binding (Resource bound to User A with User B's email)
        res_conflict = db.query(Resource).filter(Resource.user_id == created_users["sec_owner_a@pmrg.in"].id).first()
        if not res_conflict:
            res_conflict = Resource(
                market_id="kolkata",
                name="Owner A Resource with Hijacker Email",
                email="sec_hijacker_b@pmrg.in",
                phone="+91 98301 11111",
                resource_type="FIELD",
                region="Salt Lake Sector V",
                status="Available",
                user_id=created_users["sec_owner_a@pmrg.in"].id
            )
            db.add(res_conflict)
            db.commit()

        # SEC-S1-02: Setup unambiguous legacy resource (user_id IS NULL)
        res_legacy = db.query(Resource).filter(
            Resource.email == "sec_legacy_user@pmrg.in",
            Resource.user_id.is_(None)
        ).first()
        if not res_legacy:
            res_legacy = Resource(
                market_id="kolkata",
                name="Unambiguous Legacy Resource",
                email="sec_legacy_user@pmrg.in",
                phone="+91 98301 22222",
                resource_type="FIELD",
                region="Park Street",
                status="Available",
                user_id=None
            )
            db.add(res_legacy)
            db.commit()

        # SEC-S1-02: Setup ambiguous legacy resources (duplicate unassigned rows with conflicting markets)
        db.query(Resource).filter(
            Resource.email == "sec_ambiguous_user@pmrg.in",
            Resource.user_id.is_(None)
        ).delete()
        db.commit()
        res_amb1 = Resource(
            market_id="mumbai",
            name="Ambiguous Legacy Mumbai",
            email="sec_ambiguous_user@pmrg.in",
            phone="+91 98200 33333",
            resource_type="FIELD",
            region="Bandra West",
            status="Available",
            user_id=None
        )
        res_amb2 = Resource(
            market_id="kolkata",
            name="Ambiguous Legacy Kolkata",
            email="sec_ambiguous_user@pmrg.in",
            phone="+91 98301 44444",
            resource_type="FIELD",
            region="Salt Lake Sector V",
            status="Available",
            user_id=None
        )
        db.add(res_amb1)
        db.add(res_amb2)
        db.commit()

        # Configure persistent UserMarketAssignment records in DB and backwards-compatible dictionary
        from app.models import UserMarketAssignment
        db.query(UserMarketAssignment).filter(
            UserMarketAssignment.user_id.in_([
                created_users["sec_mumbai_staff@pmrg.in"].id,
                created_users["sec_multimarket_staff@pmrg.in"].id,
                cust_user.id,
                created_users["sec_imposter@pmrg.in"].id,
            ])
        ).delete(synchronize_session=False)
        db.add(UserMarketAssignment(user_id=created_users["sec_mumbai_staff@pmrg.in"].id, market_id="mumbai", is_primary=True))
        db.add(UserMarketAssignment(user_id=created_users["sec_multimarket_staff@pmrg.in"].id, market_id="mumbai", is_primary=False))
        db.add(UserMarketAssignment(user_id=created_users["sec_multimarket_staff@pmrg.in"].id, market_id="kolkata", is_primary=False))
        db.add(UserMarketAssignment(user_id=cust_user.id, market_id="mumbai", is_primary=True))
        db.add(UserMarketAssignment(user_id=created_users["sec_imposter@pmrg.in"].id, market_id="mumbai", is_primary=True))
        db.commit()

        USER_MARKET_ASSIGNMENTS["sec_mumbai_staff@pmrg.in"] = {"markets": ["mumbai"], "primary": "mumbai"}
        USER_MARKET_ASSIGNMENTS["sec_multimarket_staff@pmrg.in"] = {"markets": ["mumbai", "kolkata"], "primary": None}
        USER_MARKET_ASSIGNMENTS[mumbai_cust.email] = {"markets": ["mumbai"], "primary": "mumbai"}
        USER_MARKET_ASSIGNMENTS["sec_imposter@pmrg.in"] = {"markets": ["mumbai"], "primary": "mumbai"}

        tokens = {
            "mumbai_staff": create_access_token({"sub": "sec_mumbai_staff@pmrg.in", "role": "NOC"}),
            "kolkata_staff": create_access_token({"sub": "sec_kolkata_staff@pmrg.in", "role": "NOC"}),
            "multimarket_staff": create_access_token({"sub": "sec_multimarket_staff@pmrg.in", "role": "NOC"}),
            "unauthorized_user": create_access_token({"sub": "sec_unauthorized_user@pmrg.in", "role": "Viewer"}),
            "unassigned_care": create_access_token({"sub": "sec_unassigned_care@pmrg.in", "role": "Care"}),
            "unassigned_noc": create_access_token({"sub": "sec_unassigned_noc@pmrg.in", "role": "NOC"}),
            "super_admin": create_access_token({"sub": "sec_super_admin@pmrg.in", "role": "SUPER_ADMIN"}),
            "customer_owner": create_access_token({"sub": mumbai_cust.email, "role": "Customer"}),
            "customer_imposter": create_access_token({"sub": "sec_imposter@pmrg.in", "role": "Customer"}),
            "owner_a": create_access_token({"sub": "sec_owner_a@pmrg.in", "role": "Field Engineer"}),
            "hijacker_b": create_access_token({"sub": "sec_hijacker_b@pmrg.in", "role": "Field Engineer"}),
            "legacy_user": create_access_token({"sub": "sec_legacy_user@pmrg.in", "role": "Field Engineer"}),
            "ambiguous_user": create_access_token({"sub": "sec_ambiguous_user@pmrg.in", "role": "Field Engineer"}),
        }

        return {
            "mumbai_cust_id": mumbai_cust.id,
            "kolkata_cust_id": kolkata_cust.id,
            "mumbai_cust_code": mumbai_cust.customer_code,
            "tokens": tokens
        }
    finally:
        db.close()


# ─── 1. Customer 360 Market Authorization & Isolation Tests ───────────────────

def test_mumbai_staff_can_access_mumbai_customer(setup_stage1_fixtures):
    """A Mumbai-assigned staff user can access an authorized Mumbai customer."""
    data = setup_stage1_fixtures
    token = data["tokens"]["mumbai_staff"]
    cust_id = data["mumbai_cust_id"]

    res = client.get(
        f"/api/customers/{cust_id}/360",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "mumbai"}
    )
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    body = res.json()
    assert body["customer"]["id"] == cust_id
    assert body["customer"]["customer_code"] == data["mumbai_cust_code"]
    assert "churn_risk_score" in body
    assert "next_best_action" in body


def test_mumbai_staff_cannot_access_kolkata_customer(setup_stage1_fixtures):
    """A Mumbai-assigned staff user cannot access a Kolkata customer."""
    data = setup_stage1_fixtures
    token = data["tokens"]["mumbai_staff"]
    kolkata_cust_id = data["kolkata_cust_id"]

    res = client.get(
        f"/api/customers/{kolkata_cust_id}/360",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "mumbai"}
    )
    assert res.status_code == 403
    assert "Customer does not belong to authorized market" in res.json()["detail"]


def test_kolkata_staff_cannot_access_mumbai_customer(setup_stage1_fixtures):
    """A Kolkata-assigned staff user cannot access a Mumbai customer."""
    data = setup_stage1_fixtures
    token = data["tokens"]["kolkata_staff"]
    mumbai_cust_id = data["mumbai_cust_id"]

    res = client.get(
        f"/api/customers/{mumbai_cust_id}/360",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "kolkata"}
    )
    assert res.status_code == 403
    assert "Customer does not belong to authorized market" in res.json()["detail"]


def test_header_tampering_does_not_bypass_authorization(setup_stage1_fixtures):
    """Changing X-Market-Id to another market does not bypass authorization."""
    data = setup_stage1_fixtures
    token = data["tokens"]["mumbai_staff"]
    kolkata_cust_id = data["kolkata_cust_id"]

    # Mumbai staff attempts to claim Kolkata identity via header tampering
    res = client.get(
        f"/api/customers/{kolkata_cust_id}/360",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "kolkata"}
    )
    assert res.status_code == 403
    assert "not authorized to access market 'kolkata'" in res.json()["detail"]


def test_invalid_market_identifier_returns_400(setup_stage1_fixtures):
    """An invalid market identifier returns HTTP 400 Bad Request."""
    data = setup_stage1_fixtures
    token = data["tokens"]["mumbai_staff"]
    mumbai_cust_id = data["mumbai_cust_id"]

    # In Customer 360
    res360 = client.get(
        f"/api/customers/{mumbai_cust_id}/360",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "atlantis"}
    )
    assert res360.status_code == 400
    assert "Invalid or unsupported market identifier" in res360.json()["detail"]

    # In list_customers
    res_list = client.get(
        "/api/customers",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "invalid_market_xyz"}
    )
    assert res_list.status_code == 400
    assert "Invalid or unsupported market identifier" in res_list.json()["detail"]


# ─── 2. Header Absence & Resolution Tests ────────────────────────────────────

def test_single_market_user_without_header_scoped_to_assigned_market(setup_stage1_fixtures):
    """A single-market user without the header is automatically scoped to their assigned market."""
    data = setup_stage1_fixtures
    token = data["tokens"]["mumbai_staff"]
    mumbai_cust_id = data["mumbai_cust_id"]

    # In Customer 360
    res = client.get(
        f"/api/customers/{mumbai_cust_id}/360",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 200
    assert res.json()["customer"]["id"] == mumbai_cust_id

    # In list_customers (returns Mumbai customers)
    res_list = client.get(
        "/api/customers?limit=5",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert res_list.status_code == 200
    items = res_list.json()
    assert len(items) > 0
    assert all(c["market_id"] == "mumbai" for c in items)


def test_multi_market_user_without_header_returns_400_where_selection_mandatory(setup_stage1_fixtures):
    """A multi-market user without the required header receives HTTP 400 where market selection is mandatory."""
    data = setup_stage1_fixtures
    token = data["tokens"]["multimarket_staff"]

    res = client.get(
        "/api/customers",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 400
    assert "Multiple markets authorized. Explicit X-Market-Id header required" in res.json()["detail"]

    # With explicit header, multi-market user succeeds
    res_mumbai = client.get(
        "/api/customers",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "mumbai"}
    )
    assert res_mumbai.status_code == 200

    res_kolkata = client.get(
        "/api/customers",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "kolkata"}
    )
    assert res_kolkata.status_code == 200


def test_user_without_authorized_market_denied_access(setup_stage1_fixtures):
    """A user without an authorized market is denied access (HTTP 403). Never silently assign a market."""
    data = setup_stage1_fixtures
    token = data["tokens"]["unauthorized_user"]
    mumbai_cust_id = data["mumbai_cust_id"]

    # Without header
    res_no_header = client.get(
        "/api/customers",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert res_no_header.status_code == 403
    assert "no authorized operational markets" in res_no_header.json()["detail"]

    # With header
    res_with_header = client.get(
        "/api/customers",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "mumbai"}
    )
    assert res_with_header.status_code == 403
    assert "not authorized to access market 'mumbai'" in res_with_header.json()["detail"]

    # Customer 360
    res_360 = client.get(
        f"/api/customers/{mumbai_cust_id}/360",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert res_360.status_code == 403


# ─── 3. SUPER_ADMIN Behavior Tests ────────────────────────────────────────────

def test_super_admin_market_behavior(setup_stage1_fixtures):
    """SUPER_ADMIN behavior is explicitly tested across valid, invalid, and missing market identifiers."""
    data = setup_stage1_fixtures
    token = data["tokens"]["super_admin"]
    mumbai_cust_id = data["mumbai_cust_id"]
    kolkata_cust_id = data["kolkata_cust_id"]

    # 1. Valid Mumbai access
    res_mum = client.get(
        f"/api/customers/{mumbai_cust_id}/360",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "mumbai"}
    )
    assert res_mum.status_code == 200
    assert res_mum.json()["customer"]["id"] == mumbai_cust_id

    # 2. Valid Kolkata access
    res_kol = client.get(
        f"/api/customers/{kolkata_cust_id}/360",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "kolkata"}
    )
    assert res_kol.status_code == 200
    assert res_kol.json()["customer"]["id"] == kolkata_cust_id

    # 3. Invalid market returns 400 for Super Admin
    res_inv = client.get(
        f"/api/customers/{mumbai_cust_id}/360",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "mars_market"}
    )
    assert res_inv.status_code == 400
    assert "Invalid or unsupported market identifier" in res_inv.json()["detail"]

    # 4. Super Admin without header can access Mumbai customer (preserved default)
    res_def = client.get(
        f"/api/customers/{mumbai_cust_id}/360",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert res_def.status_code == 200

    # 5. Super Admin supplying mismatched header on customer gets 403
    res_mismatch = client.get(
        f"/api/customers/{mumbai_cust_id}/360",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "kolkata"}
    )
    assert res_mismatch.status_code == 403


# ─── 4. Nonexistent Customer & Unauthenticated Tests ─────────────────────────

def test_nonexistent_customer_returns_404(setup_stage1_fixtures):
    """Nonexistent customer IDs return 404 Not Found consistently."""
    data = setup_stage1_fixtures
    token = data["tokens"]["mumbai_staff"]

    res = client.get(
        "/api/customers/99999999/360",
        headers={"Authorization": f"Bearer {token}", "X-Market-Id": "mumbai"}
    )
    assert res.status_code == 404
    assert res.json()["detail"] == "Customer not found"


def test_unauthenticated_request_denied():
    """Unauthenticated requests remain denied with HTTP 401."""
    client.cookies.clear()
    res = client.get("/api/customers/1/360")
    assert res.status_code == 401


def test_customer_role_cannot_view_another_customer_360(setup_stage1_fixtures):
    """Customer role user cannot view another customer's 360 profile."""
    data = setup_stage1_fixtures
    owner_token = data["tokens"]["customer_owner"]
    imposter_token = data["tokens"]["customer_imposter"]
    mumbai_cust_id = data["mumbai_cust_id"]

    # Owner succeeds
    res_owner = client.get(
        f"/api/customers/{mumbai_cust_id}/360",
        headers={"Authorization": f"Bearer {owner_token}", "X-Market-Id": "mumbai"}
    )
    assert res_owner.status_code == 200

    # Imposter is denied
    res_imposter = client.get(
        f"/api/customers/{mumbai_cust_id}/360",
        headers={"Authorization": f"Bearer {imposter_token}", "X-Market-Id": "mumbai"}
    )
    assert res_imposter.status_code == 403
    assert "Cannot access another customer's 360 profile" in res_imposter.json()["detail"]


# ─── 5. Remediation Verification Tests (SEC-S1-01 through SEC-S1-05) ─────────

def test_unassigned_roles_denied_market_access(setup_stage1_fixtures):
    """SEC-S1-01: Unassigned Viewer, Care, and NOC users cannot gain market access through role defaults."""
    data = setup_stage1_fixtures
    unassigned_tokens = [
        ("Viewer", data["tokens"]["unauthorized_user"]),
        ("Care", data["tokens"]["unassigned_care"]),
        ("NOC", data["tokens"]["unassigned_noc"]),
    ]

    for role_name, token in unassigned_tokens:
        # Without header -> 403
        res_no_header = client.get(
            "/api/customers",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert res_no_header.status_code == 403, f"{role_name} unexpectedly granted access without header"
        assert "no authorized operational markets" in res_no_header.json()["detail"]

        # With explicit Mumbai header -> 403
        res_with_header = client.get(
            "/api/customers",
            headers={"Authorization": f"Bearer {token}", "X-Market-Id": "mumbai"}
        )
        assert res_with_header.status_code == 403, f"{role_name} unexpectedly granted access with Mumbai header"
        assert "not authorized to access market 'mumbai'" in res_with_header.json()["detail"]


def test_explicitly_assigned_staff_retain_access(setup_stage1_fixtures):
    """SEC-S1-01 & SEC-S1-04: Explicitly assigned staff retain access to their authorized market."""
    data = setup_stage1_fixtures
    
    # 1. Mumbai staff assigned via USER_MARKET_ASSIGNMENTS
    res_mum = client.get(
        "/api/customers?limit=5",
        headers={"Authorization": f"Bearer {data['tokens']['mumbai_staff']}", "X-Market-Id": "mumbai"}
    )
    assert res_mum.status_code == 200
    assert len(res_mum.json()) > 0

    # 2. Kolkata staff assigned strictly through database Resource.user_id (not in USER_MARKET_ASSIGNMENTS)
    res_kol = client.get(
        "/api/customers?limit=5",
        headers={"Authorization": f"Bearer {data['tokens']['kolkata_staff']}", "X-Market-Id": "kolkata"}
    )
    assert res_kol.status_code == 200
    assert len(res_kol.json()) > 0
    assert all(c["market_id"] == "kolkata" for c in res_kol.json())


def test_resource_user_id_prevents_email_hijack(setup_stage1_fixtures):
    """SEC-S1-02: A Resource row belonging to User A cannot grant its market to User B merely because email matches."""
    data = setup_stage1_fixtures
    token_owner = data["tokens"]["owner_a"]
    token_hijacker = data["tokens"]["hijacker_b"]

    # User B (hijacker) has matching email on User A's resource, but Resource.user_id == User A.
    # User B must be denied access to Kolkata market!
    res_hijacker = client.get(
        "/api/customers",
        headers={"Authorization": f"Bearer {token_hijacker}", "X-Market-Id": "kolkata"}
    )
    assert res_hijacker.status_code == 403, f"Hijacker unexpectedly granted market access: {res_hijacker.text}"

    # User A (legitimate foreign key owner) must be granted access to Kolkata market!
    res_owner = client.get(
        "/api/customers?limit=5",
        headers={"Authorization": f"Bearer {token_owner}", "X-Market-Id": "kolkata"}
    )
    assert res_owner.status_code == 200, f"Owner denied market access: {res_owner.text}"


def test_resource_unambiguous_legacy_email_fallback(setup_stage1_fixtures):
    """SEC-S1-02: Resource email fallback works only for eligible, unambiguous legacy records with user_id IS NULL."""
    data = setup_stage1_fixtures
    token_legacy = data["tokens"]["legacy_user"]

    # Legacy user with unassigned Resource row (user_id IS NULL) in Kolkata succeeds
    res = client.get(
        "/api/customers?limit=5",
        headers={"Authorization": f"Bearer {token_legacy}", "X-Market-Id": "kolkata"}
    )
    assert res.status_code == 200
    assert len(res.json()) > 0


def test_ambiguous_resource_email_fallback_fails_closed(setup_stage1_fixtures):
    """SEC-S1-02: Duplicate or ambiguous Resource email mappings across different markets fail closed."""
    data = setup_stage1_fixtures
    token_ambiguous = data["tokens"]["ambiguous_user"]

    # Ambiguous user has two legacy unassigned rows with conflicting markets (Mumbai & Kolkata).
    # Resolver must fail closed and return 403!
    res = client.get(
        "/api/customers",
        headers={"Authorization": f"Bearer {token_ambiguous}", "X-Market-Id": "kolkata"}
    )
    assert res.status_code == 403
    assert "not authorized to access market 'kolkata'" in res.json()["detail"]


def test_customer_role_cannot_enumerate_other_subscribers(setup_stage1_fixtures):
    """SEC-S1-03: Customer-role users cannot enumerate other subscribers via GET /api/customers."""
    data = setup_stage1_fixtures
    token_imposter = data["tokens"]["customer_imposter"]

    # Imposter subscriber calls GET /api/customers for Mumbai market
    res = client.get(
        "/api/customers",
        headers={"Authorization": f"Bearer {token_imposter}", "X-Market-Id": "mumbai"}
    )
    assert res.status_code == 200
    items = res.json()
    # Must NOT receive the 60+ seeded subscribers directory!
    assert len(items) == 0, f"Customer role unexpectedly enumerated directory: {len(items)} records returned"


def test_subscriber_can_access_only_own_information(setup_stage1_fixtures):
    """SEC-S1-03: A subscriber can access only their own customer information through permitted endpoints."""
    data = setup_stage1_fixtures
    token_owner = data["tokens"]["customer_owner"]
    mumbai_cust_id = data["mumbai_cust_id"]

    # 1. Listing endpoint returns strictly 1 record corresponding to authenticated subscriber
    res_list = client.get(
        "/api/customers",
        headers={"Authorization": f"Bearer {token_owner}", "X-Market-Id": "mumbai"}
    )
    assert res_list.status_code == 200
    items = res_list.json()
    assert len(items) == 1
    assert items[0]["id"] == mumbai_cust_id

    # 2. Customer 360 detail succeeds for owner
    res_360 = client.get(
        f"/api/customers/{mumbai_cust_id}/360",
        headers={"Authorization": f"Bearer {token_owner}", "X-Market-Id": "mumbai"}
    )
    assert res_360.status_code == 200
    assert res_360.json()["customer"]["id"] == mumbai_cust_id


def test_unauthenticated_request_cannot_access_market_data():
    """SEC-S1-05: Unauthenticated requests cannot access protected market-scoped data."""
    client.cookies.clear()
    
    # Unauthenticated call to /api/customers
    res_cust = client.get("/api/customers", headers={"X-Market-Id": "mumbai"})
    assert res_cust.status_code == 401

    # Unauthenticated call to /api/customers/1/360
    res_360 = client.get("/api/customers/1/360")
    assert res_360.status_code == 401

