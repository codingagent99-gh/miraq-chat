"""The /shopify/admin/conversations queries against a real Postgres.

test_shopify_admin.py mocks the database; this runs the actual SQL, with two
stores in two databases, to check paging, counts, the first question, the
transcript and that one store can't read the other's chats.

Needs a Postgres the tests may create and drop databases on. Skipped unless
MIRAQ_TEST_PG_DSN is set, e.g.:

    MIRAQ_TEST_PG_DSN=postgresql://postgres@localhost:5432/postgres \\
        python -m pytest --noconftest tests_shopify_admin
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.getenv("MIRAQ_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="MIRAQ_TEST_PG_DSN not set")

# Same session-token helper and app credentials as the route tests.
from tests_shopify_admin.test_shopify_admin import m, tok  # noqa: E402

CONTROL, STORE_A, STORE_B = "miraq_test_control", "tenant_test_conv_a", "tenant_test_conv_b"
SHOP_A, SHOP_B = "store-a.myshopify.com", "store-b.myshopify.com"
# Setup failed before its database was created: the database never exists.
SHOP_FAILED, MISSING_DB = "store-failed.myshopify.com", "tenant_test_conv_missing"
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def _dsn(db_name):
    head, _sep, _old = DSN.rpartition("/")
    return f"{head}/{db_name}"


@pytest.fixture(scope="module")
def stores():
    from sqlalchemy import create_engine, text
    from flask import Flask
    import store_registry
    from models import db, Conversation, Message, Tenant
    from models.db_engine_registry import DBEngineRegistry

    admin = create_engine(DSN, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        for name in (CONTROL, STORE_A, STORE_B):
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{name}"'))

    control = create_engine(_dsn(CONTROL))
    Tenant.__table__.create(control)
    with control.begin() as c:
        for shop, db_name, status in ((SHOP_A, STORE_A, "active"), (SHOP_B, STORE_B, "active"),
                                      (SHOP_FAILED, MISSING_DB, "provision_failed")):
            c.execute(Tenant.__table__.insert().values(
                tenant_id=uuid.uuid4(), db_name=db_name, shopify_domain=shop, status=status,
                ecommerce_backend="shopify", plan="free", features={}, build_attempts=0))

    def make_store(db_name, convs):
        eng = create_engine(_dsn(db_name))
        Conversation.__table__.create(eng)
        Message.__table__.create(eng)
        with eng.begin() as c:
            for conv_id, customer_id, updated, msgs in convs:
                c.execute(Conversation.__table__.insert().values(
                    id=conv_id, customer_id=customer_id, flow_state="idle", context_data={},
                    created_at=updated - timedelta(minutes=5), updated_at=updated))
                for i, (role, content, meta) in enumerate(msgs):
                    c.execute(Message.__table__.insert().values(
                        conversation_id=conv_id, role=role, content=content, intent="",
                        metadata_json=meta, created_at=updated - timedelta(minutes=5) + timedelta(seconds=i)))
        eng.dispose()

    ids = {k: uuid.uuid4() for k in ("a_new", "a_mid", "a_old", "a_empty", "a_bot_only", "b_only")}
    make_store(STORE_A, [
        (ids["a_new"], "7001", T0 + timedelta(hours=3), [
            ("user", "Do you have   grey\nmatte tiles?", {}),
            ("bot", "Here are some grey matte tiles.", {"products": [{"id": 1}, {"id": 2}, {"id": 3}]}),
            ("user", "Add the first one", {}),
            ("bot", "Added to your cart.", {}),
        ]),
        # Same updated_at as a_new: the cursor must still page correctly.
        (ids["a_mid"], None, T0 + timedelta(hours=3), [("user", "Shipping to Pune?", {}), ("bot", "Yes.", {})]),
        (ids["a_old"], None, T0, [("user", "Hi", {}), ("bot", "Hello!", {})]),
        (ids["a_empty"], None, T0 + timedelta(hours=5), []),
        (ids["a_bot_only"], None, T0 + timedelta(hours=6), [("bot", "Welcome back!", {})]),
    ])
    make_store(STORE_B, [(ids["b_only"], "9001", T0 + timedelta(hours=1), [("user", "Store B secret question", {})])])

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = _dsn(CONTROL)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    app.register_blueprint(m.shopify_admin_bp)
    previous = store_registry._engine_registry
    store_registry._engine_registry = DBEngineRegistry(_dsn(CONTROL))
    try:
        yield app.test_client(), ids
    finally:
        store_registry._engine_registry = previous
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        control.dispose()
        with admin.connect() as c:
            for name in (CONTROL, STORE_A, STORE_B):
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def auth(shop):
    return {"Authorization": f"Bearer {tok(shop)}"}


def test_list_pages_newest_first_and_skips_chats_without_a_question(stores):
    client, ids = stores
    r1 = client.get("/shopify/admin/conversations?limit=2", headers=auth(SHOP_A))
    assert r1.status_code == 200, r1.json
    page1 = r1.json["conversations"]
    assert r1.json["has_more"] is True
    # a_new and a_mid share updated_at; ties go by id, descending.
    tied = sorted([str(ids["a_new"]), str(ids["a_mid"])], reverse=True)
    assert [c["id"] for c in page1] == tied

    r2 = client.get("/shopify/admin/conversations", headers=auth(SHOP_A),
                    query_string={"limit": 2, "before": page1[-1]["cursor"]})
    assert r2.status_code == 200 and r2.json["has_more"] is False
    assert [c["id"] for c in r2.json["conversations"]] == [str(ids["a_old"])]

    every = {c["id"] for c in page1 + r2.json["conversations"]}
    assert str(ids["a_empty"]) not in every and str(ids["a_bot_only"]) not in every


def test_list_counts_first_question_and_customer(stores):
    client, ids = stores
    rows = {c["id"]: c for c in client.get("/shopify/admin/conversations", headers=auth(SHOP_A)).json["conversations"]}
    new = rows[str(ids["a_new"])]
    assert new["message_count"] == 4
    assert new["first_question"] == "Do you have grey matte tiles?"
    assert new["customer_id"] == "7001"
    assert rows[str(ids["a_mid"])]["customer_id"] is None
    assert new["last_active_at"] == (T0 + timedelta(hours=3)).isoformat()


def test_transcript(stores):
    client, ids = stores
    r = client.get(f"/shopify/admin/conversations/{ids['a_new']}", headers=auth(SHOP_A))
    assert r.status_code == 200
    msgs = r.json["messages"]
    assert [x["from"] for x in msgs] == ["shopper", "miraq", "shopper", "miraq"]
    assert msgs[1]["products_shown"] == 3 and msgs[0]["products_shown"] == 0
    assert r.json["truncated"] is False and r.json["customer_id"] == "7001"


def test_one_store_cannot_read_another(stores):
    client, ids = stores
    listed = client.get("/shopify/admin/conversations", headers=auth(SHOP_A)).json["conversations"]
    assert str(ids["b_only"]) not in {c["id"] for c in listed}
    assert client.get(f"/shopify/admin/conversations/{ids['b_only']}", headers=auth(SHOP_A)).status_code == 404
    # ...while store B sees its own.
    r = client.get(f"/shopify/admin/conversations/{ids['b_only']}", headers=auth(SHOP_B))
    assert r.status_code == 200 and r.json["messages"][0]["text"] == "Store B secret question"


def test_unknown_store_404(stores):
    client, _ids = stores
    assert client.get("/shopify/admin/conversations", headers=auth("nobody.myshopify.com")).status_code == 404


def test_failed_setup_without_database_is_empty_not_error(stores):
    client, _ids = stores
    r = client.get("/shopify/admin/conversations", headers=auth(SHOP_FAILED))
    assert r.status_code == 200 and r.json == {"conversations": [], "has_more": False}
