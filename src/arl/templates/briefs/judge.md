Judge {target}. This step is separate from the design: work from the files, not from memory.

1. Read {exp}/design.md (what was predicted), then `{arl} exp show {target}` (what was measured).
2. Write {exp}/analysis.md: what happened against the prediction, the most likely reason, and
   what you did not check. A result you cannot explain is worth saying so.
3. Decide. The recorded outcome is "{outcome}"; allowed verdicts: {allowed}.
   - keep: the track adopts this code and these params.
   - discard: the branch stays in git, the track does not move.
   - an improvement can still be discarded when it buys little and adds much.
   - a neutral result can be kept only when it makes things simpler (--simpler, and say how).
4. Record it, and say what this run did to each linked hypothesis:
     {arl} exp judge {target} --verdict <verdict> --reason "<one sentence>"{hyp_flags}
5. Whatever this result suggests beyond the next step goes into IDEAS.md:
     {arl} idea add --title "..." --body "..." --from {target}
