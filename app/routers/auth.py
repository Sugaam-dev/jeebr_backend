from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.schemas import Token, LoginRequest, UserResponse
from app.auth import verify_password, create_access_token, get_current_user

router = APIRouter(prefix="/auth", tags=["Authentication"])

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
