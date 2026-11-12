#!/usr/bin/env python3
"""Kotlin adapter: runs model-chosen action sequences against Kotlin (JVM) code.

It executes and reports; it judges nothing. In a binding:

    adapter := ["python3", "adapters/kotlin.py", "build/classes/kotlin/main", "libs/*"]

Every argument after the script is something the code needs, relative to the
root of the code's repository: a directory or jar of compiled classes (goes on
the classpath), or Kotlin sources, a `.kt` file or a directory of them (compiled
together with the harness). The checker adds the repository root as the last
argument. Needs `kotlinc` and `java` on the PATH.

Kotlin is compiled, so the binding's table is turned into one Kotlin file,
compiled once (and cached until the table or the code changes), and then driven
over a pipe. What the binding's fields mean here:

  setup    Kotlin declarations, placed in a class that is created afresh for
           every run. `import` lines are moved to the top of the file. It must
           declare `val system`.
  actions  Kotlin statements. `{0}`, `{1}` become the action's arguments as
           literals ("alice", 3, true), so they take whatever type the code
           expects. An enum value of the model arrives as its name in text:
           convert it, e.g. `Notice.valueOf({1})`. An action is refused when
           its statements throw; call `refuse()` to turn a return value into
           a refusal.
  (a test double of more than a few lines belongs in a `.kt` file among the
  sources, not in `setup`; inside a Lean string, Kotlin's quotes are `\"`)
  observe  Kotlin expressions. Values are shown as the model writes them:
           true/false, none for null, plain numbers and text, enum names,
           collections as ["a", "b"] (sets sorted).

The messages exchanged with the checker are those described in `python.py`;
the setup message also lists every concrete call the checker can make (`calls`).
"""
import glob
import hashlib
import json
import os
import subprocess
import sys
import tempfile

RUNTIME = r'''
class Refused(message: String) : Exception(message)
fun refuse(): Nothing = throw Refused("refused")

fun item(v: Any?): String = if (v is String) "\"" + v + "\"" else show(v)
fun show(v: Any?): String = when (v) {
    null -> "none"
    is String -> v
    is Enum<*> -> v.name
    is Set<*> -> v.map { item(it) }.sorted().joinToString(", ", "[", "]")
    is Collection<*> -> v.joinToString(", ", "[", "]") { item(it) }
    else -> v.toString()
}
fun esc(s: String): String = s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")

fun main() {
    val out = System.out
    System.setOut(System.err)  // the code under test may print
    val input = System.`in`.bufferedReader()
    out.println(Run().operations().joinToString("\t"))
    out.flush()
    while (true) {
        val line = input.readLine() ?: break
        val run = Run()
        val reply = StringBuilder(run.observe().joinToString("\t") { esc(it) })
        for (call in line.split(" ").filter { it.isNotEmpty() }) {
            var error = ""
            try { run.act(call.toInt()) } catch (e: Throwable) {
                error = (e::class.simpleName ?: "error") + ": " + (e.message ?: "")
            }
            reply.append("\n").append(if (error.isEmpty()) "0" else "1").append("\t").append(esc(error))
            for (value in run.observe()) reply.append("\t").append(esc(value))
        }
        out.println(reply)
        out.flush()
    }
}
'''


def literal(value):
    """A JSON value as a Kotlin literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value)).replace("$", "\\$")


def harness(config):
    """The Kotlin file for this binding: its table as code, plus the loop that serves the pipe."""
    imports, body = [], []
    for line in config["setup"].splitlines():
        (imports if line.strip().startswith("import ") else body).append(line)
    branches = []
    for index, call in enumerate(config["calls"]):
        code = config["actions"][call["action"]]
        for position, arg in enumerate(call["args"]):
            code = code.replace("{%d}" % position, literal(arg))
        branches.append(f"        {index} -> {{ {code} }}")
    observe = ",\n".join(f"        {{ {expression} }}" for expression in config["observe"])
    return "\n".join(imports) + RUNTIME + f'''
class Run {{
{chr(10).join("    " + line for line in body)}

    fun act(call: Int) {{ when (call) {{
{chr(10).join(branches)}
        else -> error("unknown call")
    }} }}

    fun observe(): List<String> = listOf<() -> Any?>(
{observe}
    ).map {{ try {{ show(it()) }} catch (e: Throwable) {{ "error: " + e }} }}

    fun operations(): List<String> = (system as Any).javaClass.declaredMethods
        .filter {{ java.lang.reflect.Modifier.isPublic(it.modifiers) && !it.isSynthetic }}
        .map {{ m -> if (m.name.length > 3 && m.name.startsWith("get") && m.parameterCount == 0)
            m.name.substring(3, 4).lowercase() + m.name.substring(4) else m.name }}
        .distinct().sorted()
}}
'''


def build(config, needs):
    """Compile the harness with the code's sources against its classpath; returns (jar, classpath)."""
    sources, classpath = [], []
    for need in needs:
        if need.endswith(".kt") or glob.glob(os.path.join(need, "**", "*.kt"), recursive=True):
            sources.append(need)
        else:
            classpath.extend(sorted(glob.glob(need)) or [need])
    text = harness(config)
    stamp = hashlib.sha256(text.encode())
    for path in sources + classpath:
        files = [path] if os.path.isfile(path) else sorted(glob.glob(os.path.join(path, "**", "*"), recursive=True))
        for file in files:
            if os.path.isfile(file):
                stat = os.stat(file)
                stamp.update(f"{file} {stat.st_mtime_ns} {stat.st_size}".encode())
    cache = os.path.join(tempfile.gettempdir(), "quintessence-kotlin", stamp.hexdigest()[:24])
    os.makedirs(cache, exist_ok=True)
    jar = os.path.join(cache, "harness.jar")
    if not os.path.exists(jar):
        source = os.path.join(cache, "Harness.kt")
        with open(source, "w", encoding="utf-8") as out:
            out.write(text)
        command = ["kotlinc", source, *sources, "-include-runtime", "-nowarn", "-d", jar]
        if classpath:
            command += ["-cp", os.pathsep.join(classpath)]
        done = subprocess.run(command, capture_output=True, text=True)
        if done.returncode != 0 or not os.path.exists(jar):
            raise RuntimeError("the binding does not compile:\n" + (done.stderr or done.stdout)[-1500:])
    return jar, classpath


def unesc(text):
    return text.replace("\\t", "\t").replace("\\n", "\n").replace("\\\\", "\\")


def main():
    *needs, root = sys.argv[1:]
    os.chdir(root)
    jvm, calls = None, {}
    for line in sys.stdin:
        message = json.loads(line)
        try:
            if "setup" in message:
                jar, classpath = build(message, needs)
                calls = {json.dumps([c["action"], c["args"]]): str(i) for i, c in enumerate(message["calls"])}
                jvm = subprocess.Popen(["java", "-cp", os.pathsep.join([jar, *classpath]), "HarnessKt"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                first = jvm.stdout.readline()
                if not first:
                    raise RuntimeError("the code could not be started (see the error output above)")
                reply = {"operations": [name for name in first.rstrip("\n").split("\t") if name]}
            else:
                steps = message["run"]
                jvm.stdin.write(" ".join(calls[json.dumps([s["action"], s["args"]])] for s in steps) + "\n")
                jvm.stdin.flush()
                rows = [jvm.stdout.readline().rstrip("\n").split("\t") for _ in range(len(steps) + 1)]
                if not rows[-1][0] and len(rows[-1]) == 1 and steps:
                    raise RuntimeError("the code stopped answering")
                reply = {"initial": [unesc(v) for v in rows[0]],
                         "steps": [{"refused": row[0] == "1", "error": unesc(row[1]),
                                    "observed": [unesc(v) for v in row[2:]]} for row in rows[1:]]}
        except Exception as error:
            reply = {"error": f"{type(error).__name__}: {error}"}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
