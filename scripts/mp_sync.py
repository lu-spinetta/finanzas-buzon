"""Trae los gastos recientes de Mercado Pago y los guarda en data/movimientos.json.

Lo corre GitHub Actions todos los días. El token se lee de la variable de entorno
MP_ACCESS_TOKEN (un Secret del repositorio); nunca se escribe en ningún archivo.
Solo hace consultas de lectura.
"""
import csv, io, json, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

DIAS = int(os.environ.get("DIAS", "15"))
DESDE = os.environ.get("DESDE", "2026-11-01")  # no traer gastos anteriores a esta fecha (ya están en Notion)
TOKEN = os.environ.get("MP_ACCESS_TOKEN")
if not TOKEN:
    sys.exit("Falta el secret MP_ACCESS_TOKEN")

def get(path, raw=False, method="GET", body=None):
    req = urllib.request.Request("https://api.mercadopago.com" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + TOKEN,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = r.read()
        return d if raw else (json.loads(d) if d.strip() else None)

# Todo lo que sale de la cuenta entra, aunque no sea una compra: después se clasifica o se descarta.
KIND = {"regular_payment": "compra", "money_transfer": "transferencia",
        "recurring_payment": "suscripcion", "pos_payment": "compra"}

def salidas_del_reporte(ini_utc, fin_utc):
    """Movimientos de salida que no figuran como pagos (sobre todo transferencias a otros bancos).
    Se sacan del Reporte de liquidaciones de Mercado Pago. Devuelve [] y un aviso si no se pudo."""
    t0 = datetime.now(timezone.utc) - timedelta(seconds=30)
    get("/v1/account/release_report", method="POST",
        body={"begin_date": ini_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "end_date": fin_utc.strftime("%Y-%m-%dT%H:%M:%SZ")})
    nombre = None
    for _ in range(36):  # hasta 6 minutos
        time.sleep(10)
        for x in get("/v1/account/release_report/list") or []:
            try:
                creado = datetime.fromisoformat(str(x.get("date_created")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if creado >= t0 and x.get("file_name") and "manual" in x["file_name"]:
                nombre = x["file_name"]
                break
        if nombre:
            break
    if not nombre:
        return None
    txt = get("/v1/account/release_report/" + nombre, raw=True).decode("utf-8-sig", "replace")
    cab = txt.split("\n", 1)[0]
    filas = list(csv.DictReader(io.StringIO(txt), delimiter=";" if cab.count(";") > cab.count(",") else ","))
    def num(v):
        try:
            return float((v or "0").replace(",", "."))
        except ValueError:
            return 0.0
    salen = []
    for f in filas:
        desc = (f.get("DESCRIPTION") or "").strip()
        neto = num(f.get("NET_DEBIT_AMOUNT")) - num(f.get("NET_CREDIT_AMOUNT"))
        if neto <= 0 or desc.startswith("reserve_") or not f.get("SOURCE_ID"):
            continue  # entradas, y las reservas internas que Mercado Pago anota y libera
        salen.append({"id": str(f["SOURCE_ID"]).strip(), "desc": desc, "monto": round(neto, 2),
                      "fecha": (f.get("DATE") or f.get("TRANSACTION_APPROVAL_DATE") or "").strip()})
    return salen

DESC_SALIDA = {"payout": "Transferencia a otro banco", "refund": "Devolución que hiciste",
               "chargeback": "Contracargo", "withdrawal": "Retiro de dinero"}

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
        if r.get("status") not in ("approved", "authorized"):
            continue  # rechazados, cancelados o devueltos: la plata no salió
        col = str((r.get("collector") or {}).get("id") or "")
        if col == me:
            continue
        kind = KIND.get(r.get("operation_type"), "otro")
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
    # Lo que no figura como pago: transferencias a otros bancos y otras salidas de dinero
    ids = {g["mpId"] for g in gastos} | {str(r["id"]) for r in pagos}
    if RANGO_DESDE:
        ini_utc = datetime.fromisoformat(RANGO_DESDE + "T00:00:00-03:00").astimezone(timezone.utc)
        fin_utc = (datetime.fromisoformat(RANGO_HASTA + "T23:59:59-03:00").astimezone(timezone.utc)
                   if RANGO_HASTA else datetime.now(timezone.utc))
    else:
        fin_utc = datetime.now(timezone.utc)
        ini_utc = fin_utc - timedelta(days=DIAS)
    avisos = []
    try:
        salen = salidas_del_reporte(ini_utc, fin_utc)
    except Exception as e:
        salen = None
    if salen is None:
        avisos.append("No se pudieron traer las transferencias a otros bancos esta vez.")
        salen = []
    for s in salen:
        if s["id"] in ids:
            continue  # ya vino como pago
        mid = ("po_" if s["desc"] == "payout" else "rr_") + s["id"]
        if mid in ids:
            continue
        ids.add(mid)
        es_banco = s["desc"] == "payout"
        gastos.append({"src": "mp", "mpId": mid, "date": s["fecha"], "amount": s["monto"], "currency": "ARS",
                       "kind": "transferencia" if es_banco else "otro", "estado": "pendiente",
                       "medio": "Mercado Pago", "contactKey": "b_" + s["id"],
                       "title": DESC_SALIDA.get(s["desc"], "Salida de dinero (" + (s["desc"] or "otro") + ")"),
                       "titleSource": "banco" if es_banco else "reporte",
                       **({} if es_banco else {"nota": "Movimiento de salida del reporte de Mercado Pago"})})
    if not RANGO_DESDE:  # en un pedido manual se respeta el rango elegido
        gastos = [g for g in gastos if g["date"][:10] >= DESDE]
    ahora = datetime.now(timezone.utc).isoformat()
    for g in gastos:
        g["importado"] = ahora  # cuándo se trajo de Mercado Pago (se muestra en la página)
    gastos.sort(key=lambda g: g["date"], reverse=True)
    os.makedirs("data", exist_ok=True)
    with open("data/movimientos.json", "w", encoding="utf-8") as f:
        json.dump({"generado": datetime.now(timezone.utc).isoformat(), "rango": rango,
                   "avisos": avisos, "gastos": gastos}, f, ensure_ascii=False, indent=1)
    print(f"{len(gastos)} gastos", rango, "avisos:", avisos)

if __name__ == "__main__":
    main()
