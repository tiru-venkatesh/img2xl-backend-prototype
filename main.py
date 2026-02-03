IS_PRODUCTION = os.environ.get("RAILWAY_ENVIRONMENT") is not None
import os
import re
import shutil
import uuid
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pypdf import PdfReader

# ---------------- OCR SAFE IMPORT ----------------
OCR_AVAILABLE = False

if not IS_PRODUCTION:
    try:
        import pytesseract
        from pdf2image import convert_from_path
        OCR_AVAILABLE = True
    except Exception:
        OCR_AVAILABLE = False


# Optional Tesseract path (Windows)
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if OCR_AVAILABLE and os.path.exists(TESSERACT_PATH):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

# ---------------- APP ----------------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # allow all for development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


BASE_DIR = "uploads"
PDF_DIR = os.path.join(BASE_DIR, "pdfs")
os.makedirs(PDF_DIR, exist_ok=True)

# ---------------- PAGE-LEVEL EXTRACTION ----------------
def analyze_text(text: str):
    return {
        "application_numbers": re.findall(r"\b\d{10,}\b", text),
        "ip_addresses": re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text),
        "dates": re.findall(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}-\d{2}-\d{4}\b", text),
        "times": re.findall(r"\b\d{2}:\d{2}(?::\d{2})?\b", text)
    }

# ---------------- SUMMARY ENGINE ----------------
def summarize_analysis(analysis):
    summary = {
        "pages_scanned": len(analysis),
        "text_layer_pages": [],
        "ocr_success_pages": [],
        "unique_application_like_numbers": set(),
        "unique_dates": set(),
        "unique_times": set(),
        "unique_ip_addresses": set(),
        "unique_uppercase_phrases": set()
    }

    for page in analysis:
        if page["text_layer_present"]:
            summary["text_layer_pages"].append(page["page"])

        if page["ocr_status"] == "success":
            summary["ocr_success_pages"].append(page["page"])

        details = page["details"]

        summary["unique_application_like_numbers"].update(details.get("application_numbers", []))
        summary["unique_dates"].update(details.get("dates", []))
        summary["unique_times"].update(details.get("times", []))
        summary["unique_ip_addresses"].update(details.get("ip_addresses", []))

        uppercase = re.findall(r"\b[A-Z][A-Z\s]{4,}\b", page["combined_text"])
        summary["unique_uppercase_phrases"].update(uppercase)

    # Convert sets → sorted lists
    for key in summary:
        if isinstance(summary[key], set):
            summary[key] = sorted(summary[key])

    return summary

# ---------------- HUMAN-READABLE SUMMARY ----------------
def generate_paragraph_summary(summary):
    lines = []

    lines.append(
        f"The document contains {summary['pages_scanned']} page(s). "
        f"Text content was detected on pages {summary['text_layer_pages']}."
    )

    if summary["ocr_success_pages"]:
        lines.append(
            f"Optical character recognition (OCR) successfully processed "
            f"pages {summary['ocr_success_pages']}."
        )

    if summary["unique_application_like_numbers"]:
        lines.append(
            f"The document includes {len(summary['unique_application_like_numbers'])} "
            f"long numeric identifier(s), suggesting reference or ID-like values."
        )

    if summary["unique_dates"]:
        lines.append(
            f"Date values such as {', '.join(summary['unique_dates'][:3])} were detected."
        )

    if summary["unique_times"]:
        lines.append(
            f"Time values such as {', '.join(summary['unique_times'][:3])} appear in the document."
        )

    if summary["unique_ip_addresses"]:
        lines.append(
            f"One or more IP address values were detected, indicating system-generated metadata."
        )

    if summary["unique_uppercase_phrases"]:
        sample = summary["unique_uppercase_phrases"][:4]
        lines.append(
            f"Prominent uppercase text blocks were identified, including "
            f"{', '.join(sample)}, which may represent headings, organizations, or names."
        )

    return " ".join(lines)

# ---------------- API ----------------
def detect_document_type(summary, analysis):
    scores = {
        "application_form": 0,
        "invoice_or_payment": 0,
        "government_notice": 0,
        "identity_document": 0,
        "educational_record": 0
    }

    reasons = {k: [] for k in scores}

    # ---- Application form signals ----
    if summary["unique_application_like_numbers"]:
        scores["application_form"] += 2
        reasons["application_form"].append("Contains long numeric identifiers")

    for phrase in summary["unique_uppercase_phrases"]:
        if "APPLICATION" in phrase or "CONFIRMATION" in phrase:
            scores["application_form"] += 2
            reasons["application_form"].append("Contains application-related headings")

    # ---- Invoice / payment signals ----
    for page in analysis:
        if "Amount" in page["combined_text"] or "Payment" in page["combined_text"]:
            scores["invoice_or_payment"] += 2
            reasons["invoice_or_payment"].append("Payment-related keywords found")

    # ---- Government notice signals ----
    for phrase in summary["unique_uppercase_phrases"]:
        if "GOVERNMENT" in phrase or "MINISTRY" in phrase:
            scores["government_notice"] += 2
            reasons["government_notice"].append("Government-related entities found")

    # ---- Educational record signals ----
    for page in analysis:
        if "Marks" in page["combined_text"] or "Examination" in page["combined_text"]:
            scores["educational_record"] += 1
            reasons["educational_record"].append("Education-related terms found")

    # ---- Identity document signals ----
    for page in analysis:
        if "Aadhaar" in page["combined_text"] or "Passport" in page["combined_text"]:
            scores["identity_document"] += 2
            reasons["identity_document"].append("Identity-related keywords found")

    # ---- Pick best ----
    best_type = max(scores, key=scores.get)
    best_score = scores[best_type]

    if best_score == 0:
        return {
            "document_type": "unknown",
            "confidence": 0.0,
            "reasoning": ["No strong signals detected"]
        }

    confidence = min(1.0, best_score / 5)

    return {
        "document_type": best_type,
        "confidence": round(confidence, 2),
        "reasoning": reasons[best_type]
    }
def assess_document_quality(summary, analysis):
    reasons = []

    total_pages = summary["pages_scanned"]
    ocr_pages = len(summary["ocr_success_pages"])
    text_pages = len(summary["text_layer_pages"])

    # ---------- OCR QUALITY ----------
    ocr_ratio = ocr_pages / total_pages if total_pages else 0
    if ocr_ratio > 0.8:
        ocr_quality = "high"
        reasons.append("OCR succeeded on most pages")
    elif ocr_ratio > 0.4:
        ocr_quality = "medium"
        reasons.append("OCR partially succeeded")
    else:
        ocr_quality = "low"
        reasons.append("OCR failed on many pages")

    # ---------- TEXT NOISE ----------
    uppercase_count = len(summary["unique_uppercase_phrases"])
    if uppercase_count > 40:
        text_noise = "high"
        reasons.append("High amount of OCR noise detected")
    elif uppercase_count > 15:
        text_noise = "medium"
        reasons.append("Moderate OCR noise detected")
    else:
        text_noise = "low"
        reasons.append("Low OCR noise")

    # ---------- NUMERIC DENSITY ----------
    numeric_count = len(summary["unique_application_like_numbers"]) + len(summary["unique_dates"])
    if numeric_count > 6:
        numeric_density = "high"
        reasons.append("Document contains many structured numeric values")
    elif numeric_count > 2:
        numeric_density = "medium"
        reasons.append("Some structured numeric values detected")
    else:
        numeric_density = "low"
        reasons.append("Few structured numeric values detected")

    # ---------- OVERALL CONFIDENCE ----------
    score = 0
    score += 0.4 if ocr_quality == "high" else 0.25 if ocr_quality == "medium" else 0.1
    score += 0.3 if numeric_density == "high" else 0.2 if numeric_density == "medium" else 0.1
    score += 0.2 if text_noise == "low" else 0.1
    score += 0.1 if text_pages == total_pages else 0.05

    score = round(min(score, 1.0), 2)

    # ---------- DECISION ----------
    if score >= 0.75:
        action = "auto_process"
    elif score >= 0.5:
        action = "manual_review_recommended"
    else:
        action = "manual_review_required"

    return {
        "ocr_quality": ocr_quality,
        "text_noise": text_noise,
        "numeric_density": numeric_density,
        "overall_confidence": score,
        "recommended_action": action,
        "reasoning": reasons
    }

app.mount("/static", StaticFiles(directory="static"), name="static")
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    if file.content_type != "application/pdf":
        return {"error": "Only PDF files are allowed"}

    doc_id = str(uuid.uuid4())
    pdf_path = os.path.join(PDF_DIR, f"{doc_id}.pdf")

    with open(pdf_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    reader = PdfReader(pdf_path)
    analysis = []

    for i, page in enumerate(reader.pages):
        text_layer = page.extract_text() or ""
        ocr_text = ""
        ocr_status = "skipped"

        if OCR_AVAILABLE:
            try:
                images = convert_from_path(pdf_path, first_page=i+1, last_page=i+1)
                ocr_text = pytesseract.image_to_string(images[0])
                ocr_status = "success"
            except Exception:
                ocr_status = "failed"

        combined_text = (text_layer + "\n" + ocr_text).strip()

        analysis.append({
            "page": i + 1,
            "text_layer_present": bool(text_layer.strip()),
            "ocr_status": ocr_status,
            "combined_text": combined_text,
            "details": analyze_text(combined_text)
        })

    summary = summarize_analysis(analysis)
    human_summary = generate_paragraph_summary(summary)
    document_type_info = detect_document_type(summary, analysis)
    quality = assess_document_quality(summary, analysis)


    return {
    "document_id": doc_id,
    "filename": file.filename,
    "total_pages": len(reader.pages),
    "ocr_available": OCR_AVAILABLE,
    "document_type": document_type_info,
    "quality": quality,
    "summary": summary,
    "human_summary": human_summary,
    "analysis": analysis
}



