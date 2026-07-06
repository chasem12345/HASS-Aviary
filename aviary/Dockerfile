ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# tzdata: Supervisor passes the host TZ; without zoneinfo present, localtime-based
# charts ("per day" / "by hour" / the Today boundary) silently run in UTC.
# ffmpeg: remuxes downloaded clips into standard MP4s with a real duration header
# (Frigate's streaming clips report 00:00 and fail upload validation elsewhere).
RUN apk add --no-cache tzdata ffmpeg

# The HA base-python images already ship Python 3 + pip.
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# App code
COPY app /app/app
COPY blueprints /app/blueprints
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app

CMD ["/run.sh"]
