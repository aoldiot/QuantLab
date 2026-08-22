import json
import os
import subprocess
from pathlib import Path


TOOLS = Path(__file__).parents[1] / "dsh_runtime" / "src" / "coding-tools.mjs"


def test_coding_tools_enforce_real_path_boundaries(tmp_path: Path) -> None:
    allowed = tmp_path / "app" / "strategies"
    allowed.mkdir(parents=True)
    source = allowed / "safe.py"
    source.write_text("safe", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (allowed / "escape.py").symlink_to(outside)

    script = f"""
      const {{ __testing: t }} = await import({json.dumps(TOOLS.as_uri())});
      const ignored = new Set(['.git', '.env']);
      const checks = {{
        rootDenied: !t.isPathAllowed(t.resolvePath('.'), ['app/strategies'], ignored),
        normalRead: await t.realPathAllowed(t.resolvePath('app/strategies/safe.py'), ['app/strategies'], ignored),
        traversalDenied: !await t.realPathAllowed(t.resolvePath('../secret.txt'), ['app/strategies'], ignored),
        symlinkDenied: !await t.realPathAllowed(t.resolvePath('app/strategies/escape.py'), ['app/strategies'], ignored),
        newCandidateAllowed: await t.realPathAllowed(
          t.resolvePath('app/strategies/candidates/new.py'),
          ['app/strategies/candidates'],
          ignored,
          {{ forWrite: true }},
        ),
      }};
      console.log(JSON.stringify(checks));
    """
    env = os.environ.copy()
    env["DSH_CWD"] = str(tmp_path)
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert json.loads(result.stdout) == {
        "rootDenied": True,
        "normalRead": True,
        "traversalDenied": True,
        "symlinkDenied": True,
        "newCandidateAllowed": True,
    }
