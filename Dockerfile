FROM python:3.11-slim

WORKDIR /app

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default port for SSE / HTTP transport
EXPOSE 8001

# Run as remote SSE server binding to all interfaces
CMD ["python", "server.py", "--sse", "--host", "0.0.0.0", "--port", "8001"]
