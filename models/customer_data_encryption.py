"""
models/customer_data_encryption.py - remembers that shopper data has been
encrypted (control-plane DB, one row with id=1).

Written the first time the server starts with ENCRYPT_CUSTOMER_DATA=1. From
then on customer_crypto.check_startup refuses to start without the key, and
uses `canary` (a known value encrypted with the key) to catch a wrong key
before any shopper data is read with it.
"""

from models.db_models import db


class CustomerDataEncryption(db.Model):
    __tablename__ = "customer_data_encryption"

    id               = db.Column(db.Integer, primary_key=True)
    canary           = db.Column(db.Text, nullable=False)
    first_enabled_at = db.Column(db.DateTime(timezone=True), nullable=False)
