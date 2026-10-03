FROM docker.io/alpine:3.20
RUN apk add --no-cache ffmpeg python3
WORKDIR /srv
COPY app /srv/app
ENTRYPOINT ["python3", "-m", "app"]
