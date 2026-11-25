"""
gunicorn.conf.py - loaded by ecosystem.config.js (-c gunicorn.conf.py).

Access log WITHOUT the query string. Shopify app-proxy URLs carry
logged_in_customer_id=..., and history search carries ?q=<what the shopper
typed>; gunicorn's default format logs the full request line ("%(r)s") and
the referer, so both would land in PM2's log as plain text. %(U)s is the path
only. Shopper data encryption is in customer_crypto.py.

Fields: client, time, "METHOD /path PROTOCOL", status, bytes, seconds.
"""

access_log_format = '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(L)s'
