import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid

vapid = Vapid()
vapid.generate_keys()

private_key_obj = vapid.private_key
public_key_obj = vapid.public_key

raw_private_key = private_key_obj.private_numbers().private_value.to_bytes(32, "big")
raw_public_key_point = public_key_obj.public_bytes(
    encoding=serialization.Encoding.X962,
    format=serialization.PublicFormat.UncompressedPoint,
)

b64_private_key = base64.urlsafe_b64encode(raw_private_key).rstrip(b"=").decode("utf-8")
b64_public_key = (
    base64.urlsafe_b64encode(raw_public_key_point).rstrip(b"=").decode("utf-8")
)

print(f"VAPID_PRIVATE_KEY={b64_private_key}")
print(f"VAPID_PUBLIC_KEY={b64_public_key}")
