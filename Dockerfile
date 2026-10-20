FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 facematch \
    && mkdir /data /app && chown facematch /data /app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=facematch . .
USER facematch
RUN python download_models.py

ENV FACEMATCH_DATA=/data FACEMATCH_HOST=0.0.0.0 FACEMATCH_NO_BROWSER=1 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4).status==200 else 1)"
CMD ["python", "run.py"]
