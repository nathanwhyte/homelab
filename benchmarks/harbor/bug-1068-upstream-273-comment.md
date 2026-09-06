# Additional `block=False` readiness evidence

Thanks for working on this. I see commit `dfeeecd` / PR #1324 now addresses the original `block=True` embedded-newline hang by splitting `\n`/`\r\n` into line-plus-`Enter` pairs before `_prepare_keys`. That looks useful for keeping the stdin-draining subprocess from consuming the queued `tmux wait -S` signal.

We have a related but distinct readiness race from Terminus-2's normal `block=False` path in Harbor 0.20.0:

1. A recursive `grep`/`find` still owned the foreground after the model-selected delay. The next ~250-character file-write command is intact in ATIF, but it was sent without a shell prompt, went to the foreground process's stdin, and never executed. The intended task result was otherwise correct, so the trial lost its reward solely to this race.
2. In a later no-recording run, an SSH authenticity prompt consumed queued commands. Recovery with `C-c` then killed the pane shell/tmux server, producing a loud `RuntimeError` rather than silent loss.

Neither case depends on embedded newlines in one `send_keys` input, so #1324's `block=True` fix does not appear to cover them. More generally, both the original hang and these observations suggest that a completion/readiness signal sharing the program's stdin can itself be consumed or misdirected. A robust primitive would acknowledge command completion/readiness out of band from pane stdin; the nonblocking agent path would then need to wait for that readiness signal rather than treating elapsed `duration` as proof that the shell is ready.

Our repository-side mitigation is to append an instruction requiring a fresh, prompt-visible existence/content check before `task_complete`, but that only reduces false completion; it does not replace a transport-level fix.
