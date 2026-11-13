A review comes first: {why}

This is the periodic reconsideration: is the current path still worth it, or is a bigger
change, or a different approach, needed?

1. {arl} review new          (if none is open; it writes the facts into research/reviews/)
2. Do it with fresh eyes. If you can start a subagent, give the review to one that has not
   done the work: pass it the paths (research/PROGRAM.md, STATUS.md, IDEAS.md, tracks/, the
   review file, the experiments in the window) and ask it to write sections 1 to 7.
   Otherwise reread those files before writing. Do not write from memory.
3. Do what PROGRAM.md lists under "At every review".
4. Section 3 argues against the current path as well as you can. Section 4 goes through
   every open idea.
5. Close it:
     {arl} review close <id> --decision <continue|adjust|branch|pivot|stop|escalate>
   A decision the agent may not take alone then waits for a person.
6. Carry out section 7 with the id of this review: `{arl} track new <name> --review <id>`,
   `{arl} track set <name> --status parked --review <id> --note "..."`, `{arl} idea set ...`,
   `{arl} hyp new ...`. Only the latest closed review opens, parks or reopens tracks.
