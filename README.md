# herdr-wrapped-agent-plugin

A [herdr](https://herdr.dev) plugin that detects coding agents running **inside
`docker exec` sessions** (e.g. dev-container wrappers that open a shell with
`docker exec -it`), which
herdr can't see on its own.

## Why

Herdr recognizes an agent by the pane's foreground process. In such a pane
that process is the host `docker exec` client. The real `claude` runs under the
container's shim, so herdr shows the pane as a plain terminal: no state, no
notifications, and `herdr agent wait` doesn't work on it.

## How it works

A small stdlib-only Python daemon (`wrapped_agent.py`) polls once a second:

1. `pane.list` + `pane.process_info`: find panes whose foreground process is
   `docker [container] exec ...` and parse the container name.
2. The Docker API (`/containers/<c>/json` -> `ExecIDs`, `/exec/<id>/json` -> `Pid`)
   gives the host PID of every exec session. The pane's session is the first one
   that started after its docker client did.
3. Host `/proc` shows that session's tty foreground process group (`tpgid`).
   If it's a known agent (`claude`, `codex`, `opencode`, ... or any process with
   `HERDR_AGENT=<label>` in its env), the pane is claimed with
   `pane.report_agent --source plugin:wrapped-agent`.
4. State comes from herdr's own screen classifier (`agent.explain` on the pane),
   so idle/working/blocked match what herdr would detect natively.
5. When the agent exits, or the pane leaves the container, the pane is released.

Nothing needs to change inside the container or in the wrapper that launches it.

## Install (local)

```bash
herdr plugin link ~/projects/herdr-wrapped-agent-plugin
herdr plugin action invoke wrapped-agent.start   # start now; server startup does it later
```

The daemon starts from the plugin's `[[startup]]` hook on every herdr server
start, and from a `pane.created` hook as a safety net. There is one daemon per
herdr socket, guarded by a lock file.

Actions: `wrapped-agent.start`, `wrapped-agent.stop`, `wrapped-agent.status`.

Logs and state: `$HERDR_PLUGIN_STATE_DIR/daemon-<hash>.log`
(`~/.local/state/herdr/plugins/wrapped-agent/`).

Dry run (read-only; prints what would be reported):

```bash
python3 ~/projects/herdr-wrapped-agent-plugin/wrapped_agent.py scan
```

Uninstall: `herdr plugin action invoke wrapped-agent.stop && herdr plugin unlink wrapped-agent`.

## Config

Optional `config.json` in `herdr plugin config-dir wrapped-agent`:

```json
{ "interval": 1.0, "agents": { "my-claude-wrapper": "claude" } }
```

`agents` maps an executable basename to a herdr agent label (the screen
manifest to use).

## Limitations

- Docker only, via `DOCKER_HOST` or `/var/run/docker.sock` (no podman yet).
- Session resume after a herdr restart isn't reported, since the resume command
  would have to re-enter the container.
- The host user must be able to read the container processes' `/proc`. That
  holds when the wrapper execs as your uid/gid. Without it, only the
  `HERDR_AGENT` hint fails; comm/argv detection still works.

## Tests

```bash
python3 -m unittest discover -s tests
```
