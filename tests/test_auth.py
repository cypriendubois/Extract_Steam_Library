import base64
import json
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from steam_auth import (  # noqa: E402
    AuthError,
    AuthExpired,
    encrypt_password,
    is_refresh_token,
    load_auth,
    normalize_refresh_token,
    save_auth,
    steamid_from_token,
    token_expiry,
)


def make_jwt(payload):
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


class JwtTests(unittest.TestCase):
    def test_steamid_from_sub(self):
        token = make_jwt({"sub": "76561198000000000", "aud": ["web:store"]})
        self.assertEqual(steamid_from_token(token), "76561198000000000")

    def test_steamid_from_invalid_token(self):
        self.assertIsNone(steamid_from_token("not-a-jwt"))

    def test_expiry(self):
        future = int(
            (datetime.now(timezone.utc) + timedelta(days=30)).timestamp()
        )
        token = make_jwt({"sub": "1", "exp": future})
        self.assertIsNotNone(token_expiry(token))
        self.assertEqual(int(token_expiry(token).timestamp()), future)

    def test_refresh_token_detection(self):
        refresh = make_jwt({"sub": "1", "aud": ["derive"], "exp": 9999999999})
        access = make_jwt({"sub": "1", "aud": ["web:store"], "exp": 9999999999})
        self.assertTrue(is_refresh_token(refresh))
        self.assertFalse(is_refresh_token(access))

    def test_normalize_refresh_token(self):
        token = make_jwt({"sub": "76561198000000000", "aud": ["derive"]})
        self.assertEqual(normalize_refresh_token(token), token)
        self.assertEqual(
            normalize_refresh_token(f"76561198000000000||{token}"), token
        )
        self.assertEqual(
            normalize_refresh_token(f"76561198000000000%7C%7C{token}"), token
        )
        self.assertIsNone(normalize_refresh_token(None))


class PasswordEncryptionTests(unittest.TestCase):
    def test_password_round_trip(self):
        private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        numbers = private_key.public_key().public_numbers()

        encrypted = encrypt_password(
            "hunter2", format(numbers.n, "x"), format(numbers.e, "x")
        )
        decrypted = private_key.decrypt(
            base64.b64decode(encrypted), padding.PKCS1v15()
        )

        self.assertEqual(decrypted, b"hunter2")


class AuthFileTests(unittest.TestCase):
    def test_save_is_private_and_load_round_trips(self):
        refresh = make_jwt(
            {"sub": "76561198000000000", "aud": ["derive"], "exp": 9999999999}
        )
        auth = {
            "refresh_token": refresh,
            "steamid": "76561198000000000",
            "account_name": "tester",
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            save_auth(auth, str(path))

            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)

            loaded = load_auth(str(path))
            self.assertEqual(loaded["steamid"], "76561198000000000")
            self.assertEqual(loaded["account_name"], "tester")
            self.assertEqual(loaded["refresh_token"], refresh)

    def test_load_repairs_prefixed_refresh_token(self):
        steamid = "76561198000000000"
        token = make_jwt(
            {"sub": steamid, "aud": ["derive"], "exp": 9999999999}
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "refresh_token": f"{steamid}||{token}",
                        "steamid": steamid,
                    }
                )
            )

            loaded = load_auth(str(path))

            self.assertEqual(loaded["refresh_token"], token)
            on_disk = json.loads(path.read_text())
            self.assertEqual(on_disk["refresh_token"], token)

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(
                load_auth(str(Path(directory) / "missing.json"))
            )

    def test_expired_file_raises(self):
        refresh = make_jwt({"sub": "1", "aud": ["derive"]})
        auth = {
            "refresh_token": refresh,
            "steamid": "1",
            "refresh_token_expires_at": (
                datetime.now(timezone.utc) - timedelta(days=1)
            ).isoformat(),
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            save_auth(auth, str(path))
            with self.assertRaises(AuthExpired):
                load_auth(str(path))

    def test_malformed_file_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text(json.dumps({"steamid": "1"}))
            with self.assertRaises(AuthError):
                load_auth(str(path))


if __name__ == "__main__":
    unittest.main()
