#!/bin/bash
jq -r '.tests[] | select(.outcome == "failed") |         
"--------------------------------------------------                                                   
TEST: \(.nodeid)                                                                                      
CRASH: \(.call.crash.path):\(.call.crash.lineno): \(.call.crash.message)                              
LOGS:                                                                                                 
\(.call.log // [] | map(.msg) | join("\n"))                                                           
      
"' .report.json

