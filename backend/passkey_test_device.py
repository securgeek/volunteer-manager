"""A software passkey for tests: produces real, signed WebAuthn responses.

Stands in for the browser plus authenticator, so registration and sign-in can be exercised
end to end with no browser. "none" attestation, discoverable, user-verified — the same shape a
platform passkey has. ES256 or ML-DSA keys; ML-DSA needs cryptography >= 49.

Test-only. Nothing in the app imports it.
"""
import base64
import hashlib
import json
import os
import struct
import uuid

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

FLAG_UP, FLAG_UV, FLAG_BE, FLAG_BS, FLAG_AT = 0x01, 0x04, 0x08, 0x10, 0x40


def b64url(data: bytes) -> str:
    """Base64url-encode without padding, as WebAuthn does.

    Args:
        data: The bytes to encode.

    Returns:
        The encoded text.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(s: str) -> bytes:
    """Decode unpadded base64url.

    Args:
        s: The encoded text, with or without padding.

    Returns:
        The decoded bytes.
    """
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftPasskey:
    """One software authenticator holding one credential.

    Call `register` with the server's creation options, then `assert_` with each sign-in's
    request options. Both return the JSON a browser would post.

    Args:
        alg: The COSE algorithm: -7 (ES256), or -48, -49 or -50 (ML-DSA-44, -65, -87).
        synced: Behave like a synced passkey: backup flags set, and a sign count that stays 0.
        aaguid: The authenticator model to report, as a UUID string. `None` sends all zeros.

    Raises:
        ValueError: For any other `alg`.
    """
    def __init__(self, alg: int = -7, synced: bool = False, aaguid: str = None):
        self.alg = alg
        # The authenticator model reported at registration; None sends all zeros.
        self.aaguid = uuid.UUID(aaguid).bytes if aaguid else b"\0" * 16
        self.credential_id = os.urandom(32)
        self.sign_count = 0 if synced else 1  # synced passkeys report 0 forever
        self.synced = synced
        self.user_handle = None
        if alg == -7:
            self._key = ec.generate_private_key(ec.SECP256R1())
        elif alg in (-48, -49, -50):
            from cryptography.hazmat.primitives.asymmetric import mldsa
            cls = {-48: mldsa.MLDSA44PrivateKey, -49: mldsa.MLDSA65PrivateKey,
                   -50: mldsa.MLDSA87PrivateKey}[alg]
            self._key = cls.generate()
        else:
            raise ValueError(f"unsupported test alg {alg}")

    # ── keys ──
    def _cose_key(self) -> bytes:
        if self.alg == -7:
            nums = self._key.public_key().public_numbers()
            return cbor2.dumps({1: 2, 3: -7, -1: 1,
                                -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        # ML-DSA: kty AKP (7), pub at label -1 (RFC 9964).
        return cbor2.dumps({1: 7, 3: self.alg, -1: self._key.public_key().public_bytes_raw()})

    def _sign(self, data: bytes) -> bytes:
        if self.alg == -7:
            return self._key.sign(data, ec.ECDSA(hashes.SHA256()))
        return self._key.sign(data)

    def _flags(self, extra: int = 0) -> int:
        f = FLAG_UP | FLAG_UV | extra
        if self.synced:
            f |= FLAG_BE | FLAG_BS
        return f

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin,
                           "crossOrigin": False}).encode()

    # ── ceremonies ──
    def register(self, options: dict, origin: str) -> str:
        """navigator.credentials.create() for these options, as the JSON a browser sends.

        Args:
            options: The server's `PublicKeyCredentialCreationOptions`, as JSON-decoded.
            origin: The page origin to sign into `clientDataJSON`.
        """
        rp_id = options["rp"]["id"]
        assert self.alg in [p["alg"] for p in options["pubKeyCredParams"]], "alg not offered"
        self.user_handle = b64url_decode(options["user"]["id"])
        cdj = self._client_data("webauthn.create", options["challenge"], origin)
        auth_data = (hashlib.sha256(rp_id.encode()).digest()
                     + bytes([self._flags(FLAG_AT)])
                     + struct.pack(">I", self.sign_count)
                     + self.aaguid
                     + struct.pack(">H", len(self.credential_id)) + self.credential_id
                     + self._cose_key())
        att_obj = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return json.dumps({
            "id": b64url(self.credential_id), "rawId": b64url(self.credential_id),
            "type": "public-key", "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {"clientDataJSON": b64url(cdj), "attestationObject": b64url(att_obj),
                         "transports": ["internal", "hybrid"]},
        })

    def assert_(self, options: dict, origin: str, rp_id: str = None,
                user_handle: bytes = None, flags: int = None) -> str:
        """navigator.credentials.get() for these options, as the JSON a browser sends.

        Args:
            options: The server's `PublicKeyCredentialRequestOptions`, as JSON-decoded.
            origin: The page origin to sign into `clientDataJSON`.
            rp_id: Sign for this RP id instead of the one in `options`, to test a mismatch.
            user_handle: Send this user handle instead of the registered one.
            flags: Send these authenticator flags instead of the usual UP, UV and backup bits.
        """
        rp_id = rp_id or options["rpId"]
        if not self.synced:
            self.sign_count += 1
        cdj = self._client_data("webauthn.get", options["challenge"], origin)
        auth_data = (hashlib.sha256(rp_id.encode()).digest()
                     + bytes([self._flags() if flags is None else flags])
                     + struct.pack(">I", self.sign_count))
        sig = self._sign(auth_data + hashlib.sha256(cdj).digest())
        handle = self.user_handle if user_handle is None else user_handle
        return json.dumps({
            "id": b64url(self.credential_id), "rawId": b64url(self.credential_id),
            "type": "public-key", "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {"clientDataJSON": b64url(cdj), "authenticatorData": b64url(auth_data),
                         "signature": b64url(sig), "userHandle": b64url(handle)},
        })
