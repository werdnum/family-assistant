"""Temporary, for a do-not-merge CI control run: print the shards still running."""

import pathlib
import subprocess

root = pathlib.Path(".pytest-adaptive")
lines = (root / "joblog.tsv").read_text().splitlines()[1:]
done = {line.split()[0] for line in lines}
print(f"==DIAG== {len(lines)} shards finished", flush=True)
for shard in sorted(root.glob("shards/shard-*.json")):
    number = shard.stem.split("-")[1]
    if number in done:
        continue
    print(f"==DIAG== RUNNING {shard.name}: {shard.read_text()[:400]}")
    result = root / "results" / number
    if result.exists():
        print(result.read_text()[-2000:])
print(
    subprocess.run(
        "ps -eo pid,etimes,pcpu,args --sort=-etimes | cut -c1-200 | head -30",
        shell=True,
        check=False,
        capture_output=True,
        text=True,
    ).stdout,
    flush=True,
)
