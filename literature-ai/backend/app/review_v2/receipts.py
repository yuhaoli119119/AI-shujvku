from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.models import AuditLog
RECEIPT_ACTION = "paper_review_v2_receipt"
def find_receipt(session: Session, paper_id: UUID, request_id: str) -> AuditLog | None:
    return session.scalar(select(AuditLog).where(AuditLog.paper_id == paper_id, AuditLog.action == RECEIPT_ACTION, AuditLog.target_id == request_id).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()))
def read_receipt(session: Session, paper_id: UUID, request_id: str) -> dict:
    row = find_receipt(session, paper_id, request_id)
    if not row or not isinstance(row.payload, dict): return {"found": False, "paper_id": str(paper_id), "request_id": request_id}
    return {"found": True, **row.payload, "receipt_audit_id": str(row.id)}
