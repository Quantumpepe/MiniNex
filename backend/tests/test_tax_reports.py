"""Tax-report boundaries: Privy access, verified-only history and safe exports."""
from datetime import datetime, timezone
import csv
import io
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from backend import app as api, storage, tax_reports


@pytest.fixture
def signed(monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    public=key.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)

    def make(did="did:privy:alice"):
        now=int(time.time())
        token=jwt.encode({
            "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
            "sub":did,"sid":"tax-test","iat":now,"exp":now+600,
        },key,algorithm="ES256")
        return {"Authorization":"Bearer "+token}
    return make


def record():
    return {
        "date_utc":"2026-05-03T12:00:00+00:00",
        "source":"BLOCKCHAIN","type":"DEPOSIT","asset":"BTC",
        "quantity":"0.015","amount_usd":"1000","fee_usd":"1.25",
        "realized_net_usd":"","network":"bitcoin",
        "tx_hash":"a"*64,"record_id":"btc-tx","verification":"CONFIRMED",
    }


def test_exports_and_list_require_verified_identity(signed):
    client=TestClient(api.app)
    for path in ("/api/activity/2026","/api/activity/2026/export/csv",
                 "/api/activity/2026/export/pdf"):
        for headers in ({},{"Authorization":"Bearer fake"}):
            assert client.get(path,headers=headers).status_code==401


def test_exports_only_identity_bound_records(monkeypatch,signed):
    calls=[]
    def listing(did,year):
        calls.append((did,year))
        return [record()]
    monkeypatch.setattr(api,"list_verified_activity",listing)
    client=TestClient(api.app)
    listing_response=client.get("/api/activity/2026",headers=signed("did:privy:alice"))
    csv_response=client.get("/api/activity/2026/export/csv",headers=signed("did:privy:bob"))
    pdf_response=client.get("/api/activity/2026/export/pdf",headers=signed("did:privy:bob"))
    assert listing_response.status_code==csv_response.status_code==pdf_response.status_code==200
    assert listing_response.json()["paper_excluded"] is True
    assert listing_response.json()["tax_complete"] is False
    assert listing_response.json()["rows"][0]["tx_hash"]=="a"*64
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "MiniNex_Activity_2026.csv" in csv_response.headers["content-disposition"]
    assert b"BLOCKCHAIN" in csv_response.content
    assert pdf_response.headers["content-type"]=="application/pdf"
    assert pdf_response.content.startswith(b"%PDF-")
    assert calls==[("did:privy:alice",2026),("did:privy:bob",2026),("did:privy:bob",2026)]


@pytest.mark.parametrize("path",[
    "/api/activity/2019","/api/activity/2030/export/csv",
    "/api/activity/2026/export/html",
])
def test_bad_year_or_file_type_rejected(monkeypatch,signed,path):
    monkeypatch.setattr(api,"list_verified_activity",lambda *args: pytest.fail("No query allowed"))
    assert TestClient(api.app).get(path,headers=signed()).status_code in (404,422)


def test_empty_year_never_uses_paper_history(monkeypatch,signed):
    monkeypatch.setattr(api,"list_verified_activity",lambda did,year: [])
    client=TestClient(api.app)
    r=client.get("/api/activity/2026/export/pdf",headers=signed())
    c=client.get("/api/activity/2026/export/csv",headers=signed())
    assert r.status_code==c.status_code==200
    assert r.content.startswith(b"%PDF-")
    assert b"PAPER_BUY" not in c.content and b"PAPER_SELL" not in c.content
    rows=list(csv.DictReader(io.StringIO(c.content.decode("utf-8-sig"))))
    assert rows==[]


def test_csv_preserves_loss_and_escapes_spreadsheet_formula():
    row=record()
    row.update({"realized_net_usd":"-12.50","tx_hash":"=HYPERLINK(\"fake\")"})
    content=tax_reports.make_csv([row],2026).decode("utf-8-sig")
    parsed=list(csv.DictReader(io.StringIO(content)))[0]
    assert parsed["realized_net_usd"]=="-12.50"
    assert parsed["tx_hash"].startswith("'=")


def test_schema_forbids_simulation_and_cross_user_duplicates():
    ddl="\n".join(storage.SCHEMA)
    assert "mininex_verified_activity" in ddl
    assert "verification='CONFIRMED'" in ddl
    assert "PRIMARY KEY (privy_did,source,record_id)" in ddl
    assert "mininex_activity_by_user_year" in ddl
    assert "PAPER_AUTO_DEMO" not in ddl
    assert tax_reports.validate_year(2026)==2026
    for invalid in (True,"2026",-1,2019,3000):
        with pytest.raises(ValueError):
            tax_reports.validate_year(invalid)
