# The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, and the map at /arena.
FROM python:3.13-slim
RUN pip install --no-cache-dir numpy==2.1.3
WORKDIR /bot
COPY mechfight.py navmap.py bot.py ./
ENV PYTHONDONTWRITEBYTECODE=1
RUN python3 -m compileall -q /bot
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
