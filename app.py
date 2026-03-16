"""
PDFForge Server — PyMuPDF-powered PDF editing
Handles text replacement with exact font, color, and size matching.
Deploy on Render.com (free tier).
"""

import fitz  # PyMuPDF
import base64
import json
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

# Broad CORS — allow everything
CORS(app,
     origins="*",
     methods=["GET", "POST", "OPTIONS"],
     allow_headers=["Content-Type", "Accept"],
     supports_credentials=False,
     max_age=86400)

@app.before_request
def handle_preflight():
    if request.method == 'OPTIONS':
        from flask import Response
        resp = Response()
        resp.headers['Access-Control-Allow-Origin']  = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Accept'
        resp.headers['Access-Control-Max-Age']       = '86400'
        return resp

@app.after_request
def after_request(response):
    response.headers['Access-Control-Allow-Origin']  = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Accept'
    return response


@app.route("/", methods=["GET"])
def health():
    """Health check — Render pings this to keep the service alive."""
    return jsonify({"status": "ok", "service": "PDFForge Server"})


@app.route("/analyze", methods=["POST"])
def analyze_pdf():
    """
    Accepts: { "pdf": "<base64 PDF>" }
    Returns: { "pages": [ [ {...span}, ... ], ... ] }

    Each span:
    {
      "page": 0,
      "text": "Hello",
      "x": 142.5, "y": 387.2,   # PDF coords (bottom-left origin)
      "w": 68.4,  "h": 18.0,
      "fontName": "TimesNewRomanPS-BoldMT",
      "fontSize": 14.0,
      "isBold": true,
      "isItalic": false,
      "color": [0, 0, 0],        # RGB 0-255
      "bgColor": [255, 255, 255] # sampled background RGB 0-255
    }
    """
    try:
        data = request.get_json()
        if not data or "pdf" not in data:
            return jsonify({"error": "Missing pdf field"}), 400

        pdf_bytes = base64.b64decode(data["pdf"])
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        all_pages = []

        for page_num in range(len(doc)):
            page = doc[page_num]
            page_height = page.rect.height
            page_width  = page.rect.width
            spans_out = []

            # Get all text with full detail
            blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

            for block in blocks.get("blocks", []):
                if block.get("type") != 0:  # type 0 = text
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "").strip()
                        if not text:
                            continue

                        bbox = span.get("bbox", [0,0,0,0])
                        # bbox is (x0, y0, x1, y1) in PyMuPDF top-left coords
                        # Convert to PDF bottom-left coords for browser
                        x = bbox[0]
                        y = page_height - bbox[3]   # flip y
                        w = bbox[2] - bbox[0]
                        h = bbox[3] - bbox[1]

                        font_name = span.get("font", "")
                        font_size = span.get("size", 12)

                        # Decode color integer to RGB
                        color_int = span.get("color", 0)
                        r = (color_int >> 16) & 0xFF
                        g = (color_int >> 8)  & 0xFF
                        b =  color_int        & 0xFF

                        # Detect bold/italic from font flags and name
                        flags = span.get("flags", 0)
                        is_bold   = bool(flags & 2**4) or bool(re.search(r'bold|heavy|black|semibold', font_name, re.I))
                        is_italic = bool(flags & 2**1) or bool(re.search(r'italic|oblique|slanted', font_name, re.I))

                        # Sample background by rendering a small strip above the bbox
                        try:
                            # Sample 3px strip above the text bbox
                            sample_rect = fitz.Rect(x, bbox[1] - 4, x + min(w, 20), bbox[1] - 1)
                            sample_rect = sample_rect & page.rect  # clip to page
                            if sample_rect.is_empty:
                                # Try below
                                sample_rect = fitz.Rect(x, bbox[3] + 1, x + min(w, 20), bbox[3] + 4)
                                sample_rect = sample_rect & page.rect
                            if not sample_rect.is_empty:
                                mat = fitz.Matrix(2, 2)
                                pix = page.get_pixmap(matrix=mat, clip=sample_rect, alpha=False)
                                # Average all pixels in sample
                                samples_r, samples_g, samples_b, n = 0, 0, 0, 0
                                for px in range(pix.width):
                                    for py in range(pix.height):
                                        pixel = pix.pixel(px, py)
                                        samples_r += pixel[0]; samples_g += pixel[1]; samples_b += pixel[2]; n += 1
                                if n > 0:
                                    bg_color = [round(samples_r/n), round(samples_g/n), round(samples_b/n)]
                                else:
                                    bg_color = [255, 255, 255]
                            else:
                                bg_color = [255, 255, 255]
                        except Exception:
                            bg_color = [255, 255, 255]

                        spans_out.append({
                            "page": page_num,
                            "text": text,
                            "x": round(x, 2), "y": round(y, 2),
                            "w": round(w, 2), "h": round(h, 2),
                            "fontName": font_name,
                            "fontSize": round(font_size, 2),
                            "isBold": is_bold,
                            "isItalic": is_italic,
                            "color": [r, g, b],
                            "bgColor": bg_color
                        })

            all_pages.append(spans_out)

        doc.close()
        return jsonify({"pages": all_pages})

    except Exception as e:
        print(f"Analyze error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/edit", methods=["POST"])
def edit_pdf():
    """
    Accepts JSON:
    {
      "pdf": "<base64-encoded PDF bytes>",
      "edits": [ ... ]
    }
    Returns: { "pdf": "<base64-encoded edited PDF>" }
    """
    try:
        data = request.get_json()
        if not data or "pdf" not in data:
            return jsonify({"error": "Missing pdf field"}), 400

        # Decode PDF
        pdf_bytes = base64.b64decode(data["pdf"])
        edits = data.get("edits", [])

        if not edits:
            return jsonify({"error": "No edits provided"}), 400

        # Open with PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        for edit in edits:
            edit_type = edit.get("type", "text")
            page_num = edit.get("page", 0)

            if page_num >= len(doc):
                continue

            page = doc[page_num]
            page_height = page.rect.height

            # ── Convert coordinates ──
            # Browser sends PDF coords (bottom-left origin, y up)
            # PyMuPDF uses top-left origin (y down)
            # Conversion: mupdf_y = page_height - pdf_y - bbox_height
            x = edit.get("x", 0)
            y = edit.get("y", 0)
            w = edit.get("w", 0)
            h = edit.get("h", 0)

            # Build PyMuPDF rect (top-left origin)
            rect = fitz.Rect(
                x,
                page_height - y - h,
                x + w,
                page_height - y
            )

            if edit_type == "text":
                new_text = edit.get("newText", "")
                if not new_text:
                    continue

                # ── Step 1: Find exact text properties from PDF ──
                font_name = None
                font_size = None
                text_color = None

                # Search for original text in a slightly expanded rect
                search_rect = rect + (-2, -2, 2, 2)  # expand by 2px each side
                blocks = page.get_text("dict", clip=search_rect)

                for block in blocks.get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            span_text = span.get("text", "").strip()
                            orig_text = edit.get("originalText", "").strip()
                            # Match if span contains the original text
                            if orig_text and (orig_text in span_text or span_text in orig_text):
                                font_name = span.get("font", None)
                                font_size = span.get("size", None)
                                color_int = span.get("color", 0)
                                # Convert integer color to RGB tuple (0-1 scale)
                                r = ((color_int >> 16) & 0xFF) / 255.0
                                g = ((color_int >> 8) & 0xFF) / 255.0
                                b = (color_int & 0xFF) / 255.0
                                text_color = (r, g, b)
                                break
                        if font_name:
                            break
                    if font_name:
                        break

                # ── Step 2: Sample background color from page ──
                # Render the page to a small pixmap and sample the bbox area
                try:
                    # Render at 2x for better color accuracy
                    mat = fitz.Matrix(2, 2)
                    clip = rect + (-1, -1, 1, 1)
                    pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)

                    # Sample edges of the pixmap to get background
                    # (center pixels are likely text)
                    w_px, h_px = pix.width, pix.height
                    bg_samples = []

                    # Top row, bottom row
                    for px_x in range(0, w_px, max(1, w_px // 8)):
                        if h_px > 0:
                            pixel = pix.pixel(min(px_x, w_px-1), 0)
                            bg_samples.append(pixel[:3])
                        if h_px > 1:
                            pixel = pix.pixel(min(px_x, w_px-1), h_px-1)
                            bg_samples.append(pixel[:3])

                    if bg_samples:
                        avg_r = sum(s[0] for s in bg_samples) / len(bg_samples) / 255.0
                        avg_g = sum(s[1] for s in bg_samples) / len(bg_samples) / 255.0
                        avg_b = sum(s[2] for s in bg_samples) / len(bg_samples) / 255.0
                        bg_color = (avg_r, avg_g, avg_b)
                    else:
                        bg_color = (1.0, 1.0, 1.0)  # white fallback
                except Exception:
                    bg_color = (1.0, 1.0, 1.0)

                # ── Step 3: Cover original text ──
                page.draw_rect(
                    rect + (-0.5, -0.5, 0.5, 0.5),
                    color=bg_color,
                    fill=bg_color,
                    overlay=True
                )

                # ── Step 4: Insert replacement text ──
                # Use exact font if found, otherwise fallback
                insert_color = text_color if text_color else (0, 0, 0)
                insert_size = font_size if font_size else edit.get("fontSizePDF", 12)
                insert_size = max(4, insert_size * 0.95)

                # Text baseline position in PyMuPDF coords
                # Insert point = bottom-left of text = top of rect + most of height
                insert_point = fitz.Point(rect.x0, rect.y1 - rect.height * 0.15)

                if font_name:
                    try:
                        # Try to use the exact embedded font
                        page.insert_text(
                            insert_point,
                            new_text,
                            fontname=font_name,
                            fontsize=insert_size,
                            color=insert_color,
                            overlay=True
                        )
                    except Exception:
                        # Font not available — use best fallback
                        _insert_with_fallback(page, insert_point, new_text,
                                              insert_size, insert_color, edit)
                else:
                    _insert_with_fallback(page, insert_point, new_text,
                                          insert_size, insert_color, edit)

            elif edit_type == "highlight":
                page.draw_rect(rect, color=(1, 0.87, 0), fill=(1, 0.87, 0),
                               fill_opacity=0.45, overlay=True)

            elif edit_type == "redact":
                page.draw_rect(rect, color=(0, 0, 0), fill=(0, 0, 0), overlay=True)

            elif edit_type == "addtext":
                text = edit.get("text", "")
                size = edit.get("size", 12)
                color = edit.get("color", [0, 0, 0])
                point = fitz.Point(x, page_height - y)
                try:
                    page.insert_text(point, text, fontsize=size,
                                     color=tuple(color), overlay=True)
                except Exception as e:
                    print(f"addtext error: {e}")

        # Save to bytes
        out_bytes = doc.tobytes(deflate=True, garbage=3)
        doc.close()

        # Return as base64
        result_b64 = base64.b64encode(out_bytes).decode("utf-8")
        return jsonify({"pdf": result_b64})

    except Exception as e:
        print(f"Edit error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _insert_with_fallback(page, point, text, size, color, edit):
    """Insert text using best available fallback font."""
    is_bold = edit.get("isBold", False)
    is_italic = edit.get("isItalic", False)
    font_family = edit.get("fontResPdf", "helvetica")

    # Map to PyMuPDF built-in font names
    if font_family == "times":
        if is_bold and is_italic:
            fontname = "tibo"   # Times Bold Italic
        elif is_bold:
            fontname = "tibo"   # Times Bold (tibo = Times Bold)
        elif is_italic:
            fontname = "tiit"   # Times Italic
        else:
            fontname = "tiro"   # Times Roman
    elif font_family == "courier":
        fontname = "cobo" if is_bold else "cour"
    else:
        # Helvetica family
        if is_bold and is_italic:
            fontname = "heob"   # Helvetica Bold Oblique
        elif is_bold:
            fontname = "hebo"   # Helvetica Bold
        elif is_italic:
            fontname = "heit"   # Helvetica Italic
        else:
            fontname = "helv"   # Helvetica

    try:
        page.insert_text(point, text, fontname=fontname,
                         fontsize=size, color=color, overlay=True)
    except Exception:
        # Last resort — plain helvetica
        page.insert_text(point, text, fontname="helv",
                         fontsize=size, color=color, overlay=True)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
