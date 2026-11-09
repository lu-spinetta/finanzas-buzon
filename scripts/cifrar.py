"""Cifra los gastos de Mercado Pago para dejarlos en el buzón público.

Uso: python scripts/cifrar.py <movimientos.json> <salida.json>
La clave sale del Secret CLAVE_BUZON (la genera la página "Gastos de a dos").
Cifrado AES-256-GCM con clave derivada por PBKDF2-SHA256. Sin la clave, el
archivo de salida no se puede leer.
"""
import base64, json, os, re, sys
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITER = 310000

def main(entrada, salida):
    clave = re.sub(r"[\s-]", "", os.environ.get("CLAVE_BUZON", "")).upper()
    if len(clave) < 20:
        sys.exit("Falta el Secret CLAVE_BUZON o está incompleto")
    with open(entrada, "rb") as f:
        datos = f.read()
    generado = json.loads(datos)["generado"]
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(clave.encode())
    ct = AESGCM(key).encrypt(iv, datos, None)
    b = lambda x: base64.b64encode(x).decode()
    os.makedirs(os.path.dirname(salida) or ".", exist_ok=True)
    with open(salida, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "iter": ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct), "generado": generado}, f)
    print("Buzón cifrado listo")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
