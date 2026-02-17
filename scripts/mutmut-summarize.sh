#!/usr/bin/env bash
set -euo pipefail

# Aggregate mutmut-summary.json files from CI matrix jobs into a markdown table.
#
# Usage:
#   scripts/mutmut-summarize.sh [ARTIFACTS_DIR]
#
# The script expects JSON summary files at:
#   ARTIFACTS_DIR/mutmut-results-*/mutmut-summary.json
#
# Output: Markdown table to stdout (pipe to $GITHUB_STEP_SUMMARY or gh pr comment)

ARTIFACTS_DIR="${1:-.}"

# Collect all summary JSON files
SUMMARY_FILES=()
while IFS= read -r -d '' file; do
    SUMMARY_FILES+=("$file")
done < <(find "$ARTIFACTS_DIR" -name "mutmut-summary.json" -print0 2>/dev/null | sort -z)

if [[ ${#SUMMARY_FILES[@]} -eq 0 ]]; then
    echo "No mutmut-summary.json files found in $ARTIFACTS_DIR"
    exit 1
fi

python3 -c "
import json, sys

files = sys.argv[1:]
summaries = []
for f in files:
    with open(f) as fh:
        summaries.append(json.load(fh))

# Sort by module name
summaries.sort(key=lambda s: s['module'])

# Calculate totals
total_mutants = sum(s['total'] for s in summaries)
total_killed = sum(s['killed'] for s in summaries)
total_survived = sum(s['survived'] for s in summaries)
total_no_tests = sum(s.get('no_tests', 0) for s in summaries)
total_timeout = sum(s.get('timeout', 0) for s in summaries)
total_suspicious = sum(s.get('suspicious', 0) for s in summaries)
overall_score = (total_killed / total_mutants * 100) if total_mutants > 0 else 0

# Generate markdown table
print('## Mutation Testing Results')
print()
print('| Module | Total | Killed | Survived | Score |')
print('|--------|------:|-------:|---------:|------:|')

for s in summaries:
    module = s['module']
    score_str = f\"{s['score']:.1f}%\"
    # Emoji indicators
    if s['score'] >= 80:
        indicator = '🟢'
    elif s['score'] >= 60:
        indicator = '🟡'
    else:
        indicator = '🔴'
    print(f\"| {module} | {s['total']} | {s['killed']} | {s['survived']} | {indicator} {score_str} |\")

# Totals row
if overall_score >= 80:
    total_indicator = '🟢'
elif overall_score >= 60:
    total_indicator = '🟡'
else:
    total_indicator = '🔴'
print(f'| **Total** | **{total_mutants}** | **{total_killed}** | **{total_survived}** | {total_indicator} **{overall_score:.1f}%** |')

print()

# Extra stats if present
if total_timeout > 0 or total_no_tests > 0 or total_suspicious > 0:
    extras = []
    if total_timeout > 0:
        extras.append(f'{total_timeout} timeouts')
    if total_no_tests > 0:
        extras.append(f'{total_no_tests} untested')
    if total_suspicious > 0:
        extras.append(f'{total_suspicious} suspicious')
    print(f\"_Additional: {', '.join(extras)}_\")
    print()
" "${SUMMARY_FILES[@]}"

