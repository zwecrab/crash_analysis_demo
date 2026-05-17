FROM python:3.11-slim

# Install system dependencies (needed for compilation or Postgres client libraries)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt-get/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . .

# Expose port 7860 (Hugging Face Spaces strict requirement)
EXPOSE 7860

# Run uvicorn on port 7860, host 0.0.0.0 (no --reload in production)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
