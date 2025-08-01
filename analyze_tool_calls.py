#!/usr/bin/env python3
"""Analyze the tool calls test data to understand what's available."""

import argparse
import json

# Parse command line arguments
parser = argparse.ArgumentParser(description="Analyze tool calls test data")
parser.add_argument(
    "filename",
    nargs="?",
    default="tool_calls_test_data.json",
    help="JSON file to analyze (default: tool_calls_test_data.json)",
)
args = parser.parse_args()

with open(args.filename, encoding="utf-8") as f:
    data = json.load(f)

# Find examples with actual tool calls
examples_with_calls = [d for d in data if d.get("tool_calls")]

print(f"Total records: {len(data)}")
print(f"Records with tool_calls: {len(examples_with_calls)}")
print(f"Records without tool_calls: {len(data) - len(examples_with_calls)}")

# Analyze tool usage
tool_counts = {}
for record in examples_with_calls:
    for call in record["tool_calls"]:
        tool_name = call["function"]["name"]
        tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1

print("\nTool usage summary:")
for tool, count in sorted(tool_counts.items(), key=lambda x: x[1], reverse=True):
    print(f"  {tool}: {count}")

# Show a few examples
print("\n\nExample test cases:")
print("=" * 80)

for i, record in enumerate(examples_with_calls[:5]):
    tool_call = record["tool_calls"][0]
    tool_name = tool_call["function"]["name"]
    tool_args = tool_call["function"]["arguments"]

    # Find matching response
    tool_response = None
    if record.get("tool_responses"):
        for resp in record["tool_responses"]:
            if resp["tool_call_id"] == tool_call["id"]:
                tool_response = resp
                break

    print(f"\nExample {i + 1}: {tool_name}")
    print(f"Timestamp: {record['timestamp']}")
    print(f"Turn ID: {record['turn_id']}")

    # Parse tool_args if it's a string before pretty-printing
    try:
        parsed_args = json.loads(tool_args) if isinstance(tool_args, str) else tool_args
        print(f"Arguments: {json.dumps(parsed_args, indent=2)}")
    except json.JSONDecodeError:
        print(f"Arguments: {tool_args}")

    if tool_response:
        content = tool_response["content"]
        if len(content) > 300:
            content = content[:300] + "..."
        print(f"Response: {content}")
    else:
        print("Response: No matching response found")

    print("-" * 80)
