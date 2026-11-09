"""Trae los gastos recientes de Mercado Pago y los guarda en data/movimientos.json.

Lo corre GitHub Actions todos los días. El token se lee de la variable de entorno
MP_ACCESS_TOKEN (un Secret del repositorio); nunca se escribe en ningún archivo.
Solo hace consultas de lectura.
"""
import json, os, re, sys, urllib.request, urllib.parse
from datetime import datetime, timezone

DIAS = int(os.environ.get("DIAS", "15"))
DESDE = os.environ.get("DESDE", "2026-11-01")  # no traer gastos anteriores a esta fecha (ya están en Notion)
TOKEN = os.environ.get("MP_ACCESS_TOKEN")
if not TOKEN:
    sys.exit("Falta el secret MP_ACCESS_TOKEN")

def get(path):
    req = urllib.request.Request("https://api.mercadopago.com" + path,
                                 headers={"Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

KIND = {"regular_payment": "compra", "money_transfer": "transferencia",
        "recurring_payment": "suscripcion"}

def limpiar(s):
    s = (s or "").strip()
    s = re.sub(r"^Producto de\s+", "", s, flags=re.I)
    s = re.sub(r"^Dlo\*", "", s, flags=re.I)
    s = re.sub(r"\s+compra$", "", s, flags=re.I)
    s = s.replace("*", " ").strip()
    return s[:1].upper() + s[1:] if s else "Pago"

RANGO_DESDE = os.environ.get("RANGO_DESDE", "").strip()
RANGO_HASTA = os.environ.get("RANGO_HASTA", "").strip()

def main():
    me = str(get("/users/me")["id"])
    for f in (RANGO_DESDE, RANGO_HASTA):
        if f and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f):
            sys.exit(f"Fecha inválida: {f!r}. Usá el formato AAAA-MM-DD, por ejemplo 2026-11-01")
    if RANGO_DESDE:
        ini = RANGO_DESDE + "T00:00:00.000-03:00"
        fin = RANGO_HASTA + "T23:59:59.999-03:00" if RANGO_HASTA else "NOW"
        rango = {"desde": RANGO_DESDE, "hasta": RANGO_HASTA or "hoy"}
    else:
        ini, fin, rango = f"NOW-{DIAS}DAYS", "NOW", {"ultimos_dias": DIAS}
    pagos, off = [], 0
    while True:
        q = urllib.parse.urlencode({"range": "date_created", "begin_date": ini,
                                    "end_date": fin, "sort": "date_created",
                                    "criteria": "desc", "limit": 100, "offset": off})
        d = get("/v1/payments/search?" + q)
        pagos += d["results"]
        off += 100
        if off >= d["paging"]["total"]:
            break
    apodos_mp, gastos = {}, []
    for r in pagos:
        if str(r.get("payer_id")) != me:
            continue  # ingresos: no son gastos
        if r.get("operation_type") not in KIND or r.get("status") not in ("approved", "authorized"):
            continue
        col = str((r.get("collector") or {}).get("id") or "")
        if col == me:
            continue
        kind = KIND[r["operation_type"]]
        bi = ((r.get("point_of_interaction") or {}).get("transaction_data") or {}).get("bank_info") or {}
        titular = (bi.get("collector") or {}).get("account_holder_name")
        g = {"src": "mp", "mpId": str(r["id"]), "date": r["date_created"],
             "amount": r["transaction_amount"], "currency": r.get("currency_id", "ARS"),
             "kind": kind, "estado": "pendiente", "medio": "Mercado Pago"}
        if kind == "transferencia":
            if col and col not in apodos_mp:
                try:
                    apodos_mp[col] = get("/users/" + col).get("nickname") or ""
                except Exception:
                    apodos_mp[col] = ""
            g["contactKey"] = "c_" + col
            g["title"] = titular or apodos_mp.get(col) or "Transferencia"
            g["titleSource"] = "titular" if titular else "usuario"
        else:
            g["title"] = limpiar(r.get("description"))
            g["contactKey"] = "m_" + re.sub(r"[^a-z0-9]+", "-", g["title"].lower()).strip("-")[:60]
            g["titleSource"] = "comercio"
        loc = (r.get("point_of_interaction") or {}).get("location") or {}
        try:
            lat, lng = float(loc.get("latitude")), float(loc.get("longitude"))
            if lat and lng:
                g["lat"], g["lng"] = round(lat, 6), round(lng, 6)  # ubicación del comercio (pagos con QR)
        except (TypeError, ValueError):
            pass
        if r.get("status") == "authorized":
            g["nota"] = "Pago autorizado, todavía no acreditado"
        gastos.append(g)
    if not RANGO_DESDE:  # en un pedido manual se respeta el rango elegido
        gastos = [g for g in gastos if g["date"][:10] >= DESDE]
    ahora = datetime.now(timezone.utc).isoformat()
    for g in gastos:
        g["importado"] = ahora  # cuándo se trajo de Mercado Pago (se muestra en la página)
    gastos.sort(key=lambda g: g["date"], reverse=True)
    os.makedirs("data", exist_ok=True)
    with open("data/movimientos.json", "w", encoding="utf-8") as f:
        json.dump({"generado": datetime.now(timezone.utc).isoformat(), "rango": rango,
                   "gastos": gastos}, f, ensure_ascii=False, indent=1)
    print(f"{len(gastos)} gastos", rango)

if __name__ == "__main__":
    main()
