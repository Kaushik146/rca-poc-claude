FROM python:3.11-slim

WORKDIR /app

RUN groupadd -r appuser && useradd -r -g appuser appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

USER appuser

EXPOSE 5003

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5003/health')" || exit 1

# Production: use gunicorn
# CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5003", "app:app"]
# Development:
CMD ["python", "app.py"]
