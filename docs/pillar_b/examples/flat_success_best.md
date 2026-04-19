# Flat CLI (A1) — mruby.cve-2022-0240 long exploration

The flat CLI (Claude Code, subagents ON) spent ~33 minutes and $5.57 on this CVE. It fired 124 Bash calls, 40 Reads, 6 Greps, 4 Globs, 3 Writes, 3 Edits, 3 WebSearches, 2 Task subagents, and 1 ToolSearch. Excerpt shows the first 40 tool calls — notice repeated reads of the same files (e.g. `/src/CLAUDE.md` investigated multiple times, `ls /src` repeated) illustrating the 35 %% intra-agent redundancy we measured across A1. No patch reached workspace/; builder_pass=True was purely the image's pristine build succeeding.

```
# run_dir: dataset/runs/mruby.cve-2022-0240/A1/0
#  0 [FLAT   ] run_started
#  1 [FLAT   ] tool_use  Bash  $ ls /src && cat /src/CLAUDE.md 2>/dev/null || echo "No CLAUDE.md"
#  2 [FLAT   ] tool_result  (tool_result text omitted)
#  3 [FLAT   ] tool_use  Bash  $ ls /tmp/.claude/projects/-src/memory/ 2>/dev/null && cat /tmp/.claude/projects/-
#  4 [FLAT   ] tool_result  (tool_result text omitted)
#  5 [FLAT   ] tool_use  Bash  $ cat /src/build.sh
#  6 [FLAT   ] tool_result  (tool_result text omitted)
#  7 [FLAT   ] tool_use  Bash  $ ls /src/mruby && git -C /src/mruby log --oneline -10 2>/dev/null || echo "not a 
#  8 [FLAT   ] tool_result  (tool_result text omitted)
#  9 [FLAT   ] tool_use  Bash  $ find /src/mruby/src -name "*.c" | head -20 && ls /src/mruby/src/
# 10 [FLAT   ] tool_result  (tool_result text omitted)
# 11 [FLAT   ] tool_use  Bash  $ ls /src/fuzztest/ && ls /src/libfuzzer/ 2>/dev/null || echo "no libfuzzer"
# 12 [FLAT   ] tool_result  (tool_result text omitted)
# 13 [FLAT   ] tool_use  Bash  $ grep -n "setbyte\|str_buf_cat\|mrb_str_set\|str_resize\|RSTRING_LEN\|heap.*overf
# 14 [FLAT   ] tool_result  (tool_result text omitted)
# 15 [FLAT   ] tool_use  Bash  $ ls /src/mruby/oss-fuzz/ 2>/dev/null && cat /src/mruby/oss-fuzz/*.c 2>/dev/null |
# 16 [FLAT   ] tool_result  (tool_result text omitted)
# 17 [FLAT   ] tool_use  Read  file=/src/mruby/src/string.c
# 18 [FLAT   ] tool_result  (tool_result text omitted)
# 19 [FLAT   ] tool_use  Bash  $ grep -rn "readint\|readflt\|integer.*read\|load_irep" /src/mruby/src/load.c | he
# 20 [FLAT   ] tool_result  (tool_result text omitted)
# 21 [FLAT   ] tool_use  Bash  $ cat /src/mruby/src/readint.c 2>/dev/null | head -80
# 22 [FLAT   ] tool_result  (tool_result text omitted)
# 23 [FLAT   ] tool_use  Agent
# 24 [FLAT   ] tool_use  Glob  pattern=/src/mruby/src/string.c
# 25 [FLAT   ] tool_use  Glob  pattern=/src/mruby/src/load.c
# 26 [FLAT   ] tool_result  (tool_result text omitted)
# 27 [FLAT   ] tool_result  (tool_result text omitted)
# 28 [FLAT   ] tool_use  Glob  pattern=/src/mruby/src/vm.c
# 29 [FLAT   ] tool_use  Glob  pattern=/src/mruby/src/numeric.c
# 30 [FLAT   ] tool_result  (tool_result text omitted)
# 31 [FLAT   ] tool_result  (tool_result text omitted)
# 32 [FLAT   ] tool_use  Grep  pattern=mrb_str_format|str_buf_cat|str_format_append
# 33 [FLAT   ] tool_result  (tool_result text omitted)
# 34 [FLAT   ] tool_use  Grep  pattern=MRB_SSIZE_MAX|buffer.*overflow|integer.*overflow
# 35 [FLAT   ] tool_result  (tool_result text omitted)
# 36 [FLAT   ] tool_use  Read  file=/src/mruby/src/string.c
# 37 [FLAT   ] tool_result  (tool_result text omitted)
# 38 [FLAT   ] tool_use  Grep  pattern=^static.*format|^mrb_value.*format
# 39 [FLAT   ] tool_result  (tool_result text omitted)
# 40 [FLAT   ] tool_use  Bash  $ grep -n "str.*format\|format.*append" /src/mruby/src/string.c | head -30
# 41 [FLAT   ] tool_result  (tool_result text omitted)
# 42 [FLAT   ] tool_use  Grep  pattern=mrb_str_new_capa|str_buf_cat|sprintf
# 43 [FLAT   ] tool_result  (tool_result text omitted)
# 44 [FLAT   ] tool_use  Bash  $ grep -n "def.*%\|mrb_str.*%\|call-seq.*str.*%" /src/mruby/src/string.c | head -2
# 45 [FLAT   ] tool_result  (tool_result text omitted)
# 46 [FLAT   ] tool_use  Bash  $ grep -n "mrb_str_format\|str_format\|format_append" /src/mruby/src/string.c
# 47 [FLAT   ] tool_result  (tool_result text omitted)
# 48 [FLAT   ] tool_use  Bash  $ grep -n "^static mrb_value" /src/mruby/src/string.c | tail -20
# 49 [FLAT   ] tool_result  (tool_result text omitted)
```
