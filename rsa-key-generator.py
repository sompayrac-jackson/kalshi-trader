from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

# Generate key
private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

# Serialize private key
pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)

# Serialize public key
pub_pem = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo
)

with open("private_key.pem", "wb") as f:
    f.write(pem)

with open("public_key.pem", "wb") as f:
    f.write(pub_pem)