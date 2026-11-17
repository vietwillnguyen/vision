"""A stand-in ``nmcli`` executable for the onboarding tests.

Written onto ``PATH`` by :mod:`tests.test_onboarding` so the real
``SubprocessNmcliRunner`` is exercised end to end: every invocation appends its
own ``argv`` and the bytes it was handed on stdin to a transcript, which lets a
test assert what the daemon exposed in the process list and what it kept off it.

Only the forms onboarding uses are understood - listing profile names,
``connection add``/``modify``, the connection editor, and reading a property
back.
"""

import json
import os
import sys
from pathlib import Path

_WSEC = "802-11-wireless-security."
_ALIASES = {
    "ssid": "802-11-wireless.ssid",
    "wifi.ssid": "802-11-wireless.ssid",
    "ifname": "connection.interface-name",
}


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

    # nmcli connection add type wifi con-name <ID> ifname <IF> ssid <SSID> ...
    # nmcli connection modify <ID> <property> <value> ...
    # argv carries every byte of a value as given.
    if argv[:2] in (["connection", "add"], ["connection", "modify"]):
        words = argv[2:] if argv[1] == "add" else argv[3:]
        pairs = dict(zip(words[::2], words[1::2], strict=True))
        name = pairs.pop("con-name", argv[2])
        pairs.pop("type", None)
        pending = dict(profiles.get(name, {}))
        for prop, value in pairs.items():
            pending[_ALIASES.get(prop, prop).replace("wifi-sec.", _WSEC)] = value
        profiles[name] = pending
        store.write_text(json.dumps(profiles))
        return 0

    # nmcli connection edit id <ID>
    if argv[:3] == ["connection", "edit", "id"]:
        name = argv[3]
        pending = dict(profiles.get(name, {}))
        saved = False
        lines = iter(stdin.splitlines())
        for line in lines:
            head, _, rest = line.partition(" ")
            if head == "set":
                prop, _, value = rest.partition(" ")
                if value:
                    # `set <property> <value>` strips the value and drops tabs.
                    pending[prop] = value.strip().replace("\t", "")
                else:
                    # `set <property>` prompts and reads the next line whole;
                    # readline still swallows a tab as completion.
                    pending[prop] = next(lines, "").replace("\t", "")
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
