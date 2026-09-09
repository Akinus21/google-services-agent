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
COPY a2a_server.py .

# token.json and any future context store live in the mounted /data
# volume — never baked into the image.
VOLUME ["/data"]
ENV GOOGLE_TOKEN_PATH=/data/google-token.json

EXPOSE 8200
CMD ["python3", "a2a_server.py"]
