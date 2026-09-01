from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.schemas import Token, LoginRequest, SignupRequest, UserResponse
from app.auth import verify_password, hash_password, create_access_token, get_current_user, require_roles

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/signup", response_model=Token, status_code=status.HTTP_201_CREATED)
def signup(req: SignupRequest, db: Session = Depends(get_db)):
    # 1. Validation: check if email already exists
    existing_user = db.query(User).filter(User.email == req.email.lower()).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account with this email address already exists"
        )
    
    # 2. Validate password strength
    if len(req.password.strip()) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters in length"
        )

    # 3. Validate name
    if not req.full_name or len(req.full_name.strip()) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Full name is required (minimum 2 characters)"
        )

    # 4. Normalize role
    valid_roles = ["Executive", "NOC", "Care", "Revenue", "Admin", "Viewer"]
    user_role = req.role if req.role in valid_roles else "Viewer"

    # 5. Create user with hashed password
    new_user = User(
        email=req.email.lower().strip(),
        hashed_password=hash_password(req.password),
        full_name=req.full_name.strip(),
        role=user_role,
        is_active=True
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # 6. Issue access token
    token = create_access_token({"sub": new_user.email, "role": new_user.role})
    return Token(
        access_token=token,
        role=new_user.role,
        user_name=new_user.full_name,
        email=new_user.email
    )


@router.post("/login", response_model=Token)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email).first()
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    token = create_access_token({"sub": user.email, "role": user.role})
    return Token(
        access_token=token,
        role=user.role,
        user_name=user.full_name,
        email=user.email
    )

@router.post("/demo-login/{role}", response_model=Token)
def demo_login(role: str, db: Session = Depends(get_db)):
    valid_roles = ["Executive", "NOC", "Care", "Revenue", "Admin"]
    matched_role = next((r for r in valid_roles if r.lower() == role.lower()), None)
    if not matched_role:
        raise HTTPException(status_code=400, detail=f"Invalid demo role. Valid roles: {valid_roles}")

    user = db.query(User).filter(User.role == matched_role).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"No seeded user found for role {matched_role}")

    token = create_access_token({"sub": user.email, "role": user.role})
    return Token(
        access_token=token,
        role=user.role,
        user_name=user.full_name,
        email=user.email
    )

@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user

@router.get("/users", response_model=List[UserResponse])
def get_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    return db.query(User).order_by(User.id.asc()).all()

