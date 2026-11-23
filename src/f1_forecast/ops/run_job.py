"""Trigger a Databricks job by name and wait for it to finish.

Bundle jobs deployed in development mode get a ``[dev <user>]`` prefix, so we match on the
end of the name. Usage::

    python -m f1_forecast.ops.run_job f1_forecast_jolpica_bronze
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta


def find_job_id(client, name: str) -> int:
    matches = [j for j in client.jobs.list() if j.settings and j.settings.name.endswith(name)]
    if len(matches) != 1:
        found = [j.settings.name for j in matches]
        raise SystemExit(f"Expected exactly one job ending with {name!r}, found {found}")
    return matches[0].job_id


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("job_name")
    p.add_argument("--timeout-min", type=int, default=45)
    args = p.parse_args(argv)

    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient()
    job_id = find_job_id(client, args.job_name)
    print(f"Running job {job_id} ({args.job_name})…", flush=True)
    run = client.jobs.run_now(job_id).result(timeout=timedelta(minutes=args.timeout_min))
    state = run.state.result_state.value if run.state and run.state.result_state else "UNKNOWN"
    print(f"Result: {state}  {run.run_page_url}")
    if state != "SUCCESS":
        sys.exit(1)


if __name__ == "__main__":
    main()
