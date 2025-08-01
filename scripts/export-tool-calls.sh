#!/bin/bash

# Export tool calls from message history for test case generation
# Usage: ./scripts/export-tool-calls.sh [database_url] [output_file]

set -e

# Default values
OUTPUT_FILE="${2:-tool_calls_test_data.json}"
DAYS_BACK="${DAYS_BACK:-7}"
LIMIT="${LIMIT:-100}"

# Create temporary file for error logging
ERROR_LOG=$(mktemp)
trap 'rm -f "$ERROR_LOG"' EXIT

# SQL query to extract tool calls and their results
# This gets assistant messages with tool_calls, plus related tool response messages
SQL_QUERY="
WITH tool_call_messages AS (
    -- Get assistant messages that made tool calls
    SELECT 
        internal_id,
        turn_id,
        timestamp,
        conversation_id,
        content,
        tool_calls,
        reasoning_info,
        processing_profile_id
    FROM message_history 
    WHERE tool_calls IS NOT NULL 
      AND role = 'assistant'
      AND timestamp > NOW() - INTERVAL '${DAYS_BACK} days'
),
-- Extract individual tool calls with their names
tool_calls_expanded AS (
    SELECT 
        tcm.*,
        tool_call->>'id' as tool_call_id,
        tool_call->'function'->>'name' as tool_name,
        ROW_NUMBER() OVER (
            PARTITION BY tool_call->'function'->>'name' 
            ORDER BY tcm.timestamp DESC
        ) as tool_rank
    FROM tool_call_messages tcm,
    LATERAL json_array_elements(
        CASE 
            WHEN json_typeof(tcm.tool_calls::json) = 'array' THEN tcm.tool_calls::json
            ELSE json_build_array(tcm.tool_calls::json)
        END
    ) as tool_call
),
-- Take max 3 calls per tool type
diverse_tool_calls AS (
    SELECT *
    FROM tool_calls_expanded
    WHERE tool_rank <= 3
),
tool_responses AS (
    -- Get corresponding tool responses for the same turn_id
    SELECT 
        dtc.internal_id as assistant_msg_id,
        dtc.turn_id,
        dtc.timestamp as call_timestamp,
        dtc.conversation_id,
        dtc.content as assistant_content,
        dtc.tool_calls,
        dtc.processing_profile_id,
        json_agg(
            json_build_object(
                'tool_call_id', tr.tool_call_id,
                'content', tr.content,
                'timestamp', tr.timestamp,
                'error_traceback', tr.error_traceback
            ) ORDER BY tr.timestamp
        ) FILTER (WHERE tr.tool_call_id IS NOT NULL) as tool_responses,
        COUNT(tr.tool_call_id) as response_count
    FROM diverse_tool_calls dtc
    LEFT JOIN message_history tr 
        ON dtc.turn_id = tr.turn_id 
        AND tr.role = 'tool'
        AND dtc.conversation_id = tr.conversation_id
    GROUP BY 
        dtc.internal_id,
        dtc.turn_id,
        dtc.timestamp,
        dtc.conversation_id,
        dtc.content,
        dtc.tool_calls,
        dtc.processing_profile_id
    -- Filter out calls with no responses
    HAVING COUNT(tr.tool_call_id) > 0
    ORDER BY call_timestamp DESC
    LIMIT ${LIMIT}
)
SELECT json_build_object(
    'internal_id', assistant_msg_id,
    'turn_id', turn_id,
    'timestamp', call_timestamp,
    'conversation_id', conversation_id,
    'assistant_content', assistant_content,
    'tool_calls', tool_calls::json,
    'tool_responses', tool_responses,
    'processing_profile_id', processing_profile_id
) 
FROM tool_responses
ORDER BY call_timestamp DESC;
"

echo "Exporting tool calls from the last ${DAYS_BACK} days (limit: ${LIMIT})..."
echo "Output: ${OUTPUT_FILE}"

# Execute query and format as JSON array
kubectl exec -in postgres deploy/storage-cluster-shell -- psql -U postgres mlbot -t -A \
    -c "${SQL_QUERY}" | \
    grep -v '^$' | \
    jq -s '.' > "${OUTPUT_FILE}" 2>"$ERROR_LOG"

# Check for jq errors
if [ $? -ne 0 ]; then
    echo "✗ Error processing JSON output:"
    cat "$ERROR_LOG"
    exit 1
fi

# Check if we got any results
if [ -s "${OUTPUT_FILE}" ]; then
    COUNT=$(jq 'length' "${OUTPUT_FILE}")
    echo "✓ Exported ${COUNT} tool call interactions to ${OUTPUT_FILE}"
    
    # Show a summary
    echo ""
    echo "Tool call summary:"
    jq -r '
        map(.tool_calls // [] | map(.function.name // empty)) | 
        flatten | 
        group_by(.) | 
        map({tool: .[0], count: length}) | 
        sort_by(.count) | 
        reverse | 
        .[] | 
        "  \(.tool): \(.count)"
    ' "${OUTPUT_FILE}"
else
    echo "✗ No tool calls found in the specified time range"
    rm -f "${OUTPUT_FILE}"
    exit 1
fi
