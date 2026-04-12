from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import pypdf
import pdfplumber
import zipfile
import io
import re
import os
import json

app = Flask(__name__)
CORS(app)

def extract_client_name(text):
    """
    Extract resident name. Tries Client field first (most reliable),
    falls back to To: field.
    Examples in PDF:
      "Client AIZIC, DAVID (236024004)"
      "Client Antman, Miriam (236019040)"
      "To: Bishop, Lori"
    """
    # PRIMARY: Client field — catches all cases in this PDF format
    m = re.search(r'Client\s+([A-Za-z][A-Za-z\'\-]+(?:,\s*[A-Za-z][A-Za-z\'\-. ]+)?)\s*\(', text)
    if m:
        return m.group(1).strip()
    # FALLBACK: To: field
    m = re.search(r'To:\s*([A-Za-z][A-Za-z\'\-]+(?:,\s*[A-Za-z][A-Za-z\'\- ]+)?)', text)
    if m:
        name = m.group(1).strip()
        name = re.sub(r'\s+(ON|BC|AB|QC|MB|SK|NS|NB|PE|NL|NT|NU|YT)$', '', name).strip()
        return name
    return None

def format_name(raw):
    """'AIZIC, DAVID' -> 'AIZIC DAVID'"""
    return re.sub(r'\s+', ' ', raw.replace(',', '')).strip()

def safe_filename(name):
    return re.sub(r'[\/\\?%*:|"<>]', '-', name).strip()

def scan_groups(pdf_bytes):
    """
    Use pdfplumber for reliable text extraction.
    Returns (groups, total_pages).
    """
    groups = []
    current = None

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        total_pages = len(pdf.pages)

        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""

            # Detect Page X of Y
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

    # Fallback: one group per page
    if not groups:
        groups = [{'name': None, 'pages': [i], 'total_expected': 1, 'auto_detected': False}
                  for i in range(total_pages)]

    # Assign fallback names
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
        groups, total_pages = scan_groups(pdf_bytes)
        return jsonify({
            'total_pages': total_pages,
            'total_residents': len(groups),
            'groups': groups
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/split', methods=['POST'])
def split_pdf():
    """Split PDF into one file per resident and return as ZIP."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'File must be a PDF'}), 400
    try:
        pdf_bytes = file.read()

        # Get custom names from frontend (edited by user)
        custom_names = {}
        if 'names' in request.form:
            custom_names = json.loads(request.form['names'])

        groups, total_pages = scan_groups(pdf_bytes)

        # Apply any edited names
        for idx, g in enumerate(groups):
            if str(idx) in custom_names:
                g['name'] = custom_names[str(idx)]

        # Split using pypdf (native — original quality)
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
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
