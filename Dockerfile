FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HOME=/home/landing

WORKDIR /app

RUN useradd --create-home --uid 10001 landing

COPY --chown=landing:landing . /app

USER landing

EXPOSE 3002

HEALTHCHECK --interval=30s --timeout=8s --retries=3 --start-period=15s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3002/health', timeout=5)"

CMD ["python3", "server.py", "3002"]
