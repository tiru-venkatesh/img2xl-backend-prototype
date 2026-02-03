# Use a lightweight Python image
FROM python:3.11-slim

# Install system dependencies for OCR and PDF conversion
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    poppler-utils \
    libtesseract-dev \
    && apt-get clean

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Use the shell form to allow environment variable expansion for $PORT
# Render's default port is 10000, but it provides the $PORT variable automatically
CMD uvicorn main:app --host 0.0.0.0 --port $PORT
