# models/chat_usage.py

from datetime import date
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
