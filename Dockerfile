FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir Pillow==12.3.0
WORKDIR /app
COPY portal.py /app/portal.py
COPY preview.py /app/preview.py
COPY static /app/static
EXPOSE 8787 8788
USER 1000:1001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/', timeout=3).close(); urllib.request.urlopen('http://127.0.0.1:8788/', timeout=3).close()"
CMD ["python", "-u", "/app/portal.py"]
