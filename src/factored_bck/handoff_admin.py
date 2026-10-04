"""Local administrator preflight, account provisioning and deterministic queue retry."""

import argparse
import os
from pathlib import Path

from factored_bck.handoff_store import HandoffStore
from factored_bck.settings import Settings
from factored_bck.store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    provision = commands.add_parser("provision")
    provision.add_argument("--username", required=True)
    provision.add_argument("--agent-id", required=True)
    provision.add_argument("--password-file", required=True, type=Path)
    reroute = commands.add_parser("reroute")
    reroute.add_argument("--handoff-id", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        store = Store(Settings())
        store.initialize()
        service = HandoffStore(store)
        service.check_configuration()
        if args.command == "provision":
            service.agents.provision(
                args.username, args.agent_id, args.password_file.read_text().strip()
            )
        elif args.command == "reroute":
            result = service.reroute(args.handoff_id)
            print("Routing result: " + result["status"])
        print("Operation completed. No credentials or case contents are printed.")
        return 0
    except Exception as exc:
        print("Operation failed: " + type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
