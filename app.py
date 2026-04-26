from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import pypdf
import zipfile
import io
import re
import os
import json
import uuid
import time

app = Flask(__name__)
CORS(app)

_cache = {}
CACHE_TTL = 600

def clean_cache():
    now = time.time()
    expired = [k for k, v in _cache.items() if now - v['timestamp'] > CACHE_TTL]
    for k in expired:
        del _cache[k]

def extract_client_name(text):
    """
    Primary: match 'LASTNAME, FIRSTNAME (236024004)' — 9-digit account number
    always identifies the actual resident, not the billing contact.
    Fallback: To: field.
    """
    # Account numbers in this PDF are always 9 digits e.g. (236024004)
    m = re.search(r'([A-Za-z][A-Za-z\'\-]+,\s*[A-Za-z][A-Za-z\'\-. ]+)\s*\(\d{9}\)', text)
    if m:
        return m.group(1).strip()
    # Fallback: To: field
    m = re.search(r'To:\s*([A-Za-z][A-Za-z\'\-]+(?:,\s*[A-Za-z][A-Za-z\'\- ]+)?)', text)
    if m:
        name = m.group(1).strip()
        name = re.sub(r'\s+(ON|BC|AB|QC|MB|SK|NS|NB|PE|NL|NT|NU|YT)$', '', name).strip()
        return name
    return None

def format_name(raw):
    # Normalize spacing around comma, then apply Title Case
    name = re.sub(r'\s*,\s*', ', ', raw)
    name = re.sub(r'\s+', ' ', name).strip()
    return name.title()  # 'AIZIC, DAVID' -> 'Aizic, David'

def safe_filename(name):
    return re.sub(r'[\/\\?%*:|"<>]', '-', name).strip()

def scan_groups(pdf_bytes):
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    groups = []
    current = None

    for i in range(total_pages):
        text = reader.pages[i].extract_text() or ""
        pm = re.search(r'[Pp]age\s+(\d+)\s+of\s+(\d+)', text)
        page_num = int(pm.group(1)) if pm else None
        page_of  = int(pm.group(2)) if pm else None
        is_first = (not pm) or (page_num == 1)

        raw_name    = extract_client_name(text)
        client_name = format_name(raw_name) if raw_name else None

        if is_first:
            if current:
                groups.append(current)
            current = {
                'name': client_name,
                'pages': [i],
                'total_expected': page_of or 1,
                'auto_detected': bool(client_name)
            }
        else:
            if current:
                current['pages'].append(i)
                if client_name and not current['name']:
                    current['name'] = client_name
                    current['auto_detected'] = True
            else:
                current = {
                    'name': client_name,
                    'pages': [i],
                    'total_expected': page_of or 1,
                    'auto_detected': bool(client_name)
                }

    if current:
        groups.append(current)

    if not groups:
        groups = [{'name': None, 'pages': [i], 'total_expected': 1, 'auto_detected': False}
                  for i in range(total_pages)]

    for idx, g in enumerate(groups):
        if not g['name']:
            g['name'] = f'Resident_{idx + 1}'

    return groups, total_pages

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

@app.route('/preview', methods=['POST'])
def preview_pdf():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'File must be a PDF'}), 400
    try:
        clean_cache()
        pdf_bytes = file.read()
        groups, total_pages = scan_groups(pdf_bytes)
        session_id = str(uuid.uuid4())
        _cache[session_id] = {
            'groups': groups,
            'pdf_bytes': pdf_bytes,
            'timestamp': time.time()
        }
        return jsonify({
            'session_id': session_id,
            'total_pages': total_pages,
            'total_residents': len(groups),
            'groups': groups
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/split', methods=['POST'])
def split_pdf():
    try:
        data         = request.get_json()
        session_id   = data.get('session_id')
        custom_names = data.get('names', {})

        if not session_id or session_id not in _cache:
            return jsonify({'error': 'Session expired — please re-upload your PDF.'}), 400

        cached    = _cache[session_id]
        pdf_bytes = cached['pdf_bytes']
        groups    = cached['groups']

        for idx, g in enumerate(groups):
            if str(idx) in custom_names:
                g['name'] = custom_names[str(idx)]

        reader     = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for g in groups:
                writer = pypdf.PdfWriter()
                for page_idx in g['pages']:
                    writer.add_page(reader.pages[page_idx])
                pdf_out = io.BytesIO()
                writer.write(pdf_out)
                pdf_out.seek(0)
                filename = safe_filename(g['name']) + '.pdf'
                zf.writestr(filename, pdf_out.read())

        del _cache[session_id]
        zip_buffer.seek(0)
        return send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name='resident_statements.zip'
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
