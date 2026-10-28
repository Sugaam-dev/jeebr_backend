"""
app/services/document_storage.py

SentinelOS CRM KYC & Compliance Document Storage Service.
Provides a pluggable, secure abstraction for storing, retrieving, and archiving
customer KYC documents, business registrations, and identity records.

Security & Architectural Principles:
1. Server-Generated Storage Keys: Storage paths are strictly generated server-side.
   User-supplied filenames and paths are never trusted (prevents path traversal).
2. Content-Type & Magic Byte Validation: Validates both MIME type headers and binary
   file headers (signatures) to prevent MIME spoofing.
3. Size Bounding: Enforces a strict 10 MB maximum payload limit per document.
4. Masked PII: Document identifiers (Aadhaar, PAN, GSTIN) are masked before persistence.
5. Pluggable Backends: Supports Local Sandboxed Storage (for dev/test/on-premise)
   and Object Storage (AWS S3 / Supabase Storage). Reports explicit unconfigured
   state when external storage credentials are not provided.
"""

import os
import re
import uuid
import logging
from abc import ABC, abstractmethod
from typing import Optional, Tuple, Dict, Any

logger = logging.getLogger("sentinel.document_storage")

# Configuration constants
MAX_DOCUMENT_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

ALLOWED_MIME_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp"
}

# Magic byte signatures for authorized file types
MAGIC_SIGNATURES = [
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
]


class DocumentValidationError(Exception):
    """Raised when file payload, size, or signature fails security validation."""
    pass


class StorageProvider(ABC):
    """Abstract interface for document object storage."""

    @abstractmethod
    def save_file(self, storage_key: str, content: bytes, mime_type: str) -> str:
        """Saves file binary and returns the authoritative storage key."""
        pass

    @abstractmethod
    def get_file(self, storage_key: str) -> Tuple[bytes, str]:
        """Retrieves file binary and content-type tuple."""
        pass

    @abstractmethod
    def delete_file(self, storage_key: str) -> bool:
        """Deletes file binary from storage."""
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        """Returns True if the storage provider is ready to handle real uploads."""
        pass

    @abstractmethod
    def provider_name(self) -> str:
        """Returns human-readable name of the active storage backend."""
        pass


class LocalSandboxedStorageProvider(StorageProvider):
    """
    Local filesystem storage with path-traversal prevention.
    Ideal for local development, air-gapped deployments, and automated testing.
    """

    def __init__(self, base_directory: Optional[str] = None):
        if not base_directory:
            # Default to jeebr_backend/storage/kyc_documents
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            base_directory = os.path.join(current_dir, "..", "storage", "kyc_documents")
        self.base_dir = os.path.abspath(base_directory)
        os.makedirs(self.base_dir, exist_ok=True)

    def _resolve_safe_path(self, storage_key: str) -> str:
        """Enforces that resolved path resides strictly within base_dir."""
        # Normalize separators
        clean_key = storage_key.replace("\\", "/").lstrip("/")
        full_path = os.path.abspath(os.path.join(self.base_dir, clean_key))
        if os.path.commonpath([self.base_dir, full_path]) != self.base_dir:
            raise DocumentValidationError("Path traversal attempt detected in storage key.")
        return full_path

    def save_file(self, storage_key: str, content: bytes, mime_type: str) -> str:
        full_path = self._resolve_safe_path(storage_key)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(content)
        return storage_key

    def get_file(self, storage_key: str) -> Tuple[bytes, str]:
        full_path = self._resolve_safe_path(storage_key)
        if not os.path.exists(full_path):
            raise FileNotFoundError(f"Document not found in storage: {storage_key}")
        with open(full_path, "rb") as f:
            content = f.read()
        
        # Derive mime type from extension
        ext = os.path.splitext(full_path)[1].lower()
        mime_map = {v: k for k, v in ALLOWED_MIME_TYPES.items()}
        mime_type = mime_map.get(ext, "application/octet-stream")
        return content, mime_type

    def delete_file(self, storage_key: str) -> bool:
        full_path = self._resolve_safe_path(storage_key)
        if os.path.exists(full_path):
            os.remove(full_path)
            return True
        return False

    def is_configured(self) -> bool:
        return True

    def provider_name(self) -> str:
        return "Local Sandboxed Storage"


class S3StorageProvider(StorageProvider):
    """
    AWS S3 / Supabase S3-compatible Object Storage Provider.
    """

    def __init__(self, bucket_name: str, endpoint_url: Optional[str] = None):
        self.bucket_name = bucket_name
        self.endpoint_url = endpoint_url
        self._client = None
        self._init_client()

    def _init_client(self):
        try:
            import boto3
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                region_name=os.getenv("AWS_REGION", "ap-south-1")
            )
        except Exception as exc:
            logger.warning(f"Failed to initialize S3 client: {exc}")
            self._client = None

    def save_file(self, storage_key: str, content: bytes, mime_type: str) -> str:
        if not self._client:
            raise RuntimeError("S3 client is not configured.")
        self._client.put_object(
            Bucket=self.bucket_name,
            Key=storage_key,
            Body=content,
            ContentType=mime_type
        )
        return storage_key

    def get_file(self, storage_key: str) -> Tuple[bytes, str]:
        if not self._client:
            raise RuntimeError("S3 client is not configured.")
        resp = self._client.get_object(Bucket=self.bucket_name, Key=storage_key)
        content = resp["Body"].read()
        mime_type = resp.get("ContentType", "application/octet-stream")
        return content, mime_type

    def delete_file(self, storage_key: str) -> bool:
        if not self._client:
            return False
        self._client.delete_object(Bucket=self.bucket_name, Key=storage_key)
        return True

    def is_configured(self) -> bool:
        return self._client is not None and bool(self.bucket_name)

    def provider_name(self) -> str:
        return f"Object Storage (S3 / {self.bucket_name})"


class UnconfiguredCloudStorageProvider(StorageProvider):
    """
    Fallback provider when cloud object-storage is requested but credentials are missing.
    Marks uploads explicitly BLOCKED rather than fabricating successful persistence.
    """

    def save_file(self, storage_key: str, content: bytes, mime_type: str) -> str:
        raise RuntimeError(
            "Cloud object-storage provider is unconfigured. Set AWS_ACCESS_KEY_ID, "
            "AWS_SECRET_ACCESS_KEY, and S3_BUCKET_NAME in environment."
        )

    def get_file(self, storage_key: str) -> Tuple[bytes, str]:
        raise RuntimeError("Cloud object-storage provider is unconfigured.")

    def delete_file(self, storage_key: str) -> bool:
        return False

    def is_configured(self) -> bool:
        return False

    def provider_name(self) -> str:
        return "Unconfigured Cloud Storage (BLOCKED)"


# ─── Provider Factory ─────────────────────────────────────────────────────────

_ACTIVE_PROVIDER: Optional[StorageProvider] = None

def get_storage_provider() -> StorageProvider:
    """
    Returns the singleton StorageProvider instance based on runtime environment configuration.
    """
    global _ACTIVE_PROVIDER
    if _ACTIVE_PROVIDER is not None:
        return _ACTIVE_PROVIDER

    configured_type = os.getenv("DOCUMENT_STORAGE_PROVIDER", "local").strip().lower()

    if configured_type == "s3":
        bucket = os.getenv("S3_BUCKET_NAME")
        if bucket and os.getenv("AWS_ACCESS_KEY_ID"):
            _ACTIVE_PROVIDER = S3StorageProvider(
                bucket_name=bucket,
                endpoint_url=os.getenv("S3_ENDPOINT_URL")
            )
        else:
            _ACTIVE_PROVIDER = UnconfiguredCloudStorageProvider()
    else:
        # Default to local sandboxed storage
        custom_path = os.getenv("DOCUMENT_STORAGE_PATH")
        _ACTIVE_PROVIDER = LocalSandboxedStorageProvider(base_directory=custom_path)

    return _ACTIVE_PROVIDER


def set_storage_provider_for_testing(provider: Optional[StorageProvider]) -> None:
    """Allows test fixtures to inject mock or sandbox providers."""
    global _ACTIVE_PROVIDER
    _ACTIVE_PROVIDER = provider


# ─── Validation & Sanitization Helpers ───────────────────────────────────────

def validate_document_payload(filename: str, content: bytes, declared_mime: str) -> str:
    """
    Validates document size, extension, declared MIME, and binary magic bytes.
    Returns the verified normalized MIME type.
    """
    if not content:
        raise DocumentValidationError("Document content is empty (0 bytes).")

    if len(content) > MAX_DOCUMENT_SIZE_BYTES:
        raise DocumentValidationError(
            f"Document exceeds maximum allowable size of {MAX_DOCUMENT_SIZE_BYTES // (1024 * 1024)} MB."
        )

    # 1. Extension check
    _, ext = os.path.splitext(filename or "")
    ext = ext.lower()
    if ext not in ALLOWED_MIME_TYPES.values():
        allowed_exts = ", ".join(ALLOWED_MIME_TYPES.values())
        raise DocumentValidationError(f"Invalid file extension '{ext}'. Permitted: {allowed_exts}")

    # 2. Declared MIME check
    norm_mime = declared_mime.lower().split(";")[0].strip()
    if norm_mime not in ALLOWED_MIME_TYPES:
        allowed_mimes = ", ".join(ALLOWED_MIME_TYPES.keys())
        raise DocumentValidationError(f"Unsupported MIME type '{declared_mime}'. Permitted: {allowed_mimes}")

    # 3. Magic byte signature check
    verified_by_magic = False
    for magic_prefix, expected_mime in MAGIC_SIGNATURES:
        if content.startswith(magic_prefix):
            if norm_mime != expected_mime:
                raise DocumentValidationError(
                    f"MIME type mismatch: Declared '{norm_mime}' does not match binary signature for '{expected_mime}'."
                )
            verified_by_magic = True
            break

    # WebP check (RIFF....WEBP)
    if not verified_by_magic and content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP":
        if norm_mime != "image/webp":
            raise DocumentValidationError("MIME type mismatch for WebP image.")
        verified_by_magic = True

    if not verified_by_magic:
        raise DocumentValidationError(
            "File header signature does not match any approved document type (PDF, JPEG, PNG, WEBP)."
        )

    return norm_mime


def generate_storage_key(market_id: str, crm_customer_id: str, document_type: str, ext: str) -> str:
    """
    Generates an authoritative, immutable server-side storage key.
    Pattern: crm/{market_id}/{crm_customer_id}/{doc_type}_{uuid}.{ext}
    """
    clean_market = re.sub(r"[^a-zA-Z0-9_-]", "", market_id.lower()) or "mumbai"
    clean_doc = re.sub(r"[^a-zA-Z0-9_-]", "", document_type.upper())
    file_id = uuid.uuid4().hex[:12]
    clean_ext = ext if ext.startswith(".") else f".{ext}"
    return f"crm/{clean_market}/{crm_customer_id}/{clean_doc}_{file_id}{clean_ext}"


def mask_document_identifier(doc_type: str, raw_identifier: Optional[str]) -> Optional[str]:
    """
    Securely masks sensitive KYC identifiers before storing in database or returning to UI.
    Enforces strict masked-Aadhaar policy: rejects unmasked 12 digits and validates masking format.
    """
    if not raw_identifier:
        return None
    
    clean = re.sub(r"[\s-]", "", raw_identifier.strip())
    if not clean:
        return None

    # Handle Aadhaar Masked Policy
    if doc_type in ("AADHAAR_MASKED", "AADHAAR"):
        # Reject unmasked 12 digits (strictly prohibited)
        if clean.isdigit() and len(clean) == 12:
            raise DocumentValidationError("Unmasked 12-digit Aadhaar numbers are strictly prohibited. Provide an authorized masked Aadhaar identifier (e.g. XXXX-XXXX-1234).")
        # Verify masked format: first 8 masked with X, *, •, x and last 4 digits
        masked_match = re.match(r"^[X*•x]{8}(\d{4})$", clean)
        if masked_match:
            return f"XXXX-XXXX-{masked_match.group(1)}"
        raw_clean = raw_identifier.strip()
        sep_match = re.match(r"^[X*•x]{4}[-\s]?[X*•x]{4}[-\s]?(\d{4})$", raw_clean)
        if sep_match:
            return f"XXXX-XXXX-{sep_match.group(1)}"
        raise DocumentValidationError("Invalid masked Aadhaar format. First 8 digits must be masked and last 4 digits visible (e.g. XXXX-XXXX-1234).")

    if len(clean) == 10 and clean[:5].isalpha():
        # PAN format: 5 letters, 4 digits, 1 letter
        return f"XXXXX{clean[5:9]}{clean[9:]}"
    elif len(clean) > 8:
        # Generic mask: preserve first 2 and last 4 chars
        return f"{clean[:2]}{'X' * (len(clean) - 6)}{clean[-4:]}"
    elif len(clean) > 4:
        return f"{'X' * (len(clean) - 4)}{clean[-4:]}"
    else:
        return "XXXX"



# ─── Storage Diagnostic (Phase 9B Workstream J) ──────────────────────────────

def get_storage_diagnostic() -> Dict[str, Any]:
    """
    Inspects and returns non-sensitive storage provider health and configuration.
    Never exposes raw secrets or cloud credentials.
    """
    provider = get_storage_provider()
    
    if isinstance(provider, S3StorageProvider):
        target = f"s3://{provider.bucket_name}"
        mode = "OBJECT_STORAGE_S3"
    elif isinstance(provider, LocalSandboxedStorageProvider):
        target = os.path.basename(provider.base_dir)
        mode = "LOCAL_SANDBOX"
    else:
        target = "UNCONFIGURED"
        mode = "UNCONFIGURED_FAIL_CLOSED"

    return {
        "active_provider": provider.provider_name(),
        "is_configured": provider.is_configured(),
        "storage_mode": mode,
        "storage_path_or_bucket": target,
        "allowed_mimes": list(ALLOWED_MIME_TYPES.keys()),
        "max_size_mb": MAX_DOCUMENT_SIZE_BYTES // (1024 * 1024)
    }
