import os

os.environ.setdefault("PAYPAL_MODE", "fake")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_allowance.db")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import main
from app.db import Base, make_engine
from app.paypal import FakePayPal


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


@pytest.fixture()
def paypal():
    return FakePayPal()


@pytest.fixture()
def client(engine, paypal):
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def _session():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    main.app.dependency_overrides[main.get_session] = _session
    main.app.dependency_overrides[main.get_paypal] = lambda: paypal
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()
