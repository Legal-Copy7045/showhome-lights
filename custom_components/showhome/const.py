"""Constants for the Holiday ShowHome integration."""

DOMAIN = "showhome"

CONF_PIN = "pin"
CONF_BRIGHTNESS_LEVELS = "brightness_levels"
CONF_STRANDS = "strands"

# The real top of the lights' brightness scale is not documented. Lights accept
# 1-255; the Show App's own slider range is unconfirmed, so this is an option.
DEFAULT_BRIGHTNESS_LEVELS = 10

# Drop the BLE link after this long without commands so the Show App can connect.
IDLE_DISCONNECT_SECONDS = 60

SIGNAL_STRANDS_UPDATED = f"{DOMAIN}_strands_updated"
SIGNAL_BROADCAST_STATE = f"{DOMAIN}_broadcast_state"

# Colour indexes seen in captures; others exist but are unnamed.
KNOWN_COLORS = {0: "Blue", 1: "Red", 2: "Green"}
COLOR_SLOTS = 16
EFFECT_SLOTS = 32
