#!/usr/bin/env python3
"""
Example synchronous tuber client.

Start the example server first:
    python example_server.py -p 8080

Then run this script:
    python example_client.py
"""

import warnings

import tuber

# Connect to the server.  resolve_simple() fetches metadata from the registry
# and returns a proxy whose attributes mirror the server's registered objects.
# The timeout (in seconds) applies to every HTTP request made by this client.
client = tuber.resolve_simple("localhost:8080", timeout=5.0)

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
# A client used as a context manager closes its HTTP connections on exit, which
# suits short-lived clients like this one.
with tuber.resolve_simple("localhost:8080", convert_json=False) as plain_client:
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

# ── Errors and warnings ───────────────────────────────────────────────────────
#
# An exception raised on the server is re-raised on the client as a
# TuberRemoteError carrying the server-side traceback.  Warnings emitted on the
# server are re-emitted on the client with warnings.warn().

print("\n=== Errors and warnings ===")

try:
    driver.set_knob(500)
except tuber.TuberRemoteError as e:
    print("Remote error:", str(e).strip().splitlines()[-1])

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    therm.set_calibration(10.0)
print("Remote warning:", caught[0].message)
therm.set_calibration(1.5)

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

# Each queued call also returns a future.  Calling .result() on a future sends
# every call queued so far, so results can be used without leaving the context.
# Any calls still queued when the context exits are sent automatically.

with driver.tuber_context() as ctx:
    knob = ctx.get_knob()
    print("Knob (mid-context):", knob.result())  # flushes the queue
    ctx.set_knob(knob.result() + 1)
    new_knob = ctx.get_knob()
print("Knob (after exit):", new_knob.result())  # flushed on context exit

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

# ── Errors in a batch ─────────────────────────────────────────────────────────
#
# By default, the first failing call in a batch raises an exception and the
# calls after it are skipped.  With return_exceptions=True, every call runs and
# each failure is returned in place of its result.  This also covers results
# that cannot be serialized: read_samples() returns a numpy array, which JSON
# cannot encode, but the other results in the batch are unaffected.

print("\n=== Errors in a batch ===")

with client.tuber_context() as ctx:
    ctx.driver.set_knob(500)
    ctx.thermometer.read_samples(4)
    ctx.driver.get_knob()
    results = ctx(return_exceptions=True)

for name, r in zip(["set_knob", "read_samples", "get_knob"], results):
    if isinstance(r, Exception):
        r = f"{type(r).__name__}: {str(r).strip().splitlines()[-1]}"
    print(f"{name}: {r}")

# ── Arrays of objects ─────────────────────────────────────────────────────────
#
# client.sensors is a TuberArray: a list of identical Thermometer objects.
# Items behave like any other object, and the array itself supports len(),
# indexing and iteration.

print("\n=== Sensor array ===")

sensors = client.sensors
print("Number of sensors:", len(sensors))
print(f"Sensor 2 temperature: {sensors[2].read_temperature():.2f} °C")

# tuber_call() calls a method on every item (or a subset, with keys=[...]) in
# a single request, looping on the server.
print("Calibrations:", sensors.tuber_call("get_calibration"))
print("Sensors 0 and 3:", [f"{t:.2f}" for t in sensors.tuber_call("read_temperature", keys=[0, 3])])

# Array items can also be batched individually by indexing into a context.
with client.tuber_context() as ctx:
    for i in range(len(sensors)):
        ctx.sensors[i].read_temperature()
    temps = ctx()
print("All temperatures:", [f"{t:.2f}" for t in temps])

# ── CBOR and numpy ────────────────────────────────────────────────────────────
#
# Clients accept JSON by default.  Requesting CBOR (requires the cbor2 package)
# gives a more compact binary encoding, and numpy arrays are sent as typed
# arrays that decode directly to numpy arrays on the client.

print("\n=== CBOR and numpy ===")

with tuber.resolve_simple("localhost:8080", accept_types=["application/cbor"]) as cbor_client:
    samples = cbor_client.thermometer.read_samples(8)
print(f"Samples: {type(samples).__name__} {samples.dtype} {samples.shape}")
print(f"Mean: {samples.mean():.2f} °C")

# ── Cleanup ───────────────────────────────────────────────────────────────────
#
# Each open keep-alive connection occupies one of the server's worker threads.
# Connections are closed when a client is no longer referenced, but close()
# releases them immediately.  It closes the connections shared by the whole
# object tree, so driver, therm and sensors can no longer be used either.

client.close()
