# Use official lightweight Python image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Install system dependencies (curl for healthchecks, basic build essentials)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium & system browser dependencies for the headless crawler
RUN playwright install --with-deps chromium

# Copy application files
COPY api.py exeter_hmo_matcher.py dashboard.html ./

# Create data directory for persistent SQLite database
RUN mkdir -p /app/data
ENV DB_PATH=/app/data/exeter_hmo_tracker.db

# Expose port
EXPOSE 8000

# Run uvicorn server
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
