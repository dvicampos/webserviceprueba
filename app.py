import os
from typing import Tuple, Optional
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
import phonenumbers
from phonenumbers import NumberParseException

load_dotenv()

app = Flask(__name__)

# -------------------------------
# Twilio client & Config (SOLO WHATSAPP)
# -------------------------------
ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
if not ACCOUNT_SID or not AUTH_TOKEN:
    raise RuntimeError("Faltan TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN en .env")

client = Client(ACCOUNT_SID, AUTH_TOKEN)

DEFAULT_REGION = os.getenv("DEFAULT_REGION", "MX").strip()
FROM_WHATSAPP  = os.getenv("TWILIO_WHATSAPP_FROM")  # p.ej. whatsapp:+14155238886 (sandbox) o tu WA Business
MSG_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID")  # opcional si usas Messaging Service
STRICT_WHATSAPP = (os.getenv("STRICT_WHATSAPP", "true").lower() == "true")  # WhatsApp solo "mobile"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()  # https://tu-dominio.tld

# Memoria simple para demo (usa DB/Redis en producción)
STATE = {
    "sid_to_number": {},        # sid -> e164
    "delivery": {},             # e164 -> {status, sid, ...}
    "last_summary": {}
}

# -------------------------------
# Utilidades
# -------------------------------
def valid_public_base() -> str:
    """
    Devuelve PUBLIC_BASE_URL si es https y no es localhost; en caso contrario, ''.
    Evita error 21609 de Twilio cuando se usa http/localhost.
    """
    base = PUBLIC_BASE_URL
    if base.lower().startswith("https://") and "localhost" not in base and "127.0.0.1" not in base:
        return base.rstrip("/")
    # Si estás local y sin túnel https, devolvemos vacío para omitir callback.
    return ""

def normalize_to_e164(raw_number: str, region: str = DEFAULT_REGION) -> str:
    """
    Convierte un número a E.164; lanza ValueError si no es posible.
    """
    try:
        pn = phonenumbers.parse(str(raw_number), region)
        if not phonenumbers.is_possible_number(pn) or not phonenumbers.is_valid_number(pn):
            raise ValueError("Invalid number")
        return phonenumbers.format_number(pn, phonenumbers.PhoneNumberFormat.E164)
    except NumberParseException as e:
        raise ValueError(str(e))

def with_whatsapp_prefix(e164: str) -> str:
    return f"whatsapp:{e164}"

def lookup_is_valid(e164: str) -> Tuple[bool, Optional[str]]:
    """
    Twilio Lookup v2 para validar y obtener tipo de línea.
    Devuelve (is_valid, line_type) con line_type en {'mobile','landline','fixed','voip',...} o None.
    """
    try:
        resp = client.lookups.v2.phone_numbers(e164).fetch(fields=['line_type_intelligence'])
        line_type = None
        if resp and resp.line_type_intelligence and "type" in resp.line_type_intelligence:
            # Twilio suele devolver 'mobile', 'landline' (o 'fixed'), 'voip'
            line_type = resp.line_type_intelligence["type"]
        return True, line_type
    except TwilioRestException:
        # 404/400: número no reconocido o inválido
        return False, None

def whatsapp_policy_allows(line_type: Optional[str]) -> Tuple[bool, str]:
    """
    WhatsApp: normalmente SOLO 'mobile'. Si STRICT_WHATSAPP=True, bloquea todo lo que no sea mobile.
    """
    lt = (line_type or "").lower()
    # Twilio a veces usa 'landline' en lugar de 'fixed'; tratamos ambos como no-móvil.
    if STRICT_WHATSAPP:
        if lt != "mobile":
            return False, f"WhatsApp requiere móvil (line_type='{lt or 'unknown'}')"
    else:
        # Si no es estricto y no se conoce, podrías permitir y que el DLR decida.
        if lt and lt != "mobile":
            return False, f"Tipo no móvil para WhatsApp (line_type='{lt}')"
    return True, ""

def send_one_whatsapp(to_e164: str, body: str, status_callback_url: Optional[str]) -> str:
    """
    Envía un WhatsApp y regresa el SID.
    Usa Messaging Service SID si está configurado; si no, usa FROM directo (obligatorio).
    """
    kwargs = dict(
        to=with_whatsapp_prefix(to_e164),
        body=body
    )
    if status_callback_url:
        kwargs["status_callback"] = status_callback_url

    if MSG_SERVICE_SID:
        kwargs["messaging_service_sid"] = MSG_SERVICE_SID
    else:
        if not FROM_WHATSAPP:
            raise RuntimeError("Configura TWILIO_WHATSAPP_FROM=whatsapp:+1... en .env (sandbox o WA Business)")
        kwargs["from_"] = FROM_WHATSAPP

    msg = client.messages.create(**kwargs)
    return msg.sid

# -------------------------------
# Endpoints
# -------------------------------
@app.route("/send-bulk", methods=["POST"])
def send_bulk():
    """
    Body JSON:
    {
      "mensaje": "texto",
      "telefonos": ["656123...", "..."]
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    body = (data.get("mensaje") or "").strip()
    nums = data.get("telefonos") or []

    if not body or not isinstance(nums, list) or not nums:
        return jsonify(error="Proporciona 'mensaje' y lista 'telefonos'"), 400

    invalid_by_lookup = []
    queued = []
    skipped_not_mobile = []   # compat
    skipped_detail = []

    base = valid_public_base()
    status_callback_url = f"{base}/twilio/status" if base else None

    for raw in nums:
        raw_str = str(raw).strip()

        # 1) Normaliza
        try:
            e164 = normalize_to_e164(raw_str)
        except ValueError:
            invalid_by_lookup.append(raw_str)
            continue

        # 2) Lookup
        is_valid, line_type = lookup_is_valid(e164)
        if not is_valid:
            invalid_by_lookup.append(raw_str)
            continue

        # 3) Política WhatsApp (solo mobile)
        allowed, reason = whatsapp_policy_allows(line_type)
        if not allowed:
            skipped_not_mobile.append(raw_str)
            skipped_detail.append({
                "numero": raw_str,
                "line_type": line_type,
                "canal": "whatsapp",
                "reason": reason
            })
            continue

        # 4) Enviar
        try:
            sid = send_one_whatsapp(e164, body, status_callback_url)
            STATE["sid_to_number"][sid] = e164
            STATE["delivery"][e164] = {"status": "queued", "sid": sid, "channel": "whatsapp"}
            queued.append(raw_str)
        except Exception as ex:
            STATE["delivery"][e164] = {
                "status": "failed_on_send",
                "reason": str(ex),
                "channel": "whatsapp"
            }

    summary = {
        "invalid_by_lookup": invalid_by_lookup,
        "queued": queued,
        "skipped_not_mobile": skipped_not_mobile,
        "skipped_detail": skipped_detail,
        "note": (
            "Los 'invalid_by_lookup' no pasaron Twilio Lookup. "
            "Los 'skipped' no son móviles para WhatsApp (ver 'skipped_detail'). "
            "El resultado final de entrega para 'queued' llegará vía /twilio/status "
            "y se puede consultar en /report. "
            "Si estás en local sin https, omitimos status_callback para evitar el error 21609."
        )
    }
    STATE["last_summary"] = summary
    return jsonify(summary), 200

@app.route("/debug-lookup", methods=["POST"])
def debug_lookup():
    """
    Body:
    { "telefonos": ["6568954038", "6561234657", ...] }
    Devuelve normalización + Lookup + line_type para depuración.
    """
    data = request.get_json(force=True, silent=True) or {}
    nums = data.get("telefonos") or []
    out = []

    for raw in nums:
        item = {"input": raw}
        try:
            e164 = normalize_to_e164(str(raw))
            item["e164"] = e164
            is_valid, line_type = lookup_is_valid(e164)
            item["lookup_valid"] = is_valid
            item["line_type"] = line_type
        except Exception as ex:
            item["error"] = str(ex)
        out.append(item)

    return jsonify(out), 200

@app.route("/twilio/status", methods=["POST"])
def twilio_status():
    """
    Webhook de estado (Twilio):
    Llega MessageSid, MessageStatus y (si aplica) ErrorCode / ErrorMessage.
    """
    sid = request.form.get("MessageSid")
    status = request.form.get("MessageStatus")
    error_code = request.form.get("ErrorCode")       # NUEVO
    error_msg  = request.form.get("ErrorMessage")    # NUEVO

    e164 = STATE["sid_to_number"].get(sid)
    if e164:
        prev = STATE["delivery"].get(e164, {})
        prev.update({
            "status": status,
            "sid": sid
        })
        # guarda error si existe
        if error_code or error_msg:
            prev["error_code"] = error_code
            prev["error_message"] = error_msg
        STATE["delivery"][e164] = prev

    return ("", 200)

@app.route("/report", methods=["GET"])
def report():
    """
    Resumen de entrega por estado actual.
    """
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

    return jsonify({
        "delivered": delivered,
        "failed_or_undelivered": failed,
        "pending": pending,
        "raw": STATE["delivery"],
        "last_summary": STATE.get("last_summary", {})
    }), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify(ok=True, app="whatsapp-bulk", version="1.0.0"), 200

@app.route("/twilio/incoming", methods=["POST"])
def twilio_incoming():
    # Campos típicos: From, WaId, Body, To, ProfileName, etc.
    # Aquí puedes guardar en DB, responder, etc.
    print("INCOMING:", dict(request.form))  # o log a tu sistema
    return ("", 200)

@app.route("/twilio/incoming-fallback", methods=["POST"])
def twilio_incoming_fallback():
    # Twilio cae aquí si /twilio/incoming falla
    return ("", 200)

@app.route("/status-detail/<sid>", methods=["GET"])
def status_detail(sid):
    try:
        msg = client.messages(sid).fetch()
        return jsonify({
            "sid": msg.sid,
            "status": msg.status,
            "to": msg.to,
            "from": msg.from_,
            "error_code": msg.error_code,
            "error_message": msg.error_message,
            "date_sent": str(msg.date_sent) if msg.date_sent else None
        }), 200
    except Exception as e:
        return jsonify(error=str(e)), 400
    
def send_one_whatsapp_template(to_e164: str, content_sid: str, content_variables: Optional[dict], status_callback_url: Optional[str]) -> str:
    """
    Envía WhatsApp usando una PLANTILLA (Content API de Twilio).
    - content_sid: SID del template en Twilio (CHxxxxxxxxxxxx)
    - content_variables: dict con variables de la plantilla, e.g. {"1":"Davani","2":"#1234"}
    """
    kwargs = dict(
        to=with_whatsapp_prefix(to_e164),
        content_sid=content_sid
    )
    if content_variables:
        # Twilio espera string JSON
        import json
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

@app.route("/send-template", methods=["POST"])
def send_template():
    """
    MODO DEBUG:
    - NO usa Twilio Lookup
    - NO valida tipo de línea (mobile/fijo)
    - Solo normaliza con phonenumbers y envía la plantilla
    """
    data = request.get_json(force=True, silent=True) or {}
    content_sid = (data.get("content_sid") or "").strip()
    variables_globales = data.get("variables") or {}
    nums = data.get("telefonos") or []
    lotes = data.get("lotes") or []

    if not content_sid:
        return jsonify(error="Proporciona 'content_sid' (DEBUG)"), 400

    usar_lotes = bool(lotes)

    if not usar_lotes and (not isinstance(nums, list) or not nums):
        return jsonify(error="Proporciona lista 'telefonos' o 'lotes' (DEBUG)"), 400

    invalid_by_lookup = []
    skipped_not_mobile = []
    skipped_detail = []
    queued = []

    base = valid_public_base()
    status_callback_url = f"{base}/twilio/status" if base else None

    if usar_lotes:
        for lote in lotes:
            raw_str = str(lote.get("telefono", "")).strip()
            vars_lote = lote.get("vars") or variables_globales or {}

            if not raw_str:
                continue

            try:
                e164 = normalize_to_e164(raw_str)
            except ValueError:
                invalid_by_lookup.append(raw_str)
                continue

            try:
                sid = send_one_whatsapp_template(e164, content_sid, vars_lote, status_callback_url)
                STATE["sid_to_number"][sid] = e164
                STATE["delivery"][e164] = {
                    "status": "queued",
                    "sid": sid,
                    "channel": "whatsapp",
                    "template": content_sid,
                    "vars": vars_lote
                }
                queued.append(raw_str)
            except Exception as ex:
                STATE["delivery"][e164] = {
                    "status": "failed_on_send",
                    "reason": str(ex),
                    "channel": "whatsapp",
                    "template": content_sid,
                    "vars": vars_lote
                }
    else:
        for raw in nums:
            raw_str = str(raw).strip()
            if not raw_str:
                continue

            try:
                e164 = normalize_to_e164(raw_str)
            except ValueError:
                invalid_by_lookup.append(raw_str)
                continue

            try:
                sid = send_one_whatsapp_template(e164, content_sid, variables_globales, status_callback_url)
                STATE["sid_to_number"][sid] = e164
                STATE["delivery"][e164] = {
                    "status": "queued",
                    "sid": sid,
                    "channel": "whatsapp",
                    "template": content_sid,
                    "vars": variables_globales
                }
                queued.append(raw_str)
            except Exception as ex:
                STATE["delivery"][e164] = {
                    "status": "failed_on_send",
                    "reason": str(ex),
                    "channel": "whatsapp",
                    "template": content_sid,
                    "vars": variables_globales
                }

    return jsonify({
        "debug": "SEND-TEMPLATE V3 - DVICA",
        "received": data,
        "invalid_by_lookup": invalid_by_lookup,
        "queued": queued,
        "skipped_not_mobile": skipped_not_mobile,
        "skipped_detail": skipped_detail,
        "note": "DEBUG send-template SIN Lookup v3"
    }), 200

@app.route("/tester", methods=["GET"])
def tester():
    # HTML simple embebido (sin templates) para probar tus endpoints
    html = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <title>Tester — WhatsApp Bulk</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root { --ink:#222; --muted:#666; --accent:#2563eb; --bg:#f6f7fb; }
    body{ font: 14px/1.5 system-ui, -apple-system, Segoe UI, Roboto, Arial; color:var(--ink); background:var(--bg); margin:0; }
    .wrap{ max-width: 980px; margin: 32px auto; background:#fff; border-radius:12px; padding:20px; box-shadow: 0 10px 25px rgba(0,0,0,.06); }
    h1{ font-size: 20px; margin: 0 0 12px; }
    .row{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:10px; }
    label{ font-weight:600; font-size:12px; color:var(--muted); display:block; margin-bottom:6px; }
    select, input, textarea{ width:100%; padding:10px; border:1px solid #e5e7eb; border-radius:8px; font: inherit; }
    textarea{ min-height: 180px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .btns{ display:flex; gap:10px; flex-wrap:wrap; }
    button{ padding:10px 14px; border-radius:8px; border:0; cursor:pointer; font-weight:600; }
    .primary{ background:var(--accent); color:#fff; }
    .ghost{ background:#eef2ff; color:#1e3a8a; }
    pre{ background:#0b1020; color:#e6edf3; padding:14px; border-radius:8px; overflow:auto; max-height: 55vh; }
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
          <option value="/send-bulk">/send-bulk</option>
          <option value="/debug-lookup">/debug-lookup</option>
          <option value="/report">/report</option>
          <option value="__custom">— Personalizado —</option>
        </select>
      </div>
      <div style="flex:2 1 340px;">
        <label>Endpoint (URL relativa)</label>
        <input id="endpoint" value="/send-bulk" placeholder="/send-bulk" />
      </div>
    </div>

    <label>Body JSON (solo se envía si el método es POST)</label>
    <textarea id="body"></textarea>

    <div class="btns" style="margin-top:10px;">
      <button class="ghost" id="loadSend">Ejemplo: /send-bulk</button>
      <button class="ghost" id="loadLookup">Ejemplo: /debug-lookup</button>
      <button class="primary" id="sendBtn">Enviar</button>
    </div>

    <p><small>Tip: si estás en local y sin <b>PUBLIC_BASE_URL</b> https, el envío omitirá <code>status_callback</code> para evitar error 21609.</small></p>

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
    const loadSend = document.getElementById('loadSend');
    const loadLookup = document.getElementById('loadLookup');

    // Cambia endpoint al elegir "rápido"
    quickEl.addEventListener('change', () => {
      const v = quickEl.value;
      if (v === '__custom') return;
      endEl.value = v;
    });

    // Plantilla ejemplo: /send-bulk
    loadSend.addEventListener('click', () => {
      methodEl.value = 'POST';
      endEl.value = '/send-bulk';
      bodyEl.value = JSON.stringify({
        "mensaje": "Su trámite ha sido firmado; acuda a la dependencia con su documentación.",
        "telefonos": ["6561234657","6568954038","6567689214","6566094353","6563287159"]
      }, null, 2);
    });

    // Plantilla ejemplo: /debug-lookup
    loadLookup.addEventListener('click', () => {
      methodEl.value = 'POST';
      endEl.value = '/debug-lookup';
      bodyEl.value = JSON.stringify({
        "telefonos": ["6568954038","6561234657","6567689214"]
      }, null, 2);
    });

    // Enviar
    sendBtn.addEventListener('click', async () => {
      const method = methodEl.value.trim();
      const endpoint = endEl.value.trim() || '/send-bulk';
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
        // intenta parsear JSON; si no, muestra texto
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

    // Carga por defecto
    loadSend.click();
  </script>
</body>
</html>
    """
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/send-template-bulk-personalizado", methods=["POST"])
def send_template_bulk_personalizado():
    """
    DEBUG DO:
    Envía plantillas por lote SIN Twilio Lookup.
    Body:
    {
      "content_sid": "HX84a8...",
      "lotes": [
        {
          "telefono": "2463095291",
          "vars": {
            "1": "Jaime Prueba",
            "2": "Licencia...",
            "3": "DGDU/LC/0069/2025",
            "4": "Verificación Rechazada"
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

    invalid_by_lookup = []   # aquí solo meteremos errores de normalización
    queued = []
    skipped_not_mobile = []
    skipped_detail = []

    base = valid_public_base()
    status_callback_url = f"{base}/twilio/status" if base else None

    for lote in lotes:
        raw_str = str(lote.get("telefono", "")).strip()
        vars_lote = lote.get("vars") or {}

        if not raw_str:
            continue

        # 1) SOLO normalizamos con phonenumbers
        try:
            e164 = normalize_to_e164(raw_str)
        except ValueError as e:
            invalid_by_lookup.append(f"{raw_str} ({e})")
            continue

        # 2) Enviar plantilla DIRECTO, sin lookup
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
            STATE["delivery"][e164] = {
                "status": "failed_on_send",
                "reason": str(ex),
                "channel": "whatsapp",
                "template": content_sid,
                "vars": vars_lote,
            }

    return jsonify({
        "invalid_by_lookup": invalid_by_lookup,
        "queued": queued,
        "skipped_not_mobile": skipped_not_mobile,
        "skipped_detail": skipped_detail,
        "note": "DEBUG DO: /send-template-bulk-personalizado SIN Lookup"
    }), 200


if __name__ == "__main__":
    # En desarrollo, usa puerto 5000
    app.run(host="0.0.0.0", port=5000, debug=True)
