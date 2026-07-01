ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The HA base-python images already ship Python 3 + pip.
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# App code
COPY app /app/app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app

CMD ["/run.sh"]
