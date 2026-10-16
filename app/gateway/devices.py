"""Device public keys and ES256 signature checks.

A browser's WebCrypto ECDSA signature is p1363 — r‖s, 32 bytes each. The
`cryptography` library verifies DER, so the two halves are re-encoded first.
"""

import base64
import secrets

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Devices:
    def __init__(self):
        self._keys: dict[str, tuple[str, ec.EllipticCurvePublicKey]] = {}

    def register(self, customer_id: str, jwk: dict) -> str:
        """Register a P-256 public key (JWK) for a customer; returns its key_id."""
        if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
            raise ValueError("only EC P-256 keys are accepted")
        x, y = (int.from_bytes(b64url_decode(jwk[k]), "big") for k in ("x", "y"))
        key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        key_id = f"k_{secrets.token_hex(8)}"
        self._keys[key_id] = (customer_id, key)
        return key_id

    def owner(self, key_id: str) -> str | None:
        return self._keys.get(key_id, (None,))[0]

    def verify(self, key_id: str, data: bytes, signature_b64url: str) -> bool:
        try:
            raw = b64url_decode(signature_b64url)
        except ValueError:
            return False
        if key_id not in self._keys or len(raw) != 64:
            return False
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        try:
            self._keys[key_id][1].verify(der, data, ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            return False
