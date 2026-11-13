An introspection comes first: {why}

It has four parts, in this order.
  1. The path: is the current approach still worth it, does it need a bigger change, or is
     a different approach due?
  2. How the work went: what went wrong in the way of working, and which lessons belong in
     the instructions agents load.
  3. What we know: what this window showed about the problem itself, distilled into
     research/KNOWLEDGE.md.
  4. Reload: read the knowledge and the lessons again before going on.

Steps:
1. {arl} introspect new      (if none is open; it writes the facts into research/introspections/)
2. Do it with fresh eyes. If you can start a subagent, give the writing to one that has not
   done the work: pass it the paths (research/PROGRAM.md, STATUS.md, KNOWLEDGE.md, IDEAS.md,
   tracks/, the introspection file, the experiment folders in the window) and ask it to
   write sections 1 to 11. Otherwise reread those files first. Do not write from memory.
3. Do what PROGRAM.md asks for at every introspection (its "At every introspection" or
   "At every review" section).
4. Part 1: section 3 argues against the current path as well as you can, section 4 goes
   through every open idea.
5. Part 2: each lesson is recorded, not only described:
     {arl} lesson add --scope general|project --title "..." --body "what happened, what to do instead" --from <id>
   general = any project that uses arl, project = only this one.
6. Part 3: edit research/KNOWLEDGE.md itself. It is a distillation: merge and rewrite.
7. Close it:
     {arl} introspect close <id> --decision <continue|adjust|branch|pivot|stop|escalate>
   If part 2 or part 3 truly has nothing: --no-lessons "why", --knowledge-unchanged "why".
   A decision the agent may not take alone then waits for a person.
8. {arl} reload              (part 4: prints the knowledge and the lessons; read them)
9. Carry out section 7 with the id of this introspection:
   `{arl} track new <name> --introspection <id>`,
   `{arl} track set <name> --status parked --introspection <id> --note "..."`,
   `{arl} idea set ...`, `{arl} hyp new ...`.
