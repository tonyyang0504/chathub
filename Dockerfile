FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Environment variables for config override
ENV DATABASE_URL=sqlite:///./data/app.db
ENV SECRET_KEY=sandbox-secret-key
ENV PORT=8000

EXPOSE 8000

ENTRYPOINT ["python", "run.py"]
