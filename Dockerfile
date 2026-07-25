FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -c "from rapidocr import RapidOCR; RapidOCR(); print('RapidOCR ready')"

RUN useradd --create-home --uid 10001 bot \
    && install -d --owner=bot --group=bot /data

COPY --chown=bot:bot . .

USER bot

CMD ["python", "script.py"]
