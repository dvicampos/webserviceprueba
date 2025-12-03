import os
from typing import Optional, Tuple

from flask import Flask, request, jsonify
from dotenv import load_dotenv
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
import phonenumbers
from phonenumbers import NumberParseException

load_dotenv()

app = Flask(__name__)

# =========================
#   TWILIO CONFIG
# =========================
ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

if not ACCOUNT_SID or not AUTH_TOKEN:
    raise RuntimeError("Faltan TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN en .env")

client = Client(ACCOUNT_SID, AUTH_TOKEN)

DEFAULT_REGION_ENV = (os.getenv("DEFAULT_REGION") or "").strip()
DEFAULT_REGION = DEFAULT_REGION_ENV if DEFAULT_REGION_ENV else "MX"

FROM_WHATSAPP = os.getenv("TWILIO_WHATSAPP_FROM")  # ej. whatsapp:+14155238886
MSG_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID")  # opcional
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").strip()  # https://tu-dominio.tld

STATE = {
    "sid_to_number": {},   # sid -> e164
    "delivery": {},        # e164 -> {status, sid, ...}
    "last_summary": {},
}

BACKEND_VERSION = "5.0.0"

# =========================
#   UTILIDADES
# =========================
def valid_public_base() -> str:
    """
    Devuelve PUBLIC_BASE_URL si es https y no es localhost; si no, ''.
    Evita error 21609 de Twilio (callback no https).
    """
    base = PUBLIC_BASE_URL
    if base.lower().startswith("https://") and "localhost" not in base and "127.0.0.1" not in base:
        return base.rstrip("/")
    return ""


def normalize_to_e164(raw_number: str, region: str = None) -> str:
    """
    Convierte un número a E.164; lanza ValueError si no es posible.
    Forzamos región MX si no viene una región válida.
    """
    region = region or DEFAULT_REGION

    try:
        pn = phonenumbers.parse(str(raw_number), region)
        if not phonenumbers.is_possible_number(pn) or not phonenumbers.is_valid_number(pn):
            raise ValueError(f"Invalid number for region {region}")
        return phonenumbers.format_number(pn, phonenumbers.PhoneNumberFormat.E164)
    except NumberParseException as e:
        raise ValueError(str(e))


def with_whatsapp_prefix(e164: str) -> str:
    return f"whatsapp:{e164}"


def send_one_whatsapp_template(
    to_e164: str,
    content_sid: str,
    content_variables: Optional[dict],
    status_callback_url: Optional[str],
) -> str:
    """
    Envía WhatsApp usando PLANTILLA (Content API).
    """
    import json

    kwargs = dict(
        to=with_whatsapp_prefix(to_e164),
        content_sid=content_sid,
    )
    if content_variables:
        kwargs["content_variables"] = json.dumps(content_variables)
    if status_callback_url:
        kwargs["status_callback"] = status_callback_url

    if MSG_SERVICE_SID:
        kwargs["messaging_service_sid"] = MSG_SERVICE_SID
    else:
        if not FROM_WHATSAPP:
            raise RuntimeError("Configura TWILIO_WHATSAPP_FROM=whatsapp:+1... en .env")
        kwargs["from_"] = FROM_WHATSAPP

    msg = client.messages.create(**kwargs)
    return msg.sid


# =========================
#   ENDPOINTS — DEBUG LOOKUP
# (solo para ver si Twilio conoce el número)
# =========================
def lookup_line_type(e164: str) -> Tuple[bool, Optional[str]]:
    try:
        resp = client.lookups.v2.phone_numbers(e164).fetch(fields=["line_type_intelligence"])
        line_type = None
        if resp and resp.line_type_intelligence and "type" in resp.line_type_intelligence:
            line_type = resp.line_type_intelligence["type"]
        return True, line_type
    except TwilioRestException:
        return False, None


@app.route("/debug-lookup", methods=["POST"])
def debug_lookup():
    """
    Body:
    { "telefonos": ["6568954038", "6561234657", ...] }
    Devuelve normalización + Lookup + line_type.
    """
    data = request.get_json(force=True, silent=True) or {}
    nums = data.get("telefonos") or []
    out = []

    for raw in nums:
        item = {"input": raw}
        try:
            e164 = normalize_to_e164(str(raw))
            item["e164"] = e164
            is_valid, line_type = lookup_line_type(e164)
            item["lookup_valid"] = is_valid
            item["line_type"] = line_type
        except Exception as ex:
            item["error"] = str(ex)
        out.append(item)

    return jsonify(out), 200


# =========================
#   ENDPOINT — PLANTILLA SIMPLE (mismas vars para todos)
# =========================
@app.route("/send-template", methods=["POST"])
def send_template():
    """
    Envía UNA plantilla a muchos números (mismas variables para todos).
    Body:
    {
      "content_sid": "HX...",
      "variables": { "1": "Nombre", "2": "Folio" },
      "telefonos": ["656...", "..."]
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    content_sid = (data.get("content_sid") or "").strip()
    variables = data.get("variables") or {}
    nums = data.get("telefonos") or []

    if not content_sid or not isinstance(nums, list) or not nums:
        return jsonify(error="Proporciona 'content_sid' y lista 'telefonos'"), 400

    invalid_by_norm = []
    queued = []
    failed_on_send = []

    base = valid_public_base()
    status_callback_url = f"{base}/twilio/status" if base else None

    for raw in nums:
        raw_str = str(raw).strip()
        if not raw_str:
            continue

        try:
            e164 = normalize_to_e164(raw_str)
        except ValueError as e:
            invalid_by_norm.append(f"{raw_str} ({e})")
            continue

        try:
            sid = send_one_whatsapp_template(e164, content_sid, variables, status_callback_url)
            STATE["sid_to_number"][sid] = e164
            STATE["delivery"][e164] = {
                "status": "queued",
                "sid": sid,
                "channel": "whatsapp",
                "template": content_sid,
                "vars": variables,
            }
            queued.append(raw_str)
        except Exception as ex:
            err_str = str(ex)
            STATE["delivery"][e164] = {
                "status": "failed_on_send",
                "reason": err_str,
                "channel": "whatsapp",
                "template": content_sid,
                "vars": variables,
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
            "debug": "SEND-TEMPLATE simple",
            "received": data,
            "invalid_by_norm": invalid_by_norm,
            "queued": queued,
            "failed_on_send": failed_on_send,
            "note": "Uso de plantilla con mismas variables para todos.",
        }
    ), 200


# =========================
#   ENDPOINT — PLANTILLA PERSONALIZADA POR LOTE
# =========================
@app.route("/send-template-bulk-personalizado", methods=["POST"])
def send_template_bulk_personalizado():
    """
    Envía plantillas por lote **SIN Twilio Lookup** (solo normalización E.164).
    Body:
    {
      "content_sid": "HX06db9b89b5a9653ad7d204bc5130930b",
      "lotes": [
        {
          "telefono": "6142249654",
          "vars": {
            "1": "Nombre",
            "2": "Dependencia"
          }
        },
        ...
      ]
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    content_sid = (data.get("content_sid") or "").strip()
    lotes = data.get("lotes") or []

    if not content_sid:
        return jsonify(error="Falta 'content_sid'"), 400
    if not isinstance(lotes, list) or not lotes:
        return jsonify(error="Falta lista 'lotes'"), 400

    invalid_by_norm = []
    queued = []
    failed_on_send = []

    base = valid_public_base()
    status_callback_url = f"{base}/twilio/status" if base else None

    for lote in lotes:
        raw_str = str(lote.get("telefono", "")).strip()
        vars_lote = lote.get("vars") or {}

        if not raw_str:
            continue

        # 1) Normalizar
        try:
            e164 = normalize_to_e164(raw_str)
        except ValueError as e:
            invalid_by_norm.append(f"{raw_str} ({e})")
            continue

        # 2) Enviar plantilla DIRECTO a Twilio
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
            failed_on_send.append({
                "numero": raw_str,
                "e164": e164,
                "reason": err_str,
            })

    return jsonify({
        "debug": "SEND-TEMPLATE-BULK-PERSONALIZADO v5.0",
        "received": data,
        "invalid_by_norm": invalid_by_norm,
        "queued": queued,
        "failed_on_send": failed_on_send,
        "note": "Bulk personalizado SIN Twilio Lookup (solo normalización a E.164).",
    }), 200


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
    return jsonify(ok=True, app="whatsapp-bulk", version=BACKEND_VERSION), 200


@app.route("/twilio/incoming", methods=["POST"])
def twilio_incoming():
    print("INCOMING:", dict(request.form))
    return ("", 200)


@app.route("/twilio/incoming-fallback", methods=["POST"])
def twilio_incoming_fallback():
    return ("", 200)


@app.route("/tester", methods=["GET"])
def tester():
    html = f"""
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <title>Tester — WhatsApp Bulk</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {{ --ink:#222; --muted:#666; --accent:#2563eb; --bg:#f6f7fb; }}
    body{{ font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Arial; color:var(--ink); background:var(--bg); margin:0; }}
    .wrap{{ max-width:980px; margin:32px auto; background:#fff; border-radius:12px; padding:20px; box-shadow:0 10px 25px rgba(0,0,0,.06); }}
    h1{{ font-size:20px; margin:0 0 12px; }}
    .row{{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:10px; }}
    label{{ font-weight:600; font-size:12px; color:var(--muted); display:block; margin-bottom:6px; }}
    select,input,textarea{{ width:100%; padding:10px; border:1px solid #e5e7eb; border-radius:8px; font:inherit; }}
    textarea{{ min-height:180px; font-family:ui-monospace,Menlo,Consolas,monospace; }}
    .btns{{ display:flex; gap:10px; flex-wrap:wrap; margin-top:10px; }}
    button{{ padding:10px 14px; border-radius:8px; border:0; cursor:pointer; font-weight:600; }}
    .primary{{ background:var(--accent); color:#fff; }}
    .ghost{{ background:#eef2ff; color:#1e3a8a; }}
    pre{{ background:#0b1020; color:#e6edf3; padding:14px; border-radius:8px; overflow:auto; max-height:55vh; }}
    small{{ color:var(--muted); }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Tester — WhatsApp Bulk</h1>
    <p><small>Pega tu JSON, elige método y endpoint. Esto hace <code>fetch</code> directo a tu backend.</small></p>
    <p><small>Version backend: {BACKEND_VERSION} — endpoints /send-template, /send-template-bulk-personalizado, /debug-lookup, /report.</small></p>

    <div class="row">
      <div style="flex:1 1 140px;">
        <label>Metodo</label>
        <select id="method">
          <option>POST</option>
          <option>GET</option>
        </select>
      </div>
      <div style="flex:1 1 240px;">
        <label>Endpoint rapido</label>
        <select id="quick">
          <option value="/send-template-bulk-personalizado">/send-template-bulk-personalizado</option>
          <option value="/send-template">/send-template</option>
          <option value="/debug-lookup">/debug-lookup</option>
          <option value="/report">/report</option>
          <option value="__custom">— Personalizado —</option>
        </select>
      </div>
      <div style="flex:2 1 340px;">
        <label>Endpoint (URL relativa)</label>
        <input id="endpoint" value="/send-template-bulk-personalizado" />
      </div>
    </div>

    <label>Body JSON (solo se envia si el metodo es POST)</label>
    <textarea id="body"></textarea>

    <div class="btns">
      <button class="ghost" id="loadTemplateBulk">Ejemplo: bulk personalizado</button>
      <button class="ghost" id="loadTemplate">Ejemplo: plantilla simple</button>
      <button class="ghost" id="loadLookup">Ejemplo: debug-lookup</button>
      <button class="primary" id="sendBtn">Enviar</button>
    </div>

    <h3>Respuesta</h3>
    <pre id="out">{{}}</pre>
  </div>

  <script>
    const methodEl = document.getElementById('method');
    const quickEl  = document.getElementById('quick');
    const endEl    = document.getElementById('endpoint');
    const bodyEl   = document.getElementById('body');
    const outEl    = document.getElementById('out');
    const sendBtn  = document.getElementById('sendBtn');
    const loadTemplateBulk = document.getElementById('loadTemplateBulk');
    const loadTemplate = document.getElementById('loadTemplate');
    const loadLookup = document.getElementById('loadLookup');

    quickEl.addEventListener('change', () => {{
      const v = quickEl.value;
      if (v === '__custom') return;
      endEl.value = v;
    }});

    loadTemplateBulk.addEventListener('click', () => {{
      methodEl.value = 'POST';
      endEl.value = '/send-template-bulk-personalizado';
      bodyEl.value = JSON.stringify({{
        "content_sid": "HX06db9b89b5a9653ad7d204bc5130930b",
        "lotes": [
          {{
            "telefono": "6142249654",
            "vars": {{"1":"Jaime Prueba","2":"DIF"}}
          }},
          {{
            "telefono": "2463095291",
            "vars": {{"1":"David Campos","2":"Tesoreria Municipal"}}
          }},
          {{
            "telefono": "6563023022",
            "vars": {{"1":"Raul Monares","2":"Desarrollo Urbano"}}
          }}
        ]
      }}, null, 2);
    }});

    loadTemplate.addEventListener('click', () => {{
      methodEl.value = 'POST';
      endEl.value = '/send-template';
      bodyEl.value = JSON.stringify({{
        "content_sid": "HX06db9b89b5a9653ad7d204bc5130930b",
        "variables": {{"1":"Nombre prueba","2":"Dependencia"}},
        "telefonos": ["6142249654"]
      }}, null, 2);
    }});

    loadLookup.addEventListener('click', () => {{
      methodEl.value = 'POST';
      endEl.value = '/debug-lookup';
      bodyEl.value = JSON.stringify({{
        "telefonos": ["6142249654","2463095291","6563023022"]
      }}, null, 2);
    }});

    sendBtn.addEventListener('click', async () => {{
      const method = methodEl.value.trim();
      const endpoint = endEl.value.trim() || '/send-template-bulk-personalizado';
      const init = {{ method, headers: {{}} }};

      if (method === 'POST') {{
        let payload = {{}};
        try {{
          payload = bodyEl.value ? JSON.parse(bodyEl.value) : {{}};
        }} catch (e) {{
          outEl.textContent = "JSON invalido en body: " + e.message;
          return;
        }}
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(payload);
      }}

      outEl.textContent = "Enviando...";
      try {{
        const res = await fetch(endpoint, init);
        const text = await res.text();
        try {{
          const json = JSON.parse(text);
          outEl.textContent = JSON.stringify(json, null, 2);
        }} catch (e) {{
          outEl.textContent = text;
        }}
      }} catch (err) {{
        outEl.textContent = "Error de red: " + (err && err.message ? err.message : err);
      }}
    }});

    // carga ejemplo inicial
    loadTemplateBulk.click();
  </script>
</body>
</html>
    """
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
