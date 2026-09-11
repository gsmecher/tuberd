#!/usr/bin/env python3
"""
Example asynchronous tuber client.

Start the example server first:
    python example_server.py -p 8080

Then run this script:
    python example_async_client.py
"""

import asyncio
import tuber


async def main():
    # Connect to the server asynchronously.  The returned proxy works like the
    # synchronous one except that method calls are coroutines (use ``await``).
    client = await tuber.resolve("localhost:8080")

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


if __name__ == "__main__":
    asyncio.run(main())
