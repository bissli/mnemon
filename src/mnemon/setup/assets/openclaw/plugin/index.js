export default function register(api) {
  // api.pluginConfig holds plugins.entries.mnemon.config from openclaw.json
  const cfg = api.pluginConfig ?? {};
  const remind  = cfg.remind  !== false; // default on
  const nudge   = cfg.nudge   !== false; // default on

  // ── before_prompt_build ───────────────────────────────────────
  // Always load the mnemon skill; optionally inject remind/nudge hints.
  api.on("before_prompt_build", async () => {
    const parts = ["[mnemon] load mnemon skill"];

    if (remind) {
      parts.push(
        "[mnemon] Evaluate: recall needed? After responding, evaluate: remember needed?"
      );
    }

    if (nudge) {
      parts.push(
        "[mnemon] Consider: does this exchange warrant a remember sub-agent?"
      );
    }

    return { prependContext: parts.join("\n\n") };
  });
}
