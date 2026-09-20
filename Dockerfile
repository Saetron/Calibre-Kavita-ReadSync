FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

# Create volume directories
RUN mkdir -p /app/data /app/config /calibre/library /vfs

EXPOSE 8080

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["kosync-hub"]
CMD ["serve", "--config", "/app/config/config.yaml"]
