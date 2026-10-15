#!/usr/bin/env python3
"""
Example asynchronous tuber client.

Start the example server first:
    python example_server.py -p 8080

Then run this script, passing the port the server printed if it isn't 8080:
    python example_async_client.py [-p PORT]
"""

import argparse
import asyncio
import tuber


async def main(host):
    # Connect to the server asynchronously.  The returned proxy works like the
    # synchronous one except that method calls are coroutines (use ``await``).
    client = await tuber.resolve(host, timeout=5.0)

    # ── DeviceDriver ──────────────────────────────────────────────────────────

    driver = client.driver

    print("=== DeviceDriver ===")

    # Plain attributes (fetched at resolve time) need no await.
    print("Label:", driver.label)

    # Method calls are coroutines; await each one individually.
    print("Button (initial):", await driver.get_button())
    await driver.push_button()
    print("Button (after push):", await driver.get_button())

    await driver.set_knob(42)
    print("Knob:", await driver.get_knob())

    state = await driver.get_all()
    print(f"All state: label={state.label!r}, button={state.button}, knob={state.knob}")

    # ── Thermometer ───────────────────────────────────────────────────────────

    therm = client.thermometer

    print("\n=== Thermometer ===")
    print("Label:", therm.label)
    print(f"Temperature: {await therm.read_temperature():.2f} °C")

    await therm.set_calibration(1.5)
    print(f"Calibrated temperature: {await therm.read_temperature():.2f} °C")

    stats = await therm.read_stats(n=10)
    print(f"Stats (n={stats.n}): mean={stats.mean:.2f}, min={stats.min:.2f}, max={stats.max:.2f}")

    # ── Dynamic properties ────────────────────────────────────────────────────
    #
    # Reading a dynamic property returns an awaitable, which fetches the
    # current value from the server.  Static properties (label) need no await.

    print("\n=== Dynamic properties ===")

    print(f"Temperature: {await therm.temperature:.2f} °C")
    print("Offset:", await therm.offset)

    # Assignment can't be awaited, so async objects set dynamic properties
    # with tuber_set(), which returns the value read back by the server.
    print("Offset (tuber_set):", await therm.tuber_set("offset", 1.5))

    try:
        therm.offset = 0.5
    except tuber.TuberStateError as e:
        print("Assignment:", e)

    async with therm.tuber_context() as ctx:
        ctx.tuber_set("offset", 2.0)
        ctx.tuber_get("temperature")
        results = await ctx()
    print(f"Batched: offset={results[0]}, temperature={results[1]:.2f} °C")
    await therm.tuber_set("offset", 1.5)

    # ── Errors ────────────────────────────────────────────────────────────────
    #
    # Server-side exceptions are raised on the client as TuberRemoteError.

    print("\n=== Errors ===")

    try:
        await driver.set_knob(500)
    except tuber.TuberRemoteError as e:
        print("Remote error:", str(e).strip().splitlines()[-1])

    # ── Batched calls ─────────────────────────────────────────────────────────
    #
    # An async context manager batches multiple calls into one HTTP request.
    # Calls are queued until ``await ctx()`` is called.

    print("\n=== Batched calls ===")

    await driver.set_knob(1)  # reset knob

    async with driver.tuber_context() as ctx:
        ctx.push_button()  # queued, not sent yet
        ctx.set_knob(7)  # queued
        ctx.get_all()  # queued
        results = await ctx()  # sends all three calls in one request

    print("push_button:", results[0])
    print("set_knob:", results[1])
    print("get_all:", results[2])

    # Each queued call also returns an awaitable future.  Awaiting one sends
    # every call queued so far, so results can be used without leaving the
    # context.  Any calls still queued when the context exits are sent
    # automatically.

    async with driver.tuber_context() as ctx:
        ctx.push_button()
        knob = await ctx.get_knob()  # sends push_button and get_knob together
        print("Knob (mid-context):", knob)
        ctx.set_knob(knob + 1)
        new_knob = ctx.get_knob()
    print("Knob (after exit):", await new_knob)  # flushed on context exit

    # ── Cross-object batch ────────────────────────────────────────────────────
    #
    # Open the context on the registry-level client to batch calls across objects.

    print("\n=== Cross-object batch ===")

    async with client.tuber_context() as ctx:
        ctx.driver.get_all()
        ctx.thermometer.read_temperature()
        results = await ctx()

    print("driver:", results[0])
    print("thermometer:", f"{results[1]:.2f} °C")

    # ── Errors in a batch ─────────────────────────────────────────────────────
    #
    # With return_exceptions=True, every call in the batch runs and each failure
    # is returned in place of its result, including results that JSON cannot
    # serialize (read_samples() returns a numpy array).

    print("\n=== Errors in a batch ===")

    async with client.tuber_context(return_exceptions=True) as ctx:
        ctx.driver.set_knob(500)
        ctx.thermometer.read_samples(4)
        ctx.driver.get_knob()
        results = await ctx()

    for name, r in zip(["set_knob", "read_samples", "get_knob"], results):
        if isinstance(r, Exception):
            r = f"{type(r).__name__}: {str(r).strip().splitlines()[-1]}"
        print(f"{name}: {r}")

    # ── Concurrent independent calls ──────────────────────────────────────────
    #
    # asyncio.gather dispatches multiple coroutines concurrently so they are
    # in-flight at the same time rather than waiting for each to finish before
    # starting the next.  Use this when calls are independent and latency matters.

    print("\n=== Concurrent reads ===")

    button, knob, temp = await asyncio.gather(
        driver.get_button(),
        driver.get_knob(),
        therm.read_temperature(),
    )
    print(f"button={button}, knob={knob}, temp={temp:.2f} °C")

    # The same works across the items of an array of objects.
    sensors = client.sensors
    temps = await asyncio.gather(*(s.read_temperature() for s in sensors))
    print("Sensor temperatures:", [f"{t:.2f}" for t in temps])
    print("Calibrations:", await sensors.tuber_call("get_calibration"))
    print("Offsets:", await sensors.tuber_get("offset"))

    # ── CBOR and numpy ────────────────────────────────────────────────────────
    #
    # Request CBOR to receive numpy arrays as numpy arrays.

    print("\n=== CBOR and numpy ===")

    cbor_client = await tuber.resolve(host, accept_types=["application/cbor"])
    samples = await cbor_client.thermometer.read_samples(8)
    print(f"Samples: {type(samples).__name__} {samples.dtype} {samples.shape}")
    print(f"Mean: {samples.mean():.2f} °C")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Example asynchronous tuber client")
    parser.add_argument("-p", "--port", type=int, default=8080, help="Port the example server is running on")
    args = parser.parse_args()

    asyncio.run(main(f"localhost:{args.port}"))
