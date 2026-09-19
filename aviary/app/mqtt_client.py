"""MQTT subscriber running paho's network loop in a background thread."""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from . import ingest
from .settings import Settings

log = logging.getLogger("aviary.mqtt")


class MqttIngestor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Optional[mqtt.Client] = None
        # Read by the UI to distinguish "no birds yet" from "not receiving anything".
        self.connected: bool = False
        self.last_message_at: Optional[float] = None
        self._last_fail_log: float = 0.0
        # Publishing side (0.34.0, bird statistics). Nothing here is used unless a module
        # attaches: with none, this stays the subscribe-only client it always was.
        self.on_connect_hooks: list[Callable[[], None]] = []
        self._extra: list[tuple[str, Callable[[bytes], None]]] = []
        self._status_topic: Optional[str] = None

    def enable_status(self, topic: str) -> None:
        """Announce presence on ``topic``: ``online`` on connect, ``offline`` on stop and —
        via the broker's will — on a crash. Must be called before ``start()``."""
        self._status_topic = topic

    def add_subscription(self, topic: str, handler: Callable[[bytes], None]) -> None:
        """Subscribe to one more topic, handled before the ingest chain."""
        self._extra.append((topic, handler))

    def publish(self, topic: str, payload: str, retain: bool = False, qos: int = 0) -> bool:
        """Publish best-effort. False when not connected or the client refused; never raises."""
        client = self._client
        if client is None or not self.connected:
            return False
        try:
            info = client.publish(topic, payload, qos=qos, retain=retain)
            return info.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception:  # noqa: BLE001 — a failed publish is a log line, not an outage
            log.debug("MQTT publish to %s failed", topic, exc_info=True)
            return False

    def start(self) -> None:
        s = self._settings
        if not s.mqtt_enabled:
            log.warning("MQTT host not configured; ingest disabled.")
            return

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="aviary",
        )
        if s.mqtt_user:
            client.username_pw_set(s.mqtt_user, s.mqtt_password)
        if self._status_topic:
            client.will_set(self._status_topic, "offline", qos=0, retain=True)
        client.on_connect = self._on_connect
        client.on_connect_fail = self._on_connect_fail
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

        log.info("Connecting to MQTT %s:%s", s.mqtt_host, s.mqtt_port)
        client.connect_async(s.mqtt_host, s.mqtt_port, keepalive=60)
        client.loop_start()  # spawns its own network thread
        self._client = client

    def stop(self) -> None:
        if self._client is not None:
            if self._status_topic:
                # A clean stop says goodbye itself; the will is for the unclean one.
                self.publish(self._status_topic, "offline", retain=True)
            self._client.loop_stop()
            try:
                self._client.disconnect()
            except Exception:  # noqa: BLE001 - best-effort shutdown
                pass
            self._client = None

    # ---------------------------------------------------------------- callbacks

    def _on_connect(self, client: mqtt.Client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            log.error("MQTT connect failed: %s", reason_code)
            return
        self.connected = True
        s = self._settings
        topics = [(s.frigate_topic, 0), (s.birdnet_topic, 0)]
        # Review items are optional: a blank topic means the user turned visits off (or
        # runs a Frigate older than 0.14, which has no review items to publish).
        if s.frigate_review_topic:
            topics.append((s.frigate_review_topic, 0))
        # Optional safety net for a classification Frigate publishes after an event's
        # end message (see frigate_object_update_topic).
        if s.frigate_object_update_topic:
            topics.append((s.frigate_object_update_topic, 0))
        topics.extend((t, 0) for t, _ in self._extra)
        client.subscribe(topics)
        log.info("Subscribed to %s", ", ".join(f"'{t}'" for t, _ in topics))
        for hook in self.on_connect_hooks:
            try:
                hook()
            except Exception:  # noqa: BLE001 — a publisher's hiccup must not stop ingest
                log.exception("MQTT connect hook failed")

    def _on_connect_fail(self, client: mqtt.Client, userdata) -> None:
        # paho retries with backoff; throttle so a dead broker doesn't flood the log.
        now = time.time()
        if now - self._last_fail_log >= 60:
            self._last_fail_log = now
            s = self._settings
            log.warning(
                "Cannot reach MQTT broker at %s:%s (still retrying). If this is an "
                "add-on, 'localhost' points at the add-on container itself — use the "
                "broker's hostname (e.g. core-mosquitto) or leave mqtt_host empty to "
                "use the Home Assistant mqtt service.",
                s.mqtt_host,
                s.mqtt_port,
            )

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self.connected = False
        log.warning("MQTT disconnected (%s); auto-reconnect will retry.", reason_code)

    def _on_message(self, client: mqtt.Client, userdata, message: mqtt.MQTTMessage) -> None:
        topic = message.topic
        s = self._settings
        self.last_message_at = time.time()
        try:
            for extra_topic, handler in self._extra:
                if _topic_matches(topic, extra_topic):
                    handler(message.payload)
                    return
            if _topic_matches(topic, s.frigate_topic):
                ingest.handle_frigate(message.payload)
            elif s.frigate_review_topic and _topic_matches(topic, s.frigate_review_topic):
                ingest.handle_frigate_review(message.payload)
            elif s.frigate_object_update_topic and _topic_matches(
                    topic, s.frigate_object_update_topic):
                ingest.handle_frigate_object_update(message.payload)
            elif _topic_matches(topic, s.birdnet_topic):
                ingest.handle_birdnet(message.payload)
        except Exception:  # noqa: BLE001 - never let a bad message kill the loop
            log.exception("Error handling message on %s", topic)


def _topic_matches(actual: str, subscribed: str) -> bool:
    """Match an incoming topic against a subscription that may contain + / # wildcards."""
    if "+" not in subscribed and "#" not in subscribed:
        return actual == subscribed
    a_parts = actual.split("/")
    s_parts = subscribed.split("/")
    for i, sp in enumerate(s_parts):
        if sp == "#":
            return True
        if i >= len(a_parts):
            return False
        if sp == "+":
            continue
        if sp != a_parts[i]:
            return False
    return len(a_parts) == len(s_parts)
