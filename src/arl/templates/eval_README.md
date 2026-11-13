# The evaluator

Everything in this folder is frozen: `arl freeze` hashes it, every run checks the
hashes, and the agent may not change it.

The evaluator is run by arl, from the home checkout, with the command in
`arl.toml` under `[eval]`. It reads what an experiment wrote into `{out}` and
writes one JSON object to `{metrics}`, with a number for every metric named in
`arl.toml`. It must not import code that experiments can change.
