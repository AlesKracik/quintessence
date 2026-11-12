#!/usr/bin/env python3
"""Python adapter: runs model-chosen action sequences against Python code.

It executes and reports; it judges nothing. The checker (`lake exe spec`)
starts it with the root of the code's repository as its argument, sends one JSON message per line on stdin and reads one JSON reply
per line on stdout:

  {"setup": code, "actions": {name: code}, "observe": [expression, ...]}
      -> {"operations": [public operations of the variable `system`]}
  {"run": [{"action": name, "args": [...]}, ...]}
      -> {"initial": [values], "steps": [{"refused": bool, "error": text, "observed": [values]}]}

Every run starts from a fresh `setup`. An action is refused when its code
raises; mapping code can call `refuse()` to turn a return value into a
refusal. `{0}`, `{1}`, ... in an action's code are its arguments.
"""
import json
import os
import sys


class Refused(Exception):
    pass


def refuse():
    raise Refused("refused")


def show(value):
    """A value as the model writes it: true/false, none, plain numbers and text."""
    if value is True or value is False:
        return str(value).lower()
    if value is None:
        return "none"
    if isinstance(value, (int, str)):
        return str(value)
    if isinstance(value, (set, frozenset)):
        value = sorted(value)
    return json.dumps(value, sort_keys=True, default=str)


def fresh(config):
    env = {"refuse": refuse}
    exec(config["setup"], env)
    return env


def observe(config, env):
    values = []
    for expression in config["observe"]:
        try:
            values.append(show(eval(expression, env)))
        except Exception as error:
            values.append(f"error: {error}")
    return values


def run(config, steps):
    env = fresh(config)
    reply = {"initial": observe(config, env), "steps": []}
    for step in steps:
        code = config["actions"][step["action"]]
        for index, arg in enumerate(step["args"]):
            code = code.replace("{%d}" % index, repr(arg))
        error = ""
        try:
            exec(code, env)
        except Exception as raised:
            error = f"{type(raised).__name__}: {raised}"
        reply["steps"].append({"refused": bool(error), "error": error, "observed": observe(config, env)})
    return reply


def main():
    if len(sys.argv) > 1:
        os.chdir(sys.argv[1])  # the root of the code's repository
    sys.path.insert(0, os.getcwd())
    out, sys.stdout = sys.stdout, sys.stderr  # the code under test may print
    config = None
    for line in sys.stdin:
        message = json.loads(line)
        try:
            if "setup" in message:
                config = message
                system = fresh(config).get("system")
                reply = {"operations": sorted(
                    name for name in dir(system)
                    if not name.startswith("_") and callable(getattr(system, name)))}
            else:
                reply = run(config, message["run"])
        except Exception as error:
            reply = {"error": f"{type(error).__name__}: {error}"}
        out.write(json.dumps(reply) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
