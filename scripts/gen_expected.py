#!/usr/bin/env python3
"""Reference implementation of spec/SPEC.md normalization (stdlib only).

Reads raw fixtures in spec/fixtures/{engine,cli} and writes spec/fixtures/expected/*.json.
The app tests assert normalization against these files.

    python3 scripts/gen_expected.py
"""

from __future__ import annotations

import json
import pathlib
import re

FX = pathlib.Path(__file__).resolve().parent.parent / "spec" / "fixtures"


def load(name):
    return json.loads((FX / name).read_text())


def jsonl(name):
    return [json.loads(l) for l in (FX / name).read_text().splitlines() if l.strip()]


def health(status: str):
    if "(healthy)" in status:
        return "healthy"
    if "(unhealthy)" in status:
        return "unhealthy"
    if "(health: starting)" in status:
        return "starting"
    return None


def exit_code(status: str):
    m = re.match(r"Exited \((-?\d+)\)", status)
    return int(m.group(1)) if m else None


def port_key(p: str):
    if "->" in p:
        pub, priv = p.split("->")
        return (int(pub), int(priv))
    return (int(p), int(p))


def container(c: dict) -> dict:
    ports, seen = [], set()
    for p in c.get("Ports") or []:
        if p.get("PublicPort"):
            s = f"{p['PublicPort']}->{p['PrivatePort']}"
        elif p.get("PrivatePort"):
            s = str(p["PrivatePort"])
        else:
            continue
        if s not in seen:
            seen.add(s)
            ports.append(s)
    labels = c.get("Labels") or {}
    status = c.get("Status", "")
    return {
        "id": c["Id"][:12],
        "name": ",".join(sorted(n.lstrip("/") for n in c.get("Names") or [])),
        "image": c.get("Image", ""),
        "state": c.get("State", "").lower(),
        "status": status,
        "health": health(status),
        "exit_code": exit_code(status),
        "project": labels.get("com.docker.compose.project", ""),
        "service": labels.get("com.docker.compose.service", ""),
        "ports": sorted(ports, key=port_key),
    }


def image(i: dict) -> dict:
    tags = [t for t in (i.get("RepoTags") or []) if t != "<none>:<none>"]
    if tags:
        ref = tags[0].split("@sha256:")[0]
    elif i.get("RepoDigests"):
        repo, _, digest = i["RepoDigests"][0].partition("@")
        ref = f"{repo}@{digest.removeprefix('sha256:')[:12]}"
    else:
        ref = "<none>:<none>"
    n = i.get("Containers")
    return {
        "id": i["Id"].removeprefix("sha256:")[:12],
        "ref": ref,
        "size_bytes": i["Size"],
        "containers": None if n is None or n < 0 else n,
        "dangling": not tags,
    }


def cpu_percent(s: dict, prev_cpu: dict):
    cur = s["cpu_stats"]
    cpu_delta = cur["cpu_usage"]["total_usage"] - (prev_cpu.get("cpu_usage") or {}).get("total_usage", 0)
    sys_prev = prev_cpu.get("system_cpu_usage")
    if sys_prev is None or cur.get("system_cpu_usage") is None:
        return None
    sys_delta = cur["system_cpu_usage"] - sys_prev
    ncpu = cur.get("online_cpus") or len(cur["cpu_usage"].get("percpu_usage") or []) or 1
    if sys_delta > 0 and cpu_delta >= 0:
        return round(cpu_delta / sys_delta * ncpu * 100, 4)
    return None


def mem(s: dict) -> dict:
    ms = s.get("memory_stats") or {}
    st = ms.get("stats") or {}
    cache = st.get("inactive_file", st.get("total_inactive_file", 0))
    return {"mem_bytes": max(0, ms.get("usage", 0) - cache), "mem_limit_bytes": ms.get("limit", 0)}


def demux(raw: bytes) -> list[dict]:
    frames, i = [], 0
    while i + 8 <= len(raw):
        stream, size = raw[i], int.from_bytes(raw[i + 4:i + 8], "big")
        frames.append((stream, raw[i + 8:i + 8 + size]))
        i += 8 + size
    lines, buf = [], {1: b"", 2: b""}
    name = {1: "stdout", 2: "stderr"}
    for stream, chunk in frames:
        buf[stream] += chunk
        *done, buf[stream] = buf[stream].split(b"\n")
        lines += [{"stream": name[stream], "text": d.decode().removesuffix("\r")} for d in done]
    for stream, rest in buf.items():  # final line without trailing newline
        if rest:
            lines.append({"stream": name[stream], "text": rest.decode().removesuffix("\r")})
    return lines


def frame(stream: int, payload: bytes) -> bytes:
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


def tty_lines(raw: bytes) -> list[str]:
    parts = raw.decode().split("\n")
    if parts and parts[-1] == "":
        parts.pop()  # trailing newline terminates the last line, it is not an empty line
    return [p.removesuffix("\r") for p in parts]


def remote_ports(s: str) -> list[str]:
    out, seen = [], set()
    for pub, priv in re.findall(r"(\d+)->(\d+)", s):
        p = f"{pub}->{priv}"
        if p not in seen:
            seen.add(p)
            out.append(p)
    covered = {p.split("->")[1] for p in out} | {p.split("->")[0] for p in out}
    for bare in re.findall(r"(?:^|,)\s*(\d+)/(?:tcp|udp)", s):
        if bare not in seen and bare not in covered:
            seen.add(bare)
            out.append(bare)
    return sorted(out, key=port_key)


def docker_size(s: str) -> int:
    s = s.strip()
    for suf, mult in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("kB", 10**3), ("KB", 10**3), ("B", 1)):
        if s.endswith(suf):
            return int(float(s[: -len(suf)]) * mult)
    return 0


def label_value(labels: str, key: str) -> str:
    for kv in labels.split(","):
        k, _, v = kv.strip().partition("=")
        if k == key:
            return v
    return ""


def write(name, data):
    (FX / "expected" / name).write_text(json.dumps(data, indent=2) + "\n")
    print("wrote expected/" + name)


def main():
    conts = sorted((container(c) for c in load("engine/containers_json.json")),
                   key=lambda c: (c["project"], c["name"]))
    write("containers.json", conts)
    write("images.json", sorted((image(i) for i in load("engine/images_json.json")),
                                key=lambda i: -i["size_bytes"]))

    s1, s2, sf = load("engine/stats_oneshot_1.json"), load("engine/stats_oneshot_2.json"), load("engine/stats_streamfalse.json")
    write("stats.json", {
        "oneshot_first": {"cpu_percent": cpu_percent(s1, s1["precpu_stats"]), **mem(s1)},
        "oneshot_second_vs_first": {"cpu_percent": cpu_percent(s2, s1["cpu_stats"]), **mem(s2)},
        "stream_false": {"cpu_percent": cpu_percent(sf, sf["precpu_stats"]), **mem(sf)},
    })

    raw = (FX / "engine/logs_mux.bin").read_bytes()
    tty = (FX / "engine/logs_tty.bin").read_bytes()
    write("logs.json", {"mux": demux(raw),
                        "tty": tty_lines(tty)})

    write("remote_containers.json", sorted((
        {"id": p["ID"][:12], "name": p["Names"], "image": p["Image"], "state": p["State"].lower(),
         "status": p["Status"], "health": health(p["Status"]), "exit_code": exit_code(p["Status"]),
         "project": label_value(p["Labels"], "com.docker.compose.project"),
         "service": label_value(p["Labels"], "com.docker.compose.service"),
         "ports": remote_ports(p["Ports"])}
        for p in jsonl("cli/docker_ps.jsonl")), key=lambda c: (c["project"], c["name"])))
    write("remote_images.json", sorted((
        {"id": i["ID"][:12], "ref": "<none>:<none>" if i["Repository"] == "<none>" else f"{i['Repository']}:{i['Tag']}",
         "size_bytes": docker_size(i["Size"]), "containers": int(i["Containers"]) if i["Containers"].lstrip("-").isdigit() and int(i["Containers"]) >= 0 else None,
         "dangling": i["Repository"] == "<none>"}
        for i in jsonl("cli/docker_images.jsonl")), key=lambda i: -i["size_bytes"]))

    write("parsers.json", {
        "remote_ports": {s: remote_ports(s) for s in [
            "0.0.0.0:8080->80/tcp, :::8080->80/tcp", "9000/tcp", "0.0.0.0:5432->5432/tcp, 5432/tcp",
            "", "0.0.0.0:13000->3000/tcp, :::13000->3000/tcp, 80/tcp"]},
        "docker_size": {s: docker_size(s) for s in ["376MB", "70.9MB", "22.6kB", "1.02GB", "1.5TB", "0B", "512B"]},
        "label_value": {"com.docker.compose.project=shop,com.docker.compose.service=api": "shop", "maintainer=x": ""},
        "demux_edge_cases": {
            "blank_lines_and_unterminated_tail": demux(frame(1, b"a\n\nb\nend") + frame(2, b"err\n")),
            "tty_blank_lines": tty_lines(b"one\r\n\ntwo\nlast"),
        },
    })


if __name__ == "__main__":
    main()
