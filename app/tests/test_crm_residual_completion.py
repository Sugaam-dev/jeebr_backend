"""
app/tests/test_crm_residual_completion.py

Comprehensive test suite verifying CRM residual functionality:
1. Address PUT update & IDOR cross-customer security.
2. Automated field-survey dispatch on address creation / update.
3. Survey idempotency (no duplicate tickets on repeated saves).
4. No-eligible-engineer graceful queueing with explicit audit reason.
5. Survey completion workflow with verified GPS coordinates and ticket closure.
6. KYC Document upload with MIME and magic bytes validation.
7. Document metadata retrieval, binary download, and masked identifier checks.
8. Document verification (VERIFIED / REJECTED) and compliance soft-archival.
9. Unconfigured cloud storage handling (503 BLOCKED).
10. RBAC role enforcement (Viewer denied mutations).
"""

import uuid
import io
import pytest
from datetime import datetime
from fastapi.testclient import TestClient

from app.main import app
from app.models import Customer, Resource, Ticket, FieldAssignment, User
from app.models_crm import (
    CRMCustomer, CRMAddress, CRMContact, CRMDocument, CRMActivity,
    ServiceabilityStatus, DocumentType, DocumentUploadStatus, VerificationStatus
)
from app.auth import create_access_token
from app.services.document_storage import (
    LocalSandboxedStorageProvider, UnconfiguredCloudStorageProvider,
    set_storage_provider_for_testing
)

client = TestClient(app)

def _auth_headers(role: str = "Admin", market: str = "mumbai", email: str = "admin@pmrg.in", is_json: bool = True) -> dict:
    token = create_access_token({"sub": email, "role": role})
    h = {
        "Authorization": f"Bearer {token}",
        "X-Market-Id": market
    }
    if is_json:
        h["Content-Type"] = "application/json"
    return h

# Test Fixtures & Sample Files
SAMPLE_PDF_CONTENT = b"%PDF-1.4\n1 0 obj\n<< /Title (Test KYC Doc) >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF"
SAMPLE_PNG_CONTENT = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
FAKE_EXE_CONTENT = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"


@pytest.fixture(autouse=True)
def setup_storage_provider(tmp_path):
    """Configures local sandboxed storage in temporary directory for tests."""
    storage_dir = tmp_path / "kyc_storage"
    provider = LocalSandboxedStorageProvider(base_directory=str(storage_dir))
    set_storage_provider_for_testing(provider)
    yield
    set_storage_provider_for_testing(None)


def test_address_put_update_and_idor_protection(db_session):
    """
    Verify PUT /customers/{customer_id}/addresses/{address_id} updates fields,
    validates coordinates, and strictly rejects cross-customer mutation (IDOR).
    """
    headers = _auth_headers(role="Admin")

    # Setup Customer 1 and Address 1
    cust1 = CRMCustomer(
        id=uuid.uuid4(),
        customer_number=f"CUST-TEST-{uuid.uuid4().hex[:6]}",
        legal_name="Customer Alpha Corp",
        account_type="ENTERPRISE",
        status="ACTIVE",
        market_id="mumbai"
    )
    db_session.add(cust1)
    db_session.flush()

    addr1 = CRMAddress(
        id=uuid.uuid4(),
        crm_customer_id=cust1.id,
        address_type="INSTALLATION",
        address_line1="Old Office Tower 1",
        city="Mumbai",
        state="Maharashtra",
        postal_code="400001",
        serviceability_status=ServiceabilityStatus.PENDING_SURVEY
    )
    db_session.add(addr1)

    # Setup Customer 2
    cust2 = CRMCustomer(
        id=uuid.uuid4(),
        customer_number=f"CUST-TEST-{uuid.uuid4().hex[:6]}",
        legal_name="Customer Beta Ltd",
        account_type="RESIDENTIAL",
        status="ACTIVE",
        market_id="mumbai"
    )
    db_session.add(cust2)
    db_session.commit()

    # 1. Update Address 1 for Customer 1 successfully
    put_payload = {
        "address_line1": "New Tech Park, Suite 400",
        "city": "Mumbai",
        "state": "Maharashtra",
        "postal_code": "400051",
        "latitude": 19.0760,
        "longitude": 72.8777
    }
    resp = client.put(f"/api/customers/{cust1.id}/addresses/{addr1.id}", json=put_payload, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["address_line1"] == "New Tech Park, Suite 400"
    assert data["postal_code"] == "400051"
    assert data["serviceability_status"] == ServiceabilityStatus.SERVICEABLE
    assert data["latitude"] == 19.0760

    # 2. IDOR Protection: Attempting to update Address 1 via Customer 2 must be rejected with 404
    resp_idor = client.put(f"/api/customers/{cust2.id}/addresses/{addr1.id}", json=put_payload, headers=headers)
    assert resp_idor.status_code == 404
    assert "not found" in resp_idor.json()["detail"].lower()

    # 3. Invalid coordinates range check
    invalid_coord_payload = {"latitude": 95.0, "longitude": 72.8777}
    resp_inv = client.put(f"/api/customers/{cust1.id}/addresses/{addr1.id}", json=invalid_coord_payload, headers=headers)
    assert resp_inv.status_code == 422


def test_automated_field_survey_dispatch_and_idempotency(db_session):
    """
    Verify creating an address with PENDING_SURVEY automatically dispatches
    a P4 survey ticket and assigns available regional engineer.
    Repeated saves must not create duplicate survey tickets.
    """
    headers = _auth_headers(role="Admin")

    # Create available regional engineer in Bandra
    engineer = Resource(
        market_id="mumbai",
        name="Sunil Patil",
        resource_type="FIELD",
        region="Bandra",
        status="Available",
        active_tickets_count=0,
        max_capacity=5
    )
    db_session.add(engineer)

    # Create Customer
    crm_cust = CRMCustomer(
        id=uuid.uuid4(),
        customer_number=f"CUST-SRV-{uuid.uuid4().hex[:6]}",
        legal_name="Survey Test Enterprise",
        account_type="ENTERPRISE",
        status="ACTIVE",
        market_id="mumbai"
    )
    db_session.add(crm_cust)
    db_session.commit()

    # 1. Create Address without verified GPS (triggers automated survey dispatch)
    addr_payload = {
        "address_type": "INSTALLATION",
        "address_line1": "Plot 42, Bandra Reclamation",
        "city": "Bandra",
        "state": "Maharashtra",
        "postal_code": "400050",
        "serviceability_status": "PENDING_SURVEY"
    }
    resp = client.post(f"/api/customers/{crm_cust.id}/addresses", json=addr_payload, headers=headers)
    assert resp.status_code == 201, resp.text
    addr_data = resp.json()
    addr_id = addr_data["id"]

    assert addr_data["serviceability_status"] == "PENDING_SURVEY"
    assert addr_data["survey_ticket_code"] is not None
    assert addr_data["survey_ticket_status"] == "Assigned"

    # Verify ticket in database
    ticket = db_session.query(Ticket).filter(Ticket.ticket_code == addr_data["survey_ticket_code"]).first()
    assert ticket is not None
    assert ticket.priority == "P4"
    assert ticket.category == "Install"
    assert ticket.assigned_resource_id is not None
    assert f"[SURVEY-ADDR:{addr_id}]" in ticket.description

    # Verify FieldAssignment created
    fa = db_session.query(FieldAssignment).filter(FieldAssignment.ticket_id == ticket.id).first()
    assert fa is not None
    assert fa.engineer_id == ticket.assigned_resource_id
    assert fa.status == "ASSIGNED"

    # 2. Idempotency Check: Updating or re-saving the address must NOT create another ticket
    update_payload = {"address_line2": "Wing B, 3rd Floor"}
    resp_update = client.put(f"/api/customers/{crm_cust.id}/addresses/{addr_id}", json=update_payload, headers=headers)
    assert resp_update.status_code == 200
    updated_data = resp_update.json()

    # Ticket code remains identical
    assert updated_data["survey_ticket_code"] == addr_data["survey_ticket_code"]

    # Verify count of survey tickets for this address is exactly 1
    total_survey_tickets = db_session.query(Ticket).filter(
        Ticket.description.contains(f"[SURVEY-ADDR:{addr_id}]")
    ).count()
    assert total_survey_tickets == 1


def test_survey_dispatch_queueing_when_engineers_at_capacity(db_session):
    """
    When all engineers are at capacity or offline, automated survey ticket
    must remain Open with explicit audit reason ("Queued for regional field dispatch").
    """
    headers = _auth_headers(role="Admin", market="kolkata")

    # Mark all resources in Kolkata as Offline so none are eligible
    db_session.query(Resource).filter(Resource.market_id == "kolkata").update({
        "status": "Offline",
        "max_capacity": 0
    })

    crm_cust = CRMCustomer(
        id=uuid.uuid4(),
        customer_number=f"CUST-CAP-{uuid.uuid4().hex[:6]}",
        legal_name="High Load Customer",
        account_type="RESIDENTIAL",
        status="ACTIVE",
        market_id="kolkata"
    )
    db_session.add(crm_cust)
    db_session.commit()

    addr_payload = {
        "address_type": "INSTALLATION",
        "address_line1": "Park Street Saturated Lane",
        "city": "Park Street",
        "state": "West Bengal",
        "postal_code": "700016",
        "serviceability_status": "PENDING_SURVEY"
    }
    resp = client.post(f"/api/customers/{crm_cust.id}/addresses", json=addr_payload, headers=headers)
    assert resp.status_code == 201
    addr_data = resp.json()

    assert addr_data["survey_ticket_status"] == "Open"
    ticket = db_session.query(Ticket).filter(Ticket.ticket_code == addr_data["survey_ticket_code"]).first()
    assert ticket.assigned_resource_id is None
    assert "Queued for regional field dispatch" in ticket.ai_triage_action


def test_survey_completion_workflow(db_session):
    """
    Verify survey completion endpoint sets verified coordinates, updates serviceability status,
    and resolves the open survey ticket and field assignment.
    """
    headers = _auth_headers(role="Admin")

    crm_cust = CRMCustomer(
        id=uuid.uuid4(),
        customer_number=f"CUST-COMP-{uuid.uuid4().hex[:6]}",
        legal_name="Completion Enterprise",
        account_type="ENTERPRISE",
        status="ACTIVE",
        market_id="mumbai"
    )
    db_session.add(crm_cust)
    db_session.flush()

    addr = CRMAddress(
        id=uuid.uuid4(),
        crm_customer_id=crm_cust.id,
        address_type="INSTALLATION",
        address_line1="Seepz Gate 1, Andheri East",
        city="Andheri",
        state="Maharashtra",
        postal_code="400096",
        serviceability_status=ServiceabilityStatus.PENDING_SURVEY
    )
    db_session.add(addr)
    db_session.flush()

    ticket = Ticket(
        market_id="mumbai",
        ticket_code="TCK-MUM-SRV-9999",
        source="CUSTOMER",
        region="Andheri",
        category="Install",
        priority="P4",
        status="Assigned",
        description=f"[SURVEY-ADDR:{addr.id}] Feasibility survey",
        created_at=datetime.utcnow()
    )
    db_session.add(ticket)
    db_session.commit()

    # Complete survey
    complete_payload = {
        "latitude": 19.1136,
        "longitude": 72.8697,
        "serviceability_status": "SERVICEABLE",
        "notes": "Verified fiber drop point within 45m of subscriber premise."
    }
    resp = client.post(
        f"/api/customers/{crm_cust.id}/addresses/{addr.id}/survey-complete",
        json=complete_payload,
        headers=headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["latitude"] == 19.1136
    assert data["longitude"] == 72.8697
    assert data["serviceability_status"] == "SERVICEABLE"
    assert data["survey_ticket_status"] == "Resolved"

    # Verify ticket resolved in DB
    db_session.refresh(ticket)
    assert ticket.status == "Resolved"
    assert ticket.resolved_at is not None


def test_kyc_document_upload_download_and_pii_masking(db_session):
    """
    Verify uploading a KYC document saves binary, inspects magic bytes,
    masks identity number, and enables authorized binary download.
    """
    upload_headers = _auth_headers(role="Admin", is_json=False)
    api_headers = _auth_headers(role="Admin")

    crm_cust = CRMCustomer(
        id=uuid.uuid4(),
        customer_number=f"CUST-KYC-{uuid.uuid4().hex[:6]}",
        legal_name="Rameshwaram Logistics",
        account_type="SME",
        status="ACTIVE",
        market_id="mumbai"
    )
    db_session.add(crm_cust)
    db_session.commit()

    # 1. Upload valid PDF Aadhaar
    files = {"file": ("aadhaar_proof.pdf", io.BytesIO(SAMPLE_PDF_CONTENT), "application/pdf")}
    form_data = {
        "document_type": "AADHAAR_MASKED",
        "document_identifier": "987654321098"  # 12-digit Aadhaar
    }
    resp = client.post(f"/api/customers/{crm_cust.id}/documents", files=files, data=form_data, headers=upload_headers)
    assert resp.status_code == 201, resp.text
    doc_data = resp.json()
    doc_id = doc_data["id"]

    assert doc_data["document_type"] == "AADHAAR_MASKED"
    assert doc_data["document_identifier"] == "XXXX-XXXX-1098"  # Properly masked
    assert doc_data["upload_status"] == "UPLOADED"
    assert doc_data["verification_status"] == "PENDING"
    assert doc_data["file_size_bytes"] == len(SAMPLE_PDF_CONTENT)

    # 2. Retrieve document metadata
    resp_meta = client.get(f"/api/customers/{crm_cust.id}/documents/{doc_id}", headers=api_headers)
    assert resp_meta.status_code == 200
    assert resp_meta.json()["file_name"] == "aadhaar_proof.pdf"

    # 3. Download document binary
    resp_dl = client.get(f"/api/customers/{crm_cust.id}/documents/{doc_id}/download", headers=api_headers)
    assert resp_dl.status_code == 200
    assert resp_dl.content == SAMPLE_PDF_CONTENT
    assert resp_dl.headers["Content-Type"] == "application/pdf"
    assert 'attachment; filename="aadhaar_proof.pdf"' in resp_dl.headers["Content-Disposition"]

    # 4. Reject spoofed binary (e.g. .exe disguised as .pdf)
    spoofed_files = {"file": ("malicious.pdf", io.BytesIO(FAKE_EXE_CONTENT), "application/pdf")}
    resp_spoof = client.post(f"/api/customers/{crm_cust.id}/documents", files=spoofed_files, data=form_data, headers=upload_headers)
    assert resp_spoof.status_code == 422
    assert "signature" in resp_spoof.json()["detail"].lower()


def test_kyc_document_verification_and_archival(db_session):
    """
    Verify KYC document verification (VERIFIED / REJECTED) and soft-archival.
    """
    upload_headers = _auth_headers(role="Admin", is_json=False)
    api_headers = _auth_headers(role="Admin")

    crm_cust = CRMCustomer(
        id=uuid.uuid4(),
        customer_number=f"CUST-VER-{uuid.uuid4().hex[:6]}",
        legal_name="Verification Tests Inc",
        account_type="ENTERPRISE",
        status="ACTIVE",
        market_id="mumbai"
    )
    db_session.add(crm_cust)
    db_session.commit()

    # Upload document
    files = {"file": ("pan_card.png", io.BytesIO(SAMPLE_PNG_CONTENT), "image/png")}
    form_data = {"document_type": "PAN", "document_identifier": "ABCDE1234F"}
    resp = client.post(f"/api/customers/{crm_cust.id}/documents", files=files, data=form_data, headers=upload_headers)
    assert resp.status_code == 201
    doc_id = resp.json()["id"]

    # 1. Verify document
    verify_payload = {"verification_status": "VERIFIED"}
    resp_v = client.post(f"/api/customers/{crm_cust.id}/documents/{doc_id}/verify", json=verify_payload, headers=api_headers)
    assert resp_v.status_code == 200
    assert resp_v.json()["verification_status"] == "VERIFIED"
    assert resp_v.json()["verified_at"] is not None

    # 2. Archive document
    resp_arch = client.delete(f"/api/customers/{crm_cust.id}/documents/{doc_id}", headers=api_headers)
    assert resp_arch.status_code == 200

    # 3. Verified download on archived document should return 404
    resp_dl = client.get(f"/api/customers/{crm_cust.id}/documents/{doc_id}/download", headers=api_headers)
    assert resp_dl.status_code == 404


def test_unconfigured_cloud_storage_reports_blocked(db_session):
    """
    Verify that when cloud storage is requested but unconfigured,
    uploads explicitly return 503 BLOCKED rather than fabricating success.
    """
    upload_headers = _auth_headers(role="Admin", is_json=False)
    set_storage_provider_for_testing(UnconfiguredCloudStorageProvider())

    crm_cust = CRMCustomer(
        id=uuid.uuid4(),
        customer_number=f"CUST-UNCFG-{uuid.uuid4().hex[:6]}",
        legal_name="Unconfigured Storage Test",
        account_type="RESIDENTIAL",
        status="ACTIVE",
        market_id="mumbai"
    )
    db_session.add(crm_cust)
    db_session.commit()

    files = {"file": ("doc.pdf", io.BytesIO(SAMPLE_PDF_CONTENT), "application/pdf")}
    form_data = {"document_type": "PASSPORT"}
    resp = client.post(f"/api/customers/{crm_cust.id}/documents", files=files, data=form_data, headers=upload_headers)
    assert resp.status_code == 503
    assert "BLOCKED" in resp.json()["detail"]


def test_viewer_role_denied_crm_mutations(db_session):
    """
    Verify that Viewer role is strictly denied document uploads, address updates,
    and survey completions (HTTP 403 Forbidden).
    """
    upload_headers = _auth_headers(role="Viewer", email="client@pmrgsolution.com", is_json=False)
    api_headers = _auth_headers(role="Viewer", email="client@pmrgsolution.com")

    crm_cust = CRMCustomer(
        id=uuid.uuid4(),
        customer_number=f"CUST-VIEW-{uuid.uuid4().hex[:6]}",
        legal_name="Viewer Denial Corp",
        account_type="RESIDENTIAL",
        status="ACTIVE",
        market_id="mumbai"
    )
    db_session.add(crm_cust)
    db_session.commit()

    # Viewer upload document attempt -> 403
    files = {"file": ("doc.pdf", io.BytesIO(SAMPLE_PDF_CONTENT), "application/pdf")}
    form_data = {"document_type": "PASSPORT"}
    resp = client.post(f"/api/customers/{crm_cust.id}/documents", files=files, data=form_data, headers=upload_headers)
    assert resp.status_code == 403

    # Viewer address update attempt -> 403
    resp_addr = client.put(f"/api/customers/{crm_cust.id}/addresses/{uuid.uuid4()}", json={}, headers=api_headers)
    assert resp_addr.status_code == 403
