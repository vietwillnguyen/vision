"""A stand-in ``nmcli`` executable for the onboarding tests.

Written onto ``PATH`` by :mod:`tests.test_onboarding` so the real
``SubprocessNmcliRunner`` is exercised end to end: every invocation appends its
own ``argv`` and the bytes it was handed on stdin to a transcript, which lets a
test assert what the daemon exposed in the process list and what it kept off it.

Only the three forms onboarding uses are understood - listing profile names,
the connection editor, and reading a property back.
"""

import json
import os
import sys
from pathlib import Path


def _load(store: Path) -> dict[str, dict[str, str]]:
    try:
        return json.loads(store.read_text())
    except FileNotFoundError:
        return {}


def main(argv: list[str]) -> int:
    record_dir = Path(os.environ["NMCLI_STUB_DIR"])
    store = record_dir / "profiles.json"
    stdin = sys.stdin.read()
    with (record_dir / "transcript.jsonl").open("a", encoding="utf-8") as transcript:
        transcript.write(json.dumps({"argv": argv, "stdin": stdin}) + "\n")

    if os.environ.get("NMCLI_STUB_FAIL_ON", "") in argv:
        print("Error: stub refusal", file=sys.stderr)
        return 4

    profiles = _load(store)

    # nmcli -g NAME connection show
    if argv[:3] == ["-g", "NAME", "connection"]:
        for name in profiles:
            print(name)
        return 0

    # nmcli connection add|modify ... <property> <value> ...
    # The pre-shared-key-on-argv form this package no longer uses; understood
    # so a test that asserts the secret is off argv fails on that assertion
    # rather than on an unknown command.
    if argv[:2] in (["connection", "add"], ["connection", "modify"]):
        name = argv[argv.index("con-name") + 1] if "con-name" in argv else argv[3]
        pending = dict(profiles.get(name, {}))
        words = argv[2:]
        for index, word in enumerate(words[:-1]):
            if "." in word:
                pending[word.replace("wifi-sec.", "802-11-wireless-security.")] = words[
                    index + 1
                ]
        profiles[name] = pending
        store.write_text(json.dumps(profiles))
        return 0

    # nmcli connection edit [id <ID> | type wifi con-name <ID>]
    if argv[:2] == ["connection", "edit"]:
        name = (
            argv[argv.index("id") + 1]
            if "id" in argv
            else argv[argv.index("con-name") + 1]
        )
        pending = dict(profiles.get(name, {}))
        saved = False
        for line in stdin.splitlines():
            head, _, rest = line.partition(" ")
            if head == "set":
                prop, _, value = rest.partition(" ")
                # The editor strips the separators between the property and
                # its value, and drops tabs inside it; mirroring that is the
                # point of a stub, so a test can catch a mangled secret.
                pending[prop] = value.strip().replace("\t", "")
            elif head == "save":
                profiles[name] = pending
                saved = True
            elif head == "quit":
                break
        if saved:
            store.write_text(json.dumps(profiles))
        return 0

    # nmcli -e no --show-secrets -g <property> connection show id <ID>
    if "-g" in argv and argv[-2] == "id":
        prop = argv[argv.index("-g") + 1]
        print(profiles.get(argv[-1], {}).get(prop, ""))
        return 0

    print(f"Error: stub does not understand {argv!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
