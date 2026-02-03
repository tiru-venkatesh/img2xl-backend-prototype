# Use Python 3.11 slim as the base
FROM python:3.11-slim

# Install system-level dependencies for OCR and PDF conversion
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    poppler-utils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python libraries
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your code
COPY . .

# Create the uploads directory
RUN mkdir -p uploads/pdfs

# Run using the shell form to ensure $PORT is expanded correctly
CMD uvicorn main:app --host 0.0.0.0 --port $PORT
