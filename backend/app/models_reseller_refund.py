"""Immutable source amounts for individually charged reseller services."""
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, Integer, String, Text, UniqueConstraint

from .database import Base


class ResellerSaleBasis(Base):
    __tablename__ = "reseller_sale_bases"
    id = Column(Integer, primary_key=True)
    purchase_id = Column(Integer, nullable=False, unique=True)
    debit_entry_id = Column(Integer, nullable=False, unique=True)
    sale_entry_id = Column(Integer, nullable=True, unique=True)
    purchase_count_delta = Column(Integer, nullable=False, default=0)
    charged_admin_id = Column(Integer, nullable=False)
    charged_amount = Column(BigInteger, nullable=False)
    quota_bytes = Column(BigInteger, nullable=False)
    duration_seconds = Column(BigInteger, nullable=False)
    expire_at = Column(DateTime, nullable=True)
    baseline_used_bytes = Column(BigInteger, nullable=False, default=0)
    connection_baselines = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (
        CheckConstraint("charged_amount > 0", name="ck_reseller_basis_positive_charge"),
        CheckConstraint("quota_bytes >= 0", name="ck_reseller_basis_quota"),
        CheckConstraint("duration_seconds >= 0", name="ck_reseller_basis_duration"),
        CheckConstraint("purchase_count_delta >= 0", name="ck_reseller_basis_count"),
        CheckConstraint("baseline_used_bytes >= 0", name="ck_reseller_basis_usage"),
    )


class ResellerRefundOperation(Base):
    """Retained after deletion, so retry can never pay a second refund."""
    __tablename__ = "reseller_refund_operations"
    id = Column(Integer, primary_key=True)
    basis_id = Column(Integer, nullable=False, unique=True)
    purchase_id = Column(Integer, nullable=False, unique=True)
    user_id = Column(Integer, nullable=False)
    requested_by = Column(Integer, nullable=False)
    state = Column(String(20), nullable=False, default="pending")
    baseline_used_bytes = Column(BigInteger, nullable=False)
    requested_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    refund_amount = Column(BigInteger, nullable=True)
    refund_entry_id = Column(Integer, nullable=True, unique=True)
    __table_args__ = (
        CheckConstraint("baseline_used_bytes >= 0", name="ck_reseller_op_usage"),
        CheckConstraint("state IN ('pending', 'completed')", name="ck_reseller_op_state"),
        CheckConstraint("(state = 'pending' AND completed_at IS NULL AND refund_amount IS NULL "
                        "AND refund_entry_id IS NULL) OR (state = 'completed' AND completed_at IS NOT NULL "
                        "AND refund_amount IS NOT NULL AND refund_amount >= 0 AND refund_entry_id IS NOT NULL)",
                        name="ck_reseller_op_result"),
    )


class ResellerRefundStep(Base):
    """Only an internal verified-stop worker may supply final usage."""
    __tablename__ = "reseller_refund_steps"
    id = Column(Integer, primary_key=True)
    operation_id = Column(Integer, nullable=False, index=True)
    connection_id = Column(Integer, nullable=False)
    identity_digest = Column(String(64), nullable=False)
    baseline_total_bytes = Column(BigInteger, nullable=False)
    final_total_bytes = Column(BigInteger, nullable=True)
    final_download_counter = Column(BigInteger, nullable=True)
    final_upload_counter = Column(BigInteger, nullable=True)
    captured_at = Column(DateTime, nullable=True)
    verified_at = Column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("operation_id", "connection_id", name="uq_reseller_refund_step"),
        CheckConstraint("baseline_total_bytes >= 0", name="ck_reseller_step_baseline"),
        CheckConstraint("(final_total_bytes IS NULL AND captured_at IS NULL AND verified_at IS NULL) OR "
                        "(final_total_bytes IS NOT NULL AND final_total_bytes >= baseline_total_bytes AND captured_at IS NOT NULL)",
                        name="ck_reseller_step_verification"),
        CheckConstraint("(final_download_counter IS NULL AND final_upload_counter IS NULL) OR "
                        "(final_download_counter IS NOT NULL AND final_upload_counter IS NOT NULL "
                        "AND final_download_counter >= 0 AND final_upload_counter >= 0 AND captured_at IS NOT NULL)",
                        name="ck_reseller_step_counters"),
    )
