import io
import re
from typing import List, Dict
from flask import Flask, request, jsonify, send_from_directory
import pdfplumber
from werkzeug.utils import secure_filename

# --- Flask App Setup ---
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB limit

# --- Constants ---
WEEKLY_HEADER = "Weekly Employee Timesheet"
NAME_LABEL = "Name"

# Pre-compiled regexes
DATE_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4}$")         # e.g. 16 Jul 25
TIME_RE = re.compile(r"^\d{1,2}[:.]\d{2}$")                       # e.g. 8:00 or 16:30
NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)?$")                      # e.g. 8, 8.0, 7.5


def extract_records(file_stream: io.BytesIO) -> List[Dict]:
    """
    Extracts timesheet records from a PDF file stream.
    Parses employee name, date, day, and hours (regular + OT).
    """
    records = []
    with pdfplumber.open(file_stream) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if WEEKLY_HEADER not in text:
                continue

            # --- Name Extraction ---
            name = None
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            for i, line in enumerate(lines):
                if line.lower() == NAME_LABEL.lower() and i + 1 < len(lines):
                    cand = lines[i + 1].strip()
                    if len(cand) > 2:
                        name = cand
                        break
            if not name:
                for line in lines:
                    m = re.search(r"Name[:\s]+(.+)$", line, re.IGNORECASE)
                    if m:
                        name = m.group(1).strip()
                        break

            # --- Table Extraction (use visible lines strategy) ---
            table_settings = {
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
            }
            tables = page.extract_tables(table_settings)

            for table in tables:
                header_row_index = -1

                # Find header row that contains Date, Day, Total Hour
                for i, row in enumerate(table):
                    row_text = " ".join(str(cell or "") for cell in row).lower()
                    if "date" in row_text and "day" in row_text and "total hour" in row_text:
                        header_row_index = i
                        break

                if header_row_index == -1:
                    continue

                # Data rows start after the header row
                start_row = header_row_index + 1

                for row in table[start_row:]:
                    cleaned_row = [(cell or "").strip() for cell in row]

                    if not any(cleaned_row) or len(cleaned_row) < 2:
                        continue

                    # Stop at the footer
                    if "prepared by" in " ".join(cleaned_row).lower():
                        break

                    date_str = cleaned_row[0]
                    if not DATE_RE.match(date_str):
                        continue

                    # Default
                    hours_str = ""

                    # Handle Annual Leave explicitly
                    if re.search(r"annual\s*leave", " ".join(cleaned_row), re.IGNORECASE):
                        hours_str = "Annual Leave"
                    else:
                        # Build tokens starting after Date and Day
                        tokens: List[str] = []
                        for cell in cleaned_row[2:]:
                            if cell:
                                tokens.extend(cell.split())

                        # Find time tokens (IN/OUT)
                        time_indices = [i for i, t in enumerate(tokens) if TIME_RE.match(t)]

                        reg_hours, ot_hours = None, None

                        if len(time_indices) >= 1:
                            # Choose OUT index: prefer second time token if available, otherwise first
                            out_idx = time_indices[1] if len(time_indices) >= 2 else time_indices[0]

                            # Look for numeric tokens AFTER the OUT time
                            numeric_after = []
                            for tok in tokens[out_idx + 1:]:
                                tok_clean = tok.strip().strip(",.")
                                if NUMBER_RE.match(tok_clean):
                                    numeric_after.append(tok_clean.replace(",", "."))
                                    if len(numeric_after) >= 2:
                                        break

                            if numeric_after:
                                reg_hours = numeric_after[0]
                            if len(numeric_after) >= 2:
                                ot_hours = numeric_after[1]
                        else:
                            # Fallback: scan tokens after Date/Day for the first numeric tokens
                            numeric_tokens = []
                            for tok in tokens:
                                tok_clean = tok.strip().strip(",.")
                                if NUMBER_RE.match(tok_clean):
                                    numeric_tokens.append(tok_clean.replace(",", "."))
                                    if len(numeric_tokens) >= 2:
                                        break
                            if numeric_tokens:
                                reg_hours = numeric_tokens[0]
                            if len(numeric_tokens) >= 2:
                                ot_hours = numeric_tokens[1]

                        parts = []
                        if reg_hours:
                            parts.append(f"{reg_hours} (Regular)")
                        if ot_hours:
                            parts.append(f"{ot_hours} (OT)")
                        hours_str = " | ".join(parts)

                    records.append({
                        "name": name or "",
                        "date": date_str,
                        "day": cleaned_row[1] if len(cleaned_row) > 1 else "",
                        "hours": hours_str
                    })

    return records


# --- Flask Routes ---
@app.route("/")
def home():
    return send_from_directory(".", "index.html")


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/extract", methods=["POST"])
def extract():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    f = request.files["file"]
    filename = secure_filename(f.filename or "")
    if not filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are allowed"}), 400
    try:
        file_stream = io.BytesIO(f.read())
        records = extract_records(file_stream)
        return jsonify({"records": records})
    except Exception as e:
        app.logger.error(f"Extraction failed: {e}")
        return jsonify({"error": "An unexpected error occurred during PDF processing."}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
