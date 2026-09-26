#!/usr/bin/env python3
"""
Example tuber server exposing simulated devices.

Run as:
    python example_server.py -p 8080
"""

import random
import warnings

import numpy as np

from tuber.server import TuberArray


class DeviceDriver:
    """
    A device with a toggle button and an adjustable knob.

    In a real driver, ``button`` and ``knob`` might be hardware objects
    (serial ports, GPIO handles, etc.) that cannot be serialized over the
    network.  We list them in ``__tuber_exclude__`` to hide them from clients;
    the client-facing API is provided by explicit getter/setter methods instead.
    """

    # Mark this class as a tuber-navigable object so the server will inspect
    # it for exported methods and properties.
    __tuber_object__ = True

    # Prevent these attributes from being sent to clients.  They are accessed
    # only through the public methods below.
    __tuber_exclude__ = ["button", "knob"]

    def __init__(self, label="driver"):
        self.label = label  # exported: visible to clients as a plain attribute
        self.button = False  # excluded
        self.knob = 1  # excluded

    def push_button(self):
        """Toggle the button and return its new state."""
        self.button = not self.button
        return self.button

    def set_knob(self, value: int):
        """Set the knob position (0-100) and return the updated value."""
        # Exceptions raised here are sent back to the client and re-raised
        # there as tuber.TuberRemoteError.
        if not 0 <= value <= 100:
            raise ValueError(f"Knob position {value} out of range (0-100)")
        self.knob = value
        return self.knob

    def get_button(self):
        """Return the current button state."""
        return self.button

    def get_knob(self):
        """Return the current knob position."""
        return self.knob

    def get_all(self):
        """Return all device state as a dict."""
        return {"label": self.label, "button": self.button, "knob": self.knob}


class Thermometer:
    """
    A simulated temperature sensor with a configurable calibration offset.

    Readings are Gaussian-distributed around 20 °C to mimic real sensor noise.
    The calibration offset is added before each reading is returned.
    """

    __tuber_object__ = True

    def __init__(self, label="thermometer", offset=0.0):
        self.label = label
        self.offset = offset

    def read_temperature(self):
        """Return the current temperature (°C), including calibration offset."""
        raw = 20.0 + random.gauss(0, 0.5)
        return raw + self.offset

    def set_calibration(self, offset: float):
        """Set the calibration offset (°C) and return the new value."""
        # Warnings emitted during a call are forwarded to the client, where
        # they are re-emitted with warnings.warn().
        if abs(offset) > 5:
            warnings.warn(f"Calibration offset {offset} °C is unusually large")
        self.offset = offset
        return self.offset

    def get_calibration(self):
        """Return the current calibration offset."""
        return self.offset

    def read_stats(self, n: int = 5):
        """Take n readings and return summary statistics."""
        readings = [20.0 + random.gauss(0, 0.5) + self.offset for _ in range(n)]
        return {
            "mean": sum(readings) / n,
            "min": min(readings),
            "max": max(readings),
            "n": n,
        }

    def read_samples(self, n: int = 8):
        """Return n raw readings as a numpy array.

        numpy arrays can only be sent to clients that accept CBOR, which encodes
        them as typed arrays.  JSON cannot represent them, so JSON clients receive
        a serialization error for this call instead.
        """
        return 20.0 + self.offset + np.random.normal(0, 0.5, n)


# The registry is defined at module level so that other code can import it
# (see example_embedded.py), and so that tuberd can load this file directly:
#     tuberd -r example_server.py -p 8080
registry = {
    "driver": DeviceDriver(label="main-board"),
    "thermometer": Thermometer(label="ambient", offset=0.0),
    # A TuberArray exposes a list (or dict) of identically-typed objects.
    # The server sends one shared description for all items, and clients
    # index into it like a list: client.sensors[2].read_temperature()
    # Because the description is shared, static properties (e.g. label)
    # are taken from the first item; per-item state must be read through
    # methods.  Use TuberContainer for heterogeneous items.
    "sensors": TuberArray([Thermometer(label="array-sensor", offset=round(0.1 * i, 1)) for i in range(4)]),
}


if __name__ == "__main__":
    from tuber.server import main

    main(registry)
