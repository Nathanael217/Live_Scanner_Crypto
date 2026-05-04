import yaml
import sys

with open('/home/claude/.github/workflows/scanner.yml') as f:
    data = yaml.safe_load(f)

# Required top-level keys
assert "name" in data, "missing 'name'"
assert "jobs" in data, "missing 'jobs'"

# 'on' becomes True in PyYAML — accept either key form
on_section = data.get("on") or data.get(True)
assert on_section is not None, "missing 'on' / schedule section"

job = list(data["jobs"].values())[0]
assert "runs-on" in job, "missing 'runs-on'"
assert "steps" in job, "missing 'steps'"
assert "permissions" in job, "missing 'permissions'"
assert job["permissions"].get("contents") == "write", \
    "permissions.contents must be 'write'"

# Cron schedule present and correct
assert "schedule" in on_section, "missing 'schedule' in on:"
assert on_section["schedule"][0]["cron"] == "5 0,4,8,12,16,20 * * *", \
    f"wrong cron: {on_section['schedule'][0]['cron']}"

# workflow_dispatch present for manual runs
assert "workflow_dispatch" in on_section, "missing 'workflow_dispatch'"

# Secrets referenced correctly (not hard-coded)
yml_text = open('/home/claude/.github/workflows/scanner.yml').read()
assert "secrets.TG_BOT_TOKEN" in yml_text, "TG_BOT_TOKEN secret ref missing"
assert "secrets.TG_CHAT_ID"   in yml_text, "TG_CHAT_ID secret ref missing"

# Correct run command
assert "python -m scanner_worker.scan" in yml_text, \
    "missing 'python -m scanner_worker.scan' in run step"

# Dedup commit step present
assert "scanner_worker/state.json" in yml_text, \
    "missing state.json commit step"

# concurrency block present
assert "concurrency" in data, "missing 'concurrency' block"

print("✓ Workflow YAML structure valid")
