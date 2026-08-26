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
export NOTIFY_NEW_SPECIES="$(bashio::config 'notify_new_species')"
export REQUIRE_SPECIES_CONFIRMATION="$(bashio::config 'require_species_confirmation')"
export XENO_CANTO_API_KEY="$(bashio::config 'xeno_canto_api_key')"
export LOG_LEVEL="$(bashio::config 'log_level')"
# --- External identification (aviary-id on the GPU host) ----------------------
export IDENTIFY_URL="$(bashio::config 'identify_url')"
export IDENTIFY_TOKEN="$(bashio::config 'identify_token')"
export IDENTIFY_ENABLED="$(bashio::config 'identify_enabled')"
export IDENTIFY_MIN_SCORE="$(bashio::config 'identify_min_score')"
export IDENTIFY_MIN_MARGIN="$(bashio::config 'identify_min_margin')"
export IDENTIFY_WORKERS="$(bashio::config 'identify_workers')"
export IDENTIFY_TIMEOUT="$(bashio::config 'identify_timeout')"
export IDENTIFY_RETAIN_DAYS="$(bashio::config 'identify_retain_days')"
export IDENTIFY_USE_AUDIO_PRIORS="$(bashio::config 'identify_use_audio_priors')"
export IDENTIFY_EXCLUDE_BLACKLISTED="$(bashio::config 'identify_exclude_blacklisted')"
export IDENTIFY_ZOOM_START_OFFSET="$(bashio::config 'identify_zoom_start_offset')"
# List options: bashio emits one item per line, and the app parses these comma-separated.
# An unset list yields "null", which must not become a value.
ignore_cameras="$(bashio::config 'ignore_cameras' | tr '\n' ',')"
if [ "${ignore_cameras}" = "null," ] || [ "${ignore_cameras}" = "null" ]; then
    ignore_cameras=""
fi
export IGNORE_CAMERAS="${ignore_cameras}"
zoom_map="$(bashio::config 'identify_zoom_map' | tr '\n' ',')"
if [ "${zoom_map}" = "null," ] || [ "${zoom_map}" = "null" ]; then
    zoom_map=""
fi
export IDENTIFY_ZOOM_MAP="${zoom_map}"
zoom_priority="$(bashio::config 'identify_zoom_zone_priority' | tr '\n' ',')"
if [ "${zoom_priority}" = "null," ] || [ "${zoom_priority}" = "null" ]; then
    zoom_priority=""
fi
export IDENTIFY_ZOOM_ZONE_PRIORITY="${zoom_priority}"
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
