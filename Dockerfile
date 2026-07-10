FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HOME=/home/landing

WORKDIR /app

RUN useradd --create-home --uid 10001 landing

COPY --chown=landing:landing . /app

USER landing

EXPOSE 3002

CMD ["python3", "server.py", "3002"]
