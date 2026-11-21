"""Exercise Harbor, the DSH SDK, a real shell tool and the verifier with fake inference."""

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from harbor.models.trajectories.trajectory import Trajectory

MODEL = "deepseek-flash"
PROMPT = (Path(__file__).parent / "smoke-task/instruction.md").read_text().strip()
COMMAND = "printf 'DSH_OK\\n' > /tmp/dsh-smoke-answer && printf 'tool completed\\n'"
FINAL = "fixture complete"


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer " + self.server.key:
            self.send_error(401)
            return
        if self.path != "/v1/chat/completions" or req.get("model") != MODEL:
            self.send_error(400, "Wrong route or model")
            return
        messages = req["messages"]
        if not any(
            m["role"] == "user" and PROMPT in str(m["content"]) for m in messages
        ):
            self.send_error(400, "Missing fixture prompt")
            return
        if any(
            m["role"] == "tool" and "tool completed" in str(m["content"])
            for m in messages
        ):
            delta, finish = {"content": FINAL}, "stop"
        else:
            names = [t["function"]["name"] for t in req["tools"]]
            bash = next((n for n in names if "bash" in n.lower()), None)
            if not bash:
                self.send_error(400, f"Missing bash tool in {names}")
                return
            call = {
                "index": 0,
                "id": "fixture-shell",
                "type": "function",
                "function": {
                    "name": bash,
                    "arguments": json.dumps({"command": COMMAND}),
                },
            }
            delta, finish = {"tool_calls": [call]}, "tool_calls"
        self.server.events.append(finish)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk, reason in [
            ({"role": "assistant"}, None),
            (delta, None),
            ({}, finish),
        ]:
            packet = {
                "id": "fixture",
                "model": MODEL,
                "object": "chat.completion.chunk",
                "created": 1,
                "choices": [{"index": 0, "delta": chunk, "finish_reason": reason}],
            }
            self.wfile.write(f"data: {json.dumps(packet)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path, help="Fresh directory for Harbor artifacts")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument(
        "--host", default="host.docker.internal", help="Mock host as seen by the task"
    )
    parser.add_argument(
        "--wheelhouse",
        type=Path,
        help="Install the SDK from a directory of Linux amd64 Python 3.12 wheels",
    )
    args = parser.parse_args()
    install_args = []
    if args.wheelhouse:
        if not args.wheelhouse.is_dir():
            parser.error(f"Missing wheel directory: {args.wheelhouse}")
        mounts = [
            {
                "type": "bind",
                "source": str(source.resolve()),
                "target": target,
                "read_only": True,
            }
            for source, target in (
                (args.wheelhouse, "/opt/dsh-smoke-wheels"),
                (Path(__file__).with_name("pip-offline.conf"), "/etc/pip.conf"),
            )
        ]
        install_args = ["--mounts", json.dumps(mounts)]
    job = args.job.resolve()
    if job.exists():
        parser.error(f"Job directory already exists: {job}")
    with ThreadingHTTPServer((args.bind, 0), Mock) as server:
        server.events = []
        server.key = "dsh-test-" + secrets.token_hex(16)
        env = {
            **os.environ,
            "DEEPSEEK_API_KEY": server.key,
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                [
                    str(Path(sys.executable).with_name("harbor")),
                    "run",
                    "--path",
                    str(Path(__file__).resolve().parent / "smoke-task"),
                    "-a",
                    "dsh_tb_agent:DshAgent",
                    "-m",
                    "deepseek/" + MODEL,
                    "--base-url",
                    f"http://{args.host}:{server.server_port}/v1",
                    "--api-key-name",
                    "DEEPSEEK_API_KEY",
                    "-k",
                    "1",
                    "-n",
                    "1",
                    "--max-retries",
                    "0",
                    "--jobs-dir",
                    str(job.parent),
                    "--job-name",
                    job.name,
                    *install_args,
                ],
                env=env,
                check=True,
            )
        finally:
            server.shutdown()
    (result_file,) = job.glob("*/result.json")
    result = json.loads(result_file.read_text())
    if result["exception_info"] or result["verifier_result"]["rewards"] != {
        "reward": 1.0
    }:
        raise RuntimeError(f"Trial failed. Inspect {result_file}")
    logs = result_file.parent / "agent"
    native = json.loads((logs / "dsh-result.json").read_text())
    trace = Trajectory.model_validate_json((logs / "trajectory.json").read_text())
    if not (
        native["ok"]
        and native["final_response"].strip() == FINAL
        and native["sdk_version"]
        == native["runtime_version"]
        == trace.agent.version
        == "0.1.0rc6"
        and trace.session_id == native["session_id"]
        and any(s.tool_calls for s in trace.steps)
        and server.events == ["tool_calls", "stop"]
    ):
        raise RuntimeError("Incomplete tool round trip")
    if any(
        server.key.encode() in p.read_bytes() for p in job.rglob("*") if p.is_file()
    ):
        raise RuntimeError("Test credential present in artifacts")
    print(
        f"Verified two authenticated requests, shell tool, final answer, ATIF and reward=1: {result_file}"
    )


if __name__ == "__main__":
    main()
