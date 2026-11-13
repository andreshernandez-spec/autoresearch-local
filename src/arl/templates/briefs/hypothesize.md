Nothing is in flight. Choose what to test next.

1. Read research/STATUS.md and the analysis of the last few experiments. Look at the open
   hypotheses and at IDEAS.md before inventing something new.
2. Write the hypothesis first: a claim about the problem with an observable consequence,
   not a plan.   {arl} hyp new --title "..."
3. Then pick the kind of experiment that tests it:
   - tune: the claim is about a setting. Params change, code does not.
   - feature: the claim needs new code.
   - diagnose: you do not know why something happens. Observe first. Not scored, never merged.
     {arl} exp new --type <type> --track <track> --hyp <id> --title "..."
4. One change per experiment. If two things change, the result says nothing about either.
