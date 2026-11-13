{target} was drafted before the reference of track {track} stopped counting (the evaluator
changed, or the track's branch was synced). It cannot run as it is.

1. Drop the draft:   {arl} exp abandon {target} --reason "its reference no longer counts"
2. Measure the track again:
     {arl} exp new --type baseline --track {track} --title "baseline"
3. Draft the experiment again. Its hypothesis is still there, and {exp}/design.md
   keeps what you had written.
