class DeviceDriver:
    """
    This is a simple device driver class.  A more complicated driver
    might be written in C or C++ and bound to python using pybind11
    or similar.
    """

    def __init__(self):
        self.button = False
        self.knob = 1

    def push_button(self):
        self.button = not self.button
        return self.button

    def set_knob(self, value: int):
        self.knob = value
        return self.knob

    def get_button(self):
        return self.button

    def get_knob(self):
        return self.knob

    def get_all(self):
        return {"button": self.button, "knob": self.knob}


if __name__ == "__main__":
    from tuber.server import main

    # create device registry, with the driver initialized with sensible defaults.
    registry = {"driver": DeviceDriver()}

    # run the server
    main(registry)
