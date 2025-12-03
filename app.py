import os
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify
from dotenv import load_dotenv
from twilio.rest import Client

# =========================
#   CARGA .env
# =========================
load_dotenv()

app = Flask(__name__)

BACKEND_VERSION = "5.0.0"

# =========================
#   TWILIO CONFIG
# =========================
ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

if not ACCOUNT_SID or not AUTH_TOKEN:
    raise RuntimeError("Faltan TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN en .env")

client = Client(ACCOUNT_SID, AUTH_TOKEN)

FROM_WHATSAPP = os.getenv("TWILIO_WHATSAPP_FROM")  # ej. whatsapp:+5216565533923
MSG_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID")  # opcional
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").strip()

# Memoria simple (en producción: DB / Redis)
STATE = {
    "sid_to_number": {},   # sid -> e164
    "delivery": {},        # e164 -> {status, sid, ...}
    "last_summary": {},
}


# =========================
#   UTILIDADES
# =========================
def valid_public_base() -> str:
    """
    Devuelve PUBLIC_BASE_URL si es https y no es localhost; si no, ''.
    """
    base = PUBLIC_BASE_URL
    if base.lower().startswith("https://") and "localhost" not in base and "127.0.0.1" not in base:
        return base.rstrip("/")
    return ""


def normalize_to_e164_mx(raw_number: str) -> str:
    """
    Normaliza número mexicano a E.164 SIN usar phonenumbers.
    Reglas simples:
      - Si ya empieza con '+', lo dejamos tal cual.
      - Si son 10 dígitos -> +52 + número
      - Si son 12 dígitos y empieza con '52' -> + + número
    Si no cumple, lanza ValueError.
    """
    raw = str(raw_number).strip()

    # Ya viene en formato +...
    if raw.startswith("+"):
        return raw

    # Sólo dígitos
    digits = "".join(ch for ch in raw if ch.isdigit())

    if len(digits) == 10:
        # asumimos MX
        return "+52" + digits
    if len(digits) == 12 and digits.startswith("52"):
        return "+" + digits

    raise ValueError(f"No parece número MX válido: {raw_number}")


def with_whatsapp_prefix(e164: str) -> str:
    return f"whatsapp:{e164}"


def send_one_whatsapp_template(
    to_e164: str,
    content_sid: str,
    content_variables: Optional[Dict[str, Any]],
    status_callback_url: Optional[str],
) -> str:
    """
    Envía WhatsApp usando PLANTILLA (Content API).
    NO usa Twilio Lookup.
    """
    import json

    kwargs: Dict[str, Any] = {
        "to": with_whatsapp_prefix(to_e164),
        "content_sid": content_sid,
    }

    if content_variables:
        kwargs["content_variables"] = json.dumps(content_variables)

    if status_callback_url:
        kwargs["status_callback"] = status_callback_url

    if MSG_SERVICE_SID:
        kwargs["messaging_service_sid"] = MSG_SERVICE_SID
    else:
        if not FROM_WHATSAPP:
            raise RuntimeError("Configura TWILIO_WHATSAPP_FROM=whatsapp:+52xxxxxxxxxx en .env")
        kwargs["from_"] = FROM_WHATSAPP

    msg = client.messages.create(**kwargs)
    return msg.sid


# =========================
#   ENDPOINT PLANTILLA: BULK PERSONALIZADO
# =========================
@app.route("/send-template-bulk-personalizado", methods=["POST"])
def send_template_bulk_personalizado():
    """
    Envía plantillas por lote (SIN Twilio Lookup, SIN phonenumbers).
    Body JSON:
    {
      "content_sid": "HX06db9b89b5a9653ad7d204bc5130930b",
      "lotes": [
        {
          "telefono": "6142249654",
          "vars": { "1": "Nombre", "2": "Dependencia" }
        },
        ...
      ]
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    content_sid: str = (data.get("content_sid") or "").strip()
    lotes: List[Dict[str, Any]] = data.get("lotes") or []

    if not content_sid:
        return jsonify(error="Falta 'content_sid'"), 400
    if not isinstance(lotes, list) or not lotes:
        return jsonify(error="Falta lista 'lotes'"), 400

    base = valid_public_base()
    status_callback_url = f"{base}/twilio/status" if base else None

    invalid_by_norm: List[str] = []   # errores al normalizar número
    queued: List[str] = []            # enviados correctamente a Twilio
    failed_on_send: List[Dict[str, Any]] = []  # Twilio los rechazó (ej. 20003)

    for lote in lotes:
        raw_str = str(lote.get("telefono", "")).strip()
        vars_lote = lote.get("vars") or {}

        if not raw_str:
            continue

        # 1) Normalizar simple a E.164 MX
        try:
            e164 = normalize_to_e164_mx(raw_str)
        except ValueError as e:
            invalid_by_norm.append(f"{raw_str} ({e})")
            continue

        # 2) Enviar a Twilio
        try:
            sid = send_one_whatsapp_template(e164, content_sid, vars_lote, status_callback_url)
            STATE["sid_to_number"][sid] = e164
            STATE["delivery"][e164] = {
                "status": "queued",
                "sid": sid,
                "channel": "whatsapp",
                "template": content_sid,
                "vars": vars_lote,
            }
            queued.append(raw_str)
        except Exception as ex:
            err_str = str(ex)
            STATE["delivery"][e164] = {
                "status": "failed_on_send",
                "reason": err_str,
                "channel": "whatsapp",
                "template": content_sid,
                "vars": vars_lote,
            }
            failed_on_send.append(
                {
                    "numero": raw_str,
                    "e164": e164,
                    "reason": err_str,
                }
            )

    return jsonify(
        {
            "debug": "SEND-TEMPLATE-BULK-PERSONALIZADO v5",
            "received": data,
            "invalid_by_norm": invalid_by_norm,
            "queued": queued,
            "failed_on_send": failed_on_send,
            "note": "SIN Twilio Lookup, normalización MX simple.",
        }
    ), 200


# =========================
#   STATUS / REPORTES
# =========================
@app.route("/twilio/status", methods=["POST"])
def twilio_status():
    sid = request.form.get("MessageSid")
    status = request.form.get("MessageStatus")
    error_code = request.form.get("ErrorCode")
    error_msg = request.form.get("ErrorMessage")

    e164 = STATE["sid_to_number"].get(sid)
    if e164:
        prev = STATE["delivery"].get(e164, {})
        prev.update({"status": status, "sid": sid})
        if error_code or error_msg:
            prev["error_code"] = error_code
            prev["error_message"] = error_msg
        STATE["delivery"][e164] = prev

    return ("", 200)


@app.route("/report", methods=["GET"])
def report():
    delivered = []
    failed = []
    pending = []
    for e164, info in STATE["delivery"].items():
        st = (info or {}).get("status", "")
        if st in ("delivered",):
            delivered.append(e164)
        elif st in ("failed", "undelivered", "failed_on_send"):
            failed.append(e164)
        else:
            pending.append(e164)

    return jsonify(
        {
            "delivered": delivered,
            "failed_or_undelivered": failed,
            "pending": pending,
            "raw": STATE["delivery"],
            "last_summary": STATE.get("last_summary", {}),
        }
    ), 200


@app.route("/status-detail/<sid>", methods=["GET"])
def status_detail(sid):
    try:
        msg = client.messages(sid).fetch()
        return jsonify(
            {
                "sid": msg.sid,
                "status": msg.status,
                "to": msg.to,
                "from": msg.from_,
                "error_code": msg.error_code,
                "error_message": msg.error_message,
                "date_sent": str(msg.date_sent) if msg.date_sent else None,
            }
        ), 200
    except Exception as e:
        return jsonify(error=str(e)), 400


# =========================
#   OTROS
# =========================
@app.route("/health", methods=["GET"])
def health():
    # Para confirmar que corre ESTE código
    return jsonify(
        ok=True,
        app="whatsapp-bulk",
        version=BACKEND_VERSION,
        account_sid_last4=ACCOUNT_SID[-4:],
    ), 200


@app.route("/tester", methods=["GET"])
def tester():
    # HTML simple (sin f-string, sin formato) para evitar problemas de llaves.
    html = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <title>Tester — WhatsApp Bulk</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root { --ink:#222; --muted:#666; --accent:#2563eb; --bg:#f6f7fb; }
    body{ font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Arial; color:var(--ink); background:var(--bg); margin:0; }
    .wrap{ max-width:980px; margin:32px auto; background:#fff; border-radius:12px; padding:20px;
           box-shadow:0 10px 25px rgba(0,0,0,.06); }
    h1{ font-size:20px; margin:0 0 12px; }
    .row{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:10px; }
    label{ font-weight:600; font-size:12px; color:var(--muted); display:block; margin-bottom:6px; }
    select,input,textarea{ width:100%; padding:10px; border:1px solid #e5e7eb; border-radius:8px; font:inherit; }
    textarea{ min-height:180px; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
    .btns{ display:flex; gap:10px; flex-wrap:wrap; }
    button{ padding:10px 14px; border-radius:8px; border:0; cursor:pointer; font-weight:600; }
    .primary{ background:var(--accent); color:#fff; }
    .ghost{ background:#eef2ff; color:#1e3a8a; }
    pre{ background:#0b1020; color:#e6edf3; padding:14px; border-radius:8px; overflow:auto; max-height:55vh; }
    small{ color:var(--muted); }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Tester — WhatsApp Bulk</h1>
    <p><small>Pega tu JSON, elige método y endpoint. Esto hace <code>fetch</code> directo a tu backend.</small></p>

    <div class="row">
      <div style="flex:1 1 140px;">
        <label>Método</label>
        <select id="method">
          <option>POST</option>
          <option>GET</option>
        </select>
      </div>
      <div style="flex:1 1 240px;">
        <label>Endpoint rápido</label>
        <select id="quick">
          <option value="/send-template-bulk-personalizado">/send-template-bulk-personalizado</option>
          <option value="/report">/report</option>
          <option value="/health">/health</option>
          <option value="__custom">— Personalizado —</option>
        </select>
      </div>
      <div style="flex:2 1 340px;">
        <label>Endpoint (URL relativa)</label>
        <input id="endpoint" value="/send-template-bulk-personalizado" />
      </div>
    </div>

    <label>Body JSON (solo se envía si el método es POST)</label>
    <textarea id="body"></textarea>

    <div class="btns" style="margin-top:10px;">
      <button class="ghost" id="loadTemplateBulk">Ejemplo: /send-template-bulk-personalizado</button>
      <button class="primary" id="sendBtn">Enviar</button>
    </div>

    <p><small>Regla: token exige 2 variables ("1","2"). content_di exige 4 ("1"–"4").</small></p>

    <h3>Respuesta</h3>
    <pre id="out">{}</pre>
  </div>

  <script>
    const methodEl = document.getElementById('method');
    const quickEl  = document.getElementById('quick');
    const endEl    = document.getElementById('endpoint');
    const bodyEl   = document.getElementById('body');
    const outEl    = document.getElementById('out');
    const sendBtn  = document.getElementById('sendBtn');
    const loadTemplateBulk = document.getElementById('loadTemplateBulk');

    quickEl.addEventListener('change', () => {
      const v = quickEl.value;
      if (v === '__custom') return;
      endEl.value = v;
    });

    loadTemplateBulk.addEventListener('click', () => {
      methodEl.value = 'POST';
      endEl.value = '/send-template-bulk-personalizado';
      bodyEl.value = JSON.stringify({
        "content_sid": "HX06db9b89b5a9653ad7d204bc5130930b",
        "lotes": [
          {
            "telefono": "6142249654",
            "vars": { "1": "Jaime Prueba", "2": "DIF" }
          },
          {
            "telefono": "2463095291",
            "vars": { "1": "David Campos", "2": "Tesoreria Municipal" }
          },
          {
            "telefono": "6563023022",
            "vars": { "1": "Raul Monares", "2": "Desarrollo Urbano" }
          }
        ]
      }, null, 2);
    });

    sendBtn.addEventListener('click', async () => {
      const method = methodEl.value.trim();
      const endpoint = endEl.value.trim() || '/send-template-bulk-personalizado';
      let init = { method, headers: {} };

      if (method === 'POST') {
        let payload = {};
        try {
          payload = bodyEl.value ? JSON.parse(bodyEl.value) : {};
        } catch (e) {
          outEl.textContent = "❌ JSON inválido en el body: " + e.message;
          return;
        }
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(payload);
      }

      outEl.textContent = "Enviando...";
      try {
        const res = await fetch(endpoint, init);
        const text = await res.text();
        try {
          const json = JSON.parse(text);
          outEl.textContent = JSON.stringify(json, null, 2);
        } catch {
          outEl.textContent = text;
        }
      } catch (err) {
        outEl.textContent = "❌ Error de red: " + (err?.message || err);
      }
    });

    
    loadTemplateBulk.click();
  </script>
</body>
</html>
    """
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
