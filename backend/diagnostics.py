"""Private Render Shell self-test: no real user login, trading or secrets output.

Usage from the deployed MiniNex backend shell:
    python -m backend.diagnostics

Creates a uniquely prefixed *temporary* diagnostic account, opens a separate
database connection to verify persistence, then deletes only that account.
Does not inspect, alter or delete any real user account or fee event.
"""
from __future__ import annotations

import os
import sys
from uuid import uuid4

import psycopg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException

from .auth import verify_privy_bearer
from .storage import _db_url, get_account_summary


def test_public_key() -> None:
    pem = os.getenv("MININEX_PRIVY_VERIFICATION_KEY", "").replace("\\n", "\n").strip()
    if not pem:
        raise RuntimeError("Privy Verification Key fehlt in Render Environment")
    try:
        key = serialization.load_pem_public_key(pem.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError):
        raise RuntimeError("Privy Verification Key ist kein gueltiger PEM Public Key") from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise RuntimeError("Privy Verification Key muss ein ES256/P-256 Public Key sein")
    print("Privy-Pruefschluessel: OK")


def test_unauthorized_rejected() -> None:
    for header in (None, "Bearer invalid-token"):
        try:
            verify_privy_bearer(header)
        except HTTPException as exc:
            if exc.status_code == 401:
                continue
            raise RuntimeError("Unerwartete Antwort fuer ungueltigen Token")
        raise RuntimeError("Ein ungueltiger Token wurde akzeptiert")
    print("Ohne gueltigen Privy-Token kein Kontozugang: OK")


def test_temporary_db_account() -> None:
    # Random synthetic DID, never a real person's account.
    did = "did:privy:diagnostic-" + uuid4().hex
    url = _db_url()
    created = False
    try:
        first = get_account_summary(did)
        created = True
        assert first["account_persistent"] is True
        assert first["trading_enabled"] is False
        assert first["wallet_connected"] is False
        assert first["performance_fee_pct"] == "5.00"
        assert set(first["ledger"]) == {"BTC", "ETH"}
        assert all(v["high_water_mark_usd"] == "0E-12" or
                   v["high_water_mark_usd"] == "0" for v in first["ledger"].values())
        print("PostgreSQL: Verbindung, Kontoanlage und BTC/ETH-HWM OK")

        # Fresh database connection, not an in-memory cache.
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT asset FROM mininex_asset_hwm WHERE privy_did=%s ORDER BY asset",
                    (did,),
                )
                if [item[0] for item in cur.fetchall()] != ["BTC", "ETH"]:
                    raise RuntimeError("Testkonto ist nicht persistent in PostgreSQL")
        second = get_account_summary(did)
        assert second["ledger"] == first["ledger"]
        print("Kontodaten aus neuer Datenbankverbindung erneut geladen: OK")
    finally:
        if created:
            # Only delete the uniquely generated test DID, never a real user.
            with psycopg.connect(url, connect_timeout=6) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM mininex_asset_hwm WHERE privy_did=%s", (did,)
                    )
                    cur.execute(
                        "DELETE FROM mininex_accounts WHERE privy_did=%s", (did,)
                    )
            print("Temporaeres Testkonto entfernt: OK")


def main() -> int:
    print("MiniNex V0.8.1 sicherer Backend-Systemtest")
    try:
        test_public_key()
        test_unauthorized_rejected()
        test_temporary_db_account()
    except (AssertionError, RuntimeError, HTTPException, psycopg.Error) as exc:
        # Never display URLs, connection errors or any token contents.
        if isinstance(exc, (RuntimeError, HTTPException)):
            print("TEST NICHT BESTANDEN:", str(exc)[:160])
        else:
            print("TEST NICHT BESTANDEN: Backend-Datenbank oder Testdaten inkonsistent.")
        return 1
    print("SYSTEMTEST: ALLE PRUEFUNGEN OK")
    print("Echte Privy-Anmeldung am Endgeraet: NOCH NICHT GETESTET")
    print("Live-Handel und Gebuehreneinzug: AUSGESCHALTET")
    return 0


if __name__ == "__main__":
    sys.exit(main())
