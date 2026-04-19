# Tree (B1) best case — mruby.cve-2022-0240

The boss decomposed the task, spawned two workers (build + patch), and the patch worker wrote `fix.patch`. The retrofitted mechanical evaluator applied the patch successfully (patch_exit=0) but `secb repro` still triggered the expected SEGV after the patch, so `fixer_pass` = False. This is the only B-cell run that passed both `builder_pass` and `exploiter_pass` — yet the model's patch did not actually suppress the sanitizer error.

```
# run_dir: dataset/runs/mruby.cve-2022-0240/B1/0
#  0 [BOSS   ] agent_created  task=Reproduce and patch CVE-2022-0240 in mruby. Follow the deliverable contract in t
#  1 [BOSS   ] status_changed
#  2 [BOSS   ] run_started
#  3 [BOSS   ] tool_result  domain=AgentExecutionStarted
#  4 [BOSS   ] tool_result  domain=OperationStarted
#  5 [BOSS   ] prompt_sent  type=decompose len=11845
#  6 [BOSS   ] tokens_consumed  in=274 out=301 cache_r=0 cost=$0.000222 op=context_condense
#  7 [BOSS   ] tokens_consumed  in=139 out=155 cache_r=0 cost=$0.000114 op=context_condense
#  8 [BOSS   ] tokens_consumed  in=173 out=112 cache_r=0 cost=$9.3e-05 op=context_condense
#  9 [BOSS   ] tokens_consumed  in=137 out=142 cache_r=0 cost=$0.000106 op=context_condense
# 10 [BOSS   ] tokens_consumed  in=167 out=121 cache_r=0 cost=$9.8e-05 op=context_condense
# 11 [BOSS   ] tokens_consumed  in=137 out=134 cache_r=0 cost=$0.000101 op=context_condense
# 12 [BOSS   ] tokens_consumed  in=197 out=137 cache_r=0 cost=$0.000112 op=context_condense
# 13 [BOSS   ] tokens_consumed  in=134 out=101 cache_r=0 cost=$8.1e-05 op=context_condense
# 14 [BOSS   ] tool_result  domain=ProbeStarted
# 15 [BOSS   ] tool_result  domain=ProbeCompleted
# 16 [BOSS   ] tool_result  domain=ProbeStarted
# 17 [BOSS   ] tool_result  domain=ProbeCompleted
# 18 [BOSS   ] tool_result  domain=ProbeStarted
# 19 [BOSS   ] tool_result  domain=ProbeCompleted
# 20 [BOSS   ] tool_result  domain=ProbeStarted
# 21 [BOSS   ] tool_result  domain=ProbeCompleted
# 22 [BOSS   ] tool_result  domain=ProbeStarted
# 23 [BOSS   ] tool_result  domain=ProbeCompleted
# 24 [BOSS   ] tool_result  domain=ProbeStarted
# 25 [BOSS   ] tool_result  domain=ProbeCompleted
# 26 [BOSS   ] tool_result  domain=ProbeStarted
# 27 [BOSS   ] tool_result  domain=ProbeCompleted
# 28 [BOSS   ] tool_result  domain=ProbeStarted
# 29 [BOSS   ] tool_result  domain=ProbeCompleted
# 30 [BOSS   ] tool_result  domain=ProbeStarted
# 31 [BOSS   ] tool_result  domain=ProbeCompleted
# 32 [BOSS   ] tool_result  domain=ProbeStarted
# 33 [BOSS   ] tool_result  domain=ProbeCompleted
# 34 [BOSS   ] tool_result  domain=ProbeStarted
```
