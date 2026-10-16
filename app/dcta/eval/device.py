"""Simulated customer device for tests and the eval harness.

Holds a P-256 key and signs exactly as the browser does: ECDSA/SHA-256 over the
UTF-8 signing string, output as p1363 (r‖s) in base64url. So a signature from
here goes through the same verification path as one from WebCrypto.
"""

import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class SimulatedDevice:

    def __init__(self):
        self._key = ec.generate_private_key(ec.SECP256R1())

    def jwk(self) -> dict:
        n = self._key.public_key().public_numbers()
        return {"kty": "EC", "crv": "P-256",
                "x": _b64url(n.x.to_bytes(32, "big")), "y": _b64url(n.y.to_bytes(32, "big"))}

    def sign(self, signing_input: str) -> dict:
        r, s = decode_dss_signature(self._key.sign(signing_input.encode("utf-8"),
                                                   ec.ECDSA(hashes.SHA256())))
        return {"alg": "ES256", "format": "p1363",
                "value": _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}
