import os
import re
import json
import time
import logging
from datetime import datetime
from functools import wraps
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, send_file, Response
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'quotebot-secret-key-change-in-prod')

# -----------------------------------------------------------------------------
# Configuration & Environment Variables
# -----------------------------------------------------------------------------
GROQ_KEY = os.getenv('GROQ_API_KEY', '')
ZOHO_CLIENT_ID = os.getenv('ZOHO_CLIENT_ID', '')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET', '')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN', '')
ZOHO_ORGANIZATION_ID = os.getenv('ZOHO_ORGANIZATION_ID', '')
ZOHO_ACCOUNTS_URL = os.getenv('ZOHO_ACCOUNTS_URL', 'https://accounts.zoho.com')
ZOHO_BASE_URL = os.getenv('ZOHO_BASE_URL', 'https://www.zohoapis.com/invoice/v3')

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

QUOTEFORM_PIN = os.getenv('QUOTEFORM_PIN', '1234')

# Dynamic model selection for Groq
MODELS_TO_TRY = [
    'openai/gpt-oss-120b',
    'openai/gpt-oss-20b',
    'llama-3.3-70b-versatile',
    'llama3-8b-8192'
]

# -----------------------------------------------------------------------------
# In-Memory List Caching (10-Minute TTL)
# -----------------------------------------------------------------------------
_LIST_CACHE_TTL = 600
_list_cache = {
    'customers': {'ts': 0, 'data': None},
    'items': {'ts': 0, 'data': None}
}

def _lc_get(key):
    c = _list_cache.get(key)
    if c and c['data'] is not None and (time.time() - c['ts'] < _LIST_CACHE_TTL):
        return c['data']
    return None

def _lc_set(key, data):
    _list_cache[key] = {'ts': time.time(), 'data': data}

def invalidate_caches():
    _list_cache['customers']['ts'] = 0
    _list_cache['items']['ts'] = 0

# -----------------------------------------------------------------------------
# Zoho OAuth & API Helpers
# -----------------------------------------------------------------------------
_zoho_token_cache = {'token': None, 'expires_at': 0}

def get_zoho_token():
    now = time.time()
    if _zoho_token_cache['token'] and now < _zoho_token_cache['expires_at'] - 60:
        return _zoho_token_cache['token']

    url = f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token"
    params = {
        'refresh_token': ZOHO_REFRESH_TOKEN,
        'client_id': ZOHO_CLIENT_ID,
        'client_secret': ZOHO_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    r = requests.post(url, params=params, timeout=15)
    data = r.json()
    if 'access_token' not in data:
        raise Exception(f"Failed to refresh Zoho token: {data}")

    _zoho_token_cache['token'] = data['access_token']
    _zoho_token_cache['expires_at'] = now + data.get('expires_in', 3600)
    return _zoho_token_cache['token']

def zoho_headers():
    token = get_zoho_token()
    return {
        'Authorization': f"Zoho-oauthtoken {token}",
        'X-Com-Zoho-Invoice-Organizationid': ZOHO_ORGANIZATION_ID,
        'Content-Type': 'application/json'
    }

def fetch_all_customers():
    cached = _lc_get('customers')
    if cached is not None:
        return cached

    url = f"{ZOHO_BASE_URL}/contacts"
    page = 1
    all_contacts = []
    headers = zoho_headers()

    while True:
        r = requests.get(url, headers=headers, params={'page': page, 'per_page': 200}, timeout=15)
        d = r.json()
        if d.get('code') != 0:
            break
        contacts = d.get('contacts', [])
        all_contacts.extend(contacts)
        if not d.get('page_context', {}).get('has_more_page', False):
            break
        page += 1

    _lc_set('customers', all_contacts)
    return all_contacts

def fetch_all_items():
    cached = _lc_get('items')
    if cached is not None:
        return cached

    url = f"{ZOHO_BASE_URL}/items"
    page = 1
    all_items = []
    headers = zoho_headers()

    while True:
        r = requests.get(url, headers=headers, params={'page': page, 'per_page': 200}, timeout=15)
        d = r.json()
        if d.get('code') != 0:
            break
        items = d.get('items', [])
        all_items.extend(items)
        if not d.get('page_context', {}).get('has_more_page', False):
            break
        page += 1

    _lc_set('items', all_items)
    return all_items

def create_item(name, rate, hsn_or_sac):
    url = f"{ZOHO_BASE_URL}/items"
    payload = {
        'name': name,
        'rate': float(rate),
        'product_type': 'goods',
        'hsn_or_sac': str(hsn_or_sac)
    }
    r = requests.post(url, headers=zoho_headers(), json=payload, timeout=15)
    d = r.json()
    if d.get('code') == 0:
        _list_cache['items']['ts'] = 0  # Invalidate item cache on creation
        return d.get('item')
    return None

def get_next_estimate_number(customer_name):
    prefix = "JSINT"
    m = re.search(r'\((K\d+|P\d+|[A-Z0-9]+)\)', customer_name)
    store_code = m.group(1) if m else "GEN"

    headers = zoho_headers()
    url = f"{ZOHO_BASE_URL}/estimates"
    r = requests.get(url, headers=headers, params={'sort_column': 'created_time', 'sort_order': 'A'}, timeout=15)
    d = r.json()

    max_seq = 0
    search_prefix = f"{prefix}-{store_code}-QT-"
    if d.get('code') == 0:
        for est in d.get('estimates', []):
            num = est.get('estimate_number', '')
            if num.startswith(search_prefix):
                try:
                    seq_part = int(num.replace(search_prefix, ''))
                    if seq_part > max_seq:
                        max_seq = seq_part
                except ValueError:
                    pass

    return f"{search_prefix}{str(max_seq + 1).zfill(6)}"

def send_telegram_notification(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logging.error(f"Telegram notification error: {e}")

# -----------------------------------------------------------------------------
# AI Parsing Logic
# -----------------------------------------------------------------------------
def call_groq_api(prompt, system_prompt="You are a JSON-only response generator."):
    headers = {'Authorization': f"Bearer {GROQ_KEY}", 'Content-Type': 'application/json'}

    for model in MODELS_TO_TRY:
        payload = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': prompt}
            ],
            'temperature': 0.1,
            'max_tokens': 2500
        }
        try:
            r = requests.post('https://api.groq.com/openai/v1/chat/completions', headers=headers, json=payload, timeout=30)
            if r.status_code == 200:
                res = r.json()
                return res['choices'][0]['message']['content'].strip()
        except Exception as e:
            logging.warning(f"Model {model} failed: {e}")
            continue
    raise Exception("All LLM providers failed to generate a response.")

# -----------------------------------------------------------------------------
# Flask Session Helpers
# -----------------------------------------------------------------------------
def qf_logged_in():
    return session.get('qf_auth') is True

def qf_require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not qf_logged_in():
            return redirect(url_for('quoteform_login'))
        return f(*args, **kwargs)
    return decorated

# -----------------------------------------------------------------------------
# Web Routes
# -----------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/quoteform/login', methods=['GET', 'POST'])
def quoteform_login():
    error = None
    if request.method == 'POST':
        pin = request.form.get('pin', '').strip()
        if pin == QUOTEFORM_PIN:
            session['qf_auth'] = True
            return redirect(url_for('quoteform_index'))
        else:
            error = "Invalid PIN. Please try again."
    return render_template_string(LOGIN_TEMPLATE, error=error)

@app.route('/quoteform/logout')
def quoteform_logout():
    session.pop('qf_auth', None)
    return redirect(url_for('quoteform_login'))

@app.route('/quoteform')
@qf_require_auth
def quoteform_index():
    return render_template_string(QUOTEFORM_TEMPLATE)

@app.route('/api/customers')
def api_customers():
    try:
        contacts = fetch_all_customers()
        cleaned = [{'customer_id': c['contact_id'], 'contact_name': c['contact_name']} for c in contacts]
        return jsonify({'status': 'success', 'customers': cleaned})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/items')
def api_items():
    try:
        items = fetch_all_items()
        cleaned = [{'item_id': i['item_id'], 'name': i['name'], 'rate': i.get('rate', 0), 'hsn_or_sac': i.get('hsn_or_sac', '')} for i in items]
        return jsonify({'status': 'success', 'items': cleaned})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/quoteform/parse-items', methods=['POST'])
def quoteform_parse_items():
    if not qf_logged_in():
        return jsonify({'status': 'session_expired'})
    text = (request.json or {}).get('text', '').strip()
    if not text:
        return jsonify({'status': 'error', 'message': 'No text provided.'})

    try:
        prompt = f"""Parse this item list and return ONLY a valid JSON array. No markdown, no explanation.

Input:
{text}

Rules:
1. Each line is one item. Extract: name, quantity (default 1), unit_price.
2. Separators can be dash (-), comma (,), slash (/), or spaces. Be flexible.
3. "10nos", "10 nos", "10 no", "10pcs", "qty 10" all mean quantity = 10.
4. If total price is given for multiple qty, divide to get unit_price.
5. Fix typos and abbreviations in item names (proper English).
6. Ignore any lines that are clearly not items (headers, totals, blank lines).

Return ONLY this exact JSON array format:
[
  {{"name": "Item Name", "quantity": 1, "unit_price": 0.0}}
]

ONLY return the JSON array. Nothing else."""

        r = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={'Authorization': f'Bearer {GROQ_KEY}', 'Content-Type': 'application/json'},
            json={
                'model': 'openai/gpt-oss-120b',
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.1,
                'max_tokens': 2000
            },
            timeout=30
        )
        d = r.json()

        if 'choices' not in d or not d['choices']:
            return jsonify({'status': 'error', 'message': f'AI service error: {d}'})

        raw = d['choices'][0]['message']['content'].strip()

        # Markdown fence extraction
        if '```' in raw:
            parts = raw.split('```')
            for p in parts:
                p_clean = p.strip()
                if p_clean.startswith('json'):
                    p_clean = p_clean[4:].strip()
                if p_clean.startswith('[') and p_clean.endswith(']'):
                    raw = p_clean
                    break

        # Boundary extraction for array brackets
        if '[' in raw and ']' in raw:
            start = raw.find('[')
            end = raw.rfind(']') + 1
            raw = raw[start:end]

        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError('Parsed output was not a JSON list')

        return jsonify({'status': 'success', 'items': items})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Parse error: {str(e)}'})

@app.route('/quoteform/submit', methods=['POST'])
def quoteform_submit():
    if not qf_logged_in():
        return jsonify({'status': 'session_expired'})

    data = request.json or {}
    customer_id = data.get('customer_id')
    customer_name = data.get('customer_name', 'Customer')
    items = data.get('items', [])
    markup = float(data.get('markup', 0))

    if not customer_id or not items:
        return jsonify({'status': 'error', 'message': 'Customer and at least one item are required.'})

    try:
        all_zoho_items = fetch_all_items()
        item_map = {i['name'].lower(): i for i in all_zoho_items}

        line_items = []
        for it in items:
            name = it['name'].strip()
            qty = float(it.get('quantity', 1))
            unit_price = float(it.get('unit_price', 0))
            if markup > 0:
                unit_price = round(unit_price * (1 + markup / 100.0), 2)

            z_item = item_map.get(name.lower())
            if not z_item:
                z_item = create_item(name, unit_price, "8481")  # Default HSN

            line_items.append({
                'item_id': z_item['item_id'] if z_item else None,
                'name': name,
                'rate': unit_price,
                'quantity': qty
            })

        est_num = get_next_estimate_number(customer_name)
        payload = {
            'customer_id': customer_id,
            'estimate_number': est_num,
            'line_items': line_items,
            'date': datetime.now().strftime('%Y-%m-%d')
        }

        url = f"{ZOHO_BASE_URL}/estimates"
        r = requests.post(url, headers=zoho_headers(), json=payload, timeout=20)
        res_data = r.json()

        if res_data.get('code') == 0:
            est = res_data.get('estimate', {})
            est_id = est.get('estimate_id')
            total = est.get('total', 0)

            # Mark as sent & Send Email
            requests.post(f"{ZOHO_BASE_URL}/estimates/{est_id}/status/sent", headers=zoho_headers(), timeout=10)
            
            send_telegram_notification(
                f"<b>New Estimate Created via QuoteForm!</b>\n"
                f"<b>Estimate #:</b> {est_num}\n"
                f"<b>Customer:</b> {customer_name}\n"
                f"<b>Total:</b> ₹{total}"
            )

            return jsonify({
                'status': 'success',
                'estimate_id': est_id,
                'estimate_number': est_num,
                'total': total
            })
        else:
            return jsonify({'status': 'error', 'message': res_data.get('message', 'Failed to create estimate in Zoho.')})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/quoteform/pdf/<estimate_id>')
def quoteform_pdf(estimate_id):
    if not qf_logged_in():
        return "Unauthorized", 401
    try:
        url = f"{ZOHO_BASE_URL}/estimates/{estimate_id}"
        headers = zoho_headers()
        headers['Accept'] = 'application/pdf'
        r = requests.get(url, headers=headers, params={'accept': 'pdf'}, timeout=20)
        return Response(r.content, mimetype='application/pdf')
    except Exception as e:
        return f"Error fetching PDF: {e}", 500

# -----------------------------------------------------------------------------
# HTML Templates
# -----------------------------------------------------------------------------
HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head><title>QuoteBot Dashboard</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="bg-light p-4">
<div class="container bg-white p-4 rounded shadow-sm">
    <h2>QuoteBot & QuoteForm System</h2>
    <p class="text-muted">Automated Quotation Generation & Zoho Invoice Bridge</p>
    <a href="/quoteform" class="btn btn-primary">Go to QuoteForm Portal</a>
</div>
</body>
</html>"""

LOGIN_TEMPLATE = """<!DOCTYPE html>
<html>
<head><title>QuoteForm Login</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="bg-light d-flex align-items-center justify-content-center" style="height:100vh;">
<div class="card p-4 shadow-sm" style="width:350px;">
    <h4 class="card-title text-center mb-3">QuoteForm Access</h4>
    {% if error %}<div class="alert alert-danger">{{ error }}</div>{% endif %}
    <form method="POST">
        <div class="mb-3">
            <label class="form-label">Enter PIN</label>
            <input type="password" name="pin" class="form-control" required autofocus>
        </div>
        <button type="submit" class="btn btn-primary w-100">Unlock</button>
    </form>
</div>
</body>
</html>"""

QUOTEFORM_TEMPLATE = """<!DOCTYPE html>
<html>
<head><title>QuoteForm Portal</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
</head>
<body class="bg-light p-4">
<div class="container bg-white p-4 rounded shadow-sm">
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h2>QuoteForm Portal</h2>
        <a href="/quoteform/logout" class="btn btn-outline-danger btn-sm">Logout</a>
    </div>

    <div class="mb-3">
        <label class="form-label fw-bold">Select Customer/Store</label>
        <select id="customerSelect" class="form-select"><option value="">Loading customers...</option></select>
    </div>

    <div class="mb-3">
        <label class="form-label fw-bold">Bulk Paste Items</label>
        <textarea id="bulkInput" class="form-control" rows="5" placeholder="Paste raw item list here (e.g., Ceara Long Pillar Cock – 2,400)"></textarea>
        <button id="btnParse" class="btn btn-secondary mt-2">Parse List via AI</button>
    </div>

    <table class="table table-bordered mt-4" id="itemsTable">
        <thead>
            <tr><th>Item Description</th><th>Qty</th><th>Unit Price (₹)</th><th>Action</th></tr>
        </thead>
        <tbody></tbody>
    </table>

    <div class="row align-items-center mt-3">
        <div class="col-md-3">
            <label class="form-label fw-bold">Markup Percentage (%)</label>
            <input type="number" id="markupInput" class="form-control" value="0">
        </div>
        <div class="col-md-9 text-end">
            <button id="btnSubmit" class="btn btn-success btn-lg">Generate Estimate in Zoho</button>
        </div>
    </div>
</div>

<script>
$(document).ready(function() {
    $.get('/api/customers', function(res) {
        if(res.status === 'success') {
            let html = '<option value="">Select a Customer...</option>';
            res.customers.forEach(c => {
                html += `<option value="${c.customer_id}">${c.contact_name}</option>`;
            });
            $('#customerSelect').html(html);
        }
    });

    $('#btnParse').click(function() {
        let txt = $('#bulkInput').val();
        if(!txt) return alert('Please paste items first.');
        $(this).prop('disabled', true).text('Parsing...');

        $.ajax({
            url: '/quoteform/parse-items',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({ text: txt }),
            success: function(res) {
                $('#btnParse').prop('disabled', false).text('Parse List via AI');
                if(res.status === 'success') {
                    res.items.forEach(it => {
                        appendRow(it.name, it.quantity, it.unit_price);
                    });
                    $('#bulkInput').val('');
                } else {
                    alert('Error: ' + res.message);
                }
            },
            error: function() {
                $('#btnParse').prop('disabled', false).text('Parse List via AI');
                alert('Request failed');
            }
        });
    });

    function appendRow(name, qty, price) {
        let row = `<tr>
            <td><input type="text" class="form-control item-name" value="${name}"></td>
            <td><input type="number" class="form-control item-qty" value="${qty}"></td>
            <td><input type="number" class="form-control item-price" value="${price}"></td>
            <td><button class="btn btn-danger btn-sm" onclick="$(this).closest('tr').remove()">Remove</button></td>
        </tr>`;
        $('#itemsTable tbody').append(row);
    }

    $('#btnSubmit').click(function() {
        let cid = $('#customerSelect').val();
        let cname = $('#customerSelect option:selected').text();
        if(!cid) return alert('Please select a customer.');

        let items = [];
        $('#itemsTable tbody tr').each(function() {
            items.push({
                name: $(this).find('.item-name').val(),
                quantity: $(this).find('.item-qty').val(),
                unit_price: $(this).find('.item-price').val()
            });
        });

        if(items.length === 0) return alert('No items added.');

        $(this).prop('disabled', true).text('Creating Estimate...');

        $.ajax({
            url: '/quoteform/submit',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({
                customer_id: cid,
                customer_name: cname,
                items: items,
                markup: $('#markupInput').val()
            }),
            success: function(res) {
                $('#btnSubmit').prop('disabled', false).text('Generate Estimate in Zoho');
                if(res.status === 'success') {
                    alert('Estimate ' + res.estimate_number + ' created successfully!');
                    window.open('/quoteform/pdf/' + res.estimate_id, '_blank');
                } else {
                    alert('Error: ' + res.message);
                }
            }
        });
    });
});
</script>
</body>
</html>"""

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)