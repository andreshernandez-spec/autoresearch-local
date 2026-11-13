Write up {target}. It was an observation: nothing is scored and nothing is merged.

1. Read {exp}/design.md (the question), then what the run left in {exp}/kept/ and
   {exp}/artifacts/. The end of the log: `{arl} exp log {target}`.
2. Write {exp}/analysis.md: the answer to the question, with the numbers and the files they
   come from, and where it differs from what you expected.
3. Record it:
     {arl} exp judge {target} --verdict finding --reason "<the answer in one sentence>"
4. Turn the answer into the next hypothesis (`{arl} hyp new`) or an idea (`{arl} idea add`).
