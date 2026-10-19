# Procedure Studio: one image with the API and the built UI.
#   docker build -t procedure-studio .
#   docker run -p 8790:8790 -v studio-data:/data procedure-studio

FROM node:20-bookworm-slim AS ui
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npx tsc --noEmit && npx vite build --outDir /ui --emptyOutDir

FROM python:3.11-slim-bookworm
# OCP needs a few X/GL libraries even headless
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglu1-mesa libxrender1 libxext6 libsm6 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/studio ./backend/studio
COPY packages ./packages
COPY examples ./examples
COPY --from=ui /ui ./backend/studio/web
ENV PYTHONPATH=/app/backend STUDIO_DATA=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8790
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8790/api/health')"
CMD ["python", "-m", "studio", "--host", "0.0.0.0", "--port", "8790"]
