FROM bluenviron/mediamtx:1.21.0@sha256:19fddade8d6110a3d718ac0045681fbeba344ae563a066205fe5929a87f7582f AS media
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg gosu fonts-dejavu-core ca-certificates && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 dvorcam && useradd -u 1000 -g 1000 -M dvorcam
COPY --from=media /mediamtx /usr/local/bin/mediamtx
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/
COPY docker/supervisord.conf /etc/supervisord.conf
COPY docker/entrypoint.sh /usr/local/bin/dvorcam
RUN chmod 755 /usr/local/bin/dvorcam
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC
EXPOSE 9001 8889 8189/tcp 8189/udp
ENTRYPOINT ["dvorcam"]
