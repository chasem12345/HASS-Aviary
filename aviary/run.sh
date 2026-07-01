#!/usr/bin/with-contenv bashio
# ==============================================================================
# Aviary add-on start script.
# Resolves configuration (MQTT creds from the HA `mqtt` service when available,
# otherwise from add-on options), exports it as env vars, and launches uvicorn.
# ==============================================================================
set -e

export FRIGATE_URL="$(bashio::config 'frigate_url')"
export BIRDNET_URL="$(bashio::config 'birdnet_url')"
export FRIGATE_TOPIC="$(bashio::config 'frigate_topic')"
export BIRDNET_TOPIC="$(bashio::config 'birdnet_topic')"
export BACKFILL_ON_START="$(bashio::config 'backfill_on_start')"
export IGNORE_UNCLASSIFIED="$(bashio::config 'ignore_unclassified')"
export LOG_LEVEL="$(bashio::config 'log_level')"
export DATA_DIR="/data"

# --- MQTT broker resolution ---------------------------------------------------
# Prefer explicit overrides in the add-on options; fall back to the `mqtt`
# service published by the Mosquitto broker add-on.
mqtt_host="$(bashio::config 'mqtt_host')"

if bashio::var.has_value "${mqtt_host}"; then
    bashio::log.info "Using MQTT broker from add-on options: ${mqtt_host}"
    export MQTT_HOST="${mqtt_host}"
    export MQTT_PORT="$(bashio::config 'mqtt_port')"
    export MQTT_USER="$(bashio::config 'mqtt_user')"
    export MQTT_PASSWORD="$(bashio::config 'mqtt_password')"
elif bashio::services.available "mqtt"; then
    bashio::log.info "Using MQTT broker from the Home Assistant mqtt service."
    export MQTT_HOST="$(bashio::services 'mqtt' 'host')"
    export MQTT_PORT="$(bashio::services 'mqtt' 'port')"
    export MQTT_USER="$(bashio::services 'mqtt' 'username')"
    export MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"
else
    bashio::log.warning "No MQTT service available and no mqtt_host configured; ingest will be idle."
    export MQTT_HOST=""
    export MQTT_PORT="1883"
    export MQTT_USER=""
    export MQTT_PASSWORD=""
fi

bashio::log.info "Starting Aviary on :8099 ..."
exec python3 -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8099 \
    --log-level "${LOG_LEVEL}"
