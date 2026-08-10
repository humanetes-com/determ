  bash dl_muse_glimmer.sh
  
  No GPU, resumable, ~33 GB, logs to /tmp/dl_muse_glimmer.log. Then once your run is done or stopped, in order:
  
  bash compare_models.sh snapshot baseline
  bash compare_models.sh arm baseline qwen && bash pre_run.sh && bash continue.sh
  bash compare_models.sh arm baseline muse && bash pre_run.sh && bash continue.sh
  bash compare_models.sh report qwen=<run-id> muse=<run-id>
  
  Note each arm's run id from the launch banner; bash compare_models.sh list recovers them from the sink if you miss one. And
  take the cheap validation first — run_microcrate.sh on Muse before spending anyhow hours, so a model that ignores the
  FN/FILE output contract fails in minutes rather than looking like a slow model six hours in.
  
  Verified: ruff and mypy clean on the new tool, all four shell scripts pass bash -n, the analyzer produces correct grouped
  output on two real historical runs, and the live-run refusal fires. No Python under coverage changed since the last green
  check.sh (3085 passed), and tools/ is outside the coverage ratchet. The tree stamp is still moved from the earlier
  pipeline_core edit, so pre_run.sh is required before the next launch — which is why it's in each arm line above.

