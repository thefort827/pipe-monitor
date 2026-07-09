FROM python:3.12-slim

WORKDIR /app

# Install system dependencies and Chinese fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-wqy-zenhei fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 5000

CMD ["python", "-u", "app.py"]
