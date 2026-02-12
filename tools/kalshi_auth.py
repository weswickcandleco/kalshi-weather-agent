import datetime
import base64
import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding


def load_private_key(path):
    """Load an RSA private key from a PEM file."""
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )


def make_auth_headers(pk, api_key_id, method, path):
    """Build the three Kalshi auth headers using RSA-PSS signing."""
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    path_no_query = path.split("?")[0]
    message = f"{ts}{method}{path_no_query}".encode()
    signature = pk.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }


def kalshi_get(pk, api_key_id, base_url, path, params=None):
    """Authenticated GET request to Kalshi API."""
    full_path = path
    if params:
        full_path += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    headers = make_auth_headers(pk, api_key_id, "GET", path)
    return requests.get(base_url + full_path, headers=headers, timeout=15)


def kalshi_post(pk, api_key_id, base_url, path, body):
    """Authenticated POST request to Kalshi API."""
    headers = make_auth_headers(pk, api_key_id, "POST", path)
    return requests.post(base_url + path, json=body, headers=headers, timeout=15)
