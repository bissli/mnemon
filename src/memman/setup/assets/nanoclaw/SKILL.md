---
name: add-memman
description: Add persistent graph-based memory to NanoClaw agents using memman. Agents recall context before responding and remember insights after. Each group gets isolated memory with optional global shared store.
---

# /add-memman

Add [memman](https://github.com/bissli/memman) persistent memory to your NanoClaw installation. After running this skill, every agent session will have access to a per-group memory graph that persists across conversations.

## Architecture

```
Host                              Container
~/.memman/data/{group}/ ──rw──→ /home/node/.memman/data/default/  (private)
~/.memman/data/global/  ──ro──→ /home/node/.memman/data/global/   (shared)
```

Each group gets its own isolated memman store. An optional global store provides shared read-only memory across all groups.

---

## Phase 1: Pre-flight

1. Verify memman is installed on the host:
   ```bash
   memman --version
   ```
   If not installed, follow the README at
   https://github.com/bissli/memman and stop. Do not attempt to
   install memman from this skill.

2. Verify the container image exists:
   ```bash
   docker image inspect nanoclaw-agent:latest >/dev/null 2>&1 && echo "OK"
   ```

---

## Phase 2: Apply Code Changes

### 2a. Install memman in the container image

**File**: `container/Dockerfile`

Add the following block **after** the `apt-get install` section and **before** the `npm install -g` line:

```dockerfile
# Install memman for persistent agent memory
RUN pip install --no-cache-dir git+https://github.com/bissli/memman.git

# Containers have no systemd/launchd; memman runs its own drain loop
# as PID 1 instead. The MEMMAN_SCHEDULER_KIND env var picks the serve
# mode; STATE_STARTED is bootstrapped so writes are accepted on first run.
ENV MEMMAN_SCHEDULER_KIND=serve
RUN install -d -o node -g node -m 755 /home/node/.memman \
 && printf 'started\n' > /home/node/.memman/scheduler.state \
 && chown node:node /home/node/.memman/scheduler.state \
 && chmod 600       /home/node/.memman/scheduler.state
```

### 2a-bis. Run `memman scheduler serve` as PID 1

Replace whatever long-running primitive the container currently uses
(`CMD ["sleep", "infinity"]`, an entrypoint script that waits, etc.)
with the memman scheduler loop. PID 1 *is* the scheduler — if the
container is alive, the drain loop is alive.

```dockerfile
CMD ["memman", "scheduler", "serve", "--interval", "60"]
```

If the container already has its own foreground entrypoint that must
remain PID 1, ship a wrapper that backgrounds the scheduler under a
proper signal-forwarding trap:

```sh
#!/bin/sh
set -e
: "${MEMMAN_INTERVAL:=60}"
memman scheduler serve --interval "$MEMMAN_INTERVAL" \
    >> "$HOME/.memman/logs/serve.log" 2>&1 &
scheduler_pid=$!
trap 'kill -TERM $scheduler_pid 2>/dev/null; wait $scheduler_pid' TERM INT
exec "$@"
```

The `trap` is mandatory — without it, a SIGTERM kills only the user
entrypoint and leaves the backgrounded scheduler orphaned.

**`queue.db` is per-container**: it lives at `$HOME/.memman/queue.db`
inside the container and is **not** in the volume mount. A container
restart loses any pending queue rows. The serve loop drains every
`--interval` seconds, so pending rows are typically under one minute
old.

### 2b. Add the container skill

**File**: `container/skills/memman/SKILL.md`

Create this file with the memman container skill content. This skill teaches the agent inside the container when and how to use memman. It should include:

- **Memory stores section**: Explain that the default store is per-group (private, read-write) and the global store is shared (read-only, accessed via `--store global`).
- **Recall guide**: Default recall on every new user message. Use `memman recall "<query>" --brief --limit 20` (the cross-encoder reranker runs by default on multi-token queries; auto-skipped on 1-2 token queries). Also check the global store: `memman recall "<query>" --store global --brief --limit 20`. Craft focused keyword-rich queries. Rows are relevance-ordered; judge each against the query rather than against a fixed score.
- **Remember guide**: Decision tree — Step 1: Does this exchange contain a user directive, reasoning conclusion, or durable observed state? Step 2: Does this correct something already stored? Say so in the text and remember it. Step 3: Is it worth storing?
- **Workflow**: remember → graph link (evaluate semantic/causal candidates with judgment) → recall.
- **Commands**: Full memman command reference (remember, replace, recall, forget, graph link/related, insights review/show/by-queue, status, doctor, log list).
- **Guardrails**: Never store secrets. Never write to the global store. Categories: preference, decision, insight, fact, context. Max 8,000 chars per insight.

### 2c. Add volume mounts for memman data

**File**: `src/container-runner.ts`

In the function that builds volume mounts (where the existing group folder and Claude session mounts are defined), add two new mounts **after** the Claude sessions mount:

```typescript
// Per-group memman memory store (private, read-write)
const groupMemManDir = path.join(homedir(), '.memman', 'data', group.folder);
fs.mkdirSync(groupMemManDir, { recursive: true });
mounts.push({
  hostPath: groupMemManDir,
  containerPath: '/home/node/.memman/data/default',
  readonly: false,
});

// Global shared memman memory (read-only, optional)
const globalMemManDir = path.join(homedir(), '.memman', 'data', 'global');
if (fs.existsSync(globalMemManDir)) {
  mounts.push({
    hostPath: globalMemManDir,
    containerPath: '/home/node/.memman/data/global',
    readonly: true,
  });
}
```

Adapt the mount syntax to match the existing pattern in `container-runner.ts` (it may use string format like `hostPath:containerPath:ro` or an object format — match whichever the file uses).

**Important**: The `mkdirSync` call ensures the per-group memman directory exists on the host before the container starts, preventing mount failures.

### 2d. Add lifecycle hook scripts

memman ships the three lifecycle hook scripts as files inside the
installed package. Locate them and copy them into your container build
context:

```bash
PKG=$(python -c "from importlib.resources import files; print(files('memman.setup.assets'))")
mkdir -p container/hooks/memman
cp "$PKG/nanoclaw/hooks/"*.sh container/hooks/memman/
chmod +x container/hooks/memman/*.sh
```

The three scripts are:

- `prime.sh` — SessionStart: prints `[memman] Memory active (N insights, M edges).`
- `user_prompt.sh` — UserPromptSubmit: prints a recall/remember reminder.
- `stop.sh` — Stop: returns `{"decision":"block", ...}` JSON so the agent
  gets one more turn to evaluate remembering. Honors `stop_hook_active`
  to prevent loops.

Read the actual scripts in your installed package to see the exact bytes
the test suite verifies.

### 2e. Copy hooks into container and register in settings.json

**File**: `container/Dockerfile`

Add after the memman binary install block:

```dockerfile
# Copy memman hook scripts
COPY hooks/memman/ /app/hooks/memman/
RUN chmod +x /app/hooks/memman/*.sh
```

**File**: `src/container-runner.ts`

In the block where `settings.json` is created for each group session (look for `writeFileSync` with `settings.json`), merge memman hooks into the settings object:

```typescript
// Register memman lifecycle hooks
const memmanHooks = {
  SessionStart: [{
    hooks: [{ type: 'command', command: '/app/hooks/memman/prime.sh' }]
  }],
  UserPromptSubmit: [{
    hooks: [{ type: 'command', command: '/app/hooks/memman/user_prompt.sh' }]
  }],
  Stop: [{
    hooks: [{ type: 'command', command: '/app/hooks/memman/stop.sh' }]
  }],
};

// Merge into existing settings.hooks (preserve any existing hooks)
const existingHooks = settings.hooks || {};
for (const [event, entries] of Object.entries(memmanHooks)) {
  existingHooks[event] = [...(existingHooks[event] || []), ...entries];
}
settings.hooks = existingHooks;
```

Adapt this to match the existing settings.json construction pattern in `container-runner.ts`.

---

## Phase 3: Setup

1. Initialize the global shared store on the host (optional — skip if you don't need cross-group shared memory):
   ```bash
   memman store create global
   ```

2. Rebuild the container image:
   ```bash
   ./container/build.sh
   ```

3. Restart the NanoClaw service:
   ```bash
   # macOS with launchd:
   launchctl kickstart -k "gui/$(id -u)/com.nanoclaw"
   # Or manually:
   npm run dev
   ```

---

## Phase 4: Verify

1. Verify memman works inside the container:
   ```bash
   docker run --rm --entrypoint memman nanoclaw-agent:latest --version
   ```

2. Verify memman status inside a running container:
   ```bash
   docker run --rm --entrypoint memman nanoclaw-agent:latest status
   ```

3. Send a test message to the WhatsApp bot and check that the agent mentions memory operations in its reasoning.

4. Verify data persistence on the host:
   ```bash
   ls ~/.memman/data/
   # Should show directories for each active group
   ```

---

## Removal

To remove memman from your NanoClaw installation:

1. Remove from Dockerfile: delete the `RUN pip install ... memman` block, the `ENV MEMMAN_SCHEDULER_KIND` block, the `CMD ["memman", "scheduler", "serve", ...]` line (or wrapper if used), and the `COPY hooks/memman/` line
2. Remove container skill: `rm -rf container/skills/memman/`
3. Remove hook scripts: `rm -rf container/hooks/memman/`
4. Remove volume mounts from `src/container-runner.ts`: delete the memman mount blocks
5. Remove hooks registration from `src/container-runner.ts`: delete the memman hooks merge in settings.json
6. Rebuild: `./container/build.sh`
7. (Optional) Remove data: `rm -rf ~/.memman/data/`
