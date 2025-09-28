FROM python:3.11-slim

# Install build deps (only if needed) and common extras
RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
