FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY render_service.py .

EXPOSE 8080

CMD ["python3", "render_service.py"]
