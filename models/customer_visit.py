# models/customer_visit.py

from models.db_models import db


class CustomerVisit(db.Model):
    """
    When a logged-in shopper last opened the widget — the "since your last
    visit" baseline for the welcome screen's What's new.

    Lives in the per-tenant database (like ChatUsage), so customer_id alone is
    the key. Kept server-side on purpose: the customer ID must never be stored
    in the browser (requirements doc, Privacy).

    Two timestamps, not one. If "new since" were simply last_seen_at, the
    first welcome-screen open would advance it and every reopen in the same
    visit would show nothing new. So a VISIT is a run of opens with no gap
    longer than services/whats_new.VISIT_GAP; previous_visit_at is the
    last_seen_at of the visit before this one, and it only moves when a new
    visit starts. What's new is stable for the whole visit.
    """
    __tablename__ = "customer_visits"

    customer_id       = db.Column(db.String, primary_key=True)
    last_seen_at      = db.Column(db.DateTime(timezone=True), nullable=False)
    previous_visit_at = db.Column(db.DateTime(timezone=True), nullable=True)
