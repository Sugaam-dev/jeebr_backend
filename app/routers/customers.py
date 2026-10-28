import uuid
import re
from datetime import datetime, timedelta
from typing import List, Optional, Any, Dict
from fastapi import APIRouter, Depends, HTTPException, Query, Header, Request, Response, status, UploadFile, File, Form
from fastapi.responses import StreamingResponse
import io
from sqlalchemy import func, or_, and_
from sqlalchemy.orm import Session, joinedload
from app.database import get_db
from app.models import Customer, Node, UsageRecord, Ticket, Invoice, Recommendation
from app.models_crm import (
    CRMCustomer, CRMContact, CRMAddress, CRMDocument, CRMActivity,
    AccountType, CustomerLifecycleStatus, ContactRole, AddressType,
    ServiceabilityStatus, ActivityType, DocumentType, DocumentUploadStatus, VerificationStatus
)
from app.models_finance import VoucherType
from app.schemas import (
    CustomerListResponse, Customer360Response, UsageSummary, TicketSummary,
    InvoiceSummary, NodeResponse,
    CRMCustomerCreate, CRMCustomerUpdate, CRMCustomerDetailResponse,
    CRMContactCreate, CRMContactUpdate, CRMContactResponse,
    CRMAddressCreate, CRMAddressUpdate, CRMAddressResponse,
    CRMDocumentResponse, CRMDocumentVerifyRequest, CRMSurveyCompleteRequest,
    CRMActivityResponse, PaginatedCustomersResponse,
    CRMLeadCreate, CRMLeadResponse, CRMLeadConvertRequest,
    CustomerMapSummaryResponse, CustomerMapLocationItem,
    CRMVoucherResponse
)
from app.auth import get_current_user, require_roles
from app.markets import get_current_market, get_user_authorized_markets, MARKETS
from app.services.rbac_service import is_super_admin, normalize_role_name
from app.services.churn_engine import evaluate_customer_signals
from app.services.journey_engine import evaluate_single_customer_journey
from app.services.document_storage import (
    get_storage_provider, validate_document_payload, generate_storage_key,
    mask_document_identifier, DocumentValidationError
)
from app.services.survey_dispatch_service import (
    dispatch_field_survey_if_needed, complete_field_survey
)

router = APIRouter(prefix="/customers", tags=["Customers & CRM"])

# ─── Internal Helper Functions ────────────────────────────────────────────────

def _resolve_customer_record(
    identifier: str,
    db: Session,
    current_user,
    x_market_id: Optional[str] = None
) -> tuple[Optional[Customer], Optional[CRMCustomer]]:
    """
    Safely resolves customer by legacy integer ID, UUID, or customer code.
    Enforces market-level tenant isolation and customer directory security.
    """
    customer: Optional[Customer] = None
    crm_cust: Optional[CRMCustomer] = None

    # 1. Identifier type resolution
    is_uuid = False
    try:
        parsed_uuid = uuid.UUID(str(identifier))
        is_uuid = True
    except (ValueError, AttributeError):
        parsed_uuid = None

    if is_uuid and parsed_uuid:
        crm_cust = db.query(CRMCustomer).filter(CRMCustomer.id == parsed_uuid).first()
        if crm_cust and crm_cust.legacy_customer_id:
            customer = db.query(Customer).filter(Customer.id == crm_cust.legacy_customer_id).first()
    elif str(identifier).isdigit():
        customer = db.query(Customer).filter(Customer.id == int(identifier)).first()
        if customer:
            crm_cust = db.query(CRMCustomer).filter(
                or_(
                    CRMCustomer.legacy_customer_id == customer.id,
                    CRMCustomer.customer_number == customer.customer_code
                )
            ).first()
    else:
        # Code lookup
        customer = db.query(Customer).filter(Customer.customer_code == identifier).first()
        crm_cust = db.query(CRMCustomer).filter(CRMCustomer.customer_number == identifier).first()
        if customer and not crm_cust:
            crm_cust = db.query(CRMCustomer).filter(CRMCustomer.legacy_customer_id == customer.id).first()
        elif crm_cust and not customer and crm_cust.legacy_customer_id:
            customer = db.query(Customer).filter(Customer.id == crm_cust.legacy_customer_id).first()

    if not customer and not crm_cust:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    # 2. Market authorization verification
    record_market = (customer.market_id if customer else crm_cust.market_id or "").strip().lower()
    authorized_markets, _ = get_user_authorized_markets(current_user, db)
    is_super = is_super_admin(current_user.role)

    if x_market_id:
        chosen = x_market_id.strip().lower()
        if chosen not in MARKETS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid or unsupported market identifier: '{chosen}'"
            )
        if not is_super and chosen not in authorized_markets:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"User '{current_user.email}' is not authorized to access market '{chosen}'"
            )
        if record_market != chosen:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Customer does not belong to authorized market '{chosen}'"
            )

    if not is_super and record_market not in authorized_markets:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Customer does not belong to authorized market"
        )

    # 3. Customer-role data isolation (SEC-S1-03)
    if normalize_role_name(current_user.role) == "Customer":
        user_email = (current_user.email or "").lower().strip()
        cust_email = (customer.email if customer else "").lower().strip()
        if not cust_email and crm_cust:
            primary_contact = db.query(CRMContact).filter(
                CRMContact.crm_customer_id == crm_cust.id,
                CRMContact.is_primary == True,
                CRMContact.is_archived == False
            ).first()
            if primary_contact:
                cust_email = (primary_contact.email or "").lower().strip()

        if cust_email != user_email:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: Cannot access another customer's profile"
            )

    return customer, crm_cust


def _ensure_crm_customer(customer: Customer, db: Session) -> CRMCustomer:
    """
    Finds or lazily creates a synchronized CRMCustomer record for an operational customer.
    """
    crm_cust = db.query(CRMCustomer).filter(
        or_(
            CRMCustomer.legacy_customer_id == customer.id,
            CRMCustomer.customer_number == customer.customer_code
        )
    ).first()

    if not crm_cust:
        acct_type = AccountType.RESIDENTIAL
        if customer.customer_type and "Postpaid" in customer.customer_type:
            acct_type = AccountType.ENTERPRISE if "Enterprise" in (customer.segment or "") else AccountType.SME

        crm_status = CustomerLifecycleStatus.ACTIVE
        if customer.status == "Churned":
            crm_status = CustomerLifecycleStatus.TERMINATED

        crm_cust = CRMCustomer(
            id=uuid.uuid4(),
            customer_number=customer.customer_code[:32],
            legal_name=customer.name,
            account_type=acct_type,
            status=crm_status,
            market_id=customer.market_id or "mumbai",
            legacy_customer_id=customer.id,
            billing_currency="INR",
            is_archived=False,
            created_at=customer.created_at or datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(crm_cust)
        db.flush()

        # Add initial primary contact if email or phone exists
        if customer.email or customer.phone:
            parts = customer.name.split(" ", 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else None
            contact = CRMContact(
                id=uuid.uuid4(),
                crm_customer_id=crm_cust.id,
                first_name=first_name,
                last_name=last_name,
                email=customer.email,
                phone=customer.phone,
                role=ContactRole.PRIMARY,
                is_primary=True,
                is_verified=True,
                is_archived=False
            )
            db.add(contact)

        # Add initial address if service address exists
        if customer.service_address or customer.locality:
            addr = CRMAddress(
                id=uuid.uuid4(),
                crm_customer_id=crm_cust.id,
                address_type=AddressType.INSTALLATION,
                address_line1=customer.service_address or f"Locality: {customer.locality}",
                city=customer.locality or "Mumbai",
                state="Maharashtra" if (customer.market_id or "mumbai").lower() == "mumbai" else "West Bengal",
                postal_code="400001" if (customer.market_id or "mumbai").lower() == "mumbai" else "700001",
                latitude=customer.service_latitude,
                longitude=customer.service_longitude,
                serviceability_status=ServiceabilityStatus.SERVICEABLE if (customer.service_latitude and customer.service_longitude) else ServiceabilityStatus.PENDING_SURVEY,
                is_archived=False
            )
            db.add(addr)

        try:
            db.commit()
            db.refresh(crm_cust)
        except Exception:
            db.rollback()
            crm_cust = db.query(CRMCustomer).filter(
                or_(
                    CRMCustomer.legacy_customer_id == customer.id,
                    CRMCustomer.customer_number == customer.customer_code
                )
            ).first()
            if not crm_cust:
                raise

    return crm_cust


def _build_customer_list_item(c: Customer, market: str) -> CustomerListResponse:
    node_name = c.node.node_name if c.node else None
    return CustomerListResponse(
        id=c.id,
        market_id=c.market_id or market,
        customer_code=c.customer_code,
        name=c.name,
        email=c.email,
        phone=c.phone,
        locality=c.locality,
        segment=c.segment,
        customer_type=c.customer_type or "Prepaid",
        plan_name=c.plan_name,
        plan_price=c.plan_price or c.arpu,
        revenue_30d=c.revenue_30d if hasattr(c, 'revenue_30d') and c.revenue_30d is not None else (c.actual_arpu or c.arpu),
        actual_arpu=c.actual_arpu or c.arpu,
        arpu=c.actual_arpu or c.arpu,
        recharge_validity_days=c.recharge_validity_days or 28,
        days_to_expiry=c.days_to_expiry if c.days_to_expiry is not None else 14,
        validity_status=c.validity_status or "Active",
        daily_data_quota_gb=c.daily_data_quota_gb or 1.5,
        daily_data_used_gb=c.daily_data_used_gb or 0.8,
        last_recharge_date=c.last_recharge_date,
        last_recharge_amount=c.last_recharge_amount or c.plan_price,
        payment_method=c.payment_method or "UPI",
        tenure_months=c.tenure_months,
        status=c.status,
        current_stage=c.current_stage or 'Use',
        nps_score=c.nps_score,
        node_id=c.node_id,
        node_name=node_name
    )


def _build_detail_response(customer: Optional[Customer], crm_cust: Optional[CRMCustomer], db: Session) -> CRMCustomerDetailResponse:
    market = customer.market_id if customer else (crm_cust.market_id if crm_cust else "mumbai")
    base_item = _build_customer_list_item(customer, market) if customer else CustomerListResponse(
        id=0,
        market_id=crm_cust.market_id,
        customer_code=crm_cust.customer_number,
        name=crm_cust.legal_name,
        email="",
        phone="",
        locality="",
        segment=crm_cust.account_type,
        customer_type="Prepaid",
        plan_name="Standard",
        plan_price=299.0,
        revenue_30d=299.0,
        actual_arpu=299.0,
        arpu=299.0,
        tenure_months=1,
        status=crm_cust.status,
        current_stage="Use",
        nps_score=8
    )

    contacts_res = []
    addresses_res = []
    documents_res = []
    activities_res = []

    if crm_cust:
        contacts = db.query(CRMContact).filter(
            CRMContact.crm_customer_id == crm_cust.id,
            CRMContact.is_archived == False
        ).order_by(CRMContact.is_primary.desc(), CRMContact.created_at.asc()).all()
        for ct in contacts:
            contacts_res.append(CRMContactResponse(
                id=str(ct.id),
                crm_customer_id=str(ct.crm_customer_id),
                first_name=ct.first_name,
                last_name=ct.last_name,
                email=ct.email,
                phone=ct.phone,
                role=ct.role,
                is_primary=ct.is_primary,
                is_verified=ct.is_verified,
                is_archived=ct.is_archived,
                created_at=ct.created_at,
                updated_at=ct.updated_at
            ))

        addresses = db.query(CRMAddress).filter(
            CRMAddress.crm_customer_id == crm_cust.id,
            CRMAddress.is_archived == False
        ).order_by(CRMAddress.created_at.asc()).all()
        for ad in addresses:
            addr_tag = f"[SURVEY-ADDR:{ad.id}]"
            srv_tck = db.query(Ticket).filter(
                Ticket.description.contains(addr_tag),
                Ticket.status.notin_(["Resolved", "Closed", "Rejected"])
            ).first()
            addresses_res.append(CRMAddressResponse(
                id=str(ad.id),
                crm_customer_id=str(ad.crm_customer_id),
                address_type=ad.address_type,
                address_line1=ad.address_line1,
                address_line2=ad.address_line2,
                city=ad.city,
                state=ad.state,
                postal_code=ad.postal_code,
                latitude=ad.latitude,
                longitude=ad.longitude,
                serviceability_status=ad.serviceability_status,
                candidate_node_id=ad.candidate_node_id,
                distance_to_node_meters=ad.distance_to_node_meters,
                survey_ticket_code=srv_tck.ticket_code if srv_tck else None,
                survey_ticket_status=srv_tck.status if srv_tck else None,
                is_archived=ad.is_archived,
                created_at=ad.created_at,
                updated_at=ad.updated_at
            ))

        documents = db.query(CRMDocument).filter(
            CRMDocument.crm_customer_id == crm_cust.id,
            CRMDocument.is_archived == False
        ).order_by(CRMDocument.created_at.desc()).all()
        for doc in documents:
            documents_res.append(CRMDocumentResponse(
                id=str(doc.id),
                crm_customer_id=str(doc.crm_customer_id),
                document_type=doc.document_type,
                document_identifier=doc.document_identifier,
                file_name=doc.file_name,
                file_size_bytes=doc.file_size_bytes,
                mime_type=doc.mime_type,
                upload_status=doc.upload_status,
                verification_status=doc.verification_status,
                verified_by_id=doc.verified_by_id,
                verified_at=doc.verified_at,
                rejection_reason=doc.rejection_reason,
                is_archived=doc.is_archived,
                created_at=doc.created_at,
                updated_at=doc.updated_at
            ))

        activities = db.query(CRMActivity).filter(
            CRMActivity.crm_customer_id == crm_cust.id
        ).order_by(CRMActivity.created_at.desc()).limit(25).all()
        for ac in activities:
            activities_res.append(CRMActivityResponse(
                id=str(ac.id),
                crm_customer_id=str(ac.crm_customer_id),
                activity_type=ac.activity_type,
                summary=ac.summary,
                details=ac.details,
                performed_by_id=ac.performed_by_id,
                created_at=ac.created_at
            ))

    data_dict = base_item.model_dump()
    data_dict.update({
        "crm_id": str(crm_cust.id) if crm_cust else None,
        "legal_name": crm_cust.legal_name if crm_cust else base_item.name,
        "trade_name": crm_cust.trade_name if crm_cust else None,
        "account_type": crm_cust.account_type if crm_cust else "RESIDENTIAL",
        "tax_id": crm_cust.tax_id if crm_cust else None,
        "billing_currency": crm_cust.billing_currency if crm_cust else "INR",
        "service_latitude": customer.service_latitude if customer else None,
        "service_longitude": customer.service_longitude if customer else None,
        "service_address": customer.service_address if customer else None,
        "is_archived": crm_cust.is_archived if crm_cust else False,
        "contacts": contacts_res,
        "addresses": addresses_res,
        "documents": documents_res,
        "activities": activities_res
    })
    return CRMCustomerDetailResponse(**data_dict)


# ─── 1. Customer Directory & Pagination ───────────────────────────────────────

@router.get("", response_model=List[CustomerListResponse])
def list_customers(
    response: Response,
    search: Optional[str] = None,
    locality: Optional[str] = None,
    segment: Optional[str] = None,
    status: Optional[str] = None,
    customer_type: Optional[str] = None,
    stage: Optional[str] = None,
    sort_by: Optional[str] = Query("name"),
    sort_order: Optional[str] = Query("asc"),
    page: Optional[int] = Query(None, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=200),
    limit: Optional[int] = Query(60, le=200),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """
    Search, filter, sort, and paginate customers.
    Enforces tenant/market isolation and attaches server-side pagination headers.
    """
    query = db.query(Customer).options(joinedload(Customer.node)).filter(Customer.market_id == market)

    # SEC-S1-03: Strict customer directory isolation for Customer role
    if normalize_role_name(current_user.role) == "Customer":
        user_email = (current_user.email or "").lower().strip()
        if not user_email:
            response.headers["X-Total-Count"] = "0"
            return []
        cust_records = db.query(Customer.id).filter(
            func.lower(Customer.email) == user_email,
            Customer.market_id == market
        ).all()
        cust_ids = [c[0] for c in cust_records]
        if not cust_ids:
            response.headers["X-Total-Count"] = "0"
            return []
        query = query.filter(Customer.id.in_(cust_ids))

    if search:
        s = search.strip()
        query = query.filter(
            or_(
                Customer.name.ilike(f"%{s}%"),
                Customer.customer_code.ilike(f"%{s}%"),
                Customer.email.ilike(f"%{s}%"),
                Customer.phone.ilike(f"%{s}%")
            )
        )
    if locality:
        query = query.filter(Customer.locality == locality)
    if segment:
        query = query.filter(Customer.segment == segment)
    if status:
        query = query.filter(Customer.status == status)
    if customer_type:
        query = query.filter(Customer.customer_type == customer_type)
    if stage:
        query = query.filter(Customer.current_stage == stage)

    # Sorting
    sort_col_map = {
        "name": Customer.name,
        "customer_code": Customer.customer_code,
        "created_at": Customer.created_at,
        "status": Customer.status,
        "actual_arpu": Customer.actual_arpu,
        "plan_price": Customer.plan_price,
        "id": Customer.id
    }
    col = sort_col_map.get(sort_by, Customer.id)
    query = query.order_by(col.desc() if (sort_order or "").lower() == "desc" else col.asc())

    total = query.count()
    effective_size = page_size or limit or 60
    current_page = page or 1

    if page is not None:
        offset = (current_page - 1) * effective_size
        customers = query.offset(offset).limit(effective_size).all()
    else:
        customers = query.limit(effective_size).all()

    total_pages = (total + effective_size - 1) // effective_size if effective_size > 0 else 1

    # Attach standard server-side pagination headers
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Page"] = str(current_page)
    response.headers["X-Page-Size"] = str(effective_size)
    response.headers["X-Total-Pages"] = str(total_pages)

    results = [_build_customer_list_item(c, market) for c in customers]
    return results


@router.get("/directory", response_model=PaginatedCustomersResponse)
def get_customer_directory(
    search: Optional[str] = None,
    locality: Optional[str] = None,
    segment: Optional[str] = None,
    status: Optional[str] = None,
    customer_type: Optional[str] = None,
    stage: Optional[str] = None,
    sort_by: Optional[str] = Query("name"),
    sort_order: Optional[str] = Query("asc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """
    Explicit paginated directory endpoint returning PaginatedCustomersResponse envelope.
    """
    query = db.query(Customer).options(joinedload(Customer.node)).filter(Customer.market_id == market)

    if normalize_role_name(current_user.role) == "Customer":
        user_email = (current_user.email or "").lower().strip()
        if not user_email:
            return PaginatedCustomersResponse(items=[], total=0, page=page, page_size=page_size, total_pages=0)
        cust_records = db.query(Customer.id).filter(
            func.lower(Customer.email) == user_email,
            Customer.market_id == market
        ).all()
        cust_ids = [c[0] for c in cust_records]
        if not cust_ids:
            return PaginatedCustomersResponse(items=[], total=0, page=page, page_size=page_size, total_pages=0)
        query = query.filter(Customer.id.in_(cust_ids))

    if search:
        s = search.strip()
        query = query.filter(
            or_(
                Customer.name.ilike(f"%{s}%"),
                Customer.customer_code.ilike(f"%{s}%"),
                Customer.email.ilike(f"%{s}%"),
                Customer.phone.ilike(f"%{s}%")
            )
        )
    if locality:
        query = query.filter(Customer.locality == locality)
    if segment:
        query = query.filter(Customer.segment == segment)
    if status:
        query = query.filter(Customer.status == status)
    if customer_type:
        query = query.filter(Customer.customer_type == customer_type)
    if stage:
        query = query.filter(Customer.current_stage == stage)

    sort_col_map = {
        "name": Customer.name,
        "customer_code": Customer.customer_code,
        "created_at": Customer.created_at,
        "status": Customer.status,
        "actual_arpu": Customer.actual_arpu,
        "plan_price": Customer.plan_price,
        "id": Customer.id
    }
    col = sort_col_map.get(sort_by, Customer.id)
    query = query.order_by(col.desc() if (sort_order or "").lower() == "desc" else col.asc())

    total = query.count()
    offset = (page - 1) * page_size
    customers = query.offset(offset).limit(page_size).all()
    total_pages = (total + page_size - 1) // page_size if page_size > 0 else 1

    items = [_build_customer_list_item(c, market) for c in customers]
    return PaginatedCustomersResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )


# ─── 2. Customer Master Creation ──────────────────────────────────────────────

@router.post("", response_model=CRMCustomerDetailResponse, status_code=status.HTTP_201_CREATED)
def create_customer(
    cust_in: CRMCustomerCreate,
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "NOC", "Care", "Revenue", "SUPER_ADMIN"])),
    market: str = Depends(get_current_market)
):
    """
    Create a new customer master record with CRM bridge, contact, address, and audit logging.
    Enforces validation, duplicate prevention, and transactional atomicity.
    """
    target_market = (cust_in.market_id or market).strip().lower()
    if target_market not in MARKETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid market identifier '{target_market}'. Supported: {list(MARKETS.keys())}"
        )

    # 1. Validate customer identifier
    cust_number = cust_in.customer_number
    if cust_number:
        cust_number = cust_number.strip()
        if len(cust_number) > 32:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Customer number must not exceed 32 characters."
            )
        if not re.match(r"^[A-Za-z0-9\-_]+$", cust_number):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Customer number may only contain alphanumeric characters, hyphens, and underscores."
            )
    else:
        # Generate safe canonical code
        short_id = uuid.uuid4().hex[:6].upper()
        cust_number = f"CUST-{target_market[:3].upper()}-{short_id}"

    # 2. Check for duplicate customer number
    if db.query(Customer).filter(Customer.customer_code == cust_number).first() or \
       db.query(CRMCustomer).filter(CRMCustomer.customer_number == cust_number).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Customer identifier '{cust_number}' already exists in the system."
        )

    # 3. Safe duplicate identity prevention (same phone AND email in same market)
    existing_dup = db.query(Customer).filter(
        func.lower(Customer.email) == cust_in.email.lower().strip(),
        Customer.phone == cust_in.phone.strip(),
        Customer.market_id == target_market
    ).first()
    if existing_dup:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An active subscriber with matching email and phone already exists in market '{target_market}' ({existing_dup.customer_code})."
        )

    # 4. Validate Account Type and Status
    acct_type = cust_in.account_type.upper()
    if acct_type not in AccountType.ALL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid account_type '{cust_in.account_type}'. Allowed: {AccountType.ALL}"
        )

    stat = cust_in.status.upper()
    if stat not in CustomerLifecycleStatus.ALL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid lifecycle status '{cust_in.status}'. Allowed: {CustomerLifecycleStatus.ALL}"
        )

    # 5. Transactional Creation
    try:
        # Create operational Customer row
        new_customer = Customer(
            market_id=target_market,
            customer_code=cust_number,
            name=cust_in.legal_name,
            email=cust_in.email,
            phone=cust_in.phone,
            locality=cust_in.locality or "Colaba",
            segment=cust_in.segment or "Prepaid - Daily Unlimited",
            customer_type=cust_in.customer_type or "Prepaid",
            plan_name=cust_in.plan_name or "Unlimited 30D",
            plan_price=cust_in.plan_price or 299.0,
            revenue_30d=cust_in.plan_price or 299.0,
            actual_arpu=cust_in.plan_price or 299.0,
            arpu=cust_in.plan_price or 299.0,
            status="Active" if stat in (CustomerLifecycleStatus.ACTIVE, CustomerLifecycleStatus.PENDING_VERIFICATION) else "Churned",
            current_stage="Use" if stat == CustomerLifecycleStatus.ACTIVE else "Acquisition",
            created_at=datetime.utcnow()
        )
        db.add(new_customer)
        db.flush()

        # Create CRMCustomer master row
        crm_cust_id = uuid.uuid4()
        crm_cust = CRMCustomer(
            id=crm_cust_id,
            customer_number=cust_number,
            legal_name=cust_in.legal_name,
            trade_name=cust_in.trade_name,
            account_type=acct_type,
            status=stat,
            market_id=target_market,
            legacy_customer_id=new_customer.id,
            tax_id=cust_in.tax_id,
            billing_currency=cust_in.billing_currency or "INR",
            is_archived=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(crm_cust)
        db.flush()

        # Create Primary Contact
        contact_in = cust_in.primary_contact
        first_name = contact_in.first_name if contact_in else cust_in.legal_name.split()[0]
        last_name = contact_in.last_name if contact_in else (" ".join(cust_in.legal_name.split()[1:]) if len(cust_in.legal_name.split()) > 1 else None)
        c_email = contact_in.email if contact_in and contact_in.email else cust_in.email
        c_phone = contact_in.phone if contact_in and contact_in.phone else cust_in.phone
        c_role = contact_in.role if contact_in and contact_in.role in ContactRole.ALL else ContactRole.PRIMARY

        primary_contact = CRMContact(
            id=uuid.uuid4(),
            crm_customer_id=crm_cust.id,
            first_name=first_name,
            last_name=last_name,
            email=c_email,
            phone=c_phone,
            role=c_role,
            is_primary=True,
            is_verified=True,
            is_archived=False
        )
        db.add(primary_contact)

        # Create Installation Address if specified
        addr_in = cust_in.installation_address
        if addr_in:
            lat = addr_in.latitude
            lng = addr_in.longitude
            serv_status = addr_in.serviceability_status or ServiceabilityStatus.PENDING_SURVEY
            if lat is not None and lng is not None:
                if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="Invalid coordinates: latitude must be between -90 and 90, longitude between -180 and 180."
                    )
                serv_status = ServiceabilityStatus.SERVICEABLE

            address = CRMAddress(
                id=uuid.uuid4(),
                crm_customer_id=crm_cust.id,
                address_type=addr_in.address_type if addr_in.address_type in AddressType.ALL else AddressType.INSTALLATION,
                address_line1=addr_in.address_line1,
                address_line2=addr_in.address_line2,
                city=addr_in.city,
                state=addr_in.state,
                postal_code=addr_in.postal_code,
                latitude=lat,
                longitude=lng,
                serviceability_status=serv_status,
                is_archived=False
            )
            db.add(address)
            # Update customer service coordinates
            new_customer.service_latitude = lat
            new_customer.service_longitude = lng
            new_customer.service_address = addr_in.address_line1

        # Audit Activity Log
        activity = CRMActivity(
            id=uuid.uuid4(),
            crm_customer_id=crm_cust.id,
            activity_type=ActivityType.NOTE,
            summary="Customer Master Created",
            details=f"Account created by {current_user.email} ({current_user.role}) for market {target_market}.",
            performed_by_id=current_user.id
        )
        db.add(activity)

        db.commit()
        db.refresh(new_customer)
        db.refresh(crm_cust)

        return _build_detail_response(new_customer, crm_cust, db)

    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create customer record: {str(exc)}"
        )


# ─── 3. Customer Maps ─────────────────────────────────────────────────────────

@router.get("/map-locations", response_model=CustomerMapSummaryResponse)
def get_customer_map_locations(
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "NOC", "Care", "Revenue", "Executive", "SUPER_ADMIN"])),
    market: str = Depends(get_current_market)
):
    """
    Retrieve customer GIS coordinates and serviceability status for map rendering.
    Enforces market isolation and handles empty and missing-coordinate states safely.
    """
    customers = db.query(Customer).filter(Customer.market_id == market).all()
    locations: List[CustomerMapLocationItem] = []
    with_coords = 0
    missing_coords = 0

    for c in customers:
        has_c = False
        lat = c.service_latitude
        lng = c.service_longitude
        addr_str = c.service_address or c.locality

        if lat is not None and lng is not None:
            if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
                has_c = True
                with_coords += 1
            else:
                lat = None
                lng = None
                missing_coords += 1
        else:
            missing_coords += 1

        locations.append(CustomerMapLocationItem(
            id=c.id,
            customer_code=c.customer_code,
            name=c.name,
            customer_type=c.customer_type or "Prepaid",
            status=c.status,
            locality=c.locality,
            plan_name=c.plan_name,
            latitude=lat,
            longitude=lng,
            address=addr_str,
            has_coordinates=has_c,
            serviceability_status=ServiceabilityStatus.SERVICEABLE if has_c else ServiceabilityStatus.PENDING_SURVEY
        ))

    return CustomerMapSummaryResponse(
        market_id=market,
        total_customers=len(customers),
        with_coordinates_count=with_coords,
        missing_coordinates_count=missing_coords,
        locations=locations
    )


# ─── 4. Vouchers Portfolio ────────────────────────────────────────────────────

@router.get("/vouchers", response_model=List[CRMVoucherResponse])
def list_customer_vouchers(
    customer_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """
    Inspect customer vouchers and retention concession proposals.
    Documents the dependency on the future OSS/BSS Billing & Provisioning Engine (Phase 2).
    """
    query = db.query(Customer).filter(Customer.market_id == market)
    if customer_id:
        c_id_str = str(customer_id).strip()
        if c_id_str.isdigit():
            query = query.filter(Customer.id == int(c_id_str))
        else:
            query = query.filter(Customer.customer_code == c_id_str)

    if normalize_role_name(current_user.role) == "Customer":
        user_email = (current_user.email or "").lower().strip()
        query = query.filter(func.lower(Customer.email) == user_email)

    customers = query.limit(50).all()
    vouchers: List[CRMVoucherResponse] = []

    from app.models_finance import VoucherRedemption
    redemptions = {r.voucher_code: r for r in db.query(VoucherRedemption).all()}

    for c in customers:
        # 1. 5G Data Booster Voucher for all Prepaid subscribers
        if c.customer_type == "Prepaid":
            vch_code = f"VCH-5GB-{c.customer_code.replace('CUST-', '')}"
            is_redeemed = vch_code in redemptions
            v_status = redemptions[vch_code].status if is_redeemed else ("AVAILABLE" if c.status == "Active" else "PENDING_PROVISIONING")
            dep_msg = "Fulfilled via SentinelOS Billing & Provisioning Engine" if is_redeemed else "OSS/BSS Billing & Provisioning Engine (Phase 2)"

            vouchers.append(CRMVoucherResponse(
                id=f"vch-5gb-{c.id}",
                voucher_code=vch_code,
                customer_id=c.id,
                customer_code=c.customer_code,
                customer_name=c.name,
                title="5GB High-Speed 5G Bonus Data Booster",
                benefit="5GB Data Booster valid for remaining base pack validity period.",
                type="DATA_BOOSTER",
                status=v_status,
                source_module="Predictive Churn Engine & Intelligent Journeys",
                expiry_days=max(3, c.days_to_expiry if c.days_to_expiry else 14),
                provisioning_dependency=dep_msg,
                created_at=datetime.utcnow()
            ))

        # 2. Retention Concession Voucher for At-Risk subscribers
        if c.status == "At-Risk":
            vch_code = f"VCH-RET-{c.customer_code.replace('CUST-', '')}"
            is_redeemed = vch_code in redemptions
            v_status = redemptions[vch_code].status if is_redeemed else "STAGED"
            dep_msg = "Fulfilled via SentinelOS Billing & Provisioning Engine" if is_redeemed else "OSS/BSS Billing & Provisioning Engine (Phase 2)"

            vouchers.append(CRMVoucherResponse(
                id=f"vch-retention-{c.id}",
                voucher_code=vch_code,
                customer_id=c.id,
                customer_code=c.customer_code,
                customer_name=c.name,
                title="Speed Boost & 20% UPI Concession Retention Voucher",
                benefit="Temporary speed tier boost + 20% UPI renewal concession.",
                type="SPEED_BOOST",
                status=v_status,
                source_module="Pilot Retention Bundle",
                expiry_days=7,
                provisioning_dependency=dep_msg,
                created_at=datetime.utcnow()
            ))

    return vouchers


@router.post("/{customer_id}/vouchers/issue", response_model=CRMVoucherResponse)
def issue_customer_voucher(
    customer_id: str,
    voucher_type: str = Query("SPEED_BOOST"),
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "Care", "Revenue", "SUPER_ADMIN"])),
    market: str = Depends(get_current_market)
):
    """
    Stage an autonomous customer voucher for retention or goodwill.
    Logs activity and documents the pending billing engine provisioning dependency.
    """
    v_norm = voucher_type.upper().strip()
    if v_norm == "DATA_BOOSTER":
        v_norm = VoucherType.DATA_TOPUP
    if v_norm not in VoucherType.ALL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid voucher_type '{voucher_type}'. Allowed types: {list(VoucherType.ALL)}"
        )

    customer, crm_cust = _resolve_customer_record(str(customer_id).strip(), db, current_user)
    if not customer and not crm_cust:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Customer '{customer_id}' not found.")

    if not customer and crm_cust:
        customer = _ensure_operational_customer(crm_cust, db)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Operational customer '{customer_id}' not found.")

    # Account status & eligibility check
    cust_status = getattr(customer, "status", "")
    if cust_status in ("Churned", "Terminated", "Suspended"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Cannot issue courtesy voucher: customer lifecycle status is '{cust_status}'."
        )

    crm_c = _ensure_crm_customer(customer, db)

    # Duplicate / rate limiting check: prevent issuing duplicate courtesy vouchers of same type within 5 mins
    recent = db.query(CRMActivity).filter(
        CRMActivity.crm_customer_id == crm_c.id,
        CRMActivity.summary.like(f"Issued Courtesy Voucher VCH-{v_norm[:4].upper()}%"),
        CRMActivity.created_at >= datetime.utcnow() - timedelta(minutes=5)
    ).first()
    if recent:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Duplicate request: A courtesy voucher of type '{v_norm}' was already issued for this subscriber recently."
        )

    vch_code = f"VCH-{v_norm[:4].upper()}-{customer.customer_code.replace('CUST-', '')}-{int(datetime.utcnow().timestamp())%10000}"

    activity = CRMActivity(
        id=uuid.uuid4(),
        crm_customer_id=crm_c.id,
        activity_type=ActivityType.NOTE,
        summary=f"Issued Courtesy Voucher {vch_code}",
        details=f"Authorized by {current_user.email}. Physical network activation gated on Billing Engine (Phase 2).",
        performed_by_id=current_user.id
    )
    db.add(activity)
    db.commit()

    return CRMVoucherResponse(
        id=f"vch-cust-{customer.id}-{int(datetime.utcnow().timestamp())}",
        voucher_code=vch_code,
        customer_id=customer.id,
        customer_code=customer.customer_code,
        customer_name=customer.name,
        title=f"Courtesy {v_norm.replace('_', ' ').title()} Voucher",
        benefit="Complimentary telecom concession applied to subscriber profile.",
        type=v_norm,
        status="STAGED",
        source_module="CRM Voucher Console",
        expiry_days=14,
        provisioning_dependency="OSS/BSS Billing & Provisioning Engine (Phase 2)",
        created_at=datetime.utcnow()
    )


# ─── 5. Leads Management & Conversion ─────────────────────────────────────────

@router.get("/leads", response_model=List[CRMLeadResponse])
def list_leads(
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "NOC", "Care", "Revenue", "SUPER_ADMIN"])),
    market: str = Depends(get_current_market)
):
    """
    List CRM leads / prospects for the current market.
    """
    query = db.query(CRMCustomer).filter(
        CRMCustomer.market_id == market,
        CRMCustomer.is_archived == False
    )

    if status_filter:
        query = query.filter(CRMCustomer.status == status_filter)
    else:
        # Default: Prospects and unlinked native CRM records
        query = query.filter(
            or_(
                CRMCustomer.status == CustomerLifecycleStatus.PROSPECT,
                CRMCustomer.legacy_customer_id.is_(None)
            )
        )

    if search:
        s = search.strip()
        query = query.filter(
            or_(
                CRMCustomer.legal_name.ilike(f"%{s}%"),
                CRMCustomer.trade_name.ilike(f"%{s}%"),
                CRMCustomer.customer_number.ilike(f"%{s}%")
            )
        )

    crm_leads = query.order_by(CRMCustomer.created_at.desc()).limit(100).all()
    results = []

    for l in crm_leads:
        contacts = db.query(CRMContact).filter(
            CRMContact.crm_customer_id == l.id,
            CRMContact.is_archived == False
        ).all()
        addresses = db.query(CRMAddress).filter(
            CRMAddress.crm_customer_id == l.id,
            CRMAddress.is_archived == False
        ).all()
        activities = db.query(CRMActivity).filter(
            CRMActivity.crm_customer_id == l.id
        ).all()

        p_contact = next((c for c in contacts if c.is_primary), (contacts[0] if contacts else None))

        results.append(CRMLeadResponse(
            id=str(l.id),
            customer_number=l.customer_number,
            legal_name=l.legal_name,
            trade_name=l.trade_name,
            email=p_contact.email if p_contact else None,
            phone=p_contact.phone if p_contact else None,
            market_id=l.market_id,
            account_type=l.account_type,
            status=l.status,
            is_converted=l.legacy_customer_id is not None,
            legacy_customer_id=l.legacy_customer_id,
            created_at=l.created_at,
            contacts=[
                CRMContactResponse(
                    id=str(ct.id),
                    crm_customer_id=str(ct.crm_customer_id),
                    first_name=ct.first_name,
                    last_name=ct.last_name,
                    email=ct.email,
                    phone=ct.phone,
                    role=ct.role,
                    is_primary=ct.is_primary,
                    is_verified=ct.is_verified,
                    is_archived=ct.is_archived,
                    created_at=ct.created_at,
                    updated_at=ct.updated_at
                ) for ct in contacts
            ],
            addresses=[
                CRMAddressResponse(
                    id=str(ad.id),
                    crm_customer_id=str(ad.crm_customer_id),
                    address_type=ad.address_type,
                    address_line1=ad.address_line1,
                    address_line2=ad.address_line2,
                    city=ad.city,
                    state=ad.state,
                    postal_code=ad.postal_code,
                    latitude=ad.latitude,
                    longitude=ad.longitude,
                    serviceability_status=ad.serviceability_status,
                    is_archived=ad.is_archived,
                    created_at=ad.created_at,
                    updated_at=ad.updated_at
                ) for ad in addresses
            ],
            activities=[
                CRMActivityResponse(
                    id=str(ac.id),
                    crm_customer_id=str(ac.crm_customer_id),
                    activity_type=ac.activity_type,
                    summary=ac.summary,
                    details=ac.details,
                    performed_by_id=ac.performed_by_id,
                    created_at=ac.created_at
                ) for ac in activities
            ]
        ))

    return results


@router.post("/leads", response_model=CRMLeadResponse, status_code=status.HTTP_201_CREATED)
def create_lead(
    lead_in: CRMLeadCreate,
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "NOC", "Care", "Revenue", "SUPER_ADMIN"])),
    market: str = Depends(get_current_market)
):
    """
    Capture a new sales lead / prospect in CRM with primary contact and serviceability address.
    """
    target_market = (lead_in.market_id or market).strip().lower()
    lead_code = f"LEAD-{target_market[:3].upper()}-{uuid.uuid4().hex[:6].upper()}"

    lead = CRMCustomer(
        id=uuid.uuid4(),
        customer_number=lead_code,
        legal_name=lead_in.legal_name,
        trade_name=lead_in.trade_name,
        account_type=lead_in.account_type if lead_in.account_type in AccountType.ALL else AccountType.RESIDENTIAL,
        status=CustomerLifecycleStatus.PROSPECT,
        market_id=target_market,
        legacy_customer_id=None,
        billing_currency="INR",
        is_archived=False,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(lead)
    db.flush()

    # Contact
    contact_res = []
    if lead_in.email or lead_in.phone:
        parts = lead_in.legal_name.split(" ", 1)
        c = CRMContact(
            id=uuid.uuid4(),
            crm_customer_id=lead.id,
            first_name=parts[0],
            last_name=parts[1] if len(parts) > 1 else None,
            email=lead_in.email,
            phone=lead_in.phone,
            role=ContactRole.PRIMARY,
            is_primary=True,
            is_verified=False,
            is_archived=False
        )
        db.add(c)
        db.flush()
        contact_res.append(CRMContactResponse(
            id=str(c.id),
            crm_customer_id=str(c.crm_customer_id),
            first_name=c.first_name,
            last_name=c.last_name,
            email=c.email,
            phone=c.phone,
            role=c.role,
            is_primary=c.is_primary,
            is_verified=c.is_verified,
            is_archived=c.is_archived,
            created_at=c.created_at
        ))

    # Address
    address_res = []
    if lead_in.address_line1:
        ad = CRMAddress(
            id=uuid.uuid4(),
            crm_customer_id=lead.id,
            address_type=AddressType.INSTALLATION,
            address_line1=lead_in.address_line1,
            city=lead_in.city or lead_in.locality or "Mumbai",
            state=lead_in.state or ("Maharashtra" if target_market == "mumbai" else "West Bengal"),
            postal_code=lead_in.postal_code or ("400001" if target_market == "mumbai" else "700001"),
            serviceability_status=ServiceabilityStatus.PENDING_SURVEY,
            is_archived=False
        )
        db.add(ad)
        db.flush()
        address_res.append(CRMAddressResponse(
            id=str(ad.id),
            crm_customer_id=str(ad.crm_customer_id),
            address_type=ad.address_type,
            address_line1=ad.address_line1,
            city=ad.city,
            state=ad.state,
            postal_code=ad.postal_code,
            serviceability_status=ad.serviceability_status,
            is_archived=ad.is_archived,
            created_at=ad.created_at
        ))

    # Activity log
    act = CRMActivity(
        id=uuid.uuid4(),
        crm_customer_id=lead.id,
        activity_type=ActivityType.NOTE,
        summary="Lead Captured in CRM",
        details=lead_in.notes or f"Lead registered by {current_user.email} in market {target_market}.",
        performed_by_id=current_user.id
    )
    db.add(act)
    db.commit()
    db.refresh(lead)

    return CRMLeadResponse(
        id=str(lead.id),
        customer_number=lead.customer_number,
        legal_name=lead.legal_name,
        trade_name=lead.trade_name,
        email=lead_in.email,
        phone=lead_in.phone,
        market_id=lead.market_id,
        account_type=lead.account_type,
        status=lead.status,
        is_converted=False,
        legacy_customer_id=None,
        created_at=lead.created_at,
        contacts=contact_res,
        addresses=address_res,
        activities=[
            CRMActivityResponse(
                id=str(act.id),
                crm_customer_id=str(act.crm_customer_id),
                activity_type=act.activity_type,
                summary=act.summary,
                details=act.details,
                performed_by_id=act.performed_by_id,
                created_at=act.created_at
            )
        ]
    )


@router.post("/leads/{lead_id}/convert", response_model=CRMCustomerDetailResponse)
def convert_lead_to_customer(
    lead_id: str,
    convert_in: CRMLeadConvertRequest,
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "Care", "Revenue", "SUPER_ADMIN"])),
    market: str = Depends(get_current_market)
):
    """
    Idempotent transactional conversion of a CRM lead / prospect into an operational Customer Master.
    Repeated requests safely return the existing customer without creating duplicates.
    """
    try:
        parsed_uuid = uuid.UUID(str(lead_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid lead ID format. Expected UUID.")

    lead = db.query(CRMCustomer).filter(CRMCustomer.id == parsed_uuid).with_for_update().first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    # 1. Idempotency Check: Already converted?
    if lead.legacy_customer_id is not None and lead.status != CustomerLifecycleStatus.PROSPECT:
        existing_cust = db.query(Customer).filter(Customer.id == lead.legacy_customer_id).first()
        if existing_cust:
            return _build_detail_response(existing_cust, lead, db)

    # 2. Retrieve contact & address
    primary_contact = db.query(CRMContact).filter(
        CRMContact.crm_customer_id == lead.id,
        CRMContact.is_archived == False
    ).order_by(CRMContact.is_primary.desc()).first()

    primary_addr = db.query(CRMAddress).filter(
        CRMAddress.crm_customer_id == lead.id,
        CRMAddress.is_archived == False
    ).first()

    # Generate customer code
    cust_code = f"CUST-{lead.market_id[:3].upper()}-{uuid.uuid4().hex[:6].upper()}"

    # 3. Create operational Customer
    cust = Customer(
        market_id=lead.market_id,
        customer_code=cust_code,
        name=lead.legal_name,
        email=primary_contact.email if primary_contact and primary_contact.email else f"{cust_code.lower()}@customer.pmrg.in",
        phone=primary_contact.phone if primary_contact and primary_contact.phone else "+91 98200 00000",
        locality=primary_addr.city if primary_addr else convert_in.locality or "Colaba",
        segment=convert_in.segment or "Residential - Standard",
        customer_type=convert_in.customer_type or "Prepaid",
        plan_name=convert_in.plan_name or "Fast 100M",
        plan_price=convert_in.plan_price or 499.0,
        revenue_30d=convert_in.plan_price or 499.0,
        actual_arpu=convert_in.plan_price or 499.0,
        arpu=convert_in.plan_price or 499.0,
        status="Active",
        current_stage="Install",
        service_address=primary_addr.address_line1 if primary_addr else None,
        service_latitude=primary_addr.latitude if primary_addr else None,
        service_longitude=primary_addr.longitude if primary_addr else None,
        created_at=datetime.utcnow()
    )
    db.add(cust)
    db.flush()

    # 4. Bridge & update CRMCustomer
    lead.legacy_customer_id = cust.id
    lead.status = CustomerLifecycleStatus.ACTIVE
    lead.updated_at = datetime.utcnow()

    # 5. Audit Activity
    act = CRMActivity(
        id=uuid.uuid4(),
        crm_customer_id=lead.id,
        activity_type=ActivityType.STATUS_CHANGE,
        summary="Converted Lead to Active Customer Master",
        details=f"Provisioned operational customer code {cust.customer_code} (ID: {cust.id}) by {current_user.email}.",
        performed_by_id=current_user.id
    )
    db.add(act)

    db.commit()
    db.refresh(cust)
    db.refresh(lead)

    return _build_detail_response(cust, lead, db)


# ─── 6. Customer 360 Deep Dive ────────────────────────────────────────────────

@router.get("/{customer_id}/360", response_model=Customer360Response)
def get_customer_360(
    customer_id: int,
    request: Request,
    x_market_id: Optional[str] = Header(None, alias="X-Market-Id"),
    market_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Unified Customer 360 view with operational telemetry, billing, and AI next-best-action.
    """
    raw_market = x_market_id or market_id or request.headers.get("x-market-id")
    chosen = raw_market.strip().lower() if raw_market and raw_market.strip() else None
    if chosen is not None and chosen not in MARKETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid or unsupported market identifier: '{chosen}'"
        )

    authorized_markets, _ = get_user_authorized_markets(current_user, db)
    is_super = is_super_admin(current_user.role)

    if chosen is not None and not is_super and chosen not in authorized_markets:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User '{current_user.email}' is not authorized to access market '{chosen}'"
        )

    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    customer_market = (customer.market_id or "").strip().lower()
    if chosen is not None and customer_market != chosen:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: Customer does not belong to authorized market '{chosen}'"
        )

    if not is_super and customer_market not in authorized_markets:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Customer does not belong to authorized market"
        )

    if normalize_role_name(current_user.role) == "Customer":
        if customer.email.lower().strip() != current_user.email.lower().strip():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: Cannot access another customer's 360 profile"
            )

    node = db.query(Node).filter(Node.id == customer.node_id).first() if customer.node_id else None
    usage = db.query(UsageRecord).filter(UsageRecord.customer_id == customer.id).first()
    tickets = db.query(Ticket).filter(Ticket.customer_id == customer.id).order_by(Ticket.created_at.desc()).limit(10).all()
    invoices = db.query(Invoice).filter(Invoice.customer_id == customer.id).order_by(Invoice.due_date.desc()).limit(10).all()

    score, risk_lvl, conf, factors, action, _ = evaluate_customer_signals(
        customer, usage, tickets, invoices, node
    )

    customer_journey = evaluate_single_customer_journey(customer, db, usage=usage, tickets=tickets)
    nba_payload = {
        "action": customer_journey.next_best_action if customer_journey else "Deliver Monthly Digital Health Summary",
        "reason": customer_journey.action_reason if customer_journey else "Standard lifecycle cycle",
        "channel": customer_journey.suggested_channel if customer_journey else "WhatsApp",
        "confidence": customer_journey.confidence_score if customer_journey else 0.85
    }

    recs = db.query(Recommendation).filter(
        Recommendation.target_entity_type == 'Customer',
        Recommendation.target_entity_id == customer.id
    ).order_by(Recommendation.created_at.desc()).all()

    rec_list = [
        {
            "id": r.id,
            "module": r.source_module,
            "title": r.title,
            "action": r.recommended_action,
            "status": r.status,
            "confidence": r.confidence_score,
            "created_at": r.created_at.isoformat()
        } for r in recs
    ]

    cust_summary = _build_customer_list_item(customer, customer_market)

    node_res = None
    if node:
        node_res = NodeResponse(
            id=node.id,
            node_code=node.node_code,
            node_name=node.node_name,
            area=node.area,
            node_type=node.node_type,
            utilization_pct=node.utilization_pct,
            packet_loss_pct=node.packet_loss_pct,
            latency_ms=node.latency_ms,
            optical_power_dbm=node.optical_power_dbm,
            alarm_count=node.alarm_count,
            health_score=node.health_score,
            status=node.status,
            last_telemetry_at=node.last_telemetry_at,
            impacted_customers_count=len(node.customers)
        )

    usage_res = None
    if usage:
        usage_res = UsageSummary(
            monthly_gb=usage.monthly_gb,
            quota_gb=usage.quota_gb,
            usage_trend=usage.usage_trend,
            trend_pct=usage.trend_pct,
            ott_streaming_flag=usage.ott_streaming_flag,
            gaming_flag=usage.gaming_flag
        )

    ticket_summaries = [
        TicketSummary(
            id=t.id,
            ticket_code=t.ticket_code,
            category=t.category,
            priority=t.priority,
            status=t.status,
            created_at=t.created_at,
            repeat_flag=t.repeat_flag,
            description=t.description,
            assigned_resource_name=t.assigned_resource.name if t.assigned_resource else None,
            region=t.region
        ) for t in tickets
    ]

    invoice_summaries = [
        InvoiceSummary(
            id=inv.id,
            invoice_code=inv.invoice_code,
            transaction_type=inv.transaction_type or ("Base Unlimited Recharge" if customer.customer_type == 'Prepaid' else "Monthly Bill"),
            payment_method=inv.payment_method or customer.payment_method or "UPI",
            billed_amount=inv.billed_amount,
            due_date=inv.due_date,
            paid_date=inv.paid_date,
            status=inv.status,
            waiver_amount=inv.waiver_amount,
            anomaly_flag=inv.anomaly_flag,
            anomaly_type=inv.anomaly_type
        ) for inv in invoices
    ]

    return Customer360Response(
        customer=cust_summary,
        node=node_res,
        usage=usage_res,
        recent_tickets=ticket_summaries,
        recent_invoices=invoice_summaries,
        churn_risk_score=score,
        churn_risk_level=risk_lvl,
        churn_factors=factors,
        next_best_action=nba_payload,
        active_recommendations=rec_list
    )


# ─── 7. Customer Master Detail, Update & Archive ──────────────────────────────

@router.get("/{customer_id}", response_model=CRMCustomerDetailResponse)
def get_customer_detail(
    customer_id: str,
    x_market_id: Optional[str] = Header(None, alias="X-Market-Id"),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get unified customer master record with associated contacts, addresses, and activities.
    Supports integer ID, UUID, or customer code.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user, x_market_id)
    if customer and not crm_cust:
        crm_cust = _ensure_crm_customer(customer, db)

    return _build_detail_response(customer, crm_cust, db)


@router.put("/{customer_id}", response_model=CRMCustomerDetailResponse)
@router.patch("/{customer_id}", response_model=CRMCustomerDetailResponse)
def update_customer(
    customer_id: str,
    update_in: CRMCustomerUpdate,
    x_market_id: Optional[str] = Header(None, alias="X-Market-Id"),
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "Care", "Revenue", "SUPER_ADMIN"]))
):
    """
    Update customer master record.
    Supports optimistic concurrency protection and logs CRMActivity audit trail.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user, x_market_id)

    # Concurrency verification if caller supplied expected_version
    if update_in.expected_version and customer:
        curr_v = customer.created_at.isoformat()
        if update_in.expected_version != curr_v:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conflict: Customer record has been modified concurrently. Please reload."
            )

    if customer and not crm_cust:
        crm_cust = _ensure_crm_customer(customer, db)

    changes = []

    if update_in.legal_name is not None:
        if customer:
            customer.name = update_in.legal_name
        if crm_cust:
            crm_cust.legal_name = update_in.legal_name
        changes.append("legal_name")

    if update_in.trade_name is not None and crm_cust:
        crm_cust.trade_name = update_in.trade_name
        changes.append("trade_name")

    if update_in.account_type is not None and crm_cust:
        at = update_in.account_type.upper()
        if at in AccountType.ALL:
            crm_cust.account_type = at
            changes.append("account_type")

    if update_in.status is not None:
        st = update_in.status.upper()
        if st in CustomerLifecycleStatus.ALL:
            if crm_cust:
                crm_cust.status = st
            if customer:
                customer.status = "Active" if st in (CustomerLifecycleStatus.ACTIVE, CustomerLifecycleStatus.PENDING_VERIFICATION) else "Churned"
            changes.append("status")

    if update_in.email is not None and customer:
        customer.email = update_in.email
        changes.append("email")

    if update_in.phone is not None and customer:
        customer.phone = update_in.phone
        changes.append("phone")

    if update_in.locality is not None and customer:
        customer.locality = update_in.locality
        changes.append("locality")

    if update_in.segment is not None and customer:
        customer.segment = update_in.segment
        changes.append("segment")

    if update_in.customer_type is not None and customer:
        customer.customer_type = update_in.customer_type
        changes.append("customer_type")

    if update_in.plan_name is not None and customer:
        customer.plan_name = update_in.plan_name
        changes.append("plan_name")

    if update_in.plan_price is not None and customer:
        customer.plan_price = update_in.plan_price
        changes.append("plan_price")

    if update_in.tax_id is not None and crm_cust:
        crm_cust.tax_id = update_in.tax_id
        changes.append("tax_id")

    if crm_cust:
        crm_cust.updated_at = datetime.utcnow()
        if changes:
            act = CRMActivity(
                id=uuid.uuid4(),
                crm_customer_id=crm_cust.id,
                activity_type=ActivityType.STATUS_CHANGE if "status" in changes else ActivityType.NOTE,
                summary=f"Customer Updated ({', '.join(changes)})",
                details=f"Modified by {current_user.email} ({current_user.role}).",
                performed_by_id=current_user.id
            )
            db.add(act)

    db.commit()
    if customer:
        db.refresh(customer)
    if crm_cust:
        db.refresh(crm_cust)

    return _build_detail_response(customer, crm_cust, db)


@router.delete("/{customer_id}", status_code=status.HTTP_200_OK)
@router.post("/{customer_id}/archive", status_code=status.HTTP_200_OK)
def archive_customer(
    customer_id: str,
    x_market_id: Optional[str] = Header(None, alias="X-Market-Id"),
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "SUPER_ADMIN"]))
):
    """
    Soft-archive customer master.
    Preserves audit compliance and respects ondelete='RESTRICT' retention invariants.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user, x_market_id)
    if customer and not crm_cust:
        crm_cust = _ensure_crm_customer(customer, db)

    now = datetime.utcnow()
    if crm_cust:
        crm_cust.is_archived = True
        crm_cust.archived_at = now
        crm_cust.archived_by_id = current_user.id
        crm_cust.status = CustomerLifecycleStatus.TERMINATED

        act = CRMActivity(
            id=uuid.uuid4(),
            crm_customer_id=crm_cust.id,
            activity_type=ActivityType.STATUS_CHANGE,
            summary="Customer Master Soft-Archived",
            details=f"Archived by {current_user.email}. Retained for compliance.",
            performed_by_id=current_user.id
        )
        db.add(act)

    if customer:
        customer.status = "Churned"

    db.commit()
    return {
        "status": "success",
        "message": "Customer record successfully soft-archived in CRM.",
        "customer_id": customer_id
    }


# ─── 8. Contacts Sub-Resource ─────────────────────────────────────────────────

@router.get("/{customer_id}/contacts", response_model=List[CRMContactResponse])
def get_customer_contacts(
    customer_id: str,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    List associated contacts for a customer.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    if not crm_cust:
        return []

    contacts = db.query(CRMContact).filter(
        CRMContact.crm_customer_id == crm_cust.id,
        CRMContact.is_archived == False
    ).order_by(CRMContact.is_primary.desc(), CRMContact.created_at.asc()).all()

    return [
        CRMContactResponse(
            id=str(c.id),
            crm_customer_id=str(c.crm_customer_id),
            first_name=c.first_name,
            last_name=c.last_name,
            email=c.email,
            phone=c.phone,
            role=c.role,
            is_primary=c.is_primary,
            is_verified=c.is_verified,
            is_archived=c.is_archived,
            created_at=c.created_at,
            updated_at=c.updated_at
        ) for c in contacts
    ]


@router.post("/{customer_id}/contacts", response_model=CRMContactResponse, status_code=status.HTTP_201_CREATED)
def create_customer_contact(
    customer_id: str,
    contact_in: CRMContactCreate,
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "Care", "Revenue", "SUPER_ADMIN"]))
):
    """
    Add a new contact to the customer.
    Enforces single non-archived primary contact constraint and allows shared contacts across customers.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    role = contact_in.role.upper()
    if role not in ContactRole.ALL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid contact role '{contact_in.role}'. Allowed: {ContactRole.ALL}"
        )

    # Partial unique index enforcement: if is_primary is True, demote current active primary
    if contact_in.is_primary:
        existing_primaries = db.query(CRMContact).filter(
            CRMContact.crm_customer_id == crm_cust.id,
            CRMContact.is_primary == True,
            CRMContact.is_archived == False
        ).all()
        for p in existing_primaries:
            p.is_primary = False
        db.flush()

    new_contact = CRMContact(
        id=uuid.uuid4(),
        crm_customer_id=crm_cust.id,
        first_name=contact_in.first_name,
        last_name=contact_in.last_name,
        email=contact_in.email,
        phone=contact_in.phone,
        role=role,
        is_primary=contact_in.is_primary,
        is_verified=False,
        is_archived=False,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(new_contact)

    # Activity Log
    act = CRMActivity(
        id=uuid.uuid4(),
        crm_customer_id=crm_cust.id,
        activity_type=ActivityType.CONTACT_ADDED,
        summary=f"Added Contact ({new_contact.first_name} {new_contact.last_name or ''} · {role})",
        details=f"Created by {current_user.email}.",
        performed_by_id=current_user.id
    )
    db.add(act)

    db.commit()
    db.refresh(new_contact)

    return CRMContactResponse(
        id=str(new_contact.id),
        crm_customer_id=str(new_contact.crm_customer_id),
        first_name=new_contact.first_name,
        last_name=new_contact.last_name,
        email=new_contact.email,
        phone=new_contact.phone,
        role=new_contact.role,
        is_primary=new_contact.is_primary,
        is_verified=new_contact.is_verified,
        is_archived=new_contact.is_archived,
        created_at=new_contact.created_at,
        updated_at=new_contact.updated_at
    )


@router.put("/{customer_id}/contacts/{contact_id}", response_model=CRMContactResponse)
def update_customer_contact(
    customer_id: str,
    contact_id: str,
    contact_in: CRMContactUpdate,
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "Care", "Revenue", "SUPER_ADMIN"]))
):
    """
    Update contact details or promote to primary.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    try:
        c_uuid = uuid.UUID(str(contact_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid contact ID format.")

    contact = db.query(CRMContact).filter(
        CRMContact.id == c_uuid,
        CRMContact.crm_customer_id == crm_cust.id
    ).first()

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    if contact_in.is_primary:
        # Demote others
        existing_primaries = db.query(CRMContact).filter(
            CRMContact.crm_customer_id == crm_cust.id,
            CRMContact.id != contact.id,
            CRMContact.is_primary == True,
            CRMContact.is_archived == False
        ).all()
        for p in existing_primaries:
            p.is_primary = False
        contact.is_primary = True

    if contact_in.first_name is not None:
        contact.first_name = contact_in.first_name
    if contact_in.last_name is not None:
        contact.last_name = contact_in.last_name
    if contact_in.email is not None:
        contact.email = contact_in.email
    if contact_in.phone is not None:
        contact.phone = contact_in.phone
    if contact_in.role is not None:
        r = contact_in.role.upper()
        if r in ContactRole.ALL:
            contact.role = r
    if contact_in.is_archived is not None:
        contact.is_archived = contact_in.is_archived
        if contact_in.is_archived:
            contact.archived_at = datetime.utcnow()

    contact.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(contact)

    return CRMContactResponse(
        id=str(contact.id),
        crm_customer_id=str(contact.crm_customer_id),
        first_name=contact.first_name,
        last_name=contact.last_name,
        email=contact.email,
        phone=contact.phone,
        role=contact.role,
        is_primary=contact.is_primary,
        is_verified=contact.is_verified,
        is_archived=contact.is_archived,
        created_at=contact.created_at,
        updated_at=contact.updated_at
    )


@router.delete("/{customer_id}/contacts/{contact_id}", status_code=status.HTTP_200_OK)
def archive_customer_contact(
    customer_id: str,
    contact_id: str,
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "Care", "Revenue", "SUPER_ADMIN"]))
):
    """
    Soft-archive a contact.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    try:
        c_uuid = uuid.UUID(str(contact_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid contact ID format.")

    contact = db.query(CRMContact).filter(
        CRMContact.id == c_uuid,
        CRMContact.crm_customer_id == crm_cust.id
    ).first()

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    contact.is_archived = True
    contact.archived_at = datetime.utcnow()
    contact.is_primary = False
    db.commit()

    return {"status": "success", "message": "Contact archived successfully."}


# ─── 9. Addresses Sub-Resource ────────────────────────────────────────────────

@router.get("/{customer_id}/addresses", response_model=List[CRMAddressResponse])
def get_customer_addresses(
    customer_id: str,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    List associated addresses for a customer.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    if not crm_cust:
        return []

    addresses = db.query(CRMAddress).filter(
        CRMAddress.crm_customer_id == crm_cust.id,
        CRMAddress.is_archived == False
    ).order_by(CRMAddress.created_at.asc()).all()

    return [
        CRMAddressResponse(
            id=str(a.id),
            crm_customer_id=str(a.crm_customer_id),
            address_type=a.address_type,
            address_line1=a.address_line1,
            address_line2=a.address_line2,
            city=a.city,
            state=a.state,
            postal_code=a.postal_code,
            latitude=a.latitude,
            longitude=a.longitude,
            serviceability_status=a.serviceability_status,
            candidate_node_id=a.candidate_node_id,
            distance_to_node_meters=a.distance_to_node_meters,
            is_archived=a.is_archived,
            created_at=a.created_at,
            updated_at=a.updated_at
        ) for a in addresses
    ]


@router.post("/{customer_id}/addresses", response_model=CRMAddressResponse, status_code=status.HTTP_201_CREATED)
def create_customer_address(
    customer_id: str,
    addr_in: CRMAddressCreate,
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "Care", "NOC", "Revenue", "SUPER_ADMIN"]))
):
    """
    Add a verified or unverified address to customer master.
    Preserves unresolved addresses with PENDING_SURVEY; never fabricates coordinates.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    addr_type = addr_in.address_type.upper()
    if addr_type not in AddressType.ALL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid address_type '{addr_in.address_type}'. Allowed: {AddressType.ALL}"
        )

    lat = addr_in.latitude
    lng = addr_in.longitude
    serv_status = addr_in.serviceability_status or ServiceabilityStatus.PENDING_SURVEY

    if lat is not None or lng is not None:
        if lat is None or lng is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Both latitude and longitude must be provided together."
            )
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid GPS coordinates range."
            )
        serv_status = ServiceabilityStatus.SERVICEABLE

    new_addr = CRMAddress(
        id=uuid.uuid4(),
        crm_customer_id=crm_cust.id,
        address_type=addr_type,
        address_line1=addr_in.address_line1,
        address_line2=addr_in.address_line2,
        city=addr_in.city,
        state=addr_in.state,
        postal_code=addr_in.postal_code,
        latitude=lat,
        longitude=lng,
        serviceability_status=serv_status,
        is_archived=False,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(new_addr)

    # Activity Log
    act = CRMActivity(
        id=uuid.uuid4(),
        crm_customer_id=crm_cust.id,
        activity_type=ActivityType.ADDRESS_UPDATED,
        summary=f"Added {addr_type} Address ({new_addr.city})",
        details=f"Serviceability: {serv_status}. Logged by {current_user.email}.",
        performed_by_id=current_user.id
    )
    db.add(act)

    db.commit()
    db.refresh(new_addr)

    # Trigger automated survey dispatch if PENDING_SURVEY or coordinates unverified
    srv_tck = dispatch_field_survey_if_needed(db, customer, crm_cust, new_addr, current_user)

    return CRMAddressResponse(
        id=str(new_addr.id),
        crm_customer_id=str(new_addr.crm_customer_id),
        address_type=new_addr.address_type,
        address_line1=new_addr.address_line1,
        address_line2=new_addr.address_line2,
        city=new_addr.city,
        state=new_addr.state,
        postal_code=new_addr.postal_code,
        latitude=new_addr.latitude,
        longitude=new_addr.longitude,
        serviceability_status=new_addr.serviceability_status,
        candidate_node_id=new_addr.candidate_node_id,
        distance_to_node_meters=new_addr.distance_to_node_meters,
        survey_ticket_code=srv_tck.ticket_code if srv_tck else None,
        survey_ticket_status=srv_tck.status if srv_tck else None,
        is_archived=new_addr.is_archived,
        created_at=new_addr.created_at,
        updated_at=new_addr.updated_at
    )


@router.put("/{customer_id}/addresses/{address_id}", response_model=CRMAddressResponse)
def update_customer_address(
    customer_id: str,
    address_id: str,
    addr_in: CRMAddressUpdate,
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "Care", "NOC", "Revenue", "SUPER_ADMIN"]))
):
    """
    Update installation, billing, or registered address.
    Enforces customer IDOR protection and dispatches automated field survey if unverified.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    try:
        a_uuid = uuid.UUID(str(address_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid address ID format.")

    addr = db.query(CRMAddress).filter(
        CRMAddress.id == a_uuid,
        CRMAddress.crm_customer_id == crm_cust.id
    ).first()

    if not addr:
        raise HTTPException(status_code=404, detail="Address not found")

    if addr_in.address_type is not None:
        atype = addr_in.address_type.upper()
        if atype not in AddressType.ALL:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid address_type '{addr_in.address_type}'. Allowed: {AddressType.ALL}"
            )
        addr.address_type = atype

    if addr_in.address_line1 is not None:
        addr.address_line1 = addr_in.address_line1
    if addr_in.address_line2 is not None:
        addr.address_line2 = addr_in.address_line2
    if addr_in.city is not None:
        addr.city = addr_in.city
    if addr_in.state is not None:
        addr.state = addr_in.state
    if addr_in.postal_code is not None:
        addr.postal_code = addr_in.postal_code

    if addr_in.latitude is not None or addr_in.longitude is not None:
        if addr_in.latitude is None or addr_in.longitude is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Both latitude and longitude must be provided together."
            )
        if not (-90.0 <= addr_in.latitude <= 90.0) or not (-180.0 <= addr_in.longitude <= 180.0):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid GPS coordinates range."
            )
        addr.latitude = addr_in.latitude
        addr.longitude = addr_in.longitude
        addr.serviceability_status = ServiceabilityStatus.SERVICEABLE
        if customer:
            customer.service_latitude = addr_in.latitude
            customer.service_longitude = addr_in.longitude

    if addr_in.serviceability_status is not None:
        st = addr_in.serviceability_status.upper()
        if st in ServiceabilityStatus.ALL:
            addr.serviceability_status = st

    if addr_in.is_archived is not None:
        addr.is_archived = addr_in.is_archived

    addr.updated_at = datetime.utcnow()

    # Activity Log
    act = CRMActivity(
        id=uuid.uuid4(),
        crm_customer_id=crm_cust.id,
        activity_type=ActivityType.ADDRESS_UPDATED,
        summary=f"Updated {addr.address_type} Address ({addr.city})",
        details=f"Serviceability: {addr.serviceability_status}. Logged by {current_user.email}.",
        performed_by_id=current_user.id
    )
    db.add(act)
    db.commit()
    db.refresh(addr)

    # Automated survey dispatch check
    srv_tck = dispatch_field_survey_if_needed(db, customer, crm_cust, addr, current_user)

    return CRMAddressResponse(
        id=str(addr.id),
        crm_customer_id=str(addr.crm_customer_id),
        address_type=addr.address_type,
        address_line1=addr.address_line1,
        address_line2=addr.address_line2,
        city=addr.city,
        state=addr.state,
        postal_code=addr.postal_code,
        latitude=addr.latitude,
        longitude=addr.longitude,
        serviceability_status=addr.serviceability_status,
        candidate_node_id=addr.candidate_node_id,
        distance_to_node_meters=addr.distance_to_node_meters,
        survey_ticket_code=srv_tck.ticket_code if srv_tck else None,
        survey_ticket_status=srv_tck.status if srv_tck else None,
        is_archived=addr.is_archived,
        created_at=addr.created_at,
        updated_at=addr.updated_at
    )


@router.post("/{customer_id}/addresses/{address_id}/survey-complete", response_model=CRMAddressResponse)
def complete_address_survey_endpoint(
    customer_id: str,
    address_id: str,
    survey_in: CRMSurveyCompleteRequest,
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "NOC", "Care", "SUPER_ADMIN", "Field Engineer"]))
):
    """
    Complete physical on-ground survey: record verified GPS coordinates and resolve survey ticket.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    try:
        a_uuid = uuid.UUID(str(address_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid address ID format.")

    addr = db.query(CRMAddress).filter(
        CRMAddress.id == a_uuid,
        CRMAddress.crm_customer_id == crm_cust.id
    ).first()

    if not addr:
        raise HTTPException(status_code=404, detail="Address not found")

    try:
        updated_addr, resolved_ticket = complete_field_survey(
            db=db,
            customer=customer,
            crm_cust=crm_cust,
            address=addr,
            latitude=survey_in.latitude,
            longitude=survey_in.longitude,
            serviceability_status=survey_in.serviceability_status or ServiceabilityStatus.SERVICEABLE,
            verified_by_user=current_user,
            ticket_id=survey_in.ticket_id,
            candidate_node_id=survey_in.candidate_node_id,
            distance_to_node_meters=survey_in.distance_to_node_meters,
            notes=survey_in.notes
        )
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))

    return CRMAddressResponse(
        id=str(updated_addr.id),
        crm_customer_id=str(updated_addr.crm_customer_id),
        address_type=updated_addr.address_type,
        address_line1=updated_addr.address_line1,
        address_line2=updated_addr.address_line2,
        city=updated_addr.city,
        state=updated_addr.state,
        postal_code=updated_addr.postal_code,
        latitude=updated_addr.latitude,
        longitude=updated_addr.longitude,
        serviceability_status=updated_addr.serviceability_status,
        candidate_node_id=updated_addr.candidate_node_id,
        distance_to_node_meters=updated_addr.distance_to_node_meters,
        survey_ticket_code=resolved_ticket.ticket_code if resolved_ticket else None,
        survey_ticket_status=resolved_ticket.status if resolved_ticket else "Resolved",
        is_archived=updated_addr.is_archived,
        created_at=updated_addr.created_at,
        updated_at=updated_addr.updated_at
    )


@router.delete("/{customer_id}/addresses/{address_id}", status_code=status.HTTP_200_OK)
def archive_customer_address(
    customer_id: str,
    address_id: str,
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "Care", "NOC", "Revenue", "SUPER_ADMIN"]))
):
    """
    Soft-archive an address record.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    try:
        a_uuid = uuid.UUID(str(address_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid address ID format.")

    addr = db.query(CRMAddress).filter(
        CRMAddress.id == a_uuid,
        CRMAddress.crm_customer_id == crm_cust.id
    ).first()

    if not addr:
        raise HTTPException(status_code=404, detail="Address not found")

    addr.is_archived = True
    db.commit()
    return {"status": "success", "message": "Address archived successfully."}


# ─── 9. KYC Document Management & Object Storage Integration ─────────────────

@router.get("/{customer_id}/documents", response_model=List[CRMDocumentResponse])
def get_customer_documents(
    customer_id: str,
    include_archived: bool = Query(False),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    List KYC documents, identity proofs, and business certificates for customer.
    Enforces subscriber-level isolation for Customer roles.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    if not crm_cust:
        return []

    # Subscriber role isolation check
    if normalize_role_name(current_user.role) == "Customer":
        user_email = (current_user.email or "").lower().strip()
        cust_email = (customer.email if customer else "").lower().strip()
        if user_email != cust_email:
            raise HTTPException(status_code=403, detail="Forbidden: You may only view your own KYC documents.")

    query = db.query(CRMDocument).filter(CRMDocument.crm_customer_id == crm_cust.id)
    if not include_archived:
        query = query.filter(CRMDocument.is_archived == False)

    docs = query.order_by(CRMDocument.created_at.desc()).all()
    return [
        CRMDocumentResponse(
            id=str(d.id),
            crm_customer_id=str(d.crm_customer_id),
            document_type=d.document_type,
            document_identifier=d.document_identifier,
            file_name=d.file_name,
            file_size_bytes=d.file_size_bytes,
            mime_type=d.mime_type,
            upload_status=d.upload_status,
            verification_status=d.verification_status,
            verified_by_id=d.verified_by_id,
            verified_at=d.verified_at,
            rejection_reason=d.rejection_reason,
            is_archived=d.is_archived,
            created_at=d.created_at,
            updated_at=d.updated_at
        ) for d in docs
    ]


@router.post("/{customer_id}/documents", response_model=CRMDocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_customer_document(
    customer_id: str,
    file: UploadFile = File(...),
    document_type: str = Form(...),
    document_identifier: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "Care", "SUPER_ADMIN"]))
):
    """
    Upload customer KYC identity document, GST certificate, or lease agreement.
    Validates MIME type, magic bytes, file size, server-generates storage key,
    and masks sensitive identifiers before persistence.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    DOC_TYPE_ALIASES = {
        "AADHAAR": DocumentType.AADHAAR_MASKED,
        "CERT_INCORPORATION": DocumentType.INCORPORATION_CERT,
        "GSTIN": DocumentType.INCORPORATION_CERT,
        "UTILITY_BILL": DocumentType.LEASE_AGREEMENT,
        "BOARD_RESOLUTION": DocumentType.INCORPORATION_CERT,
        "OTHER": DocumentType.LEASE_AGREEMENT
    }
    raw_type = document_type.strip().upper()
    doc_type = DOC_TYPE_ALIASES.get(raw_type, raw_type)
    if doc_type not in DocumentType.ALL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid document_type '{document_type}'. Permitted: {DocumentType.ALL}"
        )

    storage = get_storage_provider()
    if not storage.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cloud document storage backend is unconfigured (BLOCKED). Contact administrator."
        )

    content = await file.read()
    try:
        verified_mime = validate_document_payload(file.filename, content, file.content_type)
    except DocumentValidationError as dve:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(dve))

    import os
    _, ext = os.path.splitext(file.filename)
    storage_key = generate_storage_key(crm_cust.market_id, str(crm_cust.id), doc_type, ext)

    try:
        saved_key = storage.save_file(storage_key, content, verified_mime)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Storage persistence error: {exc}")

    try:
        masked_id = mask_document_identifier(doc_type, document_identifier)
    except DocumentValidationError as dve:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(dve))

    new_doc = CRMDocument(
        id=uuid.uuid4(),
        crm_customer_id=crm_cust.id,
        document_type=doc_type,
        document_identifier=masked_id,
        file_key=saved_key,
        upload_status=DocumentUploadStatus.UPLOADED,
        file_name=os.path.basename(file.filename),
        file_size_bytes=len(content),
        mime_type=verified_mime,
        verification_status=VerificationStatus.PENDING,
        is_archived=False,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(new_doc)

    act = CRMActivity(
        id=uuid.uuid4(),
        crm_customer_id=crm_cust.id,
        activity_type=ActivityType.NOTE,
        summary=f"Uploaded {doc_type} Document ({new_doc.file_name})",
        details=f"Size: {len(content)} bytes. Storage: {storage.provider_name()}. Uploaded by {current_user.email}.",
        performed_by_id=current_user.id
    )
    db.add(act)
    db.commit()
    db.refresh(new_doc)

    return CRMDocumentResponse(
        id=str(new_doc.id),
        crm_customer_id=str(new_doc.crm_customer_id),
        document_type=new_doc.document_type,
        document_identifier=new_doc.document_identifier,
        file_name=new_doc.file_name,
        file_size_bytes=new_doc.file_size_bytes,
        mime_type=new_doc.mime_type,
        upload_status=new_doc.upload_status,
        verification_status=new_doc.verification_status,
        verified_by_id=new_doc.verified_by_id,
        verified_at=new_doc.verified_at,
        rejection_reason=new_doc.rejection_reason,
        is_archived=new_doc.is_archived,
        created_at=new_doc.created_at,
        updated_at=new_doc.updated_at
    )


@router.get("/{customer_id}/documents/{document_id}", response_model=CRMDocumentResponse)
def get_customer_document_detail(
    customer_id: str,
    document_id: str,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get metadata for a specific customer document.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    try:
        d_uuid = uuid.UUID(str(document_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid document ID format.")

    doc = db.query(CRMDocument).filter(
        CRMDocument.id == d_uuid,
        CRMDocument.crm_customer_id == crm_cust.id
    ).first()

    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    return CRMDocumentResponse(
        id=str(doc.id),
        crm_customer_id=str(doc.crm_customer_id),
        document_type=doc.document_type,
        document_identifier=doc.document_identifier,
        file_name=doc.file_name,
        file_size_bytes=doc.file_size_bytes,
        mime_type=doc.mime_type,
        upload_status=doc.upload_status,
        verification_status=doc.verification_status,
        verified_by_id=doc.verified_by_id,
        verified_at=doc.verified_at,
        rejection_reason=doc.rejection_reason,
        is_archived=doc.is_archived,
        created_at=doc.created_at,
        updated_at=doc.updated_at
    )


@router.get("/{customer_id}/documents/{document_id}/download")
def download_customer_document(
    customer_id: str,
    document_id: str,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Authorized binary download of customer document content.
    Enforces customer and market authorization boundaries.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    # Subscriber role isolation check
    if normalize_role_name(current_user.role) == "Customer":
        user_email = (current_user.email or "").lower().strip()
        cust_email = (customer.email if customer else "").lower().strip()
        if user_email != cust_email:
            raise HTTPException(status_code=403, detail="Forbidden: You may only download your own KYC documents.")

    try:
        d_uuid = uuid.UUID(str(document_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid document ID format.")

    doc = db.query(CRMDocument).filter(
        CRMDocument.id == d_uuid,
        CRMDocument.crm_customer_id == crm_cust.id,
        CRMDocument.is_archived == False
    ).first()

    if not doc:
        raise HTTPException(status_code=404, detail="Document not found or has been archived.")

    if not doc.file_key:
        raise HTTPException(status_code=404, detail="Document file has not been uploaded to storage.")

    storage = get_storage_provider()
    try:
        content, mime_type = storage.get_file(doc.file_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document binary not found in storage.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Storage retrieval failure: {exc}")

    return Response(
        content=content,
        media_type=mime_type or doc.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{doc.file_name}"',
            "X-Content-Type-Options": "nosniff"
        }
    )


@router.post("/{customer_id}/documents/{document_id}/verify", response_model=CRMDocumentResponse)
def verify_customer_document(
    customer_id: str,
    document_id: str,
    verify_in: CRMDocumentVerifyRequest,
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "Care", "SUPER_ADMIN"]))
):
    """
    Approve or reject a submitted KYC document.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    try:
        d_uuid = uuid.UUID(str(document_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid document ID format.")

    doc = db.query(CRMDocument).filter(
        CRMDocument.id == d_uuid,
        CRMDocument.crm_customer_id == crm_cust.id
    ).first()

    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    doc.verification_status = verify_in.verification_status
    doc.verified_by_id = current_user.id
    doc.verified_at = datetime.utcnow()
    doc.rejection_reason = verify_in.rejection_reason if verify_in.verification_status == "REJECTED" else None
    doc.updated_at = datetime.utcnow()

    # Activity Log
    act_type = ActivityType.KYC_VERIFIED if verify_in.verification_status == "VERIFIED" else ActivityType.NOTE
    act = CRMActivity(
        id=uuid.uuid4(),
        crm_customer_id=crm_cust.id,
        activity_type=act_type,
        summary=f"KYC Document {doc.document_type} {verify_in.verification_status}",
        details=(
            f"Decision by {current_user.email}. "
            f"Notes: {verify_in.rejection_reason or 'Document verified against government register.'}"
        ),
        performed_by_id=current_user.id
    )
    db.add(act)
    db.commit()
    db.refresh(doc)

    return CRMDocumentResponse(
        id=str(doc.id),
        crm_customer_id=str(doc.crm_customer_id),
        document_type=doc.document_type,
        document_identifier=doc.document_identifier,
        file_name=doc.file_name,
        file_size_bytes=doc.file_size_bytes,
        mime_type=doc.mime_type,
        upload_status=doc.upload_status,
        verification_status=doc.verification_status,
        verified_by_id=doc.verified_by_id,
        verified_at=doc.verified_at,
        rejection_reason=doc.rejection_reason,
        is_archived=doc.is_archived,
        created_at=doc.created_at,
        updated_at=doc.updated_at
    )


@router.delete("/{customer_id}/documents/{document_id}", status_code=status.HTTP_200_OK)
def archive_customer_document(
    customer_id: str,
    document_id: str,
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "Care", "SUPER_ADMIN"]))
):
    """
    Soft-archive a customer document preserving compliance retention.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not crm_cust and customer:
        crm_cust = _ensure_crm_customer(customer, db)

    try:
        d_uuid = uuid.UUID(str(document_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid document ID format.")

    doc = db.query(CRMDocument).filter(
        CRMDocument.id == d_uuid,
        CRMDocument.crm_customer_id == crm_cust.id
    ).first()

    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    doc.is_archived = True
    doc.updated_at = datetime.utcnow()

    act = CRMActivity(
        id=uuid.uuid4(),
        crm_customer_id=crm_cust.id,
        activity_type=ActivityType.NOTE,
        summary=f"Archived {doc.document_type} Document ({doc.file_name})",
        details=f"Compliance retention preserved. Archived by {current_user.email}.",
        performed_by_id=current_user.id
    )
    db.add(act)
    db.commit()
    return {"status": "success", "message": "Document archived successfully."}


# ─── 10. Customer Tickets Integration ─────────────────────────────────────────

@router.get("/{customer_id}/tickets", response_model=List[TicketSummary])
def get_customer_tickets(
    customer_id: str,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Retrieve customer-linked ticket flow using existing Ticket module models and permissions.
    """
    customer, _ = _resolve_customer_record(customer_id, db, current_user)
    if not customer:
        return []

    tickets = db.query(Ticket).filter(Ticket.customer_id == customer.id).order_by(Ticket.created_at.desc()).all()
    return [
        TicketSummary(
            id=t.id,
            ticket_code=t.ticket_code,
            category=t.category,
            priority=t.priority,
            status=t.status,
            created_at=t.created_at,
            repeat_flag=t.repeat_flag,
            description=t.description,
            assigned_resource_name=t.assigned_resource.name if t.assigned_resource else None,
            region=t.region
        ) for t in tickets
    ]


@router.post("/{customer_id}/tickets", response_model=TicketSummary, status_code=status.HTTP_201_CREATED)
def create_customer_ticket(
    customer_id: str,
    category: str = Query("Speed"),
    priority: str = Query("P3"),
    description: str = Query("Customer reported issue"),
    db: Session = Depends(get_db),
    current_user = Depends(require_roles(["Admin", "NOC", "Care", "Customer", "SUPER_ADMIN"])),
    market: str = Depends(get_current_market)
):
    """
    Raise a ticket linked to customer master preserving existing ticket lifecycle, triage, and SLA rules.
    """
    customer, crm_cust = _resolve_customer_record(customer_id, db, current_user)
    if not customer:
        raise HTTPException(status_code=400, detail="Customer must have operational account to raise tickets.")

    ticket_code = f"TCK-{customer.market_id[:3].upper()}-{int(datetime.utcnow().timestamp())%100000:05d}"
    new_t = Ticket(
        market_id=customer.market_id,
        ticket_code=ticket_code,
        source="CUSTOMER",
        region=customer.locality,
        customer_id=customer.id,
        node_id=customer.node_id,
        category=category,
        priority=priority,
        status="Open",
        description=description,
        created_at=datetime.utcnow()
    )
    db.add(new_t)

    if crm_cust:
        act = CRMActivity(
            id=uuid.uuid4(),
            crm_customer_id=crm_cust.id,
            activity_type=ActivityType.NOTE,
            summary=f"Ticket {ticket_code} Raised ({priority} · {category})",
            details=description,
            performed_by_id=current_user.id
        )
        db.add(act)

    db.commit()
    db.refresh(new_t)

    return TicketSummary(
        id=new_t.id,
        ticket_code=new_t.ticket_code,
        category=new_t.category,
        priority=new_t.priority,
        status=new_t.status,
        created_at=new_t.created_at,
        repeat_flag=new_t.repeat_flag,
        description=new_t.description,
        assigned_resource_name=None,
        region=new_t.region
    )
