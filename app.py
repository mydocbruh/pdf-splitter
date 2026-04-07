from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import pypdf
import zipfile
import io
import re
import os

app = Flask(__name__)
CORS(app)

def extract_to_name(text):
    match = re.search(r'To:\s*([A-Za-z][A-Za-z\'\-]+,\s*[A-Za-z][A-Za-z\'\- ]+)', text)
    if match:
        name = match.group(1).strip()
        name = re.sub(r'\s+(ON|BC|AB|QC|MB|SK|NS|NB|PE|NL|NT|NU|YT)$', '', name).strip()
        return name
    return None

def format_name(raw):
    return re.sub(r'\s+', ' ', raw.replace(',', '')).strip()

def safe_filename(name):
    return re.sub(r'[\/\\?%*:|"<>]', '-', name).strip()

def scan_groups(reader):
    total_pages = len(reader.pages)
    groups = []
    current = None

    for i in range(total_pages):
        page = reader.pages[i]
        text = page.extract_text() or ''

        pm = re.search(r'[Pp]age\s+(\d+)\s+of\s+(\d+)', text)
        page_num = int(pm.group(1)) if pm else None
        page_of  = int(pm.group(2)) if pm else None
        is_first = (not pm) or (page_num == 1)

        raw_name    = extract_to_name(text)
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
    """Scan PDF and return resident groups without splitting."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'File must be a PDF'}), 400
    try:
        pdf_bytes = file.read()
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        groups, total_pages = scan_groups(reader)
        return jsonify({
            'total_pages': total_pages,
            'total_residents': len(groups),
            'groups': groups
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/split', methods=['POST'])
def split_pdf():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'File must be a PDF'}), 400
    try:
        pdf_bytes = file.read()
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))

        # Get names from form data (edited by user) or auto-detect
        import json
        custom_names = {}
        if 'names' in request.form:
            custom_names = json.loads(request.form['names'])

        groups, total_pages = scan_groups(reader)

        # Apply any custom names from the frontend
        for idx, g in enumerate(groups):
            if str(idx) in custom_names:
                g['name'] = custom_names[str(idx)]

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
