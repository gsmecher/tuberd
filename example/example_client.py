#!/usr/bin/env python3
"""
Example synchronous tuber client.

Start the example server first:
    python example_server.py -p 8080

Then run this script:
    python example_client.py
"""

import tuber

# Connect to the server.  resolve_simple() fetches metadata from the registry
# and returns a proxy whose attributes mirror the server's registered objects.
client = tuber.resolve_simple("localhost:8080")

# ── DeviceDriver ──────────────────────────────────────────────────────────────

driver = client.driver

print("=== DeviceDriver ===")

# Plain attributes (fetched once at resolve time) need no method call.
print("Label:", driver.label)

# Each method call below is a separate HTTP request.
print("Button (initial):", driver.get_button())
driver.push_button()
print("Button (after push):", driver.get_button())

driver.set_knob(42)
print("Knob:", driver.get_knob())

# Methods that return dicts come back as TuberResult objects by default.
# Use attribute access (state.button) rather than dict access (state["button"]).
state = driver.get_all()
print(f"All state: label={state.label!r}, button={state.button}, knob={state.knob}")

# Pass convert_json=False to get plain dicts instead of TuberResult objects.
plain_client = tuber.resolve_simple("localhost:8080", convert_json=False)
state_dict = plain_client.driver.get_all()
print("As plain dict:", state_dict)

# ── Thermometer ───────────────────────────────────────────────────────────────

therm = client.thermometer

print("\n=== Thermometer ===")
print("Label:", therm.label)
print(f"Temperature: {therm.read_temperature():.2f} °C")

therm.set_calibration(1.5)
print(f"Calibrated temperature: {therm.read_temperature():.2f} °C")

stats = therm.read_stats(n=10)
print(f"Stats (n={stats.n}): mean={stats.mean:.2f}, min={stats.min:.2f}, max={stats.max:.2f}")

# ── Batched calls ─────────────────────────────────────────────────────────────
#
# A context manager batches multiple calls into a single HTTP request.
# No network traffic is sent until ctx() is called.

print("\n=== Batched calls ===")

driver.set_knob(1)  # reset knob

with driver.tuber_context() as ctx:
    ctx.push_button()  # queued, not sent yet
    ctx.set_knob(7)  # queued
    ctx.get_all()  # queued
    results = ctx()  # sends all three calls in one request

print("push_button:", results[0])
print("set_knob:", results[1])
print("get_all:", results[2])

# ── Cross-object batch ────────────────────────────────────────────────────────
#
# Open the context on the registry-level client to batch calls across objects.

print("\n=== Cross-object batch ===")

with client.tuber_context() as ctx:
    ctx.driver.get_all()
    ctx.thermometer.read_temperature()
    results = ctx()

print("driver:", results[0])
print("thermometer:", f"{results[1]:.2f} °C")
