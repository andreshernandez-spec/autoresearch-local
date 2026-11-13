{target} is drafted. Finish the design, then run it.

1. Write {exp}/design.md. The prediction comes before the result: say what you expect and
   what would count against the hypothesis.
2. {change_step}
3. Run it:  {arl} exp run {target}
   Runs can be long: start it in the background and wait for it to end. Output goes to log
   files. Read the end with `{arl} exp log {target}`, never the whole log.
To drop the draft instead:  {arl} exp abandon {target} --reason "..."
