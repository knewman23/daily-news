# The local web app (serve.py) in a container. Not the pipeline: fetching needs
# a logged-in Instagram session and the transcribe/OCR steps need macOS, so
# run_daily.py still runs on the host.
#
# No application code is copied in. docker-compose.yml bind-mounts the project
# at /app instead, which keeps news/ — the private journal — out of every image
# layer, and means editing web/ takes effect on a container restart rather than
# a rebuild.

FROM python:3.13-slim

# git: skipping or restoring a topic in the UI republishes site/ (src/publish.py
# shells out to git). Without the binary that path fails on a missing command
# rather than a missing credential, which is a more confusing error.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

# The bind-mounted tree is owned by the host user, and git refuses to operate in
# a repository it thinks belongs to someone else.
RUN git config --system --add safe.directory /app

# Unbuffered so `docker compose logs -f` shows the request log live; no bytecode
# so the container does not litter the host tree with root-owned __pycache__.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8420

# 0.0.0.0 inside the container only. The published port in docker-compose.yml is
# bound to 127.0.0.1, so the app stays as unreachable from the network as it is
# when run directly.
CMD ["python", "serve.py", "--host", "0.0.0.0"]
