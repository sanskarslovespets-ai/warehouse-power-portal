FROM python:3.12-slim

# TZ as a POSIX string (IST, no DST) so bare datetime.now() is India time without tzdata.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=IST-5:30

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# One worker: each worker would run its own 9:00 refresh against the databases (README section 4).
CMD exec python -m uvicorn main:app --host=0.0.0.0 --port=${PORT:-8080} --no-server-header
