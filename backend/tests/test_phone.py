import os, tempfile
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "p.db")
from fastapi.testclient import TestClient
from app.main import app, clean_phone

def test_phone_format_check():
    assert clean_phone("98765 43210") == "+919876543210" and clean_phone("+91 98765-43210") == "+919876543210" and clean_phone("09876543210") == "+919876543210"
    assert clean_phone("5876543210") is None and clean_phone("98765") is None and clean_phone("abc") is None and clean_phone("") is None

def test_phone_is_optional_and_checked_when_given():
    with TestClient(app) as c:
        r = c.post("/api/register", json=dict(team_name="NoPhone", name="Asha", roll="N1")); assert r.status_code == 200
        code = r.json()["code"]
        assert c.post("/api/join", json=dict(code=code, name="Ben", roll="N2", phone="")).status_code == 200
        assert c.post("/api/join", json=dict(code=code, name="Cy", roll="N3", phone="123")).status_code == 422          # not 10 digits
        assert c.post("/api/join", json=dict(code=code, name="Cy", roll="N3", phone="5222222222")).status_code == 422   # not an Indian mobile
        assert c.post("/api/join", json=dict(code=code, name="Cy", roll="N3", phone="9222222222")).status_code == 200
        # rejoining: a player who gave a phone must give the same one
        assert c.post("/api/join", json=dict(code=code, name="Cy", roll="N3")).status_code == 403
        assert c.post("/api/join", json=dict(code=code, name="Cy", roll="N3", phone="9111111111")).status_code == 403
        assert c.post("/api/join", json=dict(code=code, name="Cy", roll="N3", phone="92222 22222")).status_code == 200
