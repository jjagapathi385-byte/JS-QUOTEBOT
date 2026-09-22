from flask import Flask, render_template_string, request, jsonify
import requests
import json
import os
import webbrowser
from threading import Timer

app = Flask(__name__)

# ── CREDENTIALS (all from environment variables) ──────────────────────────────
CLIENT_ID     = os.environ.get('ZOHO_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('ZOHO_CLIENT_SECRET', '')
GEMINI_KEY    = os.environ.get('GEMINI_API_KEY', '')
GROQ_KEY      = os.environ.get('GROQ_API_KEY', '')
ZOHO_BASE     = "https://invoice.zoho.in/api/v3"
TOKEN_FILE    = "zoho_tokens.json"
PORT          = int(os.environ.get('PORT', 5000))
IS_RAILWAY    = bool(os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('RAILWAY_SERVICE_NAME'))

# ── TOKEN MANAGEMENT ──────────────────────────────────────────────────────────
def load_tokens():
    env_refresh = os.environ.get('ZOHO_REFRESH_TOKEN', '')
    env_org     = os.environ.get('ZOHO_ORG_ID', '')
    if env_refresh:
        return {'refresh_token': env_refresh, 'org_id': env_org}
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    return {}

def save_tokens(updates):
    t = load_tokens()
    t.update(updates)
    try:
        with open(TOKEN_FILE, 'w') as f:
            json.dump(t, f, indent=2)
    except Exception:
        pass

# In-memory token cache to avoid refreshing on every call
_token_cache = {'access_token': None, 'expires_at': 0}

def get_fresh_token():
    import time
    t = load_tokens()
    if not t.get('refresh_token'):
        raise Exception("Not connected to Zoho. Please complete setup first.")

    # Use cached token if still valid (with 60s buffer)
    if _token_cache['access_token'] and time.time() < _token_cache['expires_at'] - 60:
        return _token_cache['access_token']

    # Refresh token from Zoho
    r = requests.post('https://accounts.zoho.in/oauth/v2/token', data={
        'grant_type':    'refresh_token',
        'client_id':     CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'refresh_token': t['refresh_token']
    })
    d = r.json()
    if 'access_token' not in d:
        raise Exception(f"Token refresh failed: {d}")

    # Cache it for 55 minutes (Zoho tokens last 1 hour)
    _token_cache['access_token'] = d['access_token']
    _token_cache['expires_at']   = time.time() + 3300
    save_tokens({'access_token': d['access_token']})
    return d['access_token']

def zh():
    token  = get_fresh_token()
    org_id = load_tokens().get('org_id', '')
    return {
        'Authorization': f'Zoho-oauthtoken {token}',
        'X-com-zoho-invoice-organizationid': org_id,
        'Content-Type': 'application/json'
    }

def zh_get(path, params=None):
    return requests.get(f'{ZOHO_BASE}{path}', headers=zh(), params=params or {})

def zh_post(path, data):
    return requests.post(f'{ZOHO_BASE}{path}', headers=zh(), json=data)

def zh_put(path, data):
    return requests.put(f'{ZOHO_BASE}{path}', headers=zh(), json=data)

# ── GEMINI PARSE ──────────────────────────────────────────────────────────────
def parse_with_gemini(message):
    # Trim message to avoid token limit issues with very long messages
    if len(message) > 3000:
        message = message[:3000]

    prompt = f"""You are a quotation parser for an Indian hardware/electrical supplies business.

Parse this WhatsApp quotation message and return ONLY a valid JSON object. No markdown, no explanation.

Message:
---
{message}
---

Rules:
1. Extract customer/store name from the first line or header
2. For each item: name, quantity (default 1), unit_price.
   - If total price given for multiple qty, divide to get unit_price (e.g. "5nos 1000" → unit=200)
   - Price may be STUCK to item name with no space — always split it:
     "scolding7500" → name="Scolding", unit_price=7500
     "labour charges11000" → name="Labour Charges", unit_price=11000
     "cement12 bags 7400" → name="Cement", qty=12, unit_price=616
   - Always separate trailing numbers as price
3. IMPORTANT - Correct item names: fix typos, expand abbreviations, use proper industry names.
   Examples:
   - "brazing rad pipe" → "Brazing Rod Pipe"
   - "let machine work" → "Lathe Machine Work"
   - "magnitor" → "Magnetron"
   - "fevi bond" → "Fevibond Adhesive"
   - "LPG gas tarch" → "LPG Gas Torch"
   - "R 32 gas" → "R32 Refrigerant Gas"
   - "led lt" → "LED Light"
   - "ac servic" → "AC Service"
   - "scolding" → "Scaffolding"
   - "Lappam patty" → "Lappam Putty"
   - "birla white care putty" → "Birla White Care Putty"
   - "paint roller.1200" → name="Paint Roller", price=1200 (period is separator not decimal)
   - "paint 15 L litre 6300" → name="Paint", qty=1, unit_price=6300 (15L is description not qty)
   - Any separator like period(.), dash(-), slash(/) between item and price should be treated as separator
   Always use proper English, correct spelling, full words.
4. Assign the most accurate 8-digit HSN code. Common ones:
   LED lights/lamps=94054090, Tape=39199090, Adhesive/Fevicol/Fevibond=35069900,
   PVC pipe=39172300, Wire/cable=85444290, Switch/socket=85363010, MCB=85362000,
   Paint=32089090, Cement=25232900, Steel/iron=72142000, Screws/bolts=73181500,
   Plywood=44121000, Glass=70051000, Fan=84145100, Pump=84137090,
   Refrigerant gas=38249099, Lathe/machine work=84589900, Magnetron=85402000,
   Brazing rod=83112000, Gas torch=84689900, Service charges=998719
   For anything else use your best judgment.

Return this exact JSON:
{{
  "customer_name": "name here",
  "is_interstate": false,
  "notes": "any subject/remarks/note text found in message, empty string if none",
  "items": [
    {{
      "name": "item name",
      "quantity": 1,
      "unit_price": 0.0,
      "hsn_or_sac": "12345678",
      "is_service": false
    }}
  ]
}}

For notes: extract text like "Add subject- payment done", "payment pending", "urgent" etc. that is NOT an item. Just the content, not the label.
ONLY return the JSON. Nothing else."""

    import time
    models = ['openai/gpt-oss-120b', 'qwen/qwen3-32b', 'llama-3.1-8b-instant'] 
    last_error = 'No response received'

    for attempt in range(4):
        model = models[min(attempt, len(models)-1)]
        try:
            r = requests.post(
                'https://api.groq.com/openai/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {GROQ_KEY}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': model,
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.1,
                    'max_tokens': 2000
                },
                timeout=60
            )
            d = r.json()

            # Rate limit — wait and retry with next model
            if r.status_code == 429 or (isinstance(d.get('error'), dict) and 'rate' in str(d['error']).lower()):
                wait = 5 * (attempt + 1)
                time.sleep(wait)
                continue

            # Other API error
            if 'error' in d:
                err_msg = d['error'].get('message', str(d['error'])) if isinstance(d['error'], dict) else str(d['error'])
                raise Exception(f"Groq error: {err_msg}")

            if 'choices' not in d or not d['choices']:
                last_error = str(d)[:300]
                time.sleep(3)
                continue

            try:
                raw = d['choices'][0]['message']['content'].strip()
            except (KeyError, IndexError) as ke:
                last_error = f"Parse error: {ke} in {str(d)[:200]}"
                time.sleep(3)
                continue

            if not raw:
                last_error = "Empty AI response"
                time.sleep(3)
                continue

            # Strip markdown fences
            if '```' in raw:
                parts = raw.split('```')
                raw = parts[1] if len(parts) > 1 else parts[0]
                if raw.startswith('json'):
                    raw = raw[4:]
                raw = raw.strip()

            return json.loads(raw)

        except json.JSONDecodeError:
            time.sleep(2)
            continue
        except requests.exceptions.Timeout:
            time.sleep(3)
            continue
        except Exception as e:
            if 'Groq error' in str(e):
                raise
            time.sleep(2)
            continue

    raise Exception(f"AI service unavailable. Last error: {last_error}. Please wait 30 seconds and try again.")

# ── ZOHO HELPERS ──────────────────────────────────────────────────────────────
def get_gst18_tax_id():
    r = zh_get('/taxes')
    for tax in r.json().get('taxes', []):
        pct  = tax.get('tax_percentage', 0)
        name = tax.get('tax_name', '').upper()
        if pct == 18 or ('18' in name and ('GST' in name or 'CGST' in name)):
            return tax['tax_id']
    return None

def find_customer(name):
    # Fetch all customers and use AI to find best match
    all_contacts = []
    page = 1
    while True:
        r = zh_get('/contacts', {'contact_type': 'customer', 'page': page, 'per_page': 200})
        data = r.json()
        batch = data.get('contacts', [])
        all_contacts.extend(batch)
        if not data.get('page_context', {}).get('has_more_page', False):
            break
        page += 1

    if not all_contacts:
        return None

    # Build name list for AI matching
    names_list = [f"{i+1}. {c['contact_name']}" for i, c in enumerate(all_contacts)]
    names_str  = "\n".join(names_list)

    prompt = f"""You are matching a WhatsApp store name to a Zoho customer list.

WhatsApp name: "{name}"

Zoho customers:
{names_str}

Find the best matching customer number. Consider abbreviations, partial matches, location names.
Reply with ONLY the number (e.g. "5") or "0" if no good match exists."""

    r2 = requests.post(
        'https://api.groq.com/openai/v1/chat/completions',
        headers={'Authorization': f'Bearer {GROQ_KEY}', 'Content-Type': 'application/json'},
        json={
            'model': 'openai/gpt-oss-120b',
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0
        }
    ,
        timeout=30
    )
    try:
        d2 = r2.json()
        if 'choices' not in d2 or not d2['choices']:
            return None
        result = d2['choices'][0]['message']['content'].strip()
    except Exception:
        return None

    try:
        idx = int(''.join(filter(str.isdigit, result))) - 1
        if 0 <= idx < len(all_contacts):
            return all_contacts[idx]
    except Exception:
        pass
    return None

def find_item(name):
    # Fetch all items and use AI to find best match
    all_items = []
    page = 1
    while True:
        r = zh_get('/items', {'page': page, 'per_page': 200})
        data = r.json()
        batch = data.get('items', [])
        all_items.extend(batch)
        if not data.get('page_context', {}).get('has_more_page', False):
            break
        page += 1

    if not all_items:
        return None

    names_list = [f"{i+1}. {itm['name']}" for i, itm in enumerate(all_items)]
    names_str  = "\n".join(names_list)

    prompt = f"""Match this item name to the closest item in a Zoho inventory list.

Item to find: "{name}"

Zoho items:
{names_str}

Find the best matching item number considering abbreviations, alternate names, and partial matches.
Reply with ONLY the number (e.g. "5") or "0" if no good match exists (similarity less than 70%)."""

    r2 = requests.post(
        'https://api.groq.com/openai/v1/chat/completions',
        headers={'Authorization': f'Bearer {GROQ_KEY}', 'Content-Type': 'application/json'},
        json={
            'model': 'openai/gpt-oss-120b',
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0
        },
        timeout=30
    )
    try:
        d2 = r2.json()
        if 'choices' not in d2 or not d2['choices']:
            return None
        result = d2['choices'][0]['message']['content'].strip()
    except Exception:
        return None

    try:
        idx = int(''.join(filter(str.isdigit, result))) - 1
        if 0 <= idx < len(all_items):
            return all_items[idx]
    except Exception:
        pass
    return None

def create_item(name, hsn_code, rate, tax_id, is_service=False):
    payload = {
        "name": name, "rate": rate,
        "product_type": "service" if is_service else "goods",
        "hsn_or_sac": str(hsn_code),
    }
    if not is_service:
        payload["item_type"] = "inventory"
    if tax_id:
        payload["tax_id"] = tax_id
    r    = zh_post('/items', payload)
    resp = r.json()
    item = resp.get('item', {})

    if not item:
        # Item already exists — fetch it and update HSN if blank
        if resp.get('code') == 1001 and 'already exists' in resp.get('message', ''):
            r2       = zh_get('/items', {'search_text': name})
            existing = r2.json().get('items', [])
            if existing:
                ex_item = existing[0]
                ex_hsn  = ex_item.get('hsn_or_sac', '').strip().replace('0','')
                # Update HSN if it's blank or was 00000000
                if not ex_hsn and hsn_code and str(hsn_code).replace('0',''):
                    upd_payload = {
                        "name":         ex_item.get('name', name),
                        "rate":         ex_item.get('rate', rate),
                        "hsn_or_sac":   str(hsn_code),
                        "product_type": "service" if is_service else "goods"
                    }
                    zh_put(f'/items/{ex_item["item_id"]}', upd_payload)
                return ex_item
        raise Exception(f"Item creation failed: {resp}")
    return item

# In-memory cache for last used seq number — prevents back-to-back conflicts
_last_seq = {'value': 0}

def get_default_notes():
    """Fetch default customer notes from Zoho estimate settings."""
    try:
        r = zh_get('/settings/estimates')
        d = r.json()
        settings = d.get('estimate_settings', d)
        return settings.get('notes', '').strip()
    except Exception:
        return ''

def get_next_seq():
    """Get next seq — combines Zoho scan + in-memory cache."""
    try:
        r = zh_get('/estimates', {'sort_column': 'created_time', 'sort_order': 'D', 'per_page': 10})
        estimates = r.json().get('estimates', [])
        zoho_max = 0
        for est in estimates:
            num   = est.get('estimate_number', '')
            parts = num.split('-')
            if parts:
                digits = ''.join(filter(str.isdigit, parts[-1]))
                if digits:
                    zoho_max = max(zoho_max, int(digits))
        max_seq = max(zoho_max, _last_seq['value'])
        return max_seq + 1 if max_seq > 0 else 1
    except Exception:
        return (_last_seq['value'] + 1) if _last_seq['value'] > 0 else 1

def create_estimate(customer_id, customer_name, line_items, notes=''):
    """Create estimate with correct number directly — works in both auto and manual Zoho mode."""
    clean_name = customer_name.strip().upper().replace(' ', '_')

    for attempt in range(5):  # Retry up to 5 times on conflict
        seq           = get_next_seq() + attempt
        custom_number = f"{clean_name}-QT-{str(seq).zfill(6)}"

        payload = {
            "customer_id":     customer_id,
            "estimate_number": custom_number,
            "line_items":      line_items,
        }
        # Fetch default Zoho notes and append new note on next line
        default_note = get_default_notes()
        if notes and notes.strip():
            combined = f"{default_note}\n{notes.strip()}" if default_note else notes.strip()
        else:
            combined = default_note
        if combined:
            payload["notes"] = combined

        r      = zh_post('/estimates', payload)
        result = r.json()

        if result.get('estimate'):
            # Success — update seq cache
            _last_seq['value'] = seq
            return result

        code = result.get('code', 0)
        if code == 1001:
            # Duplicate number — try next
            _last_seq['value'] = seq
            continue
        else:
            # Other error — return as-is
            return result

    return result

# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/status')
def status():
    t = load_tokens()
    configured = bool(t.get('refresh_token'))
    creds_ok   = bool(CLIENT_ID and CLIENT_SECRET and GROQ_KEY)
    return jsonify({'configured': configured, 'creds_ok': creds_ok, 'is_railway': IS_RAILWAY})

@app.route('/setup', methods=['POST'])
def setup():
    code = request.json.get('auth_code', '').strip()
    if not code:
        return jsonify({'success': False, 'error': 'Auth code is empty.'})
    r = requests.post('https://accounts.zoho.in/oauth/v2/token', data={
        'grant_type': 'authorization_code',
        'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
        'redirect_uri': '', 'code': code
    })
    d = r.json()
    if 'refresh_token' not in d:
        return jsonify({'success': False, 'error': d})
    save_tokens(d)
    org_r    = requests.get(f'{ZOHO_BASE}/organizations',
                            headers={'Authorization': f'Zoho-oauthtoken {d["access_token"]}'})
    orgs     = org_r.json().get('organizations', [])
    org_name = 'Unknown'
    org_id   = ''
    if orgs:
        org_id   = orgs[0]['organization_id']
        org_name = orgs[0].get('name', 'Unknown')
        save_tokens({'org_id': org_id})
    return jsonify({
        'success': True, 'org': org_name,
        'railway_vars': {
            'ZOHO_REFRESH_TOKEN': d['refresh_token'],
            'ZOHO_ORG_ID': org_id
        }
    })

@app.route('/next-number')
def next_number():
    """Show the next quote number to use for manual creation."""
    try:
        # Get last estimate to find current sequence
        r = zh_get('/estimates', {'sort_column': 'created_time', 'sort_order': 'D', 'per_page': 1})
        estimates = r.json().get('estimates', [])
        last_num = 1
        if estimates:
            last_no = estimates[0].get('estimate_number', '')
            digits = ''.join(filter(str.isdigit, last_no.split('-')[-1]))
            if digits:
                last_num = int(digits) + 1
        next_no = str(last_num).zfill(6)
        return render_template_string("""<!DOCTYPE html><html><head>
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <style>
          body{font-family:sans-serif;padding:24px;background:#f7f8fc;max-width:400px;margin:0 auto;text-align:center}
          h2{color:#111;margin-bottom:6px}
          p{color:#666;font-size:13px;margin-bottom:24px}
          .num{font-family:monospace;font-size:28px;font-weight:800;color:#4f46e5;background:#eef2ff;padding:20px;border-radius:12px;border:2px solid #c7d2fe;margin-bottom:16px}
          .format{font-size:13px;color:#6b7280;margin-bottom:20px}
          button{background:#4f46e5;color:#fff;border:none;border-radius:10px;padding:14px 28px;font-size:15px;font-weight:700;cursor:pointer;width:100%}
        </style></head><body>
        <h2>📋 Next Quote Number</h2>
        <p>Use this sequence number when creating a quote manually in Zoho.</p>
        <div class="num">{{ next_no }}</div>
        <div class="format">Format: <strong>STORENAME-QT-{{ next_no }}</strong><br>e.g. KFC-SURYAPET-QT-{{ next_no }}</div>
        <button onclick="navigator.clipboard.writeText('{{ next_no }}').then(()=>alert('Copied!'))">Copy Number</button>
        </body></html>""", next_no=next_no)
    except Exception as e:
        return f"<h3 style='font-family:sans-serif;padding:24px'>Error: {e}</h3>"

@app.route('/get-tokens')
def get_tokens():
    t = load_tokens()
    refresh = t.get('refresh_token', '')
    org_id  = t.get('org_id', '')
    if not refresh:
        return "<h2 style='font-family:sans-serif;padding:24px'>No tokens found. Please complete Zoho setup first.</h2>"
    return render_template_string("""<!DOCTYPE html><html><head>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
      body{font-family:sans-serif;padding:24px;background:#f7f8fc;max-width:540px;margin:0 auto}
      h2{color:#111;margin-bottom:6px}p{color:#666;font-size:13px;margin-bottom:20px}
      .box{background:#fff;border:1.5px solid #e4e7ef;border-radius:12px;padding:16px;margin-bottom:14px}
      .label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;margin-bottom:6px}
      .val{font-family:monospace;font-size:12px;word-break:break-all;color:#111;background:#f3f4f6;padding:10px;border-radius:8px}
      button{width:100%;background:#4f46e5;color:#fff;border:none;border-radius:10px;padding:12px;font-size:14px;font-weight:700;cursor:pointer;margin-top:8px}
    </style></head><body>
    <h2>🚂 Railway Variables</h2>
    <p>Copy these two values and add them to Railway → Variables tab. Do this now before redeploying!</p>
    <div class="box">
      <div class="label">ZOHO_REFRESH_TOKEN</div>
      <div class="val" id="r">{{ refresh }}</div>
      <button onclick="cp('r','ZOHO_REFRESH_TOKEN')">Copy</button>
    </div>
    <div class="box">
      <div class="label">ZOHO_ORG_ID</div>
      <div class="val" id="o">{{ org_id }}</div>
      <button onclick="cp('o','ZOHO_ORG_ID')">Copy</button>
    </div>
    <script>
    function cp(id,name){
      const v=document.getElementById(id).textContent;
      navigator.clipboard.writeText(v).then(()=>alert(name+' copied!')).catch(()=>{
        const t=document.createElement('textarea');t.value=v;
        document.body.appendChild(t);t.select();document.execCommand('copy');
        document.body.removeChild(t);alert(name+' copied!');
      });
    }
    </script></body></html>""", refresh=refresh, org_id=org_id)

@app.route('/process', methods=['POST'])
def process():
    message = request.json.get('message', '').strip()
    if not message:
        return jsonify({'status': 'error', 'message': 'No message provided.'})
    try:
        parsed        = parse_with_gemini(message)
        customer_name = parsed.get('customer_name', '').strip()
        items         = parsed.get('items', [])
        if not customer_name:
            return jsonify({'status': 'error', 'message': 'Could not detect customer name.'})
        customer = find_customer(customer_name)
        if not customer:
            return jsonify({
                'status': 'customer_not_found',
                'parsed_name': customer_name,
                'message': f"Customer '{customer_name}' not found in Zoho Invoice. Please add them first."
            })
        tax_id        = get_gst18_tax_id()
        line_items    = []
        new_items_log = []
        for item in items:
            name         = item.get('name', '').strip()
            qty          = float(item.get('quantity', 1))
            unit_price   = float(item.get('unit_price', 0))
            # Support both old (hsn_code) and new (hsn_or_sac) field names
            hsn          = item.get('hsn_or_sac', item.get('hsn_code', ''))
            is_service   = item.get('is_service', False)
            marked_price = round(unit_price * 1.10, 2)
            existing = find_item(name)
            if existing:
                item_id = existing['item_id']
            else:
                created = create_item(name, hsn, marked_price, tax_id, is_service)
                item_id = created.get('item_id', '')
                label   = 'SAC' if is_service else 'HSN'
                new_items_log.append({'name': name, 'hsn': f"{label}: {hsn}" if hsn else 'Auto-assigned', 'rate': marked_price})
            line_items.append({
                "item_id": item_id, "name": name,
                "quantity": qty, "rate": marked_price,
                **({"tax_id": tax_id} if tax_id else {})
            })
        notes  = parsed.get('notes', '')
        result = create_estimate(customer['contact_id'], customer.get('contact_name', customer_name), line_items, notes)
        est    = result.get('estimate')
        if est:
            return jsonify({
                'status': 'success',
                'estimate_number': est.get('estimate_number', 'N/A'),
                'customer': customer.get('contact_name', customer_name),
                'total': est.get('total', 0),
                'currency_symbol': est.get('currency_symbol', '₹'),
                'new_items': new_items_log
            })
        else:
            return jsonify({'status': 'error', 'message': f"Zoho error: {result}"})
    except json.JSONDecodeError as e:
        return jsonify({'status': 'error', 'message': f'Parse error: {e}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="QuoteBot">
<title>QuoteBot</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  :root{--bg:#F7F8FC;--surface:#fff;--border:#E4E7EF;--ink:#111827;--muted:#6B7280;--accent:#4F46E5;--accent-h:#4338CA;--green:#059669;--r:14px}
  html{-webkit-text-size-adjust:100%}
  body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);min-height:100vh;display:flex;align-items:flex-start;justify-content:center;padding:20px 16px 40px}
  .shell{width:100%;max-width:520px}
  .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
  .brand{display:flex;align-items:center;gap:10px}
  .brand-icon{width:38px;height:38px;background:var(--accent);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:19px}
  .brand-name{font-size:20px;font-weight:800;color:var(--ink);letter-spacing:-.5px}
  .brand-sub{font-size:11px;color:var(--muted);margin-top:1px}
  .pill{font-size:11px;font-weight:700;padding:4px 12px;border-radius:20px;background:#FEF3C7;color:#92400E;border:1px solid #FCD34D;transition:all .3s;white-space:nowrap}
  .pill.ok{background:#D1FAE5;color:#065F46;border-color:#6EE7B7}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:22px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.04)}
  .card-title{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-bottom:14px}
  .setup-steps{background:#EEF2FF;border:1px solid #C7D2FE;border-radius:10px;padding:14px;font-size:13px;color:#3730A3;line-height:1.75;margin-bottom:14px}
  .setup-steps a{color:#4F46E5;font-weight:600}
  .setup-steps code{font-family:'JetBrains Mono',monospace;background:#C7D2FE;padding:1px 6px;border-radius:4px;font-size:11px}
  .input-row{display:flex;gap:8px}
  input[type=text]{flex:1;border:1.5px solid var(--border);border-radius:10px;padding:11px 13px;font-size:13px;font-family:'JetBrains Mono',monospace;outline:none;transition:border .2s;color:var(--ink);min-width:0}
  input[type=text]:focus{border-color:var(--accent)}
  .btn-connect{background:var(--green);color:#fff;border:none;border-radius:10px;padding:11px 16px;font-size:13px;font-weight:700;cursor:pointer;white-space:nowrap;-webkit-tap-highlight-color:transparent}
  .btn-connect:active{background:#047857}
  .railway-box{background:#FFF7ED;border:1px solid #FED7AA;border-radius:10px;padding:14px;margin-top:14px;display:none}
  .railway-box-title{font-size:12px;font-weight:700;color:#C2410C;margin-bottom:10px;text-transform:uppercase;letter-spacing:.04em}
  .railway-var{background:#fff;border:1px solid #FED7AA;border-radius:8px;padding:10px 12px;margin-bottom:8px;cursor:pointer}
  .railway-var:active{background:#FFF7ED}
  .railway-var-label{font-size:10px;font-weight:700;color:#9A3412;text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px}
  .railway-var-val{font-family:'JetBrains Mono',monospace;font-size:11px;color:#1C1917;word-break:break-all}
  .railway-note{font-size:12px;color:#92400E;line-height:1.6;margin-top:10px}
  textarea{width:100%;border:1.5px solid var(--border);border-radius:10px;padding:13px;font-size:14px;font-family:'JetBrains Mono',monospace;line-height:1.7;resize:vertical;min-height:150px;outline:none;color:var(--ink);transition:border .2s;-webkit-appearance:none}
  textarea:focus{border-color:var(--accent)}
  textarea::placeholder{color:#9CA3AF}
  .btn-main{width:100%;background:var(--accent);color:#fff;border:none;border-radius:10px;padding:15px;font-size:15px;font-weight:700;cursor:pointer;margin-top:12px;display:flex;align-items:center;justify-content:center;gap:8px;-webkit-tap-highlight-color:transparent;touch-action:manipulation}
  .btn-main:active:not(:disabled){background:var(--accent-h)}
  .btn-main:disabled{background:#9CA3AF;cursor:not-allowed}
  .loader{display:none;text-align:center;padding:16px 0 4px;color:var(--muted);font-size:13px}
  .dots span{display:inline-block;width:6px;height:6px;background:var(--accent);border-radius:50%;margin:0 2px;animation:bounce .9s infinite ease-in-out}
  .dots span:nth-child(2){animation-delay:.15s}
  .dots span:nth-child(3){animation-delay:.3s}
  @keyframes bounce{0%,80%,100%{transform:translateY(0)}40%{transform:translateY(-6px)}}
  .result{display:none;border-radius:10px;padding:16px;margin-top:14px;font-size:14px;line-height:1.7}
  .result.success{background:#ECFDF5;border:1.5px solid #A7F3D0}
  .result.warning{background:#FFFBEB;border:1.5px solid #FDE68A}
  .result.error{background:#FEF2F2;border:1.5px solid #FECACA}
  .result-title{font-weight:800;font-size:15px;margin-bottom:10px}
  .result-row{display:flex;align-items:baseline;gap:8px;padding:2px 0;font-size:13px}
  .rl{color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;width:80px;flex-shrink:0}
  .rv{font-weight:600;color:var(--ink)}
  .rt{font-size:18px;font-weight:800;color:var(--green)}
  .new-sec{margin-top:10px;padding-top:10px;border-top:1px solid #A7F3D0}
  .new-title{font-size:11px;font-weight:700;color:#065F46;margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em}
  .new-tag{display:inline-flex;align-items:center;gap:5px;background:#fff;border:1px solid #A7F3D0;border-radius:6px;padding:3px 9px;font-size:12px;color:#065F46;font-weight:500;margin:2px}
  .new-tag .hsn{color:#9CA3AF;font-family:'JetBrains Mono',monospace;font-size:10px}
  .warn-msg{color:#92400E;font-size:13px;line-height:1.6}
  .err-msg{color:#7F1D1D;font-size:12px;font-family:'JetBrains Mono',monospace;line-height:1.5;word-break:break-all}
  .alert-box{background:#FEF2F2;border:1px solid #FECACA;border-radius:10px;padding:14px;font-size:13px;color:#7F1D1D;line-height:1.6;margin-bottom:12px;display:none}
</style>
</head>
<body>
<div class="shell">
  <div class="header">
    <div class="brand">
      <div class="brand-icon">⚡</div>
      <div>
        <div class="brand-name">QuoteBot</div>
        <div class="brand-sub">WhatsApp → Zoho Invoice</div>
      </div>
    </div>
    <span class="pill" id="pill">Checking…</span>
  </div>

  <div class="alert-box" id="creds-alert">
    ⚠️ <strong>Environment variables missing.</strong><br>
    Please set <code>ZOHO_CLIENT_ID</code>, <code>ZOHO_CLIENT_SECRET</code>, and <code>GEMINI_API_KEY</code> in Railway Variables.
  </div>

  <div class="card" id="setup-card" style="display:none">
    <div class="card-title">🔐 One-time Zoho Setup</div>
    <div class="setup-steps">
      1. Go to <a href="https://api-console.zoho.in" target="_blank">api-console.zoho.in</a><br>
      2. Open <strong>Self Client</strong> → <strong>Generate Code</strong><br>
      3. Scope: <code>ZohoInvoice.fullaccess.all</code> · Duration: <strong>10 min</strong><br>
      4. Paste the code below → Connect
    </div>
    <div class="input-row">
      <input type="text" id="auth-input" placeholder="1000.xxxx…" autocomplete="off" autocorrect="off" spellcheck="false"/>
      <button class="btn-connect" onclick="doSetup()">Connect ✓</button>
    </div>
    <div class="railway-box" id="railway-box">
      <div class="railway-box-title">🚂 Add these to Railway Variables</div>
      <div class="railway-var" onclick="copyVal('rv-refresh')">
        <div class="railway-var-label">ZOHO_REFRESH_TOKEN (tap to copy)</div>
        <div class="railway-var-val" id="rv-refresh">—</div>
      </div>
      <div class="railway-var" onclick="copyVal('rv-org')">
        <div class="railway-var-label">ZOHO_ORG_ID (tap to copy)</div>
        <div class="railway-var-val" id="rv-org">—</div>
      </div>
      <div class="railway-note">Railway dashboard → your service → <strong>Variables</strong> → add both → Redeploy once.</div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">📋 New Quotation</div>
    <textarea id="msg" placeholder="Paste your WhatsApp message here...

Example:
AMR store quotation
1.led lights -200no 3150
2.3mm tape s 2 no 600
3.fevi bond -3no 250
Total = 4000"></textarea>
    <button class="btn-main" id="btn" onclick="processMessage()">
      <span>Create Quotation in Zoho</span><span>→</span>
    </button>
    <div class="loader" id="loader">
      <div class="dots"><span></span><span></span><span></span></div>
      <div style="margin-top:8px">Parsing &amp; creating quotation…</div>
    </div>
    <div class="result" id="result"></div>
  </div>
</div>
<script>
async function checkStatus(){
  try{
    const r=await fetch('/status'),d=await r.json();
    const pill=document.getElementById('pill');
    const sc=document.getElementById('setup-card');
    const ca=document.getElementById('creds-alert');
    const rb=document.getElementById('railway-box');
    if(!d.creds_ok){ca.style.display='block';pill.textContent='⚠️ Config missing';return}
    if(d.configured){
      pill.textContent='✅ Connected';pill.className='pill ok';
      if(rb && rb.style.display==='block'){sc.style.display='block'}
      else{sc.style.display='none'}
    }
    else{pill.textContent='⚠️ Setup needed';sc.style.display='block'}
  }catch(e){document.getElementById('pill').textContent='⚠️ Offline'}
}
async function doSetup(){
  const code=document.getElementById('auth-input').value.trim();
  if(!code){alert('Paste the auth code first.');return}
  const btn=document.querySelector('.btn-connect');
  btn.disabled=true;btn.textContent='Connecting…';
  try{
    const r=await fetch('/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({auth_code:code})});
    const d=await r.json();
    if(d.success){
      if(d.railway_vars){
        document.getElementById('rv-refresh').textContent=d.railway_vars.ZOHO_REFRESH_TOKEN;
        document.getElementById('rv-org').textContent=d.railway_vars.ZOHO_ORG_ID;
        document.getElementById('railway-box').style.display='block';
      }
      checkStatus();
    }else{alert('❌ Failed: '+JSON.stringify(d.error,null,2))}
  }catch(e){alert('Error: '+e.message)}
  btn.disabled=false;btn.textContent='Connect ✓';
}
function copyVal(id){
  const val=document.getElementById(id).textContent;
  if(val==='—')return;
  navigator.clipboard.writeText(val).then(()=>alert('Copied!')).catch(()=>{
    const ta=document.createElement('textarea');ta.value=val;
    document.body.appendChild(ta);ta.select();document.execCommand('copy');
    document.body.removeChild(ta);alert('Copied!');
  });
}
async function processMessage(){
  const msg=document.getElementById('msg').value.trim();
  if(!msg){alert('Paste a WhatsApp message first.');return}
  const btn=document.getElementById('btn'),loader=document.getElementById('loader'),res=document.getElementById('result');
  btn.disabled=true;loader.style.display='block';res.style.display='none';
  try{
    const r=await fetch('/process',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
    const d=await r.json();
    res.style.display='block';
    if(d.status==='success'){
      res.className='result success';
      let ni='';
      if(d.new_items&&d.new_items.length){
        const tags=d.new_items.map(i=>`<span class="new-tag">📦 ${i.name} <span class="hsn">${i.hsn}</span></span>`).join('');
        ni=`<div class="new-sec"><div class="new-title">🆕 New items added to Zoho</div>${tags}</div>`;
      }
      res.innerHTML=`<div class="result-title" style="color:#065F46">✅ Quotation Created!</div>
        <div class="result-row"><span class="rl">Quote No.</span><span class="rv">${d.estimate_number}</span></div>
        <div class="result-row"><span class="rl">Customer</span><span class="rv">${d.customer}</span></div>
        <div class="result-row"><span class="rl">Total</span><span class="rt">${d.currency_symbol}${d.total}</span></div>${ni}`;
    }else if(d.status==='customer_not_found'){
      res.className='result warning';
      res.innerHTML=`<div class="result-title" style="color:#92400E">⚠️ Customer Not Found</div>
        <div class="warn-msg">Detected: <strong>${d.parsed_name}</strong><br>Not in Zoho Invoice. Add the customer first, then try again.</div>`;
    }else{
      res.className='result error';
      res.innerHTML=`<div class="result-title" style="color:#7F1D1D">❌ Error</div><div class="err-msg">${d.message}</div>`;
    }
  }catch(e){
    res.style.display='block';res.className='result error';
    res.innerHTML=`<div class="result-title" style="color:#7F1D1D">❌ Error</div><div class="err-msg">${e.message}</div>`;
  }
  btn.disabled=false;loader.style.display='none';
}
checkStatus();
</script>
</body>
</html>"""

# ── LAUNCH ────────────────────────────────────────────────────────────────────
def open_browser():
    webbrowser.open(f'http://localhost:{PORT}')

if __name__ == '__main__':
    print("\n" + "="*52)
    print("  ⚡  QuoteBot — Zoho Invoice Automation")
    print("="*52)
    if IS_RAILWAY:
        print(f"  Running on Railway · Port {PORT}")
    else:
        print(f"  Local: http://localhost:{PORT}")
        Timer(1.5, open_browser).start()
    print()
    app.run(debug=False, host='0.0.0.0', port=PORT, use_reloader=False)
