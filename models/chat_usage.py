# models/chat_usage.py

from datetime import datetime, date, timezone
from models.db_models import db


class ChatUsage(db.Model):
    """
    Tracks the store's total daily question count.
    One row per day — no tenant keys needed (one backend = one store).
    """
    __tablename__ = "chat_usage"

    usage_date     = db.Column(db.Date,    primary_key=True)
    question_count = db.Column(db.Integer, nullable=False, default=0)

    @classmethod
    def increment_and_check(cls, limit: int = 25) -> tuple[int, bool]:
        """
        Atomically increments today's counter and returns
        (new_count, limit_exceeded).

        Uses INSERT ... ON CONFLICT DO UPDATE so it's race-condition-safe.
        """
        today = date.today()
        from sqlalchemy.dialects.postgresql import insert

        stmt = (
            insert(cls)
            .values(usage_date=today, question_count=1)
            .on_conflict_do_update(
                index_elements=["usage_date"],
                set_={"question_count": cls.question_count + 1},
            )
            .returning(cls.question_count)
        )
        result = db.session.execute(stmt)
        db.session.commit()
        new_count = result.scalar()
        return new_count, new_count > limit

    @classmethod
    def get_count_today(cls) -> int:
        row = cls.query.filter_by(usage_date=date.today()).first()
        return row.question_count if row else 0


class CustomerPlan(db.Model):
    """
    Stores this store's premium plan status.
    There is exactly one row in this table (id=1).
    Manually inserted/updated — no billing flow yet.
    premium_until=NULL means lifetime.
    """
    __tablename__ = "customer_plans"

    id            = db.Column(db.Integer, primary_key=True, default=1)
    is_premium    = db.Column(db.Boolean, nullable=False, default=False)
    premium_since = db.Column(db.DateTime(timezone=True), nullable=True)
    premium_until = db.Column(db.DateTime(timezone=True), nullable=True)  # NULL = lifetime
    plan_ref      = db.Column(db.String(255), nullable=True)  # e.g. order ID, invoice ref

    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    @classmethod
    def get(cls) -> "CustomerPlan | None":
        """Always fetch the single store plan row."""
        return cls.query.filter_by(id=1).first()

    @property
    def is_active_premium(self) -> bool:
        if not self.is_premium:
            return False
        if self.premium_until is None:
            return True  # lifetime
        return datetime.now(timezone.utc) < self.premium_until