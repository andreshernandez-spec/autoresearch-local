STOP. The frozen contract does not match the files on disk:
  {why}

Nothing runs until they match research/frozen.lock again. Do not try to freeze: with
experiments in the ledger only a person at a terminal can.
- Changed by accident: `git -C {home} diff -- <file>` shows the change,
  `git -C {home} restore <file>` puts the frozen version back.
- Changed on purpose, or you think it should change: that is the program owner's decision.
  Stop, and tell them what changed and why it might be needed.
