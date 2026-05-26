from tuber import resolve_simple

# create a connection to the running server
obj = resolve_simple("localhost", "driver")

# call methods of the driver class
button_value = obj.get_button()
next_button_value = obj.push_button()
if next_button_value != button_value:
    print("Button push successful")

knob_value = obj.get_knob()
obj.set_knob(knob_value * 10)

# Note that the return object here is a "TuberResult" object.  This is just a fancy dictionary.
# To return a simple dictionary, pass `convert_json=False` to the resolve_simple() function at the
# top of this script.
print("Driver settings:", obj.get_all())

# Call several methods of the driver class in one go
# This will send the sequence of calls as a single packet
# and return the responses from each of them
with obj.tuber_context() as ctx:
    ctx.push_button()
    ctx.set_knob(5)
    ctx.push_button()
    results = ctx()

print("Push button:", results[0])
print("Set knob:", results[1])
print("Push button:", results[2])
