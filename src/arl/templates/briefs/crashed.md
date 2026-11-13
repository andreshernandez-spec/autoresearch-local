{target} failed: {why}

Read the end of the log:  {arl} exp log {target}
- A slip in the code (typo, shape, import): fix it in the worktree, then
    {arl} exp commit {target} -m "..."   and   {arl} exp run {target}
- The idea cannot work, the design was wrong, the attempts are used up, or the track has no
  reference any more:
    {arl} exp judge {target} --verdict crash --reason "<what broke>"
design.md and params.json are locked from the first run on. A wrong design is judged as a
crash and replaced by a new experiment.
