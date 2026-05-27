# models/chat_usage.py

from datetime import datetime, date, timezone
from models.db_models import db


class ChatUsage(db.Model):
    """
    Tracks daily question count per customer per platform.
    Composite PK prevents duplicate rows and makes upserts safe.
    """
    __tablename__ = "chat_usage"

    customer_id  = db.Column(db.String(255), primary_key=True)
    platform     = db.Column(db.String(20),  primary_key=True)  # "shopify" | "woocommerce"
    usage_date   = db.Column(db.Date,        primary_key=True)
    question_count = db.Column(db.Integer,   nullable=False, default=0)

    @classmethod
    def increment_and_check(
        cls,
        customer_id: str,
        platform: str,
        limit: int = 25,
    ) -> tuple[int, bool]:
        """
        Atomically increments the counter for today and returns
        (new_count, limit_exceeded).

        Uses INSERT ... ON CONFLICT DO UPDATE so it's race-condition-safe.
        """
        today = date.today()
        from sqlalchemy.dialects.postgresql import insert

        stmt = (
            insert(cls)
            .values(
                customer_id=customer_id,
                platform=platform,
                usage_date=today,
                question_count=1,
            )
            .on_conflict_do_update(
                index_elements=["customer_id", "platform", "usage_date"],
                set_={"question_count": cls.question_count + 1},
            )
            .returning(cls.question_count)
        )
        result = db.session.execute(stmt)
        db.session.commit()
        new_count = result.scalar()
        return new_count, new_count > limit

    @classmethod
    def get_count_today(cls, customer_id: str, platform: str) -> int:
        today = date.today()
        row = cls.query.filter_by(
            customer_id=customer_id,
            platform=platform,
            usage_date=today,
        ).first()
        return row.question_count if row else 0


class CustomerPlan(db.Model):
    """
    Tracks premium status per customer per platform.
    premium_until=NULL means the plan never expires (lifetime).
    """
    __tablename__ = "customer_plans"

    customer_id   = db.Column(db.String(255), primary_key=True)
    platform      = db.Column(db.String(20),  primary_key=True)
    is_premium    = db.Column(db.Boolean,     nullable=False, default=False)
    premium_since = db.Column(db.DateTime(timezone=True), nullable=True)
    premium_until = db.Column(db.DateTime(timezone=True), nullable=True)  # NULL = lifetime
    plan_ref      = db.Column(db.String(255), nullable=True)  # e.g. Shopify order GID or WC subscription ID

    updated_at    = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    @property
    def is_active_premium(self) -> bool:
        if not self.is_premium:
            return False
        if self.premium_until is None:
            return True  # lifetime
        return datetime.now(timezone.utc) < self.premium_until