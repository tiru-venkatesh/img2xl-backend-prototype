import os
import re
import shutil
import uuid
import pytesseract
from typing import List
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pypdf import PdfReader
from pdf2image import convert_from_path

# ---------------- CONFIGURATION ----------------
# Set to True in production to skip local Tesseract paths
IS_PRODUCTION = os.getenv("IS_PRODUCTION", "false").lower() == "true"

# Optional Tesseract path (Windows specific)
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if not IS_PRODUCTION and os.path.exists(TESSERACT_PATH):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

BASE_DIR = "uploads"
PDF_DIR = os.path.join(BASE_DIR, "pdfs")
os.makedirs(PDF_DIR, exist_ok=True)
os.makedirs("static", exist_ok=True)

# ---------------- APP SETUP ----------------
app = FastAPI(title="Img2XL Backend Prototype")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- LOGIC ENGINES ----------------

def analyze_text(text: str):
    """Extracts specific patterns like IDs, IPs, and dates using RegEx."""
    return {
        "application_numbers": re.findall(r"\b\d{10,}\b", text),
        "ip_addresses": re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text),
        "dates": re.findall(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}-\d{2}-\d{4}\b", text),
        "times": re.findall(r"\b\d{2}:\d{2}(?::\d{2})?\b", text)
    }

def summarize_analysis(analysis: List[dict]):
    """Aggregates page-level data into a document-wide summary."""
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

        # Capture noise/headings (all caps words/phrases)
        uppercase = re.findall(r"\b[A-Z][A-Z\s]{4,}\b", page["combined_text"])
        summary["unique_uppercase_phrases"].update([p.strip() for p in uppercase if p.strip()])

    # Convert sets to sorted lists for JSON serialization
    for key, value in summary.items():
        if isinstance(value, set):
            summary[key] = sorted(list(value))

    return summary

def generate_paragraph_summary(summary):
    """Converts the summary dictionary into a natural language paragraph."""
    lines = [f"The document contains {summary['pages_scanned']} page(s)."]
    
    if summary["text_layer_pages"]:
        lines.append(f"Text layer found on pages: {summary['text_layer_pages']}.")
    
    if summary["unique_application_like_numbers"]:
        lines.append(f"Detected {len(summary['unique_application_like_numbers'])} reference IDs.")

    if summary["unique_dates"]:
        lines.append(f"Key dates found: {', '.join(summary['unique_dates'][:3])}.")

    if summary["unique_uppercase_phrases"]:
        sample = summary["unique_uppercase_phrases"][:3]
        lines.append(f"Prominent headers include: {', '.join(sample)}.")

    return " ".join(lines)

def detect_document_type(summary, analysis):
    """Heuristic-based classification of the document."""
    scores = {"application_form": 0, "invoice_or_payment": 0, "government_notice": 0, "identity_document": 0, "educational_record": 0}
    reasons = {k: [] for k in scores}

    # Application signals
    if summary["unique_application_like_numbers"]:
        scores["application_form"] += 2
        reasons["application_form"].append("Long numeric IDs found")

    # Keyword signals
    full_text = " ".join([p["combined_text"] for p in analysis]).lower()
    
    keywords = {
        "invoice_or_payment": ["amount", "payment", "invoice", "total due", "tax invoice"],
        "educational_record": ["marks", "examination", "semester", "grade", "transcript"],
        "identity_document": ["aadhaar", "passport", "identity", "dob", "permanent account number"],
        "government_notice": ["government", "ministry", "official use", "department"]
    }

    for doc_type, words in keywords.items():
        for word in words:
            if word in full_text:
                scores[doc_type] += 1
                reasons[doc_type].append(f"Keyword '{word}' found")

    best_type = max(scores, key=scores.get)
    if scores[best_type] == 0:
        return {"document_type": "unknown", "confidence": 0.0, "reasoning": ["No signals detected"]}

    return {
        "document_type": best_type,
        "confidence": round(min(1.0, scores[best_type] / 5), 2),
        "reasoning": list(set(reasons[best_type]))
    }

def assess_document_quality(summary, analysis):
    """Calculates a confidence score based on OCR success and text noise."""
    reasons = []
    total_pages = summary["pages_scanned"]
    ocr_ratio = len(summary["ocr_success_pages"]) / total_pages if total_pages else 0
    
    # Simple Scoring
    score = 0.5
    if ocr_ratio > 0.8: score += 0.3; reasons.append("High OCR success rate")
    if len(summary["unique_uppercase_phrases"]) > 30: score -= 0.2; reasons.append("High text noise detected")
    
    action = "auto_process" if score >= 0.75 else "manual_review_recommended" if score >= 0.5 else "manual_review_required"
    
    return {
        "overall_confidence": round(score, 2),
        "recommended_action": action,
        "reasoning": reasons
    }

# ---------------- ROUTES ----------------

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    index_path = os.path.join("static", "index.html")
    if not os.path.exists(index_path):
        return "<html><body><h1>Backend is Running</h1><p>Please create static/index.html to view the UI.</p></body></html>"
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()

@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")

    doc_id = str(uuid.uuid4())
    pdf_path = os.path.join(PDF_DIR, f"{doc_id}.pdf")

    try:
        with open(pdf_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        reader = PdfReader(pdf_path)
        analysis = []

        for i, page in enumerate(reader.pages):
            text_layer = page.extract_text() or ""
            ocr_text, ocr_status = "", "skipped"

            # Attempt OCR
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
        
        return {
            "document_id": doc_id,
            "filename": file.filename,
            "total_pages": len(reader.pages),
            "document_type": detect_document_type(summary, analysis),
            "quality": assess_document_quality(summary, analysis),
            "summary": summary,
            "human_summary": generate_paragraph_summary(summary),
            "analysis": analysis
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Cleanup if you don't want to store files permanently
        # if os.path.exists(pdf_path): os.remove(pdf_path)
        pass
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
# ---------------- RUN APP ----------------
