FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    flask \
    google-auth \
    google-auth-oauthlib \
    google-api-python-client \
    requests

COPY auth ./auth
COPY tools ./tools
COPY context ./context
COPY a2a_server.py .
COPY agent_loop.py .
COPY mesh.py .

# token.json, context.db, and any future state live in the mounted
# /data volume — never baked into the image.
VOLUME ["/data"]
ENV GOOGLE_TOKEN_PATH=/data/google-token.json
ENV CONTEXT_DB_PATH=/data/context.db

EXPOSE 8200
CMD ["python3", "a2a_server.py"]
