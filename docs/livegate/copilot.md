# Live gate: GitHub Copilot (P4c)

**Result: untested. Copilot is not a default host.**

Copilot in VS Code (2026-11-02) could not run any model on the available subscriptions:

- Copilot's own models (Claude Sonnet 5.5, GPT-6 Luna, GPT-6.1 Sol) showed "Upgrade" in the model
  picker: the plan does not include them.
- Every model the picker marked "Claude" went through an Anthropic API key and failed with "Your
  credit balance is too low to access the Anthropic API". A Claude Pro/Max subscription cannot back
  them: Anthropic does not allow third-party apps to use those plans
  (https://code.claude.com/docs/en/legal-and-compliance).

No hook payload was captured. The Copilot field and tool names in `tempo/hosts.py` are still the
documented guesses from P4b.

**To test it:** a Copilot plan that includes agent-mode models. Copilot Pro was the cheapest documented
one (https://docs.github.com/en/copilot/concepts/billing/individual-plans). Also seen: the Copilot chat
offered to enable "Claude Code hooks" for the workspace. Turning that on would run the Claude hook file
inside Copilot too.

Settings in use: `chat.tools.terminal.enableAutoApprove` and `chat.agent.sandbox.allowAutoApprove` on.
