import io, uuid, base64, time
import fitz  # PyMuPDF
from PIL import Image
from flask import Blueprint, render_template, request, jsonify, send_file, url_for
import time


editor_bp = Blueprint(
    "editor",
    __name__,
    template_folder="templates",
    static_folder="static",
)

# In-Memory-Speicher: token -> {bytes, created}
PDF_TTL_SECONDS = 60 * 60  # 1h
pdf_storage = {}

# ---------- Helpers ----------

def _cleanup_expired():
    now = time.time()
    to_del = [t for t, v in pdf_storage.items() if now - v['created'] > PDF_TTL_SECONDS]
    for t in to_del:
        pdf_storage.pop(t, None)


def _is_pdf(data: bytes) -> bool:
    # simple magic-less check + header
    return data[:4] == b'%PDF'


# ---------- Routes ----------

@editor_bp.route("/", methods=["GET"])
def editor():
    # Cache-Busting für CSS/JS während du entwickelst
    return render_template("editor.html", cache_bust=int(time.time()))


@editor_bp.route('/upload', methods=['POST'])
def upload_file():
    _cleanup_expired()
    if 'file' not in request.files:
        return jsonify({"message": "Keine Datei hochgeladen"}), 400

    f = request.files['file']
    if not f or not f.filename.lower().endswith('.pdf'):
        return jsonify({"message": "Bitte eine PDF-Datei wählen"}), 400

    try:
        data = f.read()
        if not _is_pdf(data):
            return jsonify({"message": "Nur echte PDF-Dateien erlaubt"}), 400
        # open to ensure valid
        fitz.open(stream=data, filetype='pdf').close()
    except Exception as e:
        return jsonify({"message": "Ungültiges/korruptes PDF", "error": str(e)}), 400

    token = str(uuid.uuid4())
    pdf_storage[token] = { 'bytes': data, 'created': time.time() }

    return jsonify({
        "message": "PDF hochgeladen!",
        "pdf_token": token,
        "pdf_url": url_for('editor.get_pdf', token=token, _external=True)
    })


@editor_bp.route('/pdf/<token>', methods=['GET'])
def get_pdf(token):
    entry = pdf_storage.get(token)
    if not entry:
        return jsonify({"message": "PDF nicht gefunden"}), 404
    return send_file(io.BytesIO(entry['bytes']), mimetype='application/pdf')


@editor_bp.route('/suggest', methods=['POST'])
def suggest_positions():
    data = request.get_json(silent=True) or {}
    token = data.get('pdf_token')
    entry = pdf_storage.get(token)
    if not entry:
        return jsonify({"message": "PDF nicht gefunden"}), 404

    try:
        doc = fitz.open(stream=entry['bytes'], filetype='pdf')
        suggestions = []  # list of {page, nx, ny, nw, nh}
        KEYWORDS = ["Unterschrift", "Signatur", "Signature", "Datum", "Ort"]
        W, H = 160, 45  # default signature box in points
        M = 14
        for i, page in enumerate(doc, start=1):
            page_w, page_h = page.rect.width, page.rect.height
            added = False

            # 1) vorhandene Signature-Widgets
            try:
                for wdg in page.widgets() or []:
                    if getattr(wdg, 'field_type', '').lower() == 'signature':
                        r = wdg.rect
                        suggestions.append({
                            'page': i,
                            'nx': r.x0 / page_w,
                            'ny': r.y0 / page_h,
                            'nw': (r.x1 - r.x0) / page_w,
                            'nh': (r.y1 - r.y0) / page_h,
                        })
                        added = True
            except Exception:
                pass

            # 2) Keyword-Labels -> Box rechts/neben/unterhalb
            if not added:
                for kw in KEYWORDS:
                    for r in page.search_for(kw):
                        x = min(max(r.x1 + 8, M), page_w - W - M)
                        y = min(max(r.y0 - (H * 0.4), M), page_h - H - M)
                        suggestions.append({
                            'page': i,
                            'nx': x / page_w,
                            'ny': y / page_h,
                            'nw': W / page_w,
                            'nh': H / page_h,
                        })
                        added = True
                        break
                    if added:
                        break

            # 3) Fallback: unten rechts
            if not added:
                x = page_w - W - 36
                y = page_h - H - 36
                suggestions.append({
                    'page': i,
                    'nx': x / page_w,
                    'ny': y / page_h,
                    'nw': W / page_w,
                    'nh': H / page_h,
                })
        doc.close()
        return jsonify({ 'suggestions': suggestions })
    except Exception as e:
        return jsonify({"message": "Fehler bei Smart Detect", "error": str(e)}), 500


@editor_bp.route('/sign', methods=['POST'])
def sign_pdf():
    data = request.get_json(silent=True) or {}
    token = data.get('pdf_token')
    placements = data.get('placements') or []
    sig_b64 = data.get('signature')

    entry = pdf_storage.get(token)
    if not entry:
        return jsonify({"message": "PDF nicht gefunden"}), 404
    if not sig_b64:
        return jsonify({"message": "Signaturdaten fehlen"}), 400
    if not placements:
        return jsonify({"message": "Keine Zielpositionen vorhanden"}), 400

    try:
        sig_bytes = base64.b64decode(sig_b64)
        sig_img = Image.open(io.BytesIO(sig_bytes)).convert('RGBA')
        # normalize DPI irrelevant – we'll scale to rect
        sig_buf = io.BytesIO(); sig_img.save(sig_buf, format='PNG')
        sig_data = sig_buf.getvalue()
    except Exception as e:
        return jsonify({"message": "Signaturbild ungültig", "error": str(e)}), 400

    try:
        doc = fitz.open(stream=entry['bytes'], filetype='pdf')
        # group placements by page
        by_page = {}
        for p in placements:
            pg = int(p['page'])
            by_page.setdefault(pg, []).append(p)

        for pg, lst in by_page.items():
            if pg < 1 or pg > doc.page_count: continue
            page = doc[pg - 1]
            pw, ph = page.rect.width, page.rect.height
            for p in lst:
                x0 = float(p['nx']) * pw
                y0 = float(p['ny']) * ph
                w = float(p['nw']) * pw
                h = float(p['nh']) * ph
                rect = fitz.Rect(x0, y0, x0 + w, y0 + h)
                page.insert_image(rect, stream=sig_data, keep_proportion=True)

        out = io.BytesIO(); doc.save(out); doc.close()
        pdf_storage[token] = { 'bytes': out.getvalue(), 'created': time.time() }
        return jsonify({
            'message': 'PDF erfolgreich signiert.',
            'signed_pdf_url': url_for('editor.download_signed_pdf', token=token, _external=True)
        })
    except Exception as e:
        return jsonify({"message": "Fehler beim Signieren", "error": str(e)}), 500


@editor_bp.route('/download/<token>', methods=['GET'])
def download_signed_pdf(token):
    entry = pdf_storage.get(token)
    if not entry:
        return jsonify({"message": "Signiertes PDF nicht gefunden"}), 404
    filename = f"{token}_signed.pdf"
    return send_file(io.BytesIO(entry['bytes']), mimetype='application/pdf', as_attachment=True, download_name=filename)
