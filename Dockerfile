
FROM python:3.11-slim

WORKDIR /app

# Prevent Python from writing .pyc files and buffer-free logging.
# Configure Python HTTP clients to use the system CA certificate bundle.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# Install CA certificates and download the official Cloudflare Origin CA roots.
# This allows the container to validate the private HTTPS connection to Django.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl; \
    curl -fsSL --retry 3 \
        https://developers.cloudflare.com/ssl/static/origin_ca_rsa_root.pem \
        -o /usr/local/share/ca-certificates/cloudflare-origin-ca-rsa.crt; \
    curl -fsSL --retry 3 \
        https://developers.cloudflare.com/ssl/static/origin_ca_ecc_root.pem \
        -o /usr/local/share/ca-certificates/cloudflare-origin-ca-ecc.crt; \
    update-ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# Install Python application dependencies.
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

# Copy the MCP server application.
COPY . .

# Streamable HTTP / SSE transport port.
EXPOSE 8001

# Start the remote MCP server, binding inside the container.
CMD ["python", "server.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8001"]
